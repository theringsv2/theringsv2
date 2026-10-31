# KHC Docs — Hệ thống quản lý & tra cứu hồ sơ dự án

Google Drive (thư mục dự án) → tải file → trích xuất PDF/DOCX/XLSX/PPTX (+ OCR bản scan) → chỉ mục SQLite FTS5 → **MCP server** để Claude tìm đúng đoạn, đọc đúng đoạn, trả lời có trích dẫn.

Mục tiêu: không nạp cả bộ hồ sơ vào AI. Claude chỉ nhận các đoạn trích ~300 ký tự khi tìm, rồi đọc toàn văn **chỉ** đoạn cần (~700 token/đoạn).

## 1. Kiến trúc

```
Google Drive: NAM HÀ/01-HỒ SƠ THẦU … 18-NGÂN SÁCH
        │  Drive API (read-only) hoặc thư mục Drive for Desktop
        ▼
khc-docs sync ── tải file mới/đổi (md5) ── khử trùng nội dung ── trích xuất song song
        │            PDF: text layer; trang < 40 ký tự → OCR Tesseract vie+eng
        │            DOCX: theo mục Heading + bảng   XLSX: theo sheet, khối 40 dòng, nhãn vùng ô
        │            PPTX: theo slide + ghi chú      Ảnh: OCR      DOC/XLS/PPT: qua LibreOffice
        ▼
data/khc_docs.db  (SQLite: documents · contents · chunks · FTS5 không dấu)
        ▼
khc-docs serve (MCP stdio) ──► Claude Desktop / Claude Code
   search_docs · read_chunk · read_context · document_outline · list_documents · index_stats
```

| Thành phần | File |
|---|---|
| Nguồn Drive / local | `khc_docs/sources.py` |
| Trích xuất + OCR | `khc_docs/extract.py` |
| Chia đoạn | `khc_docs/chunk.py` |
| Chỉ mục, tìm kiếm, xếp hạng | `khc_docs/index.py` |
| Đồng bộ tăng dần | `khc_docs/sync.py` |
| MCP server | `khc_docs/mcp_server.py` |
| Dòng lệnh | `khc_docs/cli.py` |

**Nguyên tắc dữ liệu**
- Chỉ đọc Drive (scope `drive.readonly`), không sửa/xoá file nguồn.
- Mỗi đoạn mang nhãn vị trí: `tr.12`, `sheet 'BOQ'!A41:H80`, `slide 3`, `mục: Biện pháp > An toàn`.
- Đoạn lấy từ OCR được đánh dấu `ocr=true`; Claude được hướng dẫn ghi "(OCR, cần đối chiếu bản gốc)" khi dùng số liệu từ đó.
- Excel lấy **giá trị đã lưu** của công thức (không tính lại).
- File trùng nội dung ở nhiều thư mục chỉ trích xuất 1 lần; kết quả báo `duplicate_copies`.
- File bị xoá/di chuyển khỏi Drive → trạng thái `removed`, không còn xuất hiện khi tìm.

## 2. Cài đặt (máy chạy: PC văn phòng hoặc máy chủ nội bộ)

1. Python ≥ 3.11
2. Tesseract OCR có gói tiếng Việt
   - Windows: cài bản UB-Mannheim, tick **Vietnamese**; thêm `C:\Program Files\Tesseract-OCR` vào PATH.
   - Ubuntu: `sudo apt install tesseract-ocr tesseract-ocr-vie`
3. (Tuỳ chọn) LibreOffice nếu có file `.doc/.xls/.ppt` đời cũ.
4. Cài gói:
   ```bash
   pip install -e .
   ```

## 3. Kết nối Google Drive

Chọn **một** trong hai cách:

**A. Drive API (khuyến nghị cho máy chủ)**
1. Google Cloud Console → tạo project → bật **Google Drive API**.
2. *Credentials → Create credentials → OAuth client ID → Desktop app* → tải JSON về `secrets/credentials.json`.
   (Hoặc dùng *Service account* rồi share thư mục NAM HÀ cho email của service account, quyền Viewer.)
3. `khc-docs auth` → đăng nhập tài khoản có quyền xem thư mục → token lưu ở `secrets/token.json`.

**B. Drive for Desktop** — đã đồng bộ thư mục về máy thì bỏ `drive_folder_id`, đặt `local_path = "G:/My Drive/.../NAM HÀ"`.

## 4. Cấu hình & chạy

```bash
cp khc_docs.example.toml khc_docs.toml       # đã điền sẵn folder NAM HÀ: 1gIygpVT9zbsBiLxOXB0XQO_B76Wq7acQ
khc-docs sync --dry-run                       # xem file nào sẽ xử lý / bỏ qua, chưa tải
khc-docs sync --limit 30                      # chạy thử 30 file
khc-docs sync                                 # toàn bộ; chạy lại chỉ xử lý file mới/đổi
khc-docs sync --path "IPC-02/HSCL"            # chỉ thư mục/file có đường dẫn chứa chuỗi này (không phân biệt dấu)
khc-docs sync --path "KCS DOT 01" --force     # trích xuất lại riêng phạm vi đó, không đụng phần còn lại
khc-docs stats
khc-docs errors --status error                # file lỗi; --status empty: file không ra chữ
khc-docs search "tạm ứng bảo lãnh" --category 03
khc-docs show 123                             # xem toàn văn chunk 123
```

