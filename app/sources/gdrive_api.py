"""Google Drive REST API v3 source (service account).

Setup
-----
1. Google Cloud Console -> create a project -> enable **Google Drive API**.
2. Create a **service account**, create a JSON key, download it to
   ``secrets/gdrive-service-account.json``.
3. In Google Drive, share the TA's folder with the service account e-mail
   (it looks like ``something@project.iam.gserviceaccount.com``).  Viewer
   permission is enough.
4. Copy the folder id from the folder URL
   ``https://drive.google.com/drive/folders/<THIS PART>`` into
   ``[sources.gdrive_api] root_folder_id``.

Only ``google-auth`` and ``requests`` are required - the Drive calls themselves
use ``urllib``, so ``google-api-python-client`` is not needed.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .base import (Source, SourceError, ScanResult, SubmissionRef,
                   VERSION_NAMES, parse_filename)

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
API = "https://www.googleapis.com/drive/v3"
FOLDER_MIME = "application/vnd.google-apps.folder"
FILE_FIELDS = "id,name,mimeType,modifiedTime,md5Checksum,size,parents"


class GoogleDriveSource(Source):
    name = "gdrive_api"

    def __init__(self, cfg):
        self.cfg = cfg
        self.folder_id = (cfg.gdrive_api.root_folder_id or "").strip()
        self.credentials_path = Path(cfg.gdrive_api.credentials_file)
        if not self.credentials_path.is_absolute():
            from app.config import REPO_ROOT
            self.credentials_path = REPO_ROOT / self.credentials_path
        self.include_owndata = bool(cfg.gdrive_api.include_owndata)
        if self.include_owndata:
            self.include_owndata = bool(cfg.challenge.enable_owndata)
        self._token: str | None = None
        self._token_expiry = 0.0
        self._version_cache: dict[str, str | None] = {}

    # ------------------------------------------------------------------
    def describe(self) -> str:
        return f"Google Drive API  folder {self.folder_id or '(unset)'}"

    def validate(self) -> None:
        if not self.folder_id:
            raise SourceError(
                "sources.gdrive_api.root_folder_id is empty. Copy the folder id "
                "from the Drive folder URL.")
        if not self.credentials_path.is_file():
            raise SourceError(
                f"Service-account key not found: {self.credentials_path}\n"
                "Download the JSON key from Google Cloud Console and save it "
                "there.")
        try:
            import google.auth  # noqa: F401
        except ModuleNotFoundError as exc:
            raise SourceError(
                "The Drive API source needs two packages:\n"
                "    pip install -r requirements-drive.txt\n"
                "(google-auth and requests). Alternatively switch "
                "[sources] kind = \"local_mirror\".") from exc
        # fail fast on a broken key or a folder that was never shared
        self._get_token()
        try:
            self._list_folder(self.folder_id, page_size=1)
        except SourceError as exc:
            raise SourceError(
                f"Cannot list Drive folder {self.folder_id}.\n{exc}\n"
                "Did you share the folder with the service account e-mail "
                "address?") from exc

    # ------------------------------------------------------------------
    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        try:
            from google.auth.transport.requests import Request
            from google.oauth2 import service_account
        except ModuleNotFoundError as exc:
            raise SourceError(
                "google-auth is not installed. Run:\n"
                "    pip install -r requirements-drive.txt") from exc
        try:
            credentials = service_account.Credentials.from_service_account_file(
                str(self.credentials_path), scopes=SCOPES)
            credentials.refresh(Request())
        except Exception as exc:                       # noqa: BLE001
            raise SourceError(f"Service-account authentication failed: {exc}") from exc
        self._token = credentials.token
        expiry = getattr(credentials, "expiry", None)
        self._token_expiry = expiry.timestamp() if expiry else time.time() + 3000
        return self._token

    def _request(self, url: str, *, binary: bool = False) -> bytes:
        request = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {self._get_token()}"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")[:400]
            except OSError:
                pass
            raise SourceError(f"Drive API {exc.code} for {url}\n{body}") from exc
        except urllib.error.URLError as exc:
            raise SourceError(f"Cannot reach the Drive API: {exc.reason}") from exc

    def _list_folder(self, folder_id: str, page_size: int = 200) -> list[dict]:
        out: list[dict] = []
        token = None
        while True:
            params = {
                "q": f"'{folder_id}' in parents and trashed = false",
                "fields": f"nextPageToken,files({FILE_FIELDS})",
                "pageSize": str(page_size),
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            }
            if token:
                params["pageToken"] = token
            url = f"{API}/files?" + urllib.parse.urlencode(params)
            payload = json.loads(self._request(url).decode("utf-8"))
            out.extend(payload.get("files", []))
            token = payload.get("nextPageToken")
            if not token:
                break
        return out

    def _download(self, file_id: str) -> bytes:
        url = f"{API}/files/{file_id}?alt=media&supportsAllDrives=true"
        return self._request(url, binary=True)

    def _version_label(self, folder_id: str, files: list[dict],
                       group: str, kind: str, stem: str) -> str | None:
        wanted = {f"{group}_version.txt".lower(),
                  f"{group}_{kind}_version.txt".lower(),
                  f"{stem}.version.txt".lower(),
                  VERSION_NAMES[0], VERSION_NAMES[1]}
        for item in files:
            if item["name"].lower() in wanted:
                cache_key = item["id"] + (item.get("md5Checksum") or item.get("modifiedTime", ""))
                if cache_key in self._version_cache:
                    return self._version_cache[cache_key]
                try:
                    text = self._download(item["id"]).decode("utf-8", "replace")
                except SourceError:
                    return None
                label = next((line.strip() for line in text.splitlines()
                              if line.strip()), None)
                label = label[:200] if label else None
                self._version_cache[cache_key] = label
                return label
        return None

    # ------------------------------------------------------------------
    def scan(self) -> ScanResult:
        result = ScanResult()
        root_items = self._list_folder(self.folder_id)

        candidates: list[tuple[dict, dict | None]] = []   # (file, parent)
        child_cache: dict[str, list[dict]] = {}

        for item in root_items:
            if item.get("mimeType") == FOLDER_MIME:
                continue
            candidates.append((item, None))

        for item in root_items:
            if item.get("mimeType") != FOLDER_MIME:
                continue
            children = self._list_folder(item["id"])
            child_cache[item["id"]] = children
            for child in children:
                if child.get("mimeType") != FOLDER_MIME:
                    candidates.append((child, item))

        for item, parent in candidates:
            parsed = parse_filename(item.get("name", ""))
            if not parsed:
                continue
            group, kind = parsed
            if parent is None or parent.get('name', '').upper() != group:
                result.errors.append(f"Wrong folder: put {item['name']} in {group}/{item['name']}")
                continue
            if kind == "owndata" and not self.include_owndata:
                continue
            try:
                modified = _iso_to_epoch(item.get("modifiedTime"))
            except ValueError:
                modified = 0.0
            size = int(item.get("size") or 0)
            signature = item.get("md5Checksum") or f"{modified}:{size}"
            folder_id = parent["id"] if parent else self.folder_id
            siblings = child_cache.get(folder_id, root_items)
            stem = item["name"].rsplit(".", 1)[0]
            result.groups_seen.add(group)
            label = self._version_label(folder_id, siblings, group, kind, stem)
            result.refs.append(SubmissionRef(
                group=group,
                kind=kind,
                filename=item["name"],
                locator=item["id"],
                source=self.name,
                modified=modified,
                size=size,
                signature=f"{signature}:{label or ''}",
                method_label=label or "",
            ))
        return result

    def read(self, ref: SubmissionRef) -> bytes:
        return self._download(ref.locator)


def _iso_to_epoch(value: str | None) -> float:
    if not value:
        return 0.0
    from datetime import datetime
    text = value.replace("Z", "+00:00")
    return datetime.fromisoformat(text).timestamp()
