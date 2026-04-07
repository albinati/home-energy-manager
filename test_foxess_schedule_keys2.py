import os
from dotenv import load_dotenv
load_dotenv(".env")
from src.foxess.client import FoxESSClient

c = FoxESSClient(api_key=os.environ.get("FOXESS_API_KEY"), device_sn=os.environ.get("FOXESS_DEVICE_SN"))
try:
    print(c._open_post("/device/battery/schedule/get", {"sn": c.device_sn}))
except Exception as e:
    print(e)
