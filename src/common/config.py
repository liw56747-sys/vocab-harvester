"""配置管理：加载 settings.yaml 并提供 Pydantic 类型安全的配置访问"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator


def _resolve_data_dir() -> str:
    """解析数据目录：优先使用环境变量 VOCAB_DATA_DIR，否则使用默认用户数据目录"""
    env_dir = os.environ.get("VOCAB_DATA_DIR")
    if env_dir:
        return env_dir
    # 打包后使用用户主目录，开发时使用项目根目录
    import sys
    if getattr(sys, "frozen", False):
        return str(Path.home() / ".vocab-harvester")
    return "./data"


# ── Pydantic 配置模型 ──────────────────────────────────────

class AppConfig(BaseModel):
    name: str = "vocab-harvester"
    log_level: str = "INFO"
    data_dir: str = ""

    def model_post_init(self, __context):
        # 环境变量始终优先（确保打包后数据目录不受 settings.yaml 影响）
        env_dir = os.environ.get("VOCAB_DATA_DIR")
        if env_dir:
            self.data_dir = env_dir
        elif not self.data_dir or self.data_dir == "./data":
            self.data_dir = _resolve_data_dir()


class DatabaseConfig(BaseModel):
    url: str = ""

    def model_post_init(self, __context):
        # 动态生成数据库 URL，确保路径跟随 data_dir 而非硬编码
        if not self.url:
            import sys
            if getattr(sys, "frozen", False):
                db_path = Path.home() / ".vocab-harvester" / "vocab.db"
            else:
                db_path = Path("./data") / "vocab.db"
            self.url = f"sqlite+aiosqlite:///{db_path}"


class CrawlerPlatformConfig(BaseModel):
    enabled: bool = False
    rate_limit: int = 5
    search_queries: list[str] = Field(default_factory=list)
    # 平台特有字段
    app_key: str = ""
    app_secret: str = ""
    api_key: str = ""
    api_secret: str = ""
    bearer_token: str = ""


class CrawlersConfig(BaseModel):
    weibo: CrawlerPlatformConfig = Field(default_factory=CrawlerPlatformConfig)
    xiaohongshu: CrawlerPlatformConfig = Field(default_factory=CrawlerPlatformConfig)
    twitter: CrawlerPlatformConfig = Field(default_factory=CrawlerPlatformConfig)
    reddit: CrawlerPlatformConfig = Field(default_factory=CrawlerPlatformConfig)


class WorkflowApiConfig(BaseModel):
    base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    api_key: str = ""
    workflow_id: str = ""
    model: str = "glm-4-plus"
    backup_model: str = "glm-4-flash"
    timeout: int = 300
    poll_interval: int = 5
    batch_size: int = 10

    @field_validator("base_url", "model", "backup_model", mode="before")
    @classmethod
    def _empty_to_default(cls, v: str, info) -> str:
        """空字符串回退到字段默认值，防止配置文件空值覆盖"""
        if not v or not v.strip():
            defaults = {
                "base_url": "https://open.bigmodel.cn/api/paas/v4",
                "model": "glm-4-plus",
                "backup_model": "glm-4-flash",
            }
            return defaults.get(info.field_name, v)
        return v


class WorkflowQueueConfig(BaseModel):
    type: str = ""
    url: str = ""


class WorkflowConfig(BaseModel):
    adapter: str = "mock"
    api: WorkflowApiConfig = Field(default_factory=WorkflowApiConfig)
    queue: WorkflowQueueConfig = Field(default_factory=WorkflowQueueConfig)


class VocabularyConfig(BaseModel):
    auto_approve_threshold: int = 10
    max_context_samples: int = 5
    dedup_similarity: float = 0.85


class SchedulerConfig(BaseModel):
    enabled: bool = False
    cron: str = "0 */6 * * *"
    default_queries: list[str] = Field(default_factory=list)


class Settings(BaseModel):
    app: AppConfig = Field(default_factory=AppConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    crawlers: CrawlersConfig = Field(default_factory=CrawlersConfig)
    workflow: WorkflowConfig = Field(default_factory=WorkflowConfig)
    vocabulary: VocabularyConfig = Field(default_factory=VocabularyConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)


# ── 加载逻辑 ──────────────────────────────────────────────

_settings: Settings | None = None


def load_settings(config_path: str | Path | None = None) -> Settings:
    """加载配置文件，支持指定路径或默认 config/settings.yaml"""
    global _settings

    if config_path is None:
        # 从项目根目录查找
        project_root = Path(__file__).resolve().parent.parent.parent
        config_path = project_root / "config" / "settings.yaml"
    else:
        config_path = Path(config_path)

    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            raw: dict[str, Any] = yaml.safe_load(f) or {}
        _settings = Settings(**raw)
    else:
        _settings = Settings()

    return _settings


def get_settings() -> Settings:
    """获取已加载的配置，未加载则自动加载"""
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings
