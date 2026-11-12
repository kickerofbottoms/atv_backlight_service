"""
Minimal service to sync an Apple TVs power state with a Hue backlight.
Sidesteps the need to run a full Homebridge instance with the
"Apple TV Enhanced" plugin, for the very specific use-case of just turning on
and off an LED strip by directly commanding the Hue bridge.

Setup steps:
    1. Install this somewhere, install requirements.txt
    2. Pair one or more Apple TVs using the wizard:
        python3 -m atv_backlight_service atv pair
    3. Pair the Hue bridge using the wizard:
        python3 -m atv_backlight_service hue pair <bridge_address>
    4. Attach a backlight to each Apple TV using the wizard:
        python3 -m atv_backlight_service attach
    5. Run the service
        python3 -m atv_backlight_service run
    6. Probably make that last command into a systemd service, or run via
        Windows Task Manager / startup scripts, or whatever.
"""

import argparse
import asyncio
import logging
import random
import socket
import sys

import pyatv
from pyatv.storage.file_storage import FileStorage

from atv_backlight_service.libs import phue
from atv_backlight_service.utils.config import Config, AppleTVConfig, HueConfig
from atv_backlight_service.utils.hue import fetch_hue_settings, \
    register_hue_application

logging.basicConfig()
pyatv_logger = logging.getLogger("pyatv")
pyatv_logger.setLevel(logging.WARNING)

app_config = Config.load()


class ATVListener(pyatv.interface.PowerListener, pyatv.interface.DeviceListener):
    def __init__(self, identifier, bridge, shutdown: asyncio.Event):
        self.identifier = identifier
        self.bridge = bridge
        self.atv = None
        self.shutdown = shutdown
        self.connection_lost = asyncio.Event()

    @property
    def name(self):
        return app_config.atv[self.identifier].name

    @property
    def light_id(self):
        return app_config.atv[self.identifier].light_id

    def powerstate_update(
        self,
        old_state: pyatv.const.PowerState,
        new_state: pyatv.const.PowerState
    ):
        print(f"{self.name} {new_state.name} (light={self.light_id})")
        if new_state == pyatv.const.PowerState.On:
            self.bridge.set_light(
                light_id=self.light_id,
                parameter='on',
                value=True,
                transitiontime=app_config.backlight_transition
            )
        elif new_state == pyatv.const.PowerState.Off:
            self.bridge.set_light(
                light_id=self.light_id,
                parameter='on',
                value=False,
                transitiontime=app_config.backlight_transition
            )

    def connection_lost(self, exception):
        print(f"{self.name} lost connection:", str(exception))
        self.connection_lost.set()

    def connection_closed(self):
        print(f"{self.name} connection closed")
        self.connection_lost.set()

    async def persist(self, loop, storage):
        while not self.shutdown.is_set():
            self.connection_lost.clear()

            print(f"{self.name} connecting")
            connected, msg = await self.connect(loop, storage)
            print(msg)

            if not connected:
                await asyncio.sleep(1)
                continue

            while not (self.shutdown.is_set() or self.connection_lost.is_set()):
                try:
                    await asyncio.sleep(1)
                except asyncio.CancelledError:
                    print("Shutting down")
                    self.shutdown.set()

            if self.connection_lost.is_set():
                print(f"{self.name} connection lost")

            await self.close()

    async def connect(self, loop, storage):
        configs = await pyatv.scan(
            loop=loop,
            identifier=self.identifier,
            storage=storage
        )
        if len(configs) == 1:
            conf = configs[0]
        elif len(configs) > 1:
            raise RuntimeError(f"{len(configs)} ATVs with id={self.identifier}")
        else:
            return False, f"{self.name} not reachable"

        try:
            self.atv = await pyatv.connect(conf, loop)
        except Exception as e:
            return False, f"{self.name} connection failed: {e}"

        self.atv.listener = self  # Connection callbacks
        self.atv.power.listener = self  # Power state callbacks

        return True, f"{self.name} connected"

    async def close(self):
        if self.atv:
            print(f"{self.name} disconnecting")
            remaining_tasks = self.atv.close()
            print(f"{self.name} waiting for {len(remaining_tasks)} tasks")
            try:
                await asyncio.wait_for(asyncio.gather(*remaining_tasks), 10)
            except asyncio.TimeoutError:
                print(f"{self.name} timed out waiting for tasks")


