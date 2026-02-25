"""Config loader — reads from .env or environment variables."""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


class Config:
    # Fox ESS — Option A: Open API key
    FOXESS_API_KEY: str = os.getenv("FOXESS_API_KEY", "")
    # Fox ESS — Option B: username/password (unofficial, works for endUser accounts)
    FOXESS_USERNAME: str = os.getenv("FOXESS_USERNAME", "")
    FOXESS_PASSWORD: str = os.getenv("FOXESS_PASSWORD", "")
    # Fox ESS — device serial (always required)
    FOXESS_DEVICE_SN: str = os.getenv("FOXESS_DEVICE_SN", "")
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

    def foxess_client_kwargs(self) -> dict:
        """Return the right kwargs for FoxESSClient based on what's configured."""
        if not self.FOXESS_DEVICE_SN:
            raise ValueError("FOXESS_DEVICE_SN is required. Find it in foxesscloud.com → Devices.")
        kwargs = {"device_sn": self.FOXESS_DEVICE_SN}
        if self.FOXESS_API_KEY:
            kwargs["api_key"] = self.FOXESS_API_KEY
        elif self.FOXESS_USERNAME and self.FOXESS_PASSWORD:
            kwargs["username"] = self.FOXESS_USERNAME
            kwargs["password"] = self.FOXESS_PASSWORD
        else:
            raise ValueError(
                "Fox ESS auth not configured.\n"
                "Set either FOXESS_API_KEY or FOXESS_USERNAME + FOXESS_PASSWORD in .env"
            )
        return kwargs


config = Config()
