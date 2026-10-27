"""Configuration loading for the benchmark server.

Reads ``config.toml`` (stdlib ``tomllib``, Python 3.11+) and exposes a plain
dataclass tree with absolute paths already resolved.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:                                  # pragma: no cover
    sys.exit("Python 3.11 or newer is required (tomllib).  Found "
             f"{sys.version_info.major}.{sys.version_info.minor}.")

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8080
    page_title: str = "GNSS Benchmark Challenge"
    admin_token: str = "change-me-please"
    timezone: str = "Asia/Hong_Kong"


@dataclass
class ChallengeConfig:
    key: str = "benchmark"
    name: str = "Benchmark"
    gt_file: str = "data/ground_truth/gt.private.csv"
    availability_min: float = 90.0
    max_quality: int = 5
    match_grid_s: float = 0.1
    lenient_tolerance_s: float = 0.0
    enu_origin: str = "auto"
    enable_owndata: bool = True


@dataclass
class LocalMirrorConfig:
    root: str = "drive_mirror"
    include_owndata: bool = True


@dataclass
class GDriveConfig:
    root_folder_id: str = ""
    credentials_file: str = "secrets/gdrive-service-account.json"
    include_owndata: bool = True
    initial_lookback_s: int = 0


@dataclass
class GroupsConfig:
    prefix: str = "G"
    count: int = 20
    width: int = 2


@dataclass
class PollingConfig:
    interval_s: float = 10.0
    settle_s: float = 2.0


@dataclass
class FairplayConfig:
    enabled: bool = True
    implausible_rmse2d_m: float = 1.0
    gt_leak_max_3d_m: float = 0.30
    flag_identical_copies: bool = True


@dataclass
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    challenge: ChallengeConfig = field(default_factory=ChallengeConfig)
    groups: GroupsConfig = field(default_factory=GroupsConfig)
    polling: PollingConfig = field(default_factory=PollingConfig)
    fairplay: FairplayConfig = field(default_factory=FairplayConfig)
    source_kind: str = "local_mirror"
    local_mirror: LocalMirrorConfig = field(default_factory=LocalMirrorConfig)
    gdrive_api: GDriveConfig = field(default_factory=GDriveConfig)
    demo_enabled: bool = False
    path: Path | None = None

    # -- resolved absolute paths -------------------------------------------
    @property
    def gt_path(self) -> Path:
        return resolve_path(self.challenge.gt_file)

    @property
    def data_dir(self) -> Path:
        return REPO_ROOT / "data"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "competition.db"

    @property
    def archive_dir(self) -> Path:
        return self.data_dir / "submissions"

    @property
    def web_dir(self) -> Path:
        return REPO_ROOT / "web"

    def group_codes(self) -> list[str]:
        return [f"{self.groups.prefix}{i:0{self.groups.width}d}"
                for i in range(1, self.groups.count + 1)]

    def parses_group(self, name: str) -> bool:
        p, w = self.groups.prefix, self.groups.width
        if len(name) != len(p) + w or not name.startswith(p):
            return False
        return name[len(p):].isdigit()


def resolve_path(value: str) -> Path:
    p = Path(value).expanduser()
    return p if p.is_absolute() else (REPO_ROOT / p)


def _section(raw: dict, name: str) -> dict:
    value = raw.get(name)
    return value if isinstance(value, dict) else {}


def _apply(obj, section: dict) -> None:
    for key, value in section.items():
        if isinstance(value, dict):
            continue
        if hasattr(obj, key):
            current = getattr(obj, key)
            if current is not None and not isinstance(value, type(current)) \
                    and not (isinstance(current, float) and isinstance(value, int)):
                try:
                    value = type(current)(value)
                except (TypeError, ValueError):
                    continue
            setattr(obj, key, value)


def load_config(path: str | os.PathLike | None = None) -> Config:
    cfg_path = Path(path) if path else (REPO_ROOT / "config.toml")
    if not cfg_path.is_file():
        raise FileNotFoundError(
            f"Configuration file not found: {cfg_path}\n"
            "Copy config.example.toml to config.toml and edit it."
        )

    with open(cfg_path, "rb") as handle:
        raw = tomllib.load(handle)

    cfg = Config(path=cfg_path)
    _apply(cfg.server, _section(raw, "server"))
    _apply(cfg.challenge, _section(raw, "challenge"))
    _apply(cfg.groups, _section(raw, "groups"))
    _apply(cfg.polling, _section(raw, "polling"))
    _apply(cfg.fairplay, _section(raw, "fairplay"))

    env_token = os.environ.get("AAE4203_ADMIN_TOKEN")
    if env_token:
        cfg.server.admin_token = env_token

    sources = _section(raw, "sources")
    kind = str(sources.get("kind", "local_mirror")).strip().lower()
    if kind in ("gdrive", "google_drive", "gdrive_api", "api"):
        kind = "gdrive_api"
    if kind not in ("local_mirror", "gdrive_api"):
        raise ValueError(f"Unknown sources.kind = {kind!r}; "
                         "use 'local_mirror' or 'gdrive_api'.")
    cfg.source_kind = kind
    _apply(cfg.local_mirror, _section(sources, "local_mirror"))
    _apply(cfg.gdrive_api, _section(sources, "gdrive_api"))

    demo = _section(raw, "demo")
    cfg.demo_enabled = bool(demo.get("enabled", False))

    return cfg
