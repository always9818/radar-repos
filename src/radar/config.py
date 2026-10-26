"""Carga de configuración desde .env."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

RAIZ = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Config:
    github_token: str
    anthropic_api_key: str
    anthropic_model: str
    database_path: Path
    github_min_remaining: int


def cargar_config() -> Config:
    load_dotenv(RAIZ / ".env")
    db = Path(os.getenv("DATABASE_PATH", "data/radar.db"))
    if not db.is_absolute():
        db = RAIZ / db
    return Config(
        github_token=os.getenv("GITHUB_TOKEN", "").strip(),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", "").strip(),
        anthropic_model=os.getenv("ANTHROPIC_MODEL", "").strip() or "claude-opus-5-5",
        database_path=db,
        github_min_remaining=int(os.getenv("GITHUB_MIN_REMAINING", "500")),
    )
