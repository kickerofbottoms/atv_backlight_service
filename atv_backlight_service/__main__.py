# todo: write systemd def and install on pi

import argparse
import asyncio
import json
import random
import sys
from pathlib import Path

import pyatv
import requests
from pyatv.storage.file_storage import FileStorage

from atv_backlight_service.libs import phue

HUE_USERNAME = "atv_backlight_service"  # to be hashed during api registration
BRIGHTNESS = 254
TRANSITION_TIME = 10  # in decaseconds


# fixme: this needs to be refactored
class DumbConfig:
    filename = ".dumbconfig.conf"

    def __init__(self):
        self.path = Path.home().joinpath(self.filename)
        self.data = self._load()

    def _load(self):
        if self.path.exists():
            self.data = json.loads(self.path.read_text())
        else:
            self.data = {}
        return self.data

    def _save(self):
        self.path.write_text(json.dumps(self.data))

    def keys(self):
        return self.data.keys()

    def values(self):
        return self.data.values()

    def items(self):
        return self.data.items()

    def __getitem__(self, item):
        return self.data[item]

    def __setitem__(self, key, value):
        self.data[key] = value
        self._save()  # todo: make this more explicit, public save()


class AppleTVConfig(DumbConfig):
    filename = ".atvbacklight.atv.conf"


class HueBridgeConfig(DumbConfig):
    filename = ".atvbacklight.hue.conf"


atv_config = AppleTVConfig()
hue_config = HueBridgeConfig()


class PowerListener(pyatv.interface.PowerListener):
    """Listen for power updates and print changes."""

    def __init__(self, label, bridge: phue.Bridge, light_id):
        super().__init__()
        self.label = label
        self.bridge = bridge
        self.light_id = light_id

    def powerstate_update(
        self,
        old_state: pyatv.const.PowerState,
        new_state: pyatv.const.PowerState
    ):
        print(f"{self.label} {new_state.name}, setting light {self.light_id}")
        if new_state == pyatv.const.PowerState.On:
            self.bridge.set_light(
                light_id=self.light_id,
                parameter='on',
                value=True,
                transitiontime=TRANSITION_TIME
            )
        elif new_state == pyatv.const.PowerState.Off:
            self.bridge.set_light(
                light_id=self.light_id,
                parameter='on',
                value=False,
                transitiontime=TRANSITION_TIME
            )


async def _read_input(loop: asyncio.AbstractEventLoop, prompt: str):
    sys.stdout.write(prompt)
    sys.stdout.flush()
    user_input = await loop.run_in_executor(None, sys.stdin.readline)
    return user_input.strip()


async def atv_list(loop):
    print("Scanning for Apple TVs...")
    confs = await pyatv.scan(loop)
    for conf in confs:
        print(f"{conf.identifier} ({conf.name})")


async def atv_pair(loop, identifier):
    print(f"Pairing with Apple TV: {identifier}")

    storage = FileStorage.default_storage(loop)
    await storage.load()

    try:
        conf, = await pyatv.scan(loop, identifier=identifier, storage=storage)
    except ValueError:
        print("No Apple TV found with the specified ID")
        return

    service = {service.protocol: service for service in conf.services}
    service = service[pyatv.Protocol.Companion]

    if service.pairing == pyatv.const.PairingRequirement.NotNeeded:
        print("Pairing not needed")
        service.credentials = None
        return

    # Do not pair if we already have credentials
    if service.credentials:
        print("Credentials already exist")
        return

    pairing = await pyatv.pair(
        config=conf,
        protocol=service.protocol,
        loop=loop,
        storage=storage
    )
    await pairing.begin()

    # Ask for PIN if present or just wait for pairing to end
    if pairing.device_provides_pin:
        pin = await _read_input(loop, "Enter PIN on screen: ")
        pairing.pin(pin)
    else:
        pin = random.randint(1000, 9999)
        pairing.pin(pin)

        print(f"Use pin {pin} to pair, then press ENTER")
        await loop.run_in_executor(None, sys.stdin.readline)

    await pairing.finish()

    if pairing.has_paired:
        print(f"Successfully paired")
        await storage.save()
        atv_config[conf.identifier] = {"name": conf.name}
    else:
        print("Pairing failed!")

    await pairing.close()


async def atv_status(loop):
    print("Querying adopted ATVs...")
    storage = FileStorage.default_storage(loop)
    await storage.load()

    adopted_ids = set(atv_config.keys())

    atvs = await pyatv.scan(loop, identifier=adopted_ids, storage=storage)
    for conf in atvs:
        atv = await pyatv.connect(conf, loop)
        print(f"{conf.identifier} ({conf.name}): {atv.power.power_state}")
        atv.close()


async def run_service(loop):
    print("ATV backlight service started")

    print("Connecting to Hue bridge")
    bridge = phue.Bridge(ip=hue_config["address"], username=hue_config["token"])
    bridge.connect()
    hue_fetch_api(bridge)  # raises if not authorized

    storage = FileStorage.default_storage(loop)
    await storage.load()

    adopted_ids = set(atv_config.keys())

    atvs = []
    listeners = {}

    print(f"Connecting to {len(adopted_ids)} ATVs")
    configs = await pyatv.scan(loop, identifier=adopted_ids, storage=storage)
    for conf in configs:
        atv = await pyatv.connect(conf, loop)
        atv.power.listener = listeners[conf.identifier] = PowerListener(
            label=conf.name,
            bridge=bridge,
            light_id=atv_config[conf.identifier]["light_id"]
        )
        atvs.append(atv)
        print(f"{conf.name} connected ({conf.identifier})")

    # todo: how to handle disconnects?
    # todo: watch for config file changes

    try:
        while True:
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    finally:
        for atv in atvs:
            remaining_tasks = atv.close()
            print(f"Waiting for {len(remaining_tasks)} remaining tasks")
            await asyncio.wait_for(asyncio.gather(*remaining_tasks), 5)

    print("Service stopped")


