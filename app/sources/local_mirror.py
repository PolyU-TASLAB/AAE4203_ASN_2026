"""Google Drive for Desktop mirrored folder.

The TA shares a Drive folder; the machine running this server has Drive for
Desktop installed and the folder synced to a local path.  We simply watch that
path, so there is no OAuth, no service account and no network code at all.
"""

from __future__ import annotations

import os
from pathlib import Path

from .base import (Source, SourceError, ScanResult, SubmissionRef,
                   VERSION_NAMES, parse_filename, read_version_hint)


class LocalMirrorSource(Source):
    name = "local_mirror"

    def __init__(self, cfg):
        self.cfg = cfg
        self.root = Path(cfg.local_mirror.root)
        if not self.root.is_absolute():
            from app.config import REPO_ROOT
            self.root = REPO_ROOT / self.root
        self.include_owndata = bool(cfg.local_mirror.include_owndata)

    def describe(self) -> str:
        return f"local mirror  {self.root}"

    def validate(self) -> None:
        if not self.root.exists():
            raise SourceError(
                f"Local mirror folder does not exist:\n  {self.root}\n"
                "Create it, or point [sources.local_mirror] root at the folder "
                "Google Drive for Desktop syncs.")
        if not self.root.is_dir():
            raise SourceError(f"Not a folder: {self.root}")
        if not os.access(self.root, os.R_OK):
            raise SourceError(f"No read permission on {self.root}")

    # ------------------------------------------------------------------
    def scan(self) -> ScanResult:
        result = ScanResult()
        if not self.root.is_dir():
            result.errors.append(f"Mirror folder missing: {self.root}")
            return result

        for path in sorted(self.root.rglob("*")):
            try:
                if not path.is_file():
                    continue
            except OSError:
                continue
            if path.name.startswith(".") or path.name.startswith("~$"):
                continue
            parsed = parse_filename(path.name)
            if not parsed:
                continue
            group, kind = parsed
            if path.parent.name.upper() != group:
                result.errors.append(f"Wrong folder: put {path.name} in {group}/{path.name}")
                continue
            if kind == "owndata" and not self.include_owndata:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            result.groups_seen.add(group)
            folder = path.parent
            hint = read_version_hint([
                folder / f"{group}_version.txt",
                folder / f"{group}_{kind}_version.txt",
                folder / f"{path.stem}.version.txt",
                folder / VERSION_NAMES[0],
                folder / VERSION_NAMES[1],
            ])
            result.refs.append(SubmissionRef(
                group=group,
                kind=kind,
                filename=path.name,
                locator=str(path),
                source=self.name,
                modified=stat.st_mtime,
                size=stat.st_size,
                signature=f"{stat.st_mtime_ns}:{stat.st_size}:{hint or ''}",
                method_label=hint or "",
            ))
        return result

    def read(self, ref: SubmissionRef) -> bytes:
        try:
            with open(ref.locator, "rb") as handle:
                return handle.read()
        except OSError as exc:
            raise SourceError(f"Could not read {ref.filename}: {exc}") from exc
