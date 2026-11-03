"""Chỉ mục SQLite FTS5 (BM25) cho hồ sơ dự án.

Thiết kế:
- `contents`: một bản nội dung theo hash file (md5) -> file trùng ở nhiều thư mục chỉ trích xuất/lập chỉ mục 1 lần.
- `documents`: mỗi file nguồn (Drive id hoặc đường dẫn local), trỏ tới content.
- `chunks` + `chunks_fts`: đoạn văn bản có nhãn vị trí; FTS chạy trên văn bản đã bỏ dấu (gõ "tien do" vẫn ra "tiến độ"),
  sau đó xếp hạng lại ưu tiên khớp đúng dấu.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from .extract import ExtractResult, Segment
from .textutil import est_tokens, fold, nfc, query_terms

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS contents (
    content_id   INTEGER PRIMARY KEY,
    hash         TEXT UNIQUE NOT NULL,
    pages        INTEGER DEFAULT 0,
    ocr_pages    INTEGER DEFAULT 0,
    n_chunks     INTEGER DEFAULT 0,
    chars        INTEGER DEFAULT 0,
    warnings     TEXT,
    extractor_v  INTEGER DEFAULT 1,
    indexed_at   REAL
);
CREATE TABLE IF NOT EXISTS documents (
    doc_id       INTEGER PRIMARY KEY,
    project      TEXT NOT NULL,
    source_id    TEXT UNIQUE NOT NULL,      -- drive:<fileId> | local:<abs path>
    name         TEXT NOT NULL,
    path         TEXT NOT NULL,             -- đường dẫn thư mục trong dự án, vd "03-HỢP ĐỒNG/Phụ lục"
    category     TEXT,                      -- thư mục cấp 1, vd "03-HỢP ĐỒNG"
    ext          TEXT,
    mime         TEXT,
    size         INTEGER,
    modified     TEXT,
    hash         TEXT,
    web_link     TEXT,
    content_id   INTEGER REFERENCES contents(content_id),
    status       TEXT DEFAULT 'pending',    -- pending | indexed | empty | skipped | error | removed
    error        TEXT,
    seen_at      REAL
);
CREATE INDEX IF NOT EXISTS ix_doc_project ON documents(project, category);
CREATE INDEX IF NOT EXISTS ix_doc_content ON documents(content_id);
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id     INTEGER PRIMARY KEY,
    content_id   INTEGER NOT NULL REFERENCES contents(content_id) ON DELETE CASCADE,
    ord          INTEGER NOT NULL,
    label        TEXT NOT NULL,
    loc          TEXT,
    ocr          INTEGER DEFAULT 0,
    text         TEXT NOT NULL,
    tokens       INTEGER
);
CREATE INDEX IF NOT EXISTS ix_chunk_content ON chunks(content_id, ord);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    body, title, tokenize='unicode61 remove_diacritics 2'
);
"""

EXTRACTOR_VERSION = 1


@dataclass
class Hit:
    chunk_id: int
    doc_id: int
    name: str
    path: str
    category: str
    label: str
    ocr: bool
    web_link: str | None
    snippet: str
    score: float
    tokens: int
    also_at: list[str]                      # bản sao y hệt (cùng md5)
    similar: list[str] = field(default_factory=list)  # bản gần giống (bản nháp/FINAL/SUBMIT…) bị gộp khỏi kết quả

    def citation(self) -> str:
        loc = f", {self.label}" if self.label else ""
        return f"{self.path}/{self.name}{loc}" if self.path else f"{self.name}{loc}"


