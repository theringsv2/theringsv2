"""Chia Segment thành chunk vừa cỡ (~700 token) để truy xuất đúng đoạn, không vượt ranh giới trang/sheet/slide."""

from __future__ import annotations

from .extract import Segment


def _units(text: str, max_chars: int) -> list[str]:
    """Tách theo dòng; dòng dài hơn max_chars thì cắt theo khoảng trắng."""
    out = []
    for line in text.split("\n"):
        while len(line) > max_chars:
            cut = line.rfind(" ", 0, max_chars)
            cut = cut if cut > max_chars // 2 else max_chars
            out.append(line[:cut])
            line = line[cut:].lstrip()
        out.append(line)
    return out


def chunk_segments(segments: list[Segment], max_chars: int = 1800, overlap_lines: int = 1) -> list[Segment]:
    out: list[Segment] = []
    for seg in segments:
        if len(seg.text) <= max_chars:
            out.append(seg)
            continue
        parts: list[list[str]] = []
        cur: list[str] = []
        size = 0
        for u in _units(seg.text, max_chars):
            if cur and size + len(u) + 1 > max_chars:
                parts.append(cur)
                cur = cur[-overlap_lines:] if overlap_lines else []   # chồng lấn 1 dòng giữ ngữ cảnh
                size = sum(len(x) + 1 for x in cur)
                if size + len(u) + 1 > max_chars:
                    cur, size = [], 0
            cur.append(u)
            size += len(u) + 1
        if cur:
            parts.append(cur)
        for i, p in enumerate(parts, start=1):
            text = "\n".join(p).strip()
            if text:
                out.append(Segment(text, f"{seg.label} (phần {i}/{len(parts)})", dict(seg.loc, part=i), seg.ocr))
    return out
