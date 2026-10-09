from functools import lru_cache
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict
import yaml

ROOT = Path(__file__).resolve().parents[1]

class Settings(BaseSettings):
    category: str = "software tools"
    products_per_day: int = 2
    max_pages_per_product: int = 5
    run_mode: str = "fast"
    max_run_minutes: int = 15
    producthunt_token: str = ""
    saashub_api_key: str = ""
    gemini_api_key: str = ""
    llm_model_fast: str = ""
    llm_model_strong: str = ""
    category_validation_model: str = "gemini-3.6-flash"
    llm_fallback_models: str = "[]"
    llm_max_calls_per_run: int = 3
    user_agent: str = "ProductResearchBot/1.0"
    database_url: str = "sqlite:///data/app.db"
    schedule_enabled: bool = True
    schedule_cron: str = "0 6 * * *"
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    @property
    def topics(self) -> dict:
        path = ROOT / "config/topics.yaml"
        return yaml.safe_load(path.read_text()) if path.exists() else {"page_topics": {}, "required_topics": []}

@lru_cache
def get_settings() -> Settings:
    return Settings()
