"""Kiểm thử đầu-cuối trên bộ file GIẢ LẬP (không phải dữ liệu thật của KHC)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from khc_docs.config import load
from khc_docs.extract import extract, ocr_available
from khc_docs.index import Index
from khc_docs.sync import sync_project
from khc_docs.textutil import fold, nfc

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def _pdf_text(p: Path, pages: list[str]):
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(TTFont("DV", FONT))
    c = canvas.Canvas(str(p), pagesize=A4)
    for t in pages:
        c.setFont("DV", 12)
        y = 800
        for line in t.split("\n"):
            c.drawString(50, y, line)
            y -= 18
        c.showPage()
    c.save()


def _pdf_scan(p: Path, text: str):
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (1700, 2200), "white")
    d = ImageDraw.Draw(img)
    f = ImageFont.truetype(FONT, 44)
    y = 150
    for line in text.split("\n"):
        d.text((120, y), line, fill="black", font=f)
        y += 80
    img.save(p, "PDF", resolution=200)


@pytest.fixture()
def project(tmp_path: Path):
    root = tmp_path / "NAM HÀ"
    (root / "03-HỢP ĐỒNG").mkdir(parents=True)
    (root / "09-HỒ SƠ CHẤT LƯỢNG").mkdir()
    (root / "11-HÌNH ẢNH THI CÔNG").mkdir()
    (root / "04-TIẾN ĐỘ - KẾ HOẠCH").mkdir()

    _pdf_text(root / "03-HỢP ĐỒNG" / "HD_mau.pdf", [
        "ĐIỀU 1. PHẠM VI CÔNG VIỆC\nThi công phần kết cấu theo bản vẽ.",
        "ĐIỀU 5. TẠM ỨNG VÀ THANH TOÁN\nTạm ứng 15% giá trị hợp đồng sau khi có bảo lãnh tạm ứng.\nGiữ lại 5% bảo hành.",
    ])
    _pdf_scan(root / "09-HỒ SƠ CHẤT LƯỢNG" / "BBNT_scan.pdf",
              "BIÊN BẢN NGHIỆM THU\nCông tác cốt thép móng\nKết luận: đồng ý nghiệm thu")
    # bản sao trùng nội dung ở thư mục khác
    shutil.copy(root / "09-HỒ SƠ CHẤT LƯỢNG" / "BBNT_scan.pdf", root / "04-TIẾN ĐỘ - KẾ HOẠCH" / "BBNT_copy.pdf")

    import docx
    d = docx.Document()
    d.add_heading("Biện pháp thi công", 1)
    d.add_paragraph("Đổ bê tông móng bằng bơm cần.")
    d.add_heading("An toàn lao động", 2)
    d.add_paragraph("Lắp lan can tạm tại mép sàn.")
    t = d.add_table(rows=2, cols=3)
    for i, v in enumerate(["Hạng mục", "Đơn vị", "Khối lượng"]):
        t.rows[0].cells[i].text = v
    for i, v in enumerate(["Lan can", "m", "120"]):
        t.rows[1].cells[i].text = v
    d.save(root / "04-TIẾN ĐỘ - KẾ HOẠCH" / "BPTC.docx")

    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "BOQ"
    ws.append(["STT", "Mã hiệu", "Nội dung công việc", "Đơn vị", "Khối lượng"])
    ws.append([1, "AF.11111", "Bê tông lót móng", "m3", 12.5])
    ws.append([2, "AF.22222", "Ván khuôn móng", "m2", 80])
    wb.save(root / "03-HỢP ĐỒNG" / "BOQ.xlsx")

    from pptx import Presentation
    prs = Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[1])
    s.shapes.title.text = "Báo cáo tuần"
    s.placeholders[1].text = "Rủi ro: chậm cấp thép"
    prs.save(root / "04-TIẾN ĐỘ - KẾ HOẠCH" / "BC.pptx")

    (root / "11-HÌNH ẢNH THI CÔNG" / "anh.txt").write_text("không được lập chỉ mục", encoding="utf-8")

    cfg_path = tmp_path / "khc_docs.toml"
    cfg_path.write_text(f"""
