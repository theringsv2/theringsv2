"""Đồng bộ: quét nguồn -> tải file mới/thay đổi -> trích xuất (song song) -> lập chỉ mục. Chạy lại nhiều lần an toàn."""

from __future__ import annotations

import logging
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from .chunk import chunk_segments
from .config import Config, Project
from .extract import IMAGE_EXT, SUPPORTED, ExtractOptions, extract
from .index import Index
from .sources import DriveSource, LocalSource, RemoteFile
from .textutil import nfc

log = logging.getLogger(__name__)


@dataclass
class SyncReport:
    seen: int = 0
    unchanged: int = 0
    deduplicated: int = 0
    extracted: int = 0
    empty: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    removed: int = 0
    ocr_pages: int = 0
    seconds: float = 0.0


def _work(path: str, opt: ExtractOptions):
    """Chạy trong tiến trình con."""
    try:
        res = extract(Path(path), opt)
        return res, chunk_segments(res.segments), None
    except Exception as e:  # noqa: BLE001
        return None, None, f"{type(e).__name__}: {e}"


def _skip_reason(rf: RemoteFile, prj: Project) -> str | None:
    full = nfc(f"{rf.path}/{rf.name}".strip("/"))
    for ex in prj.exclude_paths:
        if full.startswith(nfc(ex).strip("/")):
            return f"thư mục loại trừ: {ex}"
    if rf.ext not in SUPPORTED:
        return f"định dạng {rf.ext or '(không đuôi)'} không trích xuất"
    if prj.include_ext and rf.ext not in prj.include_ext:
        return "ngoài include_ext"
    if rf.size and rf.size > prj.max_file_mb * 1024 * 1024:
        return f"file > {prj.max_file_mb} MB"
    if rf.ext in IMAGE_EXT and not prj.ocr_images:
        return "ảnh (ocr_images=false)"
    return None


def make_source(cfg: Config, prj: Project):
    if prj.local_path:
        return LocalSource(prj.local_path)
    return DriveSource(prj.drive_folder_id, str(cfg.credentials), str(cfg.token))


def sync_project(cfg: Config, prj: Project, *, limit: int | None = None, force: bool = False,
                 dry_run: bool = False, progress=print) -> SyncReport:
    t0 = time.time()
    rep = SyncReport()
    idx = Index(cfg.db)
    src = make_source(cfg, prj)
    cache = cfg.cache_dir / prj.code
    jobs: dict[str, list[tuple[int, RemoteFile]]] = {}   # content_hash -> các document cùng nội dung
    walk_complete = False
    try:
        for rf in src.walk():
            rep.seen += 1
            reason = _skip_reason(rf, prj)
            if dry_run:
                progress(f"{'SKIP' if reason else 'OK  '} {rf.path}/{rf.name}" + (f"  ({reason})" if reason else ""))
                rep.skipped += bool(reason)
                continue
            doc_id, changed = idx.upsert_document(
                project=prj.code, source_id=rf.source_id, name=rf.name, path=rf.path, ext=rf.ext, mime=rf.mime,
                size=rf.size, modified=rf.modified, hash_=rf.hash, web_link=rf.web_link)
            if reason:
                idx.mark(doc_id, "skipped", reason)
                rep.skipped += 1
                continue
            if not changed and not force:
                rep.unchanged += 1
                continue
            chash = src.resolve_hash(rf) if isinstance(src, LocalSource) else rf.hash
            cid = None if force else idx.content_for_hash(chash)
            if cid is not None:
                idx.link_content(doc_id, cid)
                rep.deduplicated += 1
                continue
            if chash in jobs:
                rep.deduplicated += 1
            jobs.setdefault(chash, []).append((doc_id, rf))
            if limit and len(jobs) >= limit:
                progress(f"Đạt giới hạn {limit} file mới, dừng quét (lần sau chạy tiếp).")
                break
        else:
            walk_complete = True
        idx.commit()
        if dry_run:
            return rep

        # 1) tải về (tuần tự, Drive API có rate limit)
        ready: list[tuple[str, Path, list[tuple[int, RemoteFile]]]] = []
        for n, (chash, docs) in enumerate(jobs.items(), start=1):
            doc_id, rf = docs[0]
            dest = cache / f"{rf.source_id.split(':', 1)[1].replace('/', '_')[-80:]}{rf.ext}"
            try:
                progress(f"[tải {n}/{len(jobs)}] {rf.path}/{rf.name}")
                ready.append((chash, src.download(rf, dest), docs))
            except Exception as e:  # noqa: BLE001
                msg = f"{rf.path}/{rf.name}: tải lỗi {type(e).__name__}: {e}"
                rep.errors.append(msg)
                for d, _ in docs:
                    idx.mark(d, "error", msg)
        idx.commit()

        # 2) trích xuất song song, ghi chỉ mục ở tiến trình chính
        def store(chash, path, docs, result):
            res, chunks, err = result
            first_id, rf = docs[0]
            if err:
                msg = f"{rf.path}/{rf.name}: {err}"
                rep.errors.append(msg)
                for d, _ in docs:
                    idx.mark(d, "error", err)
            else:
                cid = idx.store_content(first_id, chash, res, chunks)
                for d, _ in docs[1:]:
                    idx.link_content(d, cid)
                rep.extracted += 1
                rep.ocr_pages += res.ocr_pages
                if not chunks:
                    rep.empty += 1
                warn = f"  (!) {len(res.warnings)} cảnh báo" if res.warnings else ""
                progress(f"[xong] {rf.path}/{rf.name}: {len(chunks)} đoạn, {res.ocr_pages} trang OCR{warn}")
            idx.commit()
            if not cfg.keep_cache and isinstance(src, DriveSource):
                Path(path).unlink(missing_ok=True)

        if cfg.workers > 1 and len(ready) > 1:
            with ProcessPoolExecutor(max_workers=cfg.workers) as ex:
                futs = {ex.submit(_work, str(p), cfg.extract): (h, p, d) for h, p, d in ready}
                for fut in as_completed(futs):
                    h, p, d = futs[fut]
                    store(h, p, d, fut.result())
        else:
            for h, p, d in ready:
                store(h, p, d, _work(str(p), cfg.extract))

        if walk_complete and not limit:
            rep.removed = idx.mark_removed(prj.code, t0)
        idx.commit()
    except Exception:
        log.error(traceback.format_exc())
        raise
    finally:
        idx.close()
    rep.seconds = round(time.time() - t0, 1)
    return rep
