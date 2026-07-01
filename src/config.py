"""Config loading. Single source of truth = config.yaml + .env secrets.

Everything downstream reads a typed Config object, so nothing in the code has
hard-coded URLs, delays, or model names.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel

load_dotenv()  # pull ANTHROPIC_API_KEY etc. from .env into environment


class CategoryConfig(BaseModel):
    name: str
    root: str
    subcategories: list[str]


class SiteConfig(BaseModel):
    base_url: str
    sitemap: str


class CrawlConfig(BaseModel):
    min_delay_seconds: float = 2.0
    max_concurrency: int = 2
    timeout_seconds: int = 30
    user_agent: str = "FrontierDentalPOC/0.1"
    respect_robots: bool = True
    render_mode: str = "auto"          # auto | never | always
    render_min_bytes: int = 20000
    max_retries: int = 3
    backoff_base_seconds: float = 2.0
    max_products_per_subcategory: Optional[int] = None


class LLMConfig(BaseModel):
    enabled: bool = True
    provider: str = "anthropic"
    model: str = "claude-haiku-4-5-20251001"
    max_fallback_calls: int = 200


class StorageConfig(BaseModel):
    sqlite_path: str = "data/catalog.db"
    export_dir: str = "data"
    exports: list[str] = ["json", "csv"]


class LoggingConfig(BaseModel):
    level: str = "INFO"
    file: str = "logs/crawl.log"


class Config(BaseModel):
    site: SiteConfig
    categories: list[CategoryConfig]
    crawl: CrawlConfig
    llm: LLMConfig
    storage: StorageConfig
    logging: LoggingConfig

    @classmethod
    def load(cls, path: str | Path = "config.yaml") -> "Config":
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return cls(**data)

    def abs_url(self, path: str) -> str:
        """Join a site-relative path onto the base URL."""
        if path.startswith("http"):
            return path
        return self.site.base_url.rstrip("/") + "/" + path.lstrip("/")
