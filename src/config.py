"""Config loader — reads from .env or environment variables."""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


class Config:
    # Fox ESS
    FOXESS_API_KEY: str = os.getenv("FOXESS_API_KEY", "")
    FOXESS_DEVICE_SN: str = os.getenv("FOXESS_DEVICE_SN", "")
    FOXESS_BASE_URL: str = "https://www.foxesscloud.com/op/v0"
    FOXESS_ALERT_LOW_SOC: int = int(os.getenv("FOXESS_ALERT_LOW_SOC", "10"))

    # Daikin
    DAIKIN_CLIENT_ID: str = os.getenv("DAIKIN_CLIENT_ID", "")
    DAIKIN_CLIENT_SECRET: str = os.getenv("DAIKIN_CLIENT_SECRET", "")
    DAIKIN_REDIRECT_URI: str = os.getenv("DAIKIN_REDIRECT_URI", "http://localhost:8080/callback")
    DAIKIN_TOKEN_FILE: Path = Path(os.getenv("DAIKIN_TOKEN_FILE", ".daikin-tokens.json"))
    DAIKIN_BASE_URL: str = "https://api.onecta.daikineurope.com/v1"
    DAIKIN_AUTH_URL: str = "https://idp.onecta.daikineurope.com/v1/oidc/authorize"
    DAIKIN_TOKEN_URL: str = "https://idp.onecta.daikineurope.com/v1/oidc/token"
    DAIKIN_ALERT_TEMP_DEVIATION: float = float(os.getenv("DAIKIN_ALERT_TEMP_DEVIATION", "2"))

    # Alerts
    ALERT_WHATSAPP_NUMBER: str = os.getenv("ALERT_WHATSAPP_NUMBER", "")


config = Config()
