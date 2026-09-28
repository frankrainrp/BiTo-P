from .config import Settings
from .db import Database
from .http import FamilyOSServer


def main():
    settings = Settings()
    database = Database(settings.db_path)
    version = database.migrate()
    server = FamilyOSServer(
        (settings.host, settings.port), database,
        secure_cookies=settings.secure_cookies,
        system_status_path=settings.system_status_path,
        ollama_url=settings.ollama_url,
        ollama_model=settings.ollama_model,
        local_model_hosts=settings.local_model_hosts,
        cloud_api_url=settings.cloud_api_url,
        cloud_api_key=settings.cloud_api_key,
        cloud_model=settings.cloud_model,
        butler_state_url=settings.butler_state_url,
        voice_asr_url=settings.voice_asr_url,
        funasr_url=settings.funasr_url,
        voice_tts_url=settings.voice_tts_url,
        voice_tts_token=settings.voice_tts_token,
    )
    print(f"FamilyOS listening on http://{settings.host}:{settings.port} (schema {version})")
    server.start_workers()
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
