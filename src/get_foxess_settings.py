import os
import sys

# load env
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from foxess.client import FoxESSClient

try:
    client = FoxESSClient()
    # The endpoint might be /device/setting/get or /op/v0/device/setting/get
    # If the user's Open API is used:
    # _open_post prepends "/op/v0" or similar depending on the code
    body = {"sn": client.device_sn}
    resp = client._open_post("/device/setting/get", body)
    print("Settings get response:", resp)
except Exception as e:
    print("Error:", e)
