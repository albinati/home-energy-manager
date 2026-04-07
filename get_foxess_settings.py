import os
from dotenv import load_dotenv
load_dotenv(".env")
from src.foxess.service import get_foxess_service

srv = get_foxess_service()
client = srv._client
try:
    resp = client._open_post("/device/battery/schedule/get", {"sn": client.device_sn})
    print("Schedule GET:", resp)
except Exception as e:
    pass

try:
    resp = client._open_post("/device/setting/get", {"sn": client.device_sn})
    print("Setting GET:", resp)
except Exception as e:
    pass