class Index:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA foreign_keys=ON")
        self.con.executescript(SCHEMA)

    def close(self):
        self.con.close()

    # ------------------------------------------------------------ ghi

    def upsert_document(self, *, project: str, source_id: str, name: str, path: str, ext: str,
                        mime: str | None, size: int | None, modified: str | None, hash_: str | None,
                        web_link: str | None) -> tuple[int, bool]:
        """Trả về (doc_id, cần_trích_xuất)."""
        name, path = nfc(name), nfc(path)
        category = path.split("/", 1)[0] if path else ""
        row = self.con.execute("SELECT doc_id, hash, status, content_id FROM documents WHERE source_id=?",
                               (source_id,)).fetchone()
        now = time.time()
        if row is None:
            cur = self.con.execute(
                "INSERT INTO documents(project, source_id, name, path, category, ext, mime, size, modified, hash,"
                " web_link, seen_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (project, source_id, name, path, category, ext, mime, size, modified, hash_, web_link, now))
            return cur.lastrowid, True
        changed = row["hash"] != hash_ or row["status"] in ("pending", "error", "removed")
        self.con.execute(
            "UPDATE documents SET name=?, path=?, category=?, ext=?, mime=?, size=?, modified=?, hash=?,"
            " web_link=?, seen_at=?, status=CASE WHEN status='removed' THEN 'pending' ELSE status END"
            " WHERE doc_id=?",
            (name, path, category, ext, mime, size, modified, hash_, web_link, now, row["doc_id"]))
        return row["doc_id"], changed

    def content_for_hash(self, hash_: str) -> int | None:
        r = self.con.execute("SELECT content_id FROM contents WHERE hash=? AND extractor_v=?",
                             (hash_, EXTRACTOR_VERSION)).fetchone()
        return r["content_id"] if r else None

    def link_content(self, doc_id: int, content_id: int):
        n = self.con.execute("SELECT n_chunks FROM contents WHERE content_id=?", (content_id,)).fetchone()[0]
        self.con.execute("UPDATE documents SET content_id=?, status=?, error=NULL WHERE doc_id=?",
                         (content_id, "indexed" if n else "empty", doc_id))
        self._gc_contents()

    def store_content(self, doc_id: int, hash_: str, res: ExtractResult, chunks: list[Segment]) -> int:
        title_row = self.con.execute("SELECT name, path FROM documents WHERE doc_id=?", (doc_id,)).fetchone()
        title = fold(nfc(f"{title_row['path']} {title_row['name']}"))
        old = self.con.execute("SELECT content_id FROM contents WHERE hash=?", (hash_,)).fetchone()
        if old:
            self._delete_content(old["content_id"])
        cur = self.con.execute(
            "INSERT INTO contents(hash, pages, ocr_pages, n_chunks, chars, warnings, extractor_v, indexed_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (hash_, res.pages, res.ocr_pages, len(chunks), sum(len(c.text) for c in chunks),
             json.dumps(res.warnings, ensure_ascii=False) if res.warnings else None,
             EXTRACTOR_VERSION, time.time()))
        cid = cur.lastrowid
        for i, c in enumerate(chunks):
            r = self.con.execute(
                "INSERT INTO chunks(content_id, ord, label, loc, ocr, text, tokens) VALUES (?,?,?,?,?,?,?)",
                (cid, i, c.label, json.dumps(c.loc, ensure_ascii=False), int(c.ocr), c.text, est_tokens(c.text)))
            self.con.execute("INSERT INTO chunks_fts(rowid, body, title) VALUES (?,?,?)",
                             (r.lastrowid, fold(c.text), title))
        self.link_content(doc_id, cid)
        return cid

    def mark(self, doc_id: int, status: str, error: str | None = None):
        self.con.execute("UPDATE documents SET status=?, error=? WHERE doc_id=?", (status, error, doc_id))

    def mark_removed(self, project: str, seen_before: float) -> int:
        cur = self.con.execute(
            "UPDATE documents SET status='removed', content_id=NULL WHERE project=? AND seen_at<? AND status!='removed'",
            (project, seen_before))
        self._gc_contents()
        return cur.rowcount

    def _delete_content(self, content_id: int):
        ids = [r[0] for r in self.con.execute("SELECT chunk_id FROM chunks WHERE content_id=?", (content_id,))]
        self.con.executemany("DELETE FROM chunks_fts WHERE rowid=?", [(i,) for i in ids])
        self.con.execute("DELETE FROM chunks WHERE content_id=?", (content_id,))
        self.con.execute("UPDATE documents SET content_id=NULL, status='pending' WHERE content_id=?", (content_id,))
        self.con.execute("DELETE FROM contents WHERE content_id=?", (content_id,))

    def _gc_contents(self):
        for (cid,) in self.con.execute(
                "SELECT content_id FROM contents WHERE content_id NOT IN"
                " (SELECT content_id FROM documents WHERE content_id IS NOT NULL)").fetchall():
            self._delete_content(cid)

    def commit(self):
        self.con.commit()

    # ------------------------------------------------------------ đọc

    @staticmethod
    def _fts_query(q: str, mode: str) -> str:
        parts = []
        for t in query_terms(q):
            sub = re.findall(r"[0-9a-z]+", t)
            if not sub:
                continue
            parts.append('"' + " ".join(sub) + '"' if len(sub) > 1 else f'"{sub[0]}"')
        # cụm trong ngoặc kép của người dùng -> phrase
        phrases = ['"' + " ".join(re.findall(r"[0-9a-z]+", fold(nfc(p)))) + '"'
                   for p in re.findall(r'"([^"]+)"', q)]
        if mode == "all":
            return " AND ".join(phrases + parts)
        return " OR ".join(phrases + parts)

    def search(self, q: str, *, project: str | None = None, category: str | None = None,
               ext: str | None = None, path_contains: str | None = None, limit: int = 8,
               snippet_chars: int = 320) -> list[Hit]:
        if not query_terms(q):
            return []
        hits: list[Hit] = []
        for mode in ("all", "any"):
            fts = self._fts_query(q, mode)
            if not fts:
                return []
            where = ["d.status='indexed'"]
            args: list = [fts]
            if project:
                where.append("d.project=?"); args.append(project)
            if category:
                where.append("(d.category=? OR d.category LIKE ?)"); args += [nfc(category), nfc(category) + "%"]
            if ext:
                where.append("d.ext=?"); args.append(ext.lower() if ext.startswith(".") else "." + ext.lower())
            if path_contains:
                where.append("(d.path || '/' || d.name) LIKE ?"); args.append(f"%{nfc(path_contains)}%")
            # bm25() không dùng được cùng GROUP BY -> tính trong CTE; mỗi content có thể có nhiều document
            # (file trùng) -> lấy document đại diện có doc_id nhỏ nhất
            sql = f"""
                WITH m AS MATERIALIZED (SELECT rowid AS rid, bm25(chunks_fts, 1.0, 0.6) AS score
                           FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY score LIMIT 2000)
                SELECT c.chunk_id, c.label, c.ocr, c.text, c.tokens, c.content_id, m.score,
                       MIN(d.doc_id) AS doc_id
                FROM m
                JOIN chunks c ON c.chunk_id = m.rid
                JOIN documents d ON d.content_id = c.content_id
                WHERE {' AND '.join(where)}
                GROUP BY c.chunk_id
                ORDER BY m.score LIMIT ?"""
            rows = self.con.execute(sql, args + [limit * 4]).fetchall()
            if rows:
                break
        terms = query_terms(q)
        exact = [w for w in re.findall(r"\w+", nfc(q).lower()) if len(w) > 1]
        scored = []
        for r in rows:
            low = r["text"].lower()
            bonus = sum(0.6 for w in exact if w in low)          # khớp đúng dấu
            bonus += 1.5 if " ".join(terms) in fold(r["text"]) else 0  # khớp nguyên cụm
            scored.append((r["score"] - bonus, r))
        scored.sort(key=lambda x: x[0])
        seen_content: dict[int, int] = {}
        shingles: list[set] = []
        for s, r in scored:
            # tối đa 3 đoạn / tài liệu để kết quả đa dạng
            if seen_content.get(r["content_id"], 0) >= 3:
                continue
            d = self.con.execute("SELECT * FROM documents WHERE doc_id=?", (r["doc_id"],)).fetchone()
            # đoạn gần trùng với kết quả đã chọn (nhiều phiên bản cùng nội dung) -> gộp, không tốn chỗ/token
            sh = _shingles(r["text"])
            dup = next((i for i, o in enumerate(shingles) if _jaccard(sh, o) >= 0.6), None)
            if dup is not None:
                where_ = f"{d['path']}/{d['name']}, {r['label']}"
                if where_ not in hits[dup].similar:
                    hits[dup].similar.append(where_)
                continue
            seen_content[r["content_id"]] = seen_content.get(r["content_id"], 0) + 1
            shingles.append(sh)
            others = [f"{o['path']}/{o['name']}" for o in self.con.execute(
                "SELECT path, name FROM documents WHERE content_id=? AND doc_id!=? AND status='indexed'",
                (r["content_id"], d["doc_id"]))]
            hits.append(Hit(r["chunk_id"], d["doc_id"], d["name"], d["path"], d["category"] or "", r["label"],
                            bool(r["ocr"]), d["web_link"], make_snippet(r["text"], terms, snippet_chars),
                            round(-s, 3), r["tokens"], others))
            if len(hits) >= limit:
                break
        return hits

    def chunk(self, chunk_id: int) -> sqlite3.Row | None:
        return self.con.execute(
            "SELECT c.*, d.doc_id, d.name, d.path, d.web_link FROM chunks c"
            " JOIN documents d ON d.content_id=c.content_id WHERE c.chunk_id=? ORDER BY d.doc_id LIMIT 1",
            (chunk_id,)).fetchone()

    def neighbors(self, chunk_id: int, before: int, after: int) -> list[sqlite3.Row]:
        c = self.con.execute("SELECT content_id, ord FROM chunks WHERE chunk_id=?", (chunk_id,)).fetchone()
        if not c:
            return []
        return self.con.execute(
            "SELECT * FROM chunks WHERE content_id=? AND ord BETWEEN ? AND ? ORDER BY ord",
            (c["content_id"], c["ord"] - before, c["ord"] + after)).fetchall()

    def document(self, doc_id: int) -> sqlite3.Row | None:
        return self.con.execute(
            "SELECT d.*, ct.pages, ct.ocr_pages, ct.n_chunks, ct.chars, ct.warnings FROM documents d"
            " LEFT JOIN contents ct ON ct.content_id=d.content_id WHERE d.doc_id=?", (doc_id,)).fetchone()

    def outline(self, doc_id: int) -> list[sqlite3.Row]:
        return self.con.execute(
            "SELECT c.chunk_id, c.ord, c.label, c.ocr, c.tokens, substr(c.text, 1, 90) AS head FROM chunks c"
            " JOIN documents d ON d.content_id=c.content_id WHERE d.doc_id=? ORDER BY c.ord", (doc_id,)).fetchall()

    def list_documents(self, *, project=None, category=None, name_contains=None, status=None,
                       limit=50, offset=0) -> list[sqlite3.Row]:
        where, args = ["1=1"], []
        if project:
            where.append("d.project=?"); args.append(project)
        if category:
            where.append("(d.category=? OR d.category LIKE ?)"); args += [nfc(category), nfc(category) + "%"]
        if name_contains:
            where.append("(d.path || '/' || d.name) LIKE ?"); args.append(f"%{nfc(name_contains)}%")
        if status:
            where.append("d.status=?"); args.append(status)
        else:
            where.append("d.status!='removed'")
        return self.con.execute(
            f"SELECT d.doc_id, d.name, d.path, d.category, d.ext, d.size, d.modified, d.status, d.error,"
            f" ct.pages, ct.ocr_pages, ct.n_chunks FROM documents d LEFT JOIN contents ct ON ct.content_id=d.content_id"
            f" WHERE {' AND '.join(where)} ORDER BY d.path, d.name LIMIT ? OFFSET ?",
            args + [limit, offset]).fetchall()

    def peek(self, source_id: str) -> sqlite3.Row | None:
        return self.con.execute("SELECT hash, status FROM documents WHERE source_id=?", (source_id,)).fetchone()

    def last_sync(self, project: str | None = None) -> str | None:
        w, a = ("WHERE project=?", [project]) if project else ("", [])
        t = self.con.execute(f"SELECT MAX(seen_at) FROM documents {w}", a).fetchone()[0]
        return time.strftime("%d/%m/%Y %H:%M", time.localtime(t)) if t else None

    def stats(self, project: str | None = None) -> dict:
        w, a = ("WHERE project=?", [project]) if project else ("", [])
        by_status = dict(self.con.execute(f"SELECT status, COUNT(*) FROM documents {w} GROUP BY status", a).fetchall())
        by_cat = dict(self.con.execute(
            f"SELECT category, COUNT(*) FROM documents {w} {'AND' if w else 'WHERE'} status='indexed'"
            f" GROUP BY category ORDER BY category", a).fetchall())
        tot = self.con.execute("SELECT COUNT(*), COALESCE(SUM(chars),0), COALESCE(SUM(ocr_pages),0) FROM contents").fetchone()
        dup = self.con.execute(
            f"SELECT COUNT(*) - COUNT(DISTINCT content_id) FROM documents {w} {'AND' if w else 'WHERE'}"
            f" content_id IS NOT NULL", a).fetchone()[0]
        return {"last_sync": self.last_sync(project), "documents_by_status": by_status, "indexed_by_category": by_cat,
                "unique_contents": tot[0], "total_chars": tot[1], "est_tokens_all": int(tot[1] / 2.5),
                "ocr_pages": tot[2], "duplicate_files": dup}


def _shingles(text: str, n: int = 4) -> set:
    w = re.findall(r"[0-9a-z]+", fold(text))
    return {" ".join(w[i:i + n]) for i in range(max(1, len(w) - n + 1))}


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def make_snippet(text: str, terms: list[str], width: int) -> str:
    """Cắt đoạn trích quanh vùng tập trung nhiều term nhất (vị trí tính trên bản fold, cùng độ dài)."""
    if len(text) <= width:
        return text.replace("\n", " / ")
    f = fold(text)
    pos = [m.start() for t in set(terms) if len(t) > 1 for m in re.finditer(re.escape(t), f)]
    if not pos:
        start = 0
    else:
        pos.sort()
        best, start = 0, pos[0]
        for p in pos:
            n = sum(1 for x in pos if p <= x < p + width)
            if n > best:
                best, start = n, p
        start = max(0, start - width // 4)
    end = min(len(text), start + width)
    s = text[start:end].replace("\n", " / ")
    return ("…" if start > 0 else "") + s + ("…" if end < len(text) else "")
