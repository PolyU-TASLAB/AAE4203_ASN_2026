"""Common interface for submission sources."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

FILE_RE = re.compile(
    r"^(?P<group>G\d{2})_(?P<kind>benchmark|owndata)\.csv$",
    re.IGNORECASE)

VERSION_NAMES = ("version.txt", "method.txt")


class SourceError(RuntimeError):
    """Raised when a source cannot be reached or is misconfigured."""


@dataclass
class SubmissionRef:
    group: str
    kind: str                       # benchmark | owndata
    filename: str
    locator: str                    # local path, or Drive file id
    source: str                     # local_mirror | gdrive_api
    modified: float = 0.0           # epoch seconds
    size: int = 0
    signature: str = ""             # cheap change key
    method_label: str | None = None

    def key(self) -> tuple[str, str]:
        return (self.group, self.kind)


@dataclass
class ScanResult:
    refs: list[SubmissionRef] = field(default_factory=list)
    groups_seen: set[str] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)


def parse_filename(name: str) -> tuple[str, str] | None:
    """``G05_benchmark.csv`` -> ``("G05", "benchmark")``."""
    match = FILE_RE.match(name.strip())
    if not match:
        return None
    return match.group("group").upper(), match.group("kind").lower()


def read_version_hint(candidates: list[Path]) -> str | None:
    """First non-empty line of a version sidecar file, if one exists."""
    for path in candidates:
        try:
            if path.is_file():
                text = path.read_text(encoding="utf-8", errors="replace")
                for line in text.splitlines():
                    line = line.strip()
                    if line:
                        return line[:200]
        except OSError:
            continue
    return None


class Source:
    """Base class - subclasses implement :meth:`scan` and :meth:`read`."""

    name = "base"
    supports_method_label = True

    def scan(self) -> ScanResult:
        raise NotImplementedError

    def read(self, ref: SubmissionRef) -> bytes:
        raise NotImplementedError

    def describe(self) -> str:
        return self.name

    def validate(self) -> None:
        """Raise :class:`SourceError` if the configuration cannot work."""


def make_source(cfg) -> Source:
    """Factory driven by ``[sources] kind``."""
    if cfg.source_kind == "local_mirror":
        from .local_mirror import LocalMirrorSource
        return LocalMirrorSource(cfg)
    if cfg.source_kind == "gdrive_api":
        from .gdrive_api import GoogleDriveSource
        return GoogleDriveSource(cfg)
    raise SourceError(f"unknown source kind: {cfg.source_kind}")
