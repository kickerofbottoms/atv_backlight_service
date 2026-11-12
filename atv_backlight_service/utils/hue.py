import requests

from atv_backlight_service.libs import phue


def fetch_hue_settings(bridge: phue.Bridge):
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


def register_hue_application(bridge_ip: str, app_username: str):
    token = None
    resp = requests.post(
        url=f"http://{bridge_ip}/api",
        json={"devicetype": app_username}
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
