import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

import backend  # noqa: E402

SECRET = "test-secret"


def test_secret_rejects_a_missing_or_wrong_header_and_accepts_a_match(monkeypatch):
    monkeypatch.setenv("SCREENER_MODE", "demo")
    monkeypatch.setenv("SCREENER_API_SECRET", SECRET)
    with TestClient(backend.app) as client:
        assert client.get("/").status_code == 200
        assert "X-Screener-Secret" in client.get("/").text
        assert client.get("/api/orders/open").status_code == 401
        assert client.post("/api/orders/send", json={"preview_id": "x"}).status_code == 401
        assert client.get("/api/orders/open", headers={"X-Screener-Secret": "nope"}).status_code == 401
        opened = client.get("/api/orders/open", headers={"X-Screener-Secret": SECRET})
        assert opened.status_code == 200
        assert opened.json()["mode"] == "demo"


def test_demo_without_a_secret_stays_open(monkeypatch):
    monkeypatch.setenv("SCREENER_MODE", "demo")
    monkeypatch.delenv("SCREENER_API_SECRET", raising=False)
    with TestClient(backend.app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/screener-data").status_code == 200
        assert client.get("/api/orders/alerts").status_code == 200


def test_live_mode_without_a_secret_refuses_api(monkeypatch):
    monkeypatch.setenv("SCREENER_MODE", "schwab")
    monkeypatch.delenv("SCREENER_API_SECRET", raising=False)
    with TestClient(backend.app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/candles", params={"symbol": "NVDA"}).status_code == 401
        assert client.post("/api/orders/quick", json={"symbol": "NVDA"}).status_code == 401
        allowed = client.get("/api/orders/alerts", headers={"X-Screener-Secret": "anything"})
        assert allowed.status_code == 401