[index]
db = "data/t.db"
workers = 2
[[projects]]
code = "TEST"
name = "Test"
local_path = "{root.as_posix()}"
exclude_paths = ["11-HÌNH ẢNH THI CÔNG"]
""", encoding="utf-8")
    return load(str(cfg_path))


def test_fold_keeps_length():
    s = nfc("Biên bản nghiệm thu ĐỢT 3 – Đường")
    assert len(fold(s)) == len(s)
    assert fold(s).startswith("bien ban nghiem thu dot 3")


def test_end_to_end(project):
    cfg = project
    rep = sync_project(cfg, cfg.project("TEST"), progress=lambda *_: None)
    assert not rep.errors, rep.errors
    assert rep.skipped == 1           # thư mục ảnh bị loại trừ
    assert rep.deduplicated == 1      # bản sao BBNT chỉ trích xuất 1 lần
    idx = Index(cfg.db)

    h = idx.search("tam ung bao lanh")          # gõ không dấu
    assert h and h[0].name == "HD_mau.pdf" and h[0].label == "tr.2"

    h = idx.search("AF.22222")
    assert h and "Ván khuôn" in h[0].snippet and h[0].label.startswith("sheet 'BOQ'!")

    h = idx.search("lan can", category="04")
    assert h and h[0].name == "BPTC.docx" and "An toàn lao động" in h[0].label

    h = idx.search("chậm cấp thép")
    assert h and h[0].label == "slide 1"

    if ocr_available():
        h = idx.search("nghiệm thu cốt thép móng")
        assert h and h[0].ocr and h[0].also_at       # tìm được trong bản scan + báo bản trùng

    # chạy lại: không trích xuất lại gì
    rep2 = sync_project(cfg, cfg.project("TEST"), progress=lambda *_: None)
    assert rep2.extracted == 0 and rep2.unchanged == rep2.seen - rep2.skipped

    st = idx.stats("TEST")
    assert st["duplicate_files"] == 1


def test_removed_file(project):
    cfg = project
    sync_project(cfg, cfg.project("TEST"), progress=lambda *_: None)
    (Path(cfg.project("TEST").local_path) / "04-TIẾN ĐỘ - KẾ HOẠCH" / "BC.pptx").unlink()
    rep = sync_project(cfg, cfg.project("TEST"), progress=lambda *_: None)
    assert rep.removed == 1
    assert all(h.name != "BC.pptx" for h in Index(cfg.db).search("chậm cấp thép"))


def test_mcp_tools(project, monkeypatch):
    cfg = project
    sync_project(cfg, cfg.project("TEST"), progress=lambda *_: None)
    monkeypatch.setenv("KHC_DOCS_CONFIG", str(cfg.base / "khc_docs.toml"))
    import importlib

    import khc_docs.mcp_server as m
    m = importlib.reload(m)
    res = json.loads(m.search_docs("giữ lại bảo hành"))["results"]
    assert res[0]["cite"].endswith("HD_mau.pdf, tr.2")
    body = json.loads(m.read_chunk([res[0]["chunk_id"]]))[0]
    assert "5% bảo hành" in body["text"]
    ctx = json.loads(m.read_context(res[0]["chunk_id"], before=1, after=0))
    assert [c["cite"].split(", ")[-1] for c in ctx["chunks"]] == ["tr.1", "tr.2"]
    outline = json.loads(m.document_outline(res[0]["doc_id"]))
    assert outline["pages"] == 2


def test_xls_legacy(tmp_path):
    xlwt = pytest.importorskip("xlwt")
    wb = xlwt.Workbook()
    ws = wb.add_sheet("NGÂN SÁCH")
    for c, v in enumerate(["Mã", "Nội dung", "Giá trị"]):
        ws.write(0, c, v)
    ws.write(1, 0, "NS.01"); ws.write(1, 1, "Chi phí nhân công"); ws.write(1, 2, 1234567.5)
    wb.save(str(tmp_path / "ns.xls"))
    seg = extract(tmp_path / "ns.xls").segments[0]
    assert seg.label == "sheet 'NGÂN SÁCH'!A1:C2"
    assert "R2: NS.01 | Chi phí nhân công | 1,234,567.5" in seg.text
