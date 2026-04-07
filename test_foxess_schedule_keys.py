import os
from dotenv import load_dotenv
load_dotenv(".env")
from src.foxess.client import FoxESSClient

c = FoxESSClient(api_key=os.environ.get("FOXESS_API_KEY"), device_sn=os.environ.get("FOXESS_DEVICE_SN"))
try:
    print("Testing /device/setting/get with id")
    print(c._open_post("/device/setting/get", {"id": c.device_sn, "keys": ["times1", "times2"]}))
except Exception as e:
    print(e)

try:
    print("Testing /device/setting/get with sn and keys")
    print(c._open_post("/device/setting/get", {"sn": c.device_sn, "keys": ["times1", "times2"]}))
except Exception as e:
    print(e)
