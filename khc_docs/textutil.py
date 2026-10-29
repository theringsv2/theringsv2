"""Chuẩn hoá tiếng Việt cho tìm kiếm: bỏ dấu giữ nguyên độ dài để map vị trí về văn bản gốc."""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

_WS = re.compile(r"[ \t ]+")
_MULTI_NL = re.compile(r"\n{3,}")


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


@lru_cache(maxsize=4096)
def _fold_char(ch: str) -> str:
    if ch in "đĐ":
        return "d"
    if ord(ch) < 128:
        return ch.lower()
    base = unicodedata.normalize("NFD", ch)[0]
    base = base.lower()
    # Ký tự tổ hợp lẻ (dấu đứng riêng) -> khoảng trắng để giữ độ dài 1:1
    return " " if unicodedata.combining(base) else base[:1] or " "


def fold(s: str) -> str:
    """Bỏ dấu + lowercase, đ→d. Đầu vào phải là NFC; đầu ra có cùng độ dài với đầu vào."""
    return "".join(_fold_char(c) for c in s)


def clean(s: str) -> str:
    s = nfc(s).replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    s = "\n".join(_WS.sub(" ", line).strip() for line in s.split("\n"))
    return _MULTI_NL.sub("\n\n", s).strip()


def est_tokens(s: str) -> int:
    # Tiếng Việt có dấu ~ 2.5 ký tự/token với tokenizer Claude (ước lượng bảo thủ)
    return max(1, int(len(s) / 2.5))


_TERM = re.compile(r"[0-9a-z]+(?:[./-][0-9a-z]+)*")


def query_terms(q: str) -> list[str]:
    """Tách truy vấn thành các term đã fold; giữ mã kiểu 'BB-12/2026', 'C30', '3.2'."""
    return _TERM.findall(fold(nfc(q)))
