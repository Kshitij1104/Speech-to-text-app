import os

import numpy as np
import pytest

os.environ["STT_BACKEND"] = "mock"
os.environ["DATABASE_URL"] = "sqlite:///./test_transcriptions.db"

from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base, engine  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    Base.metadata.drop_all(engine)
    with TestClient(app) as c:  # lifespan creates tables
        yield c


def test_health(client):
    assert client.get("/health").json()["status"] == "ok"


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "Speech" in r.text


def test_crud_flow(client):
    r = client.post("/api/transcriptions", json={"text": "नमस्ते, how are you?", "language": "hi"})
    assert r.status_code == 201
    tid = r.json()["id"]
    assert r.json()["title"].startswith("नमस्ते")  # auto title, unicode intact

    assert client.get(f"/api/transcriptions/{tid}").json()["text"] == "नमस्ते, how are you?"

    r = client.put(f"/api/transcriptions/{tid}", json={"title": "Greeting"})
    assert r.json()["title"] == "Greeting"
    assert r.json()["text"] == "नमस्ते, how are you?"  # untouched

    page = client.get("/api/transcriptions").json()
    assert page["total"] == 1 and page["items"][0]["id"] == tid

    assert client.get(f"/api/transcriptions/{tid}/download").text == "नमस्ते, how are you?"

    assert client.delete(f"/api/transcriptions/{tid}").status_code == 204
    assert client.get(f"/api/transcriptions/{tid}").status_code == 404
    assert client.delete(f"/api/transcriptions/{tid}").status_code == 404


def test_validation(client):
    assert client.post("/api/transcriptions", json={"text": ""}).status_code == 422
    tid = client.post("/api/transcriptions", json={"text": "x"}).json()["id"]
    assert client.put(f"/api/transcriptions/{tid}", json={}).status_code == 422
    assert client.put(f"/api/transcriptions/{tid}", json={"text": ""}).status_code == 422


def test_search_and_paging(client):
    for i in range(5):
        client.post("/api/transcriptions", json={"text": f"apple {i}" if i % 2 == 0 else f"banana {i}"})
    assert client.get("/api/transcriptions", params={"q": "apple"}).json()["total"] == 3
    p = client.get("/api/transcriptions", params={"limit": 2, "skip": 4}).json()
    assert p["total"] == 5 and len(p["items"]) == 1


def test_upload(client):
    r = client.post("/api/transcribe-file", files={"file": ("a.wav", b"x" * 100)}, data={"mode": "hinglish"})
    assert r.status_code == 201
    assert r.json()["source"] == "upload" and r.json()["text"].startswith("mock")
    bad = client.post("/api/transcribe-file", files={"file": ("a.wav", b"x")}, data={"mode": "nope"})
    assert bad.status_code == 422


def _tone(seconds, loud=True):
    n = int(16000 * seconds)
    t = np.arange(n) / 16000
    amp = 8000 if loud else 0
    return (amp * np.sin(2 * np.pi * 220 * t)).astype("<i2").tobytes()


def test_websocket_live_flow_saves_to_db(client):
    with client.websocket_connect("/ws/transcribe") as ws:
        ws.send_json({"type": "start", "mode": "hinglish", "title": "Live test"})
        assert ws.receive_json()["type"] == "ready"
        for _ in range(4):  # 2s of speech in 0.5s chunks
            ws.send_bytes(_tone(0.5))
        ws.send_bytes(_tone(1.2, loud=False))  # pause -> sentence ends
        events = []
        ws.send_json({"type": "stop"})
        while True:
            ev = ws.receive_json()
            events.append(ev)
            if ev["type"] == "saved":
                break
    kinds = [e["type"] for e in events]
    assert "final" in kinds
    saved = events[-1]["transcription"]
    assert saved["source"] == "live" and saved["title"] == "Live test"
    assert client.get("/api/transcriptions").json()["total"] == 1


def test_websocket_silence_saves_nothing(client):
    with client.websocket_connect("/ws/transcribe") as ws:
        ws.send_json({"type": "start"})
        ws.receive_json()
        ws.send_bytes(_tone(1.0, loud=False))
        ws.send_json({"type": "stop"})
        assert ws.receive_json() == {"type": "saved", "transcription": None}
    assert client.get("/api/transcriptions").json()["total"] == 0


def test_websocket_requires_start(client):
    with client.websocket_connect("/ws/transcribe") as ws:
        ws.send_bytes(b"\x00\x01" * 100)
        assert ws.receive_json()["type"] == "error"
