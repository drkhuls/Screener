import base64
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import backend  # noqa: E402


def _basic(user, password):
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def test_basic_auth_check():
    assert backend.basic_auth_ok(_basic("pardeep", "s3cret:x"), "pardeep", "s3cret:x")
    assert not backend.basic_auth_ok(_basic("pardeep", "wrong"), "pardeep", "s3cret:x")
    assert not backend.basic_auth_ok("Basic !!!", "pardeep", "s3cret:x")
    assert not backend.basic_auth_ok(None, "pardeep", "s3cret:x")


@pytest.fixture
def demo_client(monkeypatch):
    monkeypatch.setenv("SCREENER_MODE", "demo")
    with TestClient(backend.app) as client:
        yield client


def test_auth_enforced_only_when_configured(demo_client, monkeypatch):
    monkeypatch.delenv("DASHBOARD_USER", raising=False)
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    assert demo_client.get("/api/screener-data").status_code == 200
    monkeypatch.setenv("DASHBOARD_USER", "pardeep")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "s3cret")
    response = demo_client.get("/")
    assert response.status_code == 401 and response.headers["www-authenticate"].startswith("Basic")
    assert demo_client.get("/api/screener-data", headers={"Authorization": _basic("pardeep", "nope")}).status_code == 401
    assert demo_client.get("/api/screener-data", headers={"Authorization": _basic("pardeep", "s3cret")}).status_code == 200


def test_schwab_mode_starts_without_token_and_loads_new_login(tmp_path, monkeypatch):
    token = tmp_path / "token.json"
    monkeypatch.setenv("SCHWAB_TOKEN_PATH", str(token))
    created = []

    def fake_source(mode, *_):
        created.append(mode)
        return object()

    monkeypatch.setattr(backend, "make_source", fake_source)
    screener = backend.Screener(backend.load_config(), "schwab")
    assert screener.source is None and "No Schwab token" in screener.errors["schwab"]
    assert screener.snapshot()["meta"]["token"]["level"] == "missing"

    token.write_text(json.dumps({"creation_timestamp": time.time() - 3600, "token": {}}))
    screener.ensure_source()
    first = screener.source
    assert first is not None and "schwab" not in screener.errors
    screener.ensure_source()
    assert screener.source is first and len(created) == 1

    token.write_text(json.dumps({"creation_timestamp": time.time(), "token": {}}))
    screener.ensure_source()
    assert screener.source is not first and len(created) == 2
    assert screener.last_poll is None
