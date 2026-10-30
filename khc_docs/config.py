"""Đọc cấu hình TOML (mặc định ./khc_docs.toml hoặc biến môi trường KHC_DOCS_CONFIG)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .extract import ExtractOptions


@dataclass
class Project:
    code: str
    name: str
    drive_folder_id: str | None = None
    local_path: str | None = None
    exclude_paths: list[str] = field(default_factory=list)   # tiền tố thư mục bỏ qua, vd "11-HÌNH ẢNH THI CÔNG"
    include_ext: list[str] | None = None
    max_file_mb: float = 200
    ocr_images: bool = True


@dataclass
class Config:
    base: Path
    db: Path
    cache_dir: Path
    credentials: Path
    token: Path
    keep_cache: bool
    workers: int
    extract: ExtractOptions
    projects: dict[str, Project]

    def project(self, code: str | None) -> Project:
        if code is None:
            if len(self.projects) == 1:
                return next(iter(self.projects.values()))
            raise SystemExit(f"Cần chỉ định --project, có: {', '.join(self.projects)}")
        try:
            return self.projects[code.upper()]
        except KeyError:
            raise SystemExit(f"Không có dự án '{code}' trong cấu hình. Có: {', '.join(self.projects)}") from None


def load(path: str | None = None) -> Config:
    p = Path(path or os.environ.get("KHC_DOCS_CONFIG", "khc_docs.toml")).expanduser().resolve()
    if not p.exists():
        raise SystemExit(f"Không thấy file cấu hình {p}. Sao chép khc_docs.example.toml -> khc_docs.toml")
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8-sig"))
    except tomllib.TOMLDecodeError as e:
        raise SystemExit(
            f"Lỗi cú pháp trong {p}: {e}\n"
            "Gợi ý: đường dẫn Windows phải đặt trong ngoặc đơn 'G:\\My Drive\\...' "
            "hoặc dùng dấu / trong ngoặc kép \"G:/My Drive/...\"") from None
    base = p.parent

    def rel(v: str) -> Path:
        q = Path(v).expanduser()
        return q if q.is_absolute() else base / q

    idx, g, ex = raw.get("index", {}), raw.get("google", {}), raw.get("extract", {})
    projects = {}
    for pr in raw.get("projects", []):
        prj = Project(code=pr["code"].upper(), name=pr.get("name", pr["code"]),
                      drive_folder_id=pr.get("drive_folder_id"), local_path=pr.get("local_path"),
                      exclude_paths=pr.get("exclude_paths", []), include_ext=pr.get("include_ext"),
                      max_file_mb=pr.get("max_file_mb", 200), ocr_images=pr.get("ocr_images", True))
        if not (prj.drive_folder_id or prj.local_path):
            raise SystemExit(f"Dự án {prj.code}: cần drive_folder_id hoặc local_path")
        projects[prj.code] = prj
    return Config(
        base=base,
        db=rel(idx.get("db", "data/khc_docs.db")),
        cache_dir=rel(idx.get("cache_dir", "data/cache")),
        credentials=rel(g.get("credentials", "secrets/credentials.json")),
        token=rel(g.get("token", "secrets/token.json")),
        keep_cache=idx.get("keep_cache", True),
        workers=int(idx.get("workers", max(1, (os.cpu_count() or 2) - 1))),
        extract=ExtractOptions(**{k: v for k, v in ex.items() if k in ExtractOptions.__dataclass_fields__}),
        projects=projects,
    )
