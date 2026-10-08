"""Settings read from the environment (and an optional .env file in the working directory)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path = Path(".env")) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


@dataclass(frozen=True)
class Settings:
    mode: str = "private"
    db_path: Path = Path("data/jobtool.db")
    cache_ttl_min: int = 60
    refresh_interval_min: int = 0
    rate_limit_read: int = 120
    rate_limit_write: int = 30
    trust_proxy: bool = False
    demo_session_days: int = 7
    discord_webhook_url: str = ""
    user_agent: str = "JobAppToolBot/0.1 (personal job search tool)"
    presets_path: Path = field(default_factory=lambda: Path(__file__).with_name("presets.json"))

    @property
    def is_demo(self) -> bool:
        return self.mode == "demo"


def load_settings() -> Settings:
    _load_dotenv()
    mode = os.environ.get("JOBTOOL_MODE", "private").strip().lower()
    if mode not in {"private", "demo"}:
        raise ValueError(f"JOBTOOL_MODE must be 'private' or 'demo', got {mode!r}")
    presets = os.environ.get("JOBTOOL_PRESETS")
    return Settings(
        mode=mode,
        db_path=Path(os.environ.get("JOBTOOL_DB_PATH", "data/jobtool.db")),
        cache_ttl_min=_int("JOBTOOL_CACHE_TTL_MIN", 60),
        refresh_interval_min=_int("JOBTOOL_REFRESH_INTERVAL_MIN", 0),
        rate_limit_read=_int("JOBTOOL_RATE_LIMIT_READ", 120),
        rate_limit_write=_int("JOBTOOL_RATE_LIMIT_WRITE", 30),
        trust_proxy=_bool("JOBTOOL_TRUST_PROXY", False),
        demo_session_days=_int("JOBTOOL_DEMO_SESSION_DAYS", 7),
        discord_webhook_url=os.environ.get("DISCORD_WEBHOOK_URL", "").strip(),
        user_agent=os.environ.get("JOBTOOL_USER_AGENT", Settings.user_agent),
        **({"presets_path": Path(presets)} if presets else {}),
    )
