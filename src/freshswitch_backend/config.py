from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    data_go_kr_service_key: str = ""
    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model_name: str = ""
    cors_origins: list[str] = ["*"]
    explain_use_llm: bool = False
    offline_mode: bool = False
    replan_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    explain_timeout_seconds: float = Field(default=8.0, gt=0, le=10)


@lru_cache
def get_settings() -> Settings:
    return Settings()
