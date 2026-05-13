import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(pymupdf_main):
    return TestClient(pymupdf_main.app)


def test_health_live(client):
    r = client.get("/health/live")
    assert r.status_code == 200
    assert r.json()["status"] == "alive"


def test_health_ready(client):
    r = client.get("/health/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert body["parser"] == "pymupdf4llm"


def test_health_alias(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ready"


def test_process_empty_body(client):
    r = client.put("/process", content=b"")
    assert r.status_code == 400


def test_process_oversize_by_content_length(client, pymupdf_main, monkeypatch):
    monkeypatch.setattr(pymupdf_main, "MAX_UPLOAD_BYTES", 10)
    r = client.put(
        "/process",
        content=b"x" * 100,
        headers={"X-Filename": "big.pdf", "Content-Length": "100"},
    )
    assert r.status_code == 413


def test_process_oversize_by_stream(client, pymupdf_main, monkeypatch):
    monkeypatch.setattr(pymupdf_main, "MAX_UPLOAD_BYTES", 10)
    r = client.put("/process", content=b"x" * 100, headers={"X-Filename": "big.pdf"})
    assert r.status_code == 413


def test_tempfile_cleaned_up_on_success(client, pymupdf_main, text_pdf_bytes):
    import os
    before = set(os.listdir(pymupdf_main.TMP_DIR))
    r = client.put(
        "/process",
        content=text_pdf_bytes,
        headers={"X-Filename": "t.pdf", "Content-Type": "application/pdf"},
    )
    assert r.status_code == 200
    after = set(os.listdir(pymupdf_main.TMP_DIR))
    assert after == before


def test_tempfile_cleaned_up_on_oversize(client, pymupdf_main, monkeypatch):
    import os
    monkeypatch.setattr(pymupdf_main, "MAX_UPLOAD_BYTES", 10)
    before = set(os.listdir(pymupdf_main.TMP_DIR))
    r = client.put(
        "/process",
        content=b"x" * 100,
        headers={"X-Filename": "big.pdf"},
    )
    assert r.status_code == 413
    after = set(os.listdir(pymupdf_main.TMP_DIR))
    assert after == before


def test_process_invalid_pdf_returns_500(client):
    r = client.put(
        "/process",
        content=b"definitely not a pdf",
        headers={"X-Filename": "broken.pdf", "Content-Type": "application/pdf"},
    )
    assert r.status_code == 500


def test_process_valid_text_pdf(client, text_pdf_bytes):
    r = client.put(
        "/process",
        content=text_pdf_bytes,
        headers={"X-Filename": "sample.pdf", "Content-Type": "application/pdf"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["metadata"]["parser"] == "pymupdf4llm"
    assert data["metadata"]["filename"] == "sample.pdf"
    assert data["metadata"]["size_bytes"] == len(text_pdf_bytes)
    assert data["page_content"].strip()


def test_process_normalizes_filename(client, text_pdf_bytes):
    r = client.put(
        "/process",
        content=text_pdf_bytes,
        headers={"X-Filename": "../../../etc/passwd.pdf", "Content-Type": "application/pdf"},
    )
    assert r.status_code == 200
    assert r.json()["metadata"]["filename"] == "passwd.pdf"
