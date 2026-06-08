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
