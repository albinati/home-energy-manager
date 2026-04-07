import os
from dotenv import load_dotenv
load_dotenv(".env")
from src.foxess.client import FoxESSClient

c = FoxESSClient(api_key=os.environ.get("FOXESS_API_KEY"), device_sn=os.environ.get("FOXESS_DEVICE_SN"))
try:
    print(c._cloud_post("/c/v0/device/battery/schedule/get", {"sn": c.device_sn}))
except Exception as e:
    print("Cloud err:", e)

try:
    print(c._cloud_post("/c/v0/device/setting/get", {"sn": c.device_sn, "keys": ["times1", "times2"]}))
except Exception as e:
    print("Cloud err:", e)
