import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    # Canvas
    CANVAS_BASE_URL = os.getenv("CANVAS_BASE_URL", "https://osu.instructure.com")
    CANVAS_TOKEN = os.getenv("CANVAS_TOKEN", "")

    # AI — uses Google Gemini (free tier) by default
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
    GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

    # Twilio (for outbound alerts/notifications only)
    TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
    TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
    TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER", "")
    YOUR_PHONE_NUMBER = os.getenv("YOUR_PHONE_NUMBER", "")

    # Piazza (optional)
    PIAZZA_EMAIL = os.getenv("PIAZZA_EMAIL", "")
    PIAZZA_PASSWORD = os.getenv("PIAZZA_PASSWORD", "")
    PIAZZA_NETWORK_ID = os.getenv("PIAZZA_NETWORK_ID", "")

    # Schedule prefs
    DIGEST_HOUR = int(os.getenv("DIGEST_HOUR", "14"))
    WAKE_HOUR = int(os.getenv("WAKE_HOUR", "12"))
    NO_WORK_AFTER_HOUR = int(os.getenv("NO_WORK_AFTER_HOUR", "22"))
    BUFFER_DAYS = int(os.getenv("BUFFER_DAYS", "1"))

    # Web UI
    WEB_PORT = int(os.getenv("WEB_PORT", "5000"))

    @classmethod
    def validate(cls):
        missing = []
        if not cls.CANVAS_TOKEN:
            missing.append("CANVAS_TOKEN")
        if not cls.GEMINI_API_KEY:
            missing.append("GEMINI_API_KEY")
        return missing

    @classmethod
    def sms_enabled(cls):
        return bool(cls.TWILIO_ACCOUNT_SID and cls.TWILIO_AUTH_TOKEN
                    and cls.TWILIO_PHONE_NUMBER and cls.YOUR_PHONE_NUMBER)
