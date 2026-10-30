"""MCP server: cho Claude tìm và đọc ĐÚNG đoạn hồ sơ, kèm trích dẫn nguồn, thay vì nạp cả file.

Quy trình gợi ý cho Claude: search_docs (đoạn trích ngắn) -> read_chunk / read_context (chỉ đoạn cần) -> trả lời kèm [S#].
Chỉ đọc, không sửa chỉ mục hay Drive.
"""

from __future__ import annotations

import json
import os

from .config import load
from .index import Index

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server

INSTRUCTIONS = """Kho hồ sơ dự án KHC (đã trích xuất + lập chỉ mục, gồm cả bản scan đã OCR).
Cách dùng tiết kiệm token:
1. search_docs với từ khoá cụ thể (mã hồ sơ, tên hạng mục, số hợp đồng…). Có thể gõ không dấu. Dùng "cụm từ" để tìm chính xác.
   Lọc theo category = thư mục cấp 1 (vd "03-HỢP ĐỒNG", "09-HỒ SƠ CHẤT LƯỢNG") nếu biết.
2. Chỉ gọi read_chunk cho các kết quả thực sự liên quan; read_context khi cần đoạn trước/sau (vd bảng bị cắt).
3. Khi trả lời: mỗi dữ kiện lấy từ hồ sơ phải kèm trích dẫn dạng [Tên file, vị trí] theo trường `cite`.
   Không suy diễn ngoài nội dung đã đọc. Nếu kết quả có similar_versions (nhiều phiên bản gần giống), nêu rõ
   phiên bản đang trích dẫn và ưu tiên bản FINAL/đã ký/đã gửi khi tên file thể hiện điều đó. Đoạn có ocr=true có thể sai chữ/số -> ghi rõ "(OCR, cần đối chiếu bản gốc)"
   khi dùng số liệu, ngày tháng, giá trị tiền từ đoạn đó.
4. Không tìm thấy -> nói rõ là không thấy trong kho đã lập chỉ mục (kho có thể chưa đồng bộ file mới)."""

_cfg = load(os.environ.get("KHC_DOCS_CONFIG"))
_idx = Index(_cfg.db)
mcp = _Server("khc-docs", instructions=INSTRUCTIONS)


def _cite(name, path, label):
    return f"{path}/{name}, {label}" if path else f"{name}, {label}"


@mcp.tool()
def search_docs(query: str, category: str | None = None, file_type: str | None = None,
                path_contains: str | None = None, project: str | None = None, limit: int = 8) -> str:
    """Tìm đoạn hồ sơ liên quan (BM25, không phân biệt dấu). Trả về đoạn trích ngắn + chunk_id để đọc tiếp.

    Args:
        query: từ khoá/câu hỏi ngắn. Dùng "..." cho cụm chính xác. Ví dụ: "biên bản nghiệm thu cốt thép móng M3"
        category: thư mục cấp 1, vd "03-HỢP ĐỒNG" (có thể chỉ gõ tiền tố "03")
        file_type: pdf | docx | xlsx | pptx | jpg ...
        path_contains: lọc theo một phần đường dẫn/tên file
        project: mã dự án (vd NAMHA); bỏ trống = tất cả
        limit: số kết quả (1-20)
    """
    hits = _idx.search(query, project=project and project.upper(), category=category, ext=file_type,
                       path_contains=path_contains, limit=max(1, min(limit, 20)))
    if not hits:
        return json.dumps({"results": [], "note": "Không có kết quả. Thử từ khoá khác/ít từ hơn, hoặc bỏ bộ lọc."},
                          ensure_ascii=False)
    out = []
    for i, h in enumerate(hits, start=1):
        r = {"ref": f"S{i}", "chunk_id": h.chunk_id, "doc_id": h.doc_id, "cite": h.citation(),
             "snippet": h.snippet, "chunk_tokens": h.tokens}
        if h.ocr:
            r["ocr"] = True
        if h.also_at:
            r["duplicate_copies"] = h.also_at[:3]
        if h.similar:
            r["similar_versions"] = h.similar[:5]   # nội dung gần giống ở file khác (nháp/FINAL/SUBMIT)
        out.append(r)
    return json.dumps({"results": out}, ensure_ascii=False)


