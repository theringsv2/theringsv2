"""Trích xuất nội dung PDF / DOCX / XLSX / PPTX / ảnh (OCR) / TXT thành các đoạn có vị trí trích dẫn.

Mỗi Segment giữ vị trí nguồn (trang, sheet + vùng ô, slide, mục) để Claude trích dẫn chính xác.
Không suy diễn nội dung: ô trống giữ trống, công thức lấy giá trị đã lưu (cached) và ghi chú nếu thiếu.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .textutil import clean

log = logging.getLogger(__name__)

SUPPORTED = {
    ".pdf", ".docx", ".xlsx", ".xlsm", ".pptx",
    ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp",
    ".txt", ".csv", ".md",
    # định dạng cũ: chuyển qua LibreOffice nếu máy có cài
    ".doc", ".xls", ".ppt",
}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}


@dataclass
class Segment:
    text: str
    label: str                      # nhãn trích dẫn hiển thị: "tr.12", "Sheet BOQ!A1:H40", "slide 3"
    loc: dict = field(default_factory=dict)
    ocr: bool = False


@dataclass
class ExtractResult:
    segments: list[Segment]
    pages: int = 0
    ocr_pages: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass
class ExtractOptions:
    ocr: bool = True
    ocr_lang: str = "vie+eng"
    ocr_dpi: int = 300
    min_text_chars: int = 40         # trang PDF ít hơn ngưỡng này -> coi là scan, chạy OCR
    max_ocr_pages: int = 300         # chặn file scan khổng lồ (bản vẽ) làm treo đồng bộ
    xlsx_rows_per_block: int = 40
    xlsx_max_rows: int = 20000


# ---------------------------------------------------------------- OCR

_WIN_TESSERACT = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    str(Path.home() / "AppData" / "Local" / "Programs" / "Tesseract-OCR" / "tesseract.exe"),
]


def tesseract_cmd() -> str | None:
    """Tìm tesseract trong PATH, rồi ở thư mục cài mặc định trên Windows (không bắt buộc sửa PATH)."""
    found = shutil.which("tesseract")
    if found:
        return found
    return next((p for p in _WIN_TESSERACT if Path(p).is_file()), None)


def ocr_available() -> bool:
    return tesseract_cmd() is not None


def _ocr_image(img, lang: str) -> tuple[str, float]:
    import pytesseract

    pytesseract.pytesseract.tesseract_cmd = tesseract_cmd() or "tesseract"
    data = pytesseract.image_to_data(img, lang=lang, output_type=pytesseract.Output.DICT,
                                     config="--psm 3")
    lines: dict[tuple, list[str]] = {}
    confs = []
    for i, word in enumerate(data["text"]):
        if not word.strip():
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        lines.setdefault(key, []).append(word)
        try:
            c = float(data["conf"][i])
            if c >= 0:
                confs.append(c)
        except (TypeError, ValueError):
            pass
    text = "\n".join(" ".join(ws) for _, ws in sorted(lines.items()))
    return text, (sum(confs) / len(confs) if confs else 0.0)


# ---------------------------------------------------------------- PDF

def _extract_pdf(path: Path, opt: ExtractOptions) -> ExtractResult:
    import pypdfium2 as pdfium

    res = ExtractResult(segments=[])
    pdf = pdfium.PdfDocument(str(path))
    res.pages = len(pdf)
    can_ocr = opt.ocr and ocr_available()
    for i in range(len(pdf)):
        page = pdf[i]
        tp = page.get_textpage()
        text = clean(tp.get_text_range())
        tp.close()
        ocr = False
        if len(text) < opt.min_text_chars:
            if can_ocr and res.ocr_pages < opt.max_ocr_pages:
                img = page.render(scale=opt.ocr_dpi / 72).to_pil()
                otext, conf = _ocr_image(img, opt.ocr_lang)
                res.ocr_pages += 1
                if len(clean(otext)) > len(text):
                    text, ocr = clean(otext), True
                    if conf < 60:
                        res.warnings.append(f"tr.{i + 1}: OCR độ tin cậy thấp ({conf:.0f}%)")
            elif not can_ocr:
                res.warnings.append(f"tr.{i + 1}: trang scan, chưa OCR (thiếu tesseract)")
            else:
                res.warnings.append(f"tr.{i + 1}: vượt giới hạn {opt.max_ocr_pages} trang OCR")
        page.close()
        if text:
            res.segments.append(Segment(text, f"tr.{i + 1}", {"page": i + 1}, ocr))
    pdf.close()
    return res


# ---------------------------------------------------------------- DOCX

def _iter_docx_blocks(doc):
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    for child in doc.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            yield Paragraph(child, doc)
        elif tag == "tbl":
            yield Table(child, doc)


def _table_rows(table) -> list[str]:
    rows = []
    for r in table.rows:
        cells, prev = [], None
        for c in r.cells:
            t = clean(c.text).replace("\n", " ")
            if c._tc is prev:          # ô gộp ngang -> python-docx lặp lại, bỏ qua
                continue
            prev = c._tc
            cells.append(t)
        if any(cells):
            rows.append(" | ".join(cells))
    return rows


def _extract_docx(path: Path, opt: ExtractOptions) -> ExtractResult:
    import docx

    d = docx.Document(str(path))
    res = ExtractResult(segments=[])
    heading: list[str] = []
    buf: list[str] = []
    sec_no = 0

    def flush():
        nonlocal buf, sec_no
        text = clean("\n".join(buf))
        if text:
            sec_no += 1
            label = "mục: " + " > ".join(heading) if heading else f"đoạn {sec_no}"
            res.segments.append(Segment(text, label, {"section": " > ".join(heading), "seq": sec_no}))
        buf = []

    for block in _iter_docx_blocks(d):
        if block.__class__.__name__ == "Table":
            rows = _table_rows(block)
            if rows:
                buf.append("[Bảng]\n" + "\n".join(rows))
            continue
        text = block.text.strip()
        if not text:
            continue
        style = (block.style.name or "").lower() if block.style is not None else ""
        if style.startswith(("heading", "title")) or style.startswith("tiêu đề"):
            flush()
            try:
                level = int(style.split()[-1])
            except ValueError:
                level = 1
            heading = heading[: max(0, level - 1)] + [text[:120]]
            buf.append(text)
        else:
            buf.append(text)
    flush()
    for s in d.sections:
        for part, name in ((s.header, "header"), (s.footer, "footer")):
            t = clean("\n".join(p.text for p in part.paragraphs)) if part is not None else ""
            if t:
                res.segments.append(Segment(t, name, {"part": name}))
        break
    res.pages = sec_no
    return res


# ---------------------------------------------------------------- XLSX

def _cell_str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:,.4f}".rstrip("0").rstrip(".")
    if hasattr(v, "strftime"):
        return v.strftime("%d/%m/%Y")
    return clean(str(v)).replace("\n", " ")


def _sheet_segments(title: str, rows, opt: ExtractOptions, res: ExtractResult):
    """rows: iterable các tuple giá trị ô theo thứ tự dòng (dòng 1 = phần tử đầu)."""
    from openpyxl.utils import get_column_letter

    header: str | None = None
    block: list[str] = []
    start_row = None
    last_row = 0
    max_col = 1
    n = 0

    def flush():
        rng = f"A{start_row}:{get_column_letter(max_col)}{last_row}"
        body = "\n".join(block)
        if header and not body.startswith(header):
            body = f"[Tiêu đề] {header}\n{body}"
        res.segments.append(Segment(body, f"sheet '{title}'!{rng}", {"sheet": title, "range": rng}))

    for r_idx, row in enumerate(rows, start=1):
        if n >= opt.xlsx_max_rows:
            res.warnings.append(f"sheet '{title}': cắt ở {opt.xlsx_max_rows} dòng")
            break
        vals = [_cell_str(v) for v in row]
        while vals and not vals[-1]:
            vals.pop()
        if not any(vals):
            continue
        n += 1
        max_col = max(max_col, len(vals))
        line = f"R{r_idx}: " + " | ".join(vals)
        # dòng tiêu đề bảng: dòng đầu tiên có >= 3 ô chữ
        if header is None and sum(1 for v in vals if v and not v.replace(",", "").replace(".", "").isdigit()) >= 3:
            header = line
        if start_row is None:
            start_row = r_idx
        block.append(line)
        last_row = r_idx
        if len(block) >= opt.xlsx_rows_per_block:
            flush()
            block, start_row = [], None
    if block:
        flush()
    res.pages += 1


def _extract_xlsx(path: Path, opt: ExtractOptions) -> ExtractResult:
    import openpyxl

    res = ExtractResult(segments=[])
    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    for ws in wb.worksheets:
        if getattr(ws, "sheet_state", "visible") != "visible":
            res.warnings.append(f"sheet ẩn '{ws.title}' vẫn được lập chỉ mục")
        _sheet_segments(ws.title, ws.iter_rows(values_only=True), opt, res)
    wb.close()
    return res


def _extract_xls(path: Path, opt: ExtractOptions) -> ExtractResult:
    """Excel 97-2003 (.xls) đọc trực tiếp bằng xlrd, không cần LibreOffice."""
    import datetime

    import xlrd

    res = ExtractResult(segments=[])
    wb = xlrd.open_workbook(str(path), on_demand=True)
    for sh in wb.sheets():
        if sh.visibility:
            res.warnings.append(f"sheet ẩn '{sh.name}' vẫn được lập chỉ mục")

        def rows(sh=sh):
            for r in range(sh.nrows):
                out = []
                for c in sh.row(r):
                    if c.ctype == xlrd.XL_CELL_DATE:
                        try:
                            out.append(datetime.datetime(*xlrd.xldate_as_tuple(c.value, wb.datemode)))
                        except (ValueError, xlrd.xldate.XLDateError):
                            out.append(c.value)
                    elif c.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK, xlrd.XL_CELL_ERROR):
                        out.append(None)
                    else:
                        out.append(c.value)
                yield out

        _sheet_segments(sh.name, rows(), opt, res)
        wb.unload_sheet(sh.name)
    wb.release_resources()
    return res


# ---------------------------------------------------------------- PPTX

def _extract_pptx(path: Path, opt: ExtractOptions) -> ExtractResult:
    from pptx import Presentation

    prs = Presentation(str(path))
    res = ExtractResult(segments=[])
    for i, slide in enumerate(prs.slides, start=1):
        parts = []
        for shp in slide.shapes:
            if shp.has_text_frame:
                parts.append(shp.text_frame.text)
            elif getattr(shp, "has_table", False) and shp.has_table:
                parts.append("[Bảng]\n" + "\n".join(_table_rows(shp.table)))
        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame.text if slide.notes_slide.notes_text_frame else ""
            if notes.strip():
                parts.append("[Ghi chú] " + notes)
        text = clean("\n".join(parts))
        if text:
            res.segments.append(Segment(text, f"slide {i}", {"slide": i}))
    res.pages = len(prs.slides)
    return res


# ---------------------------------------------------------------- Ảnh / text / định dạng cũ

def _extract_image(path: Path, opt: ExtractOptions) -> ExtractResult:
    from PIL import Image

    res = ExtractResult(segments=[], pages=1)
    if not (opt.ocr and ocr_available()):
        res.warnings.append("ảnh chưa OCR (thiếu tesseract)")
        return res
    with Image.open(path) as im:
        text, conf = _ocr_image(im.convert("RGB"), opt.ocr_lang)
    res.ocr_pages = 1
    text = clean(text)
    if conf < 60:
        res.warnings.append(f"OCR độ tin cậy thấp ({conf:.0f}%)")
    if text:
        res.segments.append(Segment(text, "ảnh (OCR)", {"page": 1}, ocr=True))
    return res


def _extract_text(path: Path, opt: ExtractOptions) -> ExtractResult:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp1258", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    text = clean(text)
    return ExtractResult(segments=[Segment(text, "toàn văn", {})] if text else [], pages=1)


def _convert_legacy(path: Path, opt: ExtractOptions) -> ExtractResult:
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        return ExtractResult(segments=[], warnings=[f"{path.suffix}: cần LibreOffice để đọc định dạng cũ"])
    target = {".doc": "docx", ".xls": "xlsx", ".ppt": "pptx"}[path.suffix.lower()]
    with tempfile.TemporaryDirectory() as td:
        subprocess.run([soffice, "--headless", "--convert-to", target, "--outdir", td, str(path)],
                       check=True, capture_output=True, timeout=300)
        out = Path(td) / (path.stem + "." + target)
        return extract(out, opt)


_HANDLERS = {
    ".pdf": _extract_pdf,
    ".docx": _extract_docx,
    ".xlsx": _extract_xlsx, ".xlsm": _extract_xlsx,
    ".pptx": _extract_pptx,
    ".txt": _extract_text, ".csv": _extract_text, ".md": _extract_text,
    ".xls": _extract_xls,
    ".doc": _convert_legacy, ".ppt": _convert_legacy,
    **{e: _extract_image for e in IMAGE_EXT},
}


def extract(path: Path, opt: ExtractOptions | None = None) -> ExtractResult:
    opt = opt or ExtractOptions()
    handler = _HANDLERS.get(path.suffix.lower())
    if handler is None:
        return ExtractResult(segments=[], warnings=[f"không hỗ trợ định dạng {path.suffix}"])
    return handler(path, opt)
