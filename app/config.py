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
    linkup_api_key: str = ""  # place data: coords, ratings, review quotes
    serpapi_api_key: str = ""  # used only to fetch a real photo per place
    default_destination: str = "Goa, India"  # when the user names no destination
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
    # Model label shown in the run trace (provider is not scored; display only).
    hermes_model: str = "gpt-5.6-sol"

    # Per-token cost estimate for the observability trace. Real token counts are
    # not exposed by `hermes -z`, so the trace estimates tokens at ~4 chars each
    # and prices them at these blended rates ($/1M tokens). Tune to your model.
    cost_input_per_mtok: float = 2.5
    cost_output_per_mtok: float = 10.0

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
    def places_dir(self) -> Path:
        return self.data_dir / "places"


settings = Settings()

for _dir in (
    settings.trips_dir,
    settings.videos_dir,
    settings.sessions_dir,
    settings.hermes_workdir,
    settings.places_dir,
):
    _dir.mkdir(parents=True, exist_ok=True)
