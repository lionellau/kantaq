"""Runtime bootstrap tests (FR-E01-3): the app boots and serves a UI surface."""

from fastapi.testclient import TestClient

from kantaq_runtime.app import app

client = TestClient(app)


def test_healthz_ok() -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == "0.0.5"


def test_root_serves_a_ui_surface() -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "kantaq" in response.text


def test_spa_fallback_serves_index_for_client_routes() -> None:
    # A client-side route that is not a real file falls back to index.html
    # (or the placeholder when the UI is not built) — never a 404 (E22-T1).
    response = client.get("/agents")
    assert response.status_code == 200
    assert "kantaq" in response.text.lower()
