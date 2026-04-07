import os
from dotenv import load_dotenv
load_dotenv(".env")
from src.foxess.client import FoxESSClient
from src.foxess.models import ChargePeriod

c = FoxESSClient(api_key=os.environ.get("FOXESS_API_KEY"), device_sn=os.environ.get("FOXESS_DEVICE_SN"))
try:
    print("Disabling times1...")
    p1 = ChargePeriod(start_time="00:00", end_time="00:00", target_soc=10, enable=False)
    c.set_charge_period(0, p1)
    print("Success times1")
    
    print("Disabling times2...")
    p2 = ChargePeriod(start_time="00:00", end_time="00:00", target_soc=10, enable=False)
    c.set_charge_period(1, p2)
    print("Success times2")
except Exception as e:
    print("Error:", e)
