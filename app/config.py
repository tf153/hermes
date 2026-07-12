from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    telegram_bot_token: str = ""
    linkup_api_key: str = ""
    public_base_url: str = "http://localhost:8000"
    trip_ttl_hours: int = 24

    # ElevenLabs text-to-speech for the video narration.
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = "21m00Tcm4TlvDq8ikWAM"  # "Rachel" default prebuilt voice
    elevenlabs_model_id: str = "eleven_multilingual_v2"

    # Video output.
    video_width: int = 720
    video_height: int = 1280
    music_path: str = ""  # optional background music file

    hermes_bin: str = "hermes"
    hermes_timeout_seconds: int = 420

    data_dir: Path = BASE_DIR / "data"

    @property
    def trips_dir(self) -> Path:
        return self.data_dir / "trips"

    @property
    def videos_dir(self) -> Path:
        return self.data_dir / "videos"

    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "sessions"

    @property
    def hermes_workdir(self) -> Path:
        return self.data_dir / "hermes_workdir"

    @property
    def places_cache_path(self) -> Path:
        return self.data_dir / "goa_places.json"


settings = Settings()

for _dir in (
    settings.trips_dir,
    settings.videos_dir,
    settings.sessions_dir,
    settings.hermes_workdir,
):
    _dir.mkdir(parents=True, exist_ok=True)
