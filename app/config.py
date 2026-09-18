"""集中配置：所有可调参数都从环境变量来，代码里不出现魔法数字。"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ---- 数据库 ----
    db_host: str = "localhost"
    db_port: int = 5432
    db_user: str = "power"
    db_password: str = "power"
    db_name: str = "power"

    # ---- Redis ----
    redis_host: str = "localhost"
    redis_port: int = 6379

    # ---- MQTT ----
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    mqtt_topic_prefix: str = "power/breaker"

    # ---- 采集 ----
    batch_size: int = 500
    batch_flush_seconds: float = 2.0

    # ---- 异常检测（Day 3 调参只改环境变量）----
    detect_window: int = 60
    detect_mad_k: float = 4.0
    detect_min_confirm: int = 2
    leakage_limit_ma: float = 30.0
    voltage_min_v: float = 198.0
    temp_limit_c: float = 55.0
    rated_current_a: float = 40.0
    # 统计异常还需同时越过物理线，避免把"正常爬坡"判成故障
    overload_floor_a: float = 20.0

    # ---- LLM（Phase 6）----
    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_model: str = "deepseek-chat"

    @property
    def dsn(self) -> str:
        return (
            f"postgresql+asyncpg://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