def hue_fetch_api(bridge):
    api_resp = bridge.get_api()
    for resp in api_resp:
        if not isinstance(resp, dict):
            return api_resp  # success, not a list of errors
        for k, v in resp.items():
            if k == "error":
                raise phue.PhueRegistrationException(
                    v["type"], v["description"]
                )
    return api_resp


def hue_register_application(bridge):
    token = None
    resp = requests.post(
        url=f"http://{bridge.ip}/api",
        json={"devicetype": HUE_USERNAME}
    )
    api_resp = resp.json()
    for resp in api_resp:
        for k, v in resp.items():
            if k == "error":
                raise phue.PhueRegistrationException(
                    v["type"], v["description"]
                )
            elif k == "success":
                token = v["username"]  # hashed username
    return token


async def hue_pair(loop, address):
    hue_config["address"] = address
    bridge = phue.Bridge(ip=address, username=hue_config["token"])

    try:
        bridge.connect()
        hue_fetch_api(bridge)
    except phue.PhueRegistrationException:
        pass
    else:
        print(f"Already paired.")
        return

    print("Press the button on the bridge, then press ENTER")
    await loop.run_in_executor(None, sys.stdin.readline)

    try:
        token = hue_register_application(bridge)
    except Exception as e:
        print(f"Failed to register application: {e}")
        return
    else:
        hue_config["token"] = token
        print(f"Register OK")

    bridge = phue.Bridge(ip=address, username=token)

    try:
        bridge.connect()
        hue_fetch_api(bridge)
    except Exception as e:
        print(f"Failed to connect after registration: {e}")
        return

    print(f"Successfully paired.")


async def hue_list(loop):
    bridge = phue.Bridge(ip=hue_config["address"], username=hue_config["token"])
    bridge.connect()
    api_resp = hue_fetch_api(bridge)

    light_to_group = {}
    for group_id, group in api_resp["groups"].items():
        for light_id in group["lights"]:
            light_to_group[light_id] = group["name"]

    for light_id, light in api_resp["lights"].items():
        print(f"{light_id}: {light['name']} [{light_to_group[light_id]}]")


async def attach_backlight(loop, atv_id, light_id):
    try:
        atv_config[atv_id]
    except KeyError:
        print(f"Error: no ATV paired with ID {atv_id}")
        return

    storage = FileStorage.default_storage(loop)
    await storage.load()

    conf, = await pyatv.scan(loop, identifier=atv_id, storage=storage)
    atv = await pyatv.connect(conf, loop)
    print(f"ATV reachable")
    atv.close()

    bridge = phue.Bridge(ip=hue_config["address"], username=hue_config["token"])
    bridge.connect()
    api_resp = hue_fetch_api(bridge)
    print("Hue bridge reachable")

    if light_id not in api_resp["lights"]:
        print(f"Error: light does not exist with ID {light_id}")

    obj = atv_config[atv_id]
    obj["light_id"] = int(light_id)
    atv_config[atv_id] = obj  # fixme: dumb config


def get_args():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()

    # atv
    atv = subparsers.add_parser("atv")
    atv_command = atv.add_subparsers()

    # --> atv list
    atv_command_scan = atv_command.add_parser("list")
    atv_command_scan.set_defaults(fn=atv_list)

    # --> atv pair
    atv_command_pair = atv_command.add_parser("pair")
    atv_command_pair.set_defaults(fn=atv_pair)
    atv_command_pair.add_argument("identifier")

    # --> atv status
    atv_command_status = atv_command.add_parser("status")
    atv_command_status.set_defaults(fn=atv_status)

    # hue
    hue_parser = subparsers.add_parser("hue")
    hue_command = hue_parser.add_subparsers()

    # --> hue pair
    hue_command_pair = hue_command.add_parser("pair")
    hue_command_pair.set_defaults(fn=hue_pair)
    hue_command_pair.add_argument("address")

    # --> hue list
    hue_command_list = hue_command.add_parser("list")
    hue_command_list.set_defaults(fn=hue_list)

    # attach
    attach_parser = subparsers.add_parser("attach")
    attach_parser.set_defaults(fn=attach_backlight)
    attach_parser.add_argument("atv_id")
    attach_parser.add_argument("light_id")

    return vars(parser.parse_args())


async def main():
    loop = asyncio.get_event_loop()

    if sys.argv[1:]:
        args = get_args()
        run_command = args.pop("fn")
        await run_command(loop, **args)
    else:
        await run_service(loop)
    return 0


def test():
    bridge = phue.Bridge(ip=hue_config["address"], username=hue_config["token"])
    bridge.connect()
    bridge.set_light(
        light_id=6,
        parameter='on',
        value=True,
        transitiontime=TRANSITION_TIME
    )
    return


if __name__ == '__main__':
    # test()
    asyncio.run(main())
