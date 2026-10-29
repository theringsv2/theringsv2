"""Nguồn hồ sơ: Google Drive API (đọc-only) hoặc thư mục local (Google Drive for Desktop / ổ mạng)."""

from __future__ import annotations

import hashlib
import io
import logging
import mimetypes
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
FOLDER = "application/vnd.google-apps.folder"
SHORTCUT = "application/vnd.google-apps.shortcut"
# Google Docs/Sheets/Slides -> xuất sang định dạng Office để dùng chung bộ trích xuất
EXPORTS = {
    "application/vnd.google-apps.document":
        (".docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    "application/vnd.google-apps.spreadsheet":
        (".xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    "application/vnd.google-apps.presentation":
        (".pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
}


@dataclass
class RemoteFile:
    source_id: str
    name: str
    path: str            # thư mục chứa, tương đối so với gốc dự án
    ext: str
    mime: str | None
    size: int | None
    modified: str | None
    hash: str            # md5 của Drive / local; Google-native dùng "rev:<modifiedTime>"
    web_link: str | None
    _ref: object = None  # dữ liệu riêng của nguồn để tải file


class DriveSource:
    def __init__(self, folder_id: str, credentials: str, token: str):
        self.folder_id = folder_id
        self.service = self._build(credentials, token)

    @staticmethod
    def _build(credentials: str, token: str):
        import json

        from googleapiclient.discovery import build

        info = json.loads(Path(credentials).read_text(encoding="utf-8"))
        if info.get("type") == "service_account":
            from google.oauth2 import service_account
            creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
        else:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            creds = None
            if Path(token).exists():
                creds = Credentials.from_authorized_user_file(token, SCOPES)
            if not creds or not creds.valid:
                if creds and creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                else:
                    from google_auth_oauthlib.flow import InstalledAppFlow
                    flow = InstalledAppFlow.from_client_secrets_file(credentials, SCOPES)
                    creds = flow.run_local_server(port=0)
                Path(token).parent.mkdir(parents=True, exist_ok=True)
                Path(token).write_text(creds.to_json(), encoding="utf-8")
                os.chmod(token, 0o600)
        return build("drive", "v3", credentials=creds, cache_discovery=False)

    def _children(self, folder_id: str) -> Iterator[dict]:
        token = None
        while True:
            resp = self.service.files().list(
                q=f"'{folder_id}' in parents and trashed=false",
                fields="nextPageToken, files(id,name,mimeType,md5Checksum,modifiedTime,size,webViewLink,"
                       "shortcutDetails)",
                pageSize=1000, pageToken=token,
                supportsAllDrives=True, includeItemsFromAllDrives=True,
            ).execute(num_retries=5)
            yield from resp.get("files", [])
            token = resp.get("nextPageToken")
            if not token:
                return

    def walk(self) -> Iterator[RemoteFile]:
        stack = [(self.folder_id, "")]
        visited = {self.folder_id}
        while stack:
            fid, path = stack.pop()
            for f in self._children(fid):
                mime = f["mimeType"]
                if mime == SHORTCUT:
                    sd = f.get("shortcutDetails", {})
                    tgt = sd.get("targetId")
                    if not tgt:
                        continue
                    if sd.get("targetMimeType") == FOLDER:
                        if tgt not in visited:
                            visited.add(tgt)
                            stack.append((tgt, f"{path}/{f['name']}".strip("/")))
                        continue
                    try:
                        f = self.service.files().get(
                            fileId=tgt, supportsAllDrives=True,
                            fields="id,name,mimeType,md5Checksum,modifiedTime,size,webViewLink").execute(num_retries=5)
                    except Exception as e:  # noqa: BLE001 - shortcut hỏng không làm dừng cả lượt quét
                        log.warning("Bỏ qua shortcut hỏng %s: %s", f["name"], e)
                        continue
                    mime = f["mimeType"]
                if mime == FOLDER:
                    if f["id"] not in visited:
                        visited.add(f["id"])
                        stack.append((f["id"], f"{path}/{f['name']}".strip("/")))
                    continue
                name = f["name"]
                if mime in EXPORTS:
                    ext = EXPORTS[mime][0]
                    hash_ = "rev:" + f.get("modifiedTime", "")
                elif mime.startswith("application/vnd.google-apps."):
                    continue  # form, map, drawing… không có nội dung văn bản
                else:
                    ext = Path(name).suffix.lower()
                    hash_ = f.get("md5Checksum") or "rev:" + f.get("modifiedTime", "")
                yield RemoteFile(
                    source_id=f"drive:{f['id']}", name=name, path=path, ext=ext, mime=mime,
                    size=int(f["size"]) if f.get("size") else None, modified=f.get("modifiedTime"),
                    hash=hash_, web_link=f.get("webViewLink"), _ref=f)

    def download(self, rf: RemoteFile, dest: Path) -> Path:
        from googleapiclient.http import MediaIoBaseDownload

        f = rf._ref
        dest.parent.mkdir(parents=True, exist_ok=True)
        if rf.mime in EXPORTS:
            req = self.service.files().export_media(fileId=f["id"], mimeType=EXPORTS[rf.mime][1])
        else:
            req = self.service.files().get_media(fileId=f["id"], supportsAllDrives=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        with io.FileIO(tmp, "wb") as fh:
            dl = MediaIoBaseDownload(fh, req, chunksize=16 * 1024 * 1024)
            done = False
            while not done:
                _, done = dl.next_chunk(num_retries=5)
        tmp.replace(dest)
        return dest


class LocalSource:
    """Quét thư mục đã đồng bộ về máy (vd. Google Drive for Desktop: G:/My Drive/.../NAM HÀ)."""

    def __init__(self, root: str):
        self.root = Path(root).expanduser().resolve()
        if not self.root.is_dir():
            raise FileNotFoundError(f"Không thấy thư mục: {self.root}")

    @staticmethod
    def _md5(p: Path) -> str:
        h = hashlib.md5()
        with p.open("rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                h.update(block)
        return h.hexdigest()

    def walk(self) -> Iterator[RemoteFile]:
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            rel = Path(dirpath).relative_to(self.root).as_posix()
            rel = "" if rel == "." else rel
            for fn in sorted(filenames):
                if fn.startswith((".", "~$")) or fn.endswith((".gdoc", ".gsheet", ".gslides")):
                    continue
                p = Path(dirpath) / fn
                st = p.stat()
                yield RemoteFile(
                    source_id=f"local:{p}", name=fn, path=rel, ext=p.suffix.lower(),
                    mime=mimetypes.guess_type(fn)[0], size=st.st_size,
                    modified=str(int(st.st_mtime)), hash=f"lazy:{st.st_size}:{int(st.st_mtime)}", web_link=None,
                    _ref=p)

    def resolve_hash(self, rf: RemoteFile) -> str:
        return self._md5(rf._ref)

    def download(self, rf: RemoteFile, dest: Path) -> Path:
        return rf._ref  # đọc trực tiếp, không sao chép
