"""Settings: union.toml with defaults for every key. Same shape as DittoRoo's
config, but built as an object so tests can construct one directly."""
from __future__ import annotations

import pathlib
import re
import secrets
import tomllib
from dataclasses import dataclass, field
from urllib.parse import urlparse

_ROOT = pathlib.Path(__file__).resolve().parent.parent


@dataclass
class ServerSettings:
    name: str = "Union"
    host: str = "127.0.0.1"
    port: int = 8484
    public_url: str = "http://localhost:8484"
    reverse_proxy: bool = False


@dataclass
class DataSettings:
    dir: str = "data"


@dataclass
class SessionSettings:
    secret_key: str = ""
    remember_me_days: int = 30


@dataclass
class SecuritySettings:
    registration_enabled: bool = True
    # If set, the register form requires this code (NaturalRoo's pattern).
    registration_code: str = ""
    login_rate_limit: str = "10 per 5 minutes"
    # Register attempts per IP, successful or not, and wrong-code attempts per IP.
    register_rate_limit: str = "5 per 5 minutes"
    bad_code_rate_limit: str = "5 per 5 minutes"


@dataclass
class RelaySettings:
    ping_seconds: float = 20
    delivery_window_seconds: float = 30
    blob_window_seconds: float = 300


@dataclass
class Settings:
    server: ServerSettings = field(default_factory=ServerSettings)
    data: DataSettings = field(default_factory=DataSettings)
    session: SessionSettings = field(default_factory=SessionSettings)
    security: SecuritySettings = field(default_factory=SecuritySettings)
    relay: RelaySettings = field(default_factory=RelaySettings)
    config_path: pathlib.Path | None = None
    secret_generated: bool = False

    def __post_init__(self) -> None:
        if not self.session.secret_key:
            self.session.secret_key = secrets.token_hex(32)
            self.secret_generated = True

    @property
    def data_dir(self) -> pathlib.Path:
        p = pathlib.Path(self.data.dir)
        if not p.is_absolute():
            base = self.config_path.parent if self.config_path else _ROOT
            p = base / p
        return p

    @property
    def rp_id(self) -> str:
        return urlparse(self.server.public_url).hostname or "localhost"

    @property
    def origin(self) -> str:
        u = urlparse(self.server.public_url)
        return f"{u.scheme}://{u.netloc}" if u.netloc else "http://localhost:8484"

    @property
    def https(self) -> bool:
        return self.server.public_url.startswith("https://")

    @property
    def login_rate(self) -> tuple[int, int]:
        """(count, seconds) parsed from strings like '10 per 15 minutes'."""
        return parse_rate(self.security.login_rate_limit)

    @property
    def register_rate(self) -> tuple[int, int]:
        return parse_rate(self.security.register_rate_limit)

    @property
    def bad_code_rate(self) -> tuple[int, int]:
        return parse_rate(self.security.bad_code_rate_limit)


_RATE = re.compile(r"^\s*(\d+)\s*per\s*(\d+)?\s*(second|minute|hour|day)s?\s*$", re.I)
_UNIT = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}


def parse_rate(text: str) -> tuple[int, int]:
    m = _RATE.match(text)
    if not m:
        raise ValueError(f"bad rate limit: {text!r}")
    count = int(m.group(1))
    n = int(m.group(2) or 1)
    return count, n * _UNIT[m.group(3).lower()]


def load(path: str | pathlib.Path | None = None) -> Settings:
    p = pathlib.Path(path) if path else _ROOT / "union.toml"
    raw: dict = {}
    if p.exists():
        with open(p, "rb") as f:
            raw = tomllib.load(f)
    return Settings(
        server=ServerSettings(**raw.get("server", {})),
        data=DataSettings(**raw.get("data", {})),
        session=SessionSettings(**raw.get("session", {})),
        security=SecuritySettings(**raw.get("security", {})),
        relay=RelaySettings(**raw.get("relay", {})),
        config_path=p if p.exists() else None,
    )