async def _read_input(loop: asyncio.AbstractEventLoop, prompt: str):
    sys.stdout.write(prompt)
    sys.stdout.flush()
    user_input = await loop.run_in_executor(None, sys.stdin.readline)
    return user_input.strip()


async def _choose_from_list(loop, choices: list[str], prompt="Selection: ") -> int:
    for n, row in enumerate(choices, 1):
        print(f"{str(n):}: {row}")

    i = -1
    while not (0 <= i < len(choices)):
        choice = str(await _read_input(loop, prompt))
        if choice.isdecimal():
            i = int(choice) - 1

    return i


async def atv_list(loop):
    if not app_config.atv:
        print("No configured ATVs")
        return

    print("Paired ATVs:")
    for identifier, atv_info in app_config.atv.items():
        print(
            f"{atv_info.name:<16}  "
            f"{identifier:<17}  "
            f"light={str(atv_info.light_id).lower()}"
        )


async def atv_pair(loop):
    print("Pairing flow started")

    storage = FileStorage.default_storage(loop)
    await storage.load()

    print("Scanning for Apple TVs...")
    confs = await pyatv.scan(loop)
    confs_paired = [conf.identifier in app_config.atv for conf in confs]
    choices = []
    for n, conf in enumerate(confs, 1):
        is_paired = confs_paired[n - 1]
        choices.append(
            f"{conf.name:<16}  "
            f"{conf.identifier:<17}  "
            f"{str(conf.address):<12}  "
            f"paired={str(is_paired).lower():<5}"
        )

    i = await _choose_from_list(loop, choices)
    conf = confs[i]

    print(f"Attempting to pair with {conf.name}")

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

    device_name = f"Backlight Service on {socket.gethostname()}"

    pairing = await pyatv.pair(
        config=conf,
        protocol=service.protocol,
        loop=loop,
        storage=storage,
        name=device_name
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
        await storage.save()
        app_config.atv[conf.identifier] = AppleTVConfig(name=conf.name)
        app_config.save()
        print(f"Successfully paired")
    else:
        print("Pairing failed!")

    await pairing.close()


async def atv_status(loop):
    print("Querying adopted ATVs...")
    storage = FileStorage.default_storage(loop)
    await storage.load()

    atv_ids = set(app_config.atv.keys())
    if not atv_ids:
        print("No paired ATVs found in config")
        return

    atvs = await pyatv.scan(loop, identifier=atv_ids, storage=storage)
    for conf in atvs:
        atv = await pyatv.connect(conf, loop, storage=storage)
        print(f"{conf.name}: {atv.power.power_state}")
        atv.close()


async def hue_pair(loop, address):
    if app_config.hue:
        bridge = phue.Bridge(
            ip=app_config.hue.address,
            username=app_config.hue.token
        )

        try:
            bridge.connect()
            fetch_hue_settings(bridge)
        except phue.PhueRegistrationException:
            pass
        else:
            print(f"Already paired.")
            return

    print("Press the button on the bridge, then press ENTER")
    await loop.run_in_executor(None, sys.stdin.readline)

    try:
        token = register_hue_application(address, app_config.bridge_username)
    except Exception as e:
        print(f"Failed to register application: {e}")
        return
    else:
        app_config.hue = HueConfig(address=address, token=token)
        app_config.save()
        print(f"Register OK")

    bridge = phue.Bridge(
        ip=app_config.hue.address,
        username=app_config.hue.token
    )

    try:
        bridge.connect()
        fetch_hue_settings(bridge)
    except Exception as e:
        print(f"Failed to connect after registration: {e}")
        return

    print(f"Successfully paired.")


async def hue_list(loop):
    bridge = phue.Bridge(
        ip=app_config.hue.address,
        username=app_config.hue.token
    )
    bridge.connect()
    api_resp = fetch_hue_settings(bridge)

    light_to_group = {}
    for group_id, group in api_resp["groups"].items():
        for light_id in group["lights"]:
            light_to_group[light_id] = group["name"]

    for light_id, light in api_resp["lights"].items():
        print(f"{light_id}: {light['name']} [{light_to_group[light_id]}]")


async def attach_backlight(loop):
    if not app_config.atv:
        print("No configured ATVs")
        return

    atv_choices = []
    for identifier, atv_info in app_config.atv.items():
        atv_choices.append(
            f"{atv_info.name:<16}  "
            f"{identifier:<17}  "
            f"light={str(atv_info.light_id).lower()}"
        )

    i = await _choose_from_list(
        loop=loop,
        choices=atv_choices,
        prompt="Choose Apple TV: "
    )
    atv_id = list(app_config.atv.keys())[i]

    hue_choices = []
    bridge = phue.Bridge(
        ip=app_config.hue.address,
        username=app_config.hue.token
    )
    bridge.connect()
    api_resp = fetch_hue_settings(bridge)
    light_to_group = {}
    for group_id, group in api_resp["groups"].items():
        for light_id in group["lights"]:
            light_to_group[light_id] = group["name"]
    for light_id, light in api_resp["lights"].items():
        hue_choices.append(f"{light['name']} [{light_to_group[light_id]}]")

    i = await _choose_from_list(
        loop=loop,
        choices=hue_choices,
        prompt="Choose backlight: "
    )
    light_id = list(api_resp["lights"].keys())[i]
    assert str(i + 1) == light_id

    storage = FileStorage.default_storage(loop)
    await storage.load()

    app_config.atv[atv_id].light_id = int(light_id)
    app_config.save()

    print("Config saved")


async def run_service(loop):
    print("ATV backlight service started")
    shutdown = asyncio.Event()

    if not app_config.atv:
        print("No ATVs configured")
        shutdown.set()
        return

    if app_config.hue is None:
        print("No Hue bridge configured")
        shutdown.set()
        return

    print("Connecting to Hue bridge")
    bridge = phue.Bridge(
        ip=app_config.hue.address,
        username=app_config.hue.token  # hash returned by registering username
    )
    bridge.connect()
    fetch_hue_settings(bridge)  # raises if not authorized
    print("Bridge connected")

    storage = FileStorage.default_storage(loop)
    await storage.load()

    print(f"Connecting to {len(app_config.atv)} ATV(s)")
    connections = []
    tasks = []
    for atv_id, atv_info in app_config.atv.items():
        if atv_info.light_id is None:
            print(f"{atv_info.name} has no attached lights, skipping")
            continue
        connection = ATVListener(atv_id, bridge, shutdown)
        connections.append(connection)
        task = asyncio.create_task(connection.persist(loop, storage))
        tasks.append(task)

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass

    print("Service stopped")


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

    # run
    run_parser = subparsers.add_parser("run")
    run_parser.set_defaults(fn=run_service)

    if not sys.argv[1:]:
        parser.print_help()
        parser.exit(-1, "No arguments specified")

    return vars(parser.parse_args())


async def main():
    loop = asyncio.get_event_loop()

    args = get_args()
    run_command = args.pop("fn")
    await run_command(loop, **args)

    return 0


def test():
    bridge = phue.Bridge(
        ip=app_config.hue.address,
        username=app_config.hue.token
    )
    bridge.connect()
    bridge.set_light(
        light_id=6,
        parameter='on',
        value=True,
        transitiontime=app_config.backlight_transition
    )
    return


if __name__ == '__main__':
    # test()
    asyncio.run(main())