@mcp.tool()
def read_chunk(chunk_ids: list[int], max_tokens: int = 4000) -> str:
    """Đọc toàn văn một hoặc vài đoạn theo chunk_id (từ search_docs). Dừng khi chạm max_tokens."""
    out, used = [], 0
    for cid in chunk_ids[:10]:
        c = _idx.chunk(int(cid))
        if not c:
            out.append({"chunk_id": cid, "error": "không tồn tại"})
            continue
        if used + c["tokens"] > max_tokens and out:
            out.append({"chunk_id": cid, "skipped": "vượt max_tokens, gọi lại riêng nếu cần"})
            continue
        used += c["tokens"]
        r = {"chunk_id": cid, "cite": _cite(c["name"], c["path"], c["label"]), "link": c["web_link"],
             "text": c["text"]}
        if c["ocr"]:
            r["ocr"] = True
        out.append(r)
    return json.dumps(out, ensure_ascii=False)


@mcp.tool()
def read_context(chunk_id: int, before: int = 1, after: int = 1) -> str:
    """Đọc đoạn kèm các đoạn liền trước/sau trong cùng tài liệu (tối đa 3 mỗi phía) — dùng khi bảng/điều khoản bị cắt."""
    c = _idx.chunk(int(chunk_id))
    if not c:
        return json.dumps({"error": "chunk_id không tồn tại"}, ensure_ascii=False)
    rows = _idx.neighbors(int(chunk_id), max(0, min(before, 3)), max(0, min(after, 3)))
    return json.dumps({
        "document": f"{c['path']}/{c['name']}", "link": c["web_link"],
        "chunks": [{"chunk_id": r["chunk_id"], "cite": _cite(c["name"], c["path"], r["label"]),
                    "ocr": bool(r["ocr"]), "text": r["text"]} for r in rows]}, ensure_ascii=False)


@mcp.tool()
def document_outline(doc_id: int) -> str:
    """Mục lục một tài liệu: danh sách đoạn (trang/sheet/slide/mục) + 90 ký tự đầu + số token. Dùng để chọn đoạn cần đọc."""
    d = _idx.document(int(doc_id))
    if not d:
        return json.dumps({"error": "doc_id không tồn tại"}, ensure_ascii=False)
    rows = _idx.outline(int(doc_id))
    return json.dumps({
        "document": f"{d['path']}/{d['name']}", "status": d["status"], "link": d["web_link"],
        "pages": d["pages"], "ocr_pages": d["ocr_pages"],
        "warnings": json.loads(d["warnings"])[:10] if d["warnings"] else [],
        "total_tokens": sum(r["tokens"] for r in rows),
        "chunks": [{"chunk_id": r["chunk_id"], "label": r["label"], "tokens": r["tokens"],
                    "head": r["head"].replace("\n", " ")} for r in rows[:200]],
    }, ensure_ascii=False)


@mcp.tool()
def list_documents(category: str | None = None, name_contains: str | None = None, status: str | None = None,
                   project: str | None = None, limit: int = 50, offset: int = 0) -> str:
    """Liệt kê file trong kho (theo thư mục/tên/trạng thái). status: indexed | empty | skipped | error | pending."""
    rows = _idx.list_documents(project=project and project.upper(), category=category,
                               name_contains=name_contains, status=status, limit=min(limit, 200), offset=offset)
    return json.dumps([{"doc_id": r["doc_id"], "file": f"{r['path']}/{r['name']}", "status": r["status"],
                        "pages": r["pages"], "ocr_pages": r["ocr_pages"], "chunks": r["n_chunks"],
                        **({"note": r["error"]} if r["error"] else {})} for r in rows], ensure_ascii=False)


@mcp.tool()
def index_stats(project: str | None = None) -> str:
    """Thống kê kho: số file theo trạng thái/thư mục, số trang OCR, số file trùng, tổng token nếu nạp toàn bộ."""
    return json.dumps(_idx.stats(project and project.upper()), ensure_ascii=False)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