Lịch đồng bộ: Task Scheduler (Windows) hoặc cron chạy `khc-docs sync` mỗi đêm.

## 5. Gắn vào Claude

**Claude Desktop** — `claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "khc-docs": {
      "command": "khc-docs",
      "args": ["-c", "D:/KHC/khc-docs/khc_docs.toml", "serve"]
    }
  }
}
```

**Claude Code**:
```bash
claude mcp add khc-docs -- khc-docs -c /duong-dan/khc_docs.toml serve
```

Ví dụ hỏi: *"Theo hợp đồng A-B Nam Hà 2, điều kiện tạm ứng và tỷ lệ giữ lại bảo hành là gì? Trích dẫn nguồn."*
Claude sẽ gọi `search_docs` → `read_chunk` 1–3 đoạn → trả lời kèm `[03-HỢP ĐỒNG/3.1. HĐ A-B/…pdf, tr.12]`.

## 6. Công cụ MCP

| Tool | Dùng khi | Token trả về (ước lượng) |
|---|---|---|
| `search_docs(query, category, file_type, path_contains, limit)` | luôn bắt đầu ở đây | ~100–150/kết quả |
| `read_chunk(chunk_ids, max_tokens)` | đọc toàn văn đoạn đã chọn | ≤ 700/đoạn, chặn ở `max_tokens` |
| `read_context(chunk_id, before, after)` | bảng/điều khoản bị cắt ngang | ≤ 7 đoạn |
| `document_outline(doc_id)` | xem mục lục 1 file để chọn đoạn | nhỏ |
| `list_documents(category, name_contains, status)` | kiểm kê | nhỏ |
| `index_stats()` | kiểm tra độ phủ kho, file lỗi, file trùng | nhỏ |

Tìm kiếm: BM25 trên văn bản bỏ dấu (gõ "tien do" vẫn ra "tiến độ"), ưu tiên lại kết quả khớp đúng dấu và khớp nguyên cụm; tối đa 3 đoạn/tài liệu để kết quả đa dạng; `"cụm từ"` để tìm chính xác; mã như `AF.11111`, `BB-12/2026` được tìm theo cụm.

## 7. Giới hạn đã biết — cần lưu ý khi dùng

| Vấn đề | Ảnh hưởng | Xử lý |
|---|---|---|
| Văn bản font **TCVN3 / VNI** (hồ sơ cũ) | chữ trích ra bị lỗi mã, không tìm được | REVIEW REQUIRED: cần kiểm trên file thật; nếu có sẽ bổ sung bộ chuyển mã sang Unicode |
| OCR số liệu, ngày, chữ ký, con dấu | có thể sai ký tự | đã gắn cờ `ocr`; luôn đối chiếu bản gốc trước khi dùng số liệu cho thanh toán/claim |
| Bản vẽ PDF (CAD) | text rời rạc, OCR chậm | giới hạn `max_ocr_pages`; cân nhắc thêm `06-BẢN VẼ` vào `exclude_paths` nếu không cần tra cứu chữ trên bản vẽ |
| Tìm theo từ khoá, chưa theo ngữ nghĩa | câu hỏi diễn đạt khác từ trong hồ sơ có thể trượt | Claude tự thử từ khoá khác; giai đoạn 2 có thể thêm embedding (hybrid search) |
| Excel chưa từng mở bằng Excel (công thức chưa có giá trị cache) | ô công thức trống | mở & lưu lại bằng Excel |
| Quyền truy cập | MCP trả nội dung mọi file trong kho cho ai dùng máy đó | chỉ cài trên máy của người có quyền xem hồ sơ dự án |

## 8. Kiểm thử

```bash
pip install -e ".[dev]" && pytest -q
```
Bộ test dùng **file giả lập** (không phải dữ liệu thật KHC): PDF text, PDF scan (OCR), DOCX có heading + bảng, XLSX BOQ, PPTX, file trùng, thư mục loại trừ, file bị xoá, gọi tool MCP, duyệt Drive có shortcut và Google Docs.

**Tiêu chí nghiệm thu trên dữ liệu thật Nam Hà (đề xuất)**
1. `sync` chạy hết, `errors` = 0 hoặc có lý do rõ cho từng file.
2. Lấy 20 câu hỏi thật (hợp đồng, phát sinh, nghiệm thu, tiến độ, ngân sách) → ≥ 17/20 câu Claude tìm đúng file + đúng trang/sheet.
3. Kiểm 10 trang OCR ngẫu nhiên: số liệu then chốt (giá trị, ngày, mã hồ sơ) đúng ≥ 95%.
4. Không có trích dẫn trỏ sai file/trang trong 20 câu trên.
