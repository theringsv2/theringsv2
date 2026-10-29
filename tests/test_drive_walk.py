"""Kiểm tra logic duyệt Drive (đường dẫn, shortcut, Google Docs export) bằng service giả lập."""

from khc_docs.sources import DriveSource

TREE = {
    "ROOT": [
        {"id": "F03", "name": "03-HỢP ĐỒNG", "mimeType": "application/vnd.google-apps.folder"},
        {"id": "S1", "name": "lối tắt BB", "mimeType": "application/vnd.google-apps.shortcut",
         "shortcutDetails": {"targetId": "F09", "targetMimeType": "application/vnd.google-apps.folder"}},
    ],
    "F03": [
        {"id": "P1", "name": "HĐ A-B.pdf", "mimeType": "application/pdf", "md5Checksum": "abc", "size": "10"},
        {"id": "G1", "name": "Ghi chú", "mimeType": "application/vnd.google-apps.document", "modifiedTime": "t1"},
        {"id": "X1", "name": "Form", "mimeType": "application/vnd.google-apps.form"},
        {"id": "LOOP", "name": "vòng lặp", "mimeType": "application/vnd.google-apps.shortcut",
         "shortcutDetails": {"targetId": "ROOT", "targetMimeType": "application/vnd.google-apps.folder"}},
    ],
    "F09": [{"id": "P2", "name": "BBNT.pdf", "mimeType": "application/pdf", "md5Checksum": "def"}],
}


class _Req:
    def __init__(self, v): self.v = v
    def execute(self, num_retries=0): return self.v


class _Files:
    def list(self, q, **kw):
        return _Req({"files": TREE[q.split("'")[1]]})


class _Svc:
    def files(self): return _Files()


def test_walk():
    src = DriveSource.__new__(DriveSource)
    src.folder_id, src.service = "ROOT", _Svc()
    got = {(f.path, f.name, f.ext, f.hash) for f in src.walk()}
    assert got == {
        ("03-HỢP ĐỒNG", "HĐ A-B.pdf", ".pdf", "abc"),
        ("03-HỢP ĐỒNG", "Ghi chú", ".docx", "rev:t1"),
        ("lối tắt BB", "BBNT.pdf", ".pdf", "def"),
    }
