"""Dòng lệnh: khc-docs sync | search | show | stats | serve | auth."""

from __future__ import annotations

import argparse
import json
import logging
import sys

from . import config as config_mod


def main(argv=None):
    ap = argparse.ArgumentParser(prog="khc-docs", description="Quản lý & tra cứu hồ sơ dự án KHC")
    ap.add_argument("-c", "--config", help="file cấu hình TOML (mặc định ./khc_docs.toml)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sync", help="quét nguồn, tải file mới/thay đổi, trích xuất, lập chỉ mục")
    s.add_argument("-p", "--project")
    s.add_argument("--limit", type=int, help="chỉ xử lý N file mới (chạy thử)")
    s.add_argument("--force", action="store_true", help="trích xuất lại kể cả file không đổi")
    s.add_argument("--dry-run", action="store_true", help="chỉ liệt kê, không tải/ghi")
    s.add_argument("--path", help="chỉ xử lý file có đường dẫn/tên chứa chuỗi này (không phân biệt dấu), "
                                   "vd \"IPC-02/HSCL\"; không gỡ file ngoài phạm vi")

    q = sub.add_parser("search", help="tìm thử từ dòng lệnh")
    q.add_argument("query")
    q.add_argument("-p", "--project")
    q.add_argument("--category")
    q.add_argument("--type")
    q.add_argument("-n", type=int, default=8)

    sh = sub.add_parser("show", help="in toàn văn một chunk")
    sh.add_argument("chunk_id", type=int)

    st = sub.add_parser("stats", help="thống kê kho")
    st.add_argument("-p", "--project")

    ls = sub.add_parser("errors", help="liệt kê file lỗi / bỏ qua")
    ls.add_argument("-p", "--project")
    ls.add_argument("--status", default="error", choices=["error", "skipped", "empty", "pending"])

    sub.add_parser("auth", help="đăng nhập Google (OAuth) và lưu token")
    sub.add_parser("serve", help="chạy MCP server (stdio)")

    a = ap.parse_args(argv)
    if a.cmd != "serve":
        # Windows: khi chuyển hướng/pipe (| Tee-Object, > file) Python mặc định dùng cp1252 -> lỗi chữ tiếng Việt
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    if a.config:
        import os
        os.environ["KHC_DOCS_CONFIG"] = a.config

    if a.cmd == "serve":
        from .mcp_server import main as serve
        return serve()

    cfg = config_mod.load(a.config)
    from .index import Index

    if a.cmd == "auth":
        from .sources import DriveSource
        DriveSource._build(str(cfg.credentials), str(cfg.token))
        print(f"OK. Token lưu tại {cfg.token}")
    elif a.cmd == "sync":
        from .sync import sync_project
        prj = cfg.project(a.project)
        rep = sync_project(cfg, prj, limit=a.limit, force=a.force, dry_run=a.dry_run, path_filter=a.path)
        print(json.dumps(rep.__dict__, ensure_ascii=False, indent=2))
    elif a.cmd == "search":
        idx = Index(cfg.db)
        for i, h in enumerate(idx.search(a.query, project=a.project and a.project.upper(), category=a.category,
                                         ext=a.type, limit=a.n), start=1):
            print(f"[S{i}] chunk {h.chunk_id} · {h.citation()}{' · OCR' if h.ocr else ''} · {h.tokens} tok")
            print(f"     {h.snippet}")
            for o in h.also_at[:3]:
                print(f"     = bản sao: {o}")
            for o in h.similar[:5]:
                print(f"     ~ gần giống: {o}")
            print()
    elif a.cmd == "show":
        c = Index(cfg.db).chunk(a.chunk_id)
        print(f"{c['path']}/{c['name']} — {c['label']}\n{'-' * 60}\n{c['text']}" if c else "Không tồn tại")
    elif a.cmd == "stats":
        print(json.dumps(Index(cfg.db).stats(a.project and a.project.upper()), ensure_ascii=False, indent=2))
    elif a.cmd == "errors":
        for r in Index(cfg.db).list_documents(project=a.project and a.project.upper(), status=a.status, limit=1000):
            print(f"{r['path']}/{r['name']}  →  {r['error'] or ''}")


if __name__ == "__main__":
    main()
