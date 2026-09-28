from dataclasses import dataclass
import os


@dataclass(frozen=True)
class Settings:
    db_path: str = os.environ.get("FAMILYOS_DB", "data/familyos.db")
    host: str = os.environ.get("FAMILYOS_HOST", "127.0.0.1")
    port: int = int(os.environ.get("FAMILYOS_PORT", "8080"))
    secure_cookies: bool = os.environ.get("FAMILYOS_SECURE_COOKIES", "false").lower() in {"1", "true", "yes"}
    system_status_path: str = os.environ.get("FAMILYOS_SYSTEM_STATUS", "")
    ollama_url: str = os.environ.get("FAMILYOS_OLLAMA_URL", "")
    ollama_model: str = os.environ.get("FAMILYOS_OLLAMA_MODEL", "")
    local_model_hosts: str = os.environ.get("FAMILYOS_LOCAL_MODEL_HOSTS", "")
    cloud_api_url: str = os.environ.get("FAMILYOS_CLOUD_API_URL", "")
    cloud_api_key: str = os.environ.get("FAMILYOS_CLOUD_API_KEY", "")
    cloud_model: str = os.environ.get("FAMILYOS_CLOUD_MODEL", "")
    butler_state_url: str = os.environ.get("FAMILYOS_BUTLER_STATE_URL", "http://butler-web:3000/api/state?key=ddls")
    voice_asr_url: str = os.environ.get("FAMILYOS_VOICE_ASR_URL", "http://voice-asr:8091")
    voice_tts_url: str = os.environ.get("FAMILYOS_VOICE_TTS_URL", "")
    voice_tts_token: str = os.environ.get("FAMILYOS_VOICE_TTS_TOKEN", "")
    funasr_url: str = os.environ.get("FAMILYOS_FUNASR_URL", "")
