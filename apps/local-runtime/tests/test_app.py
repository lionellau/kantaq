"""Runtime bootstrap tests (FR-E01-3): the app boots and serves a UI surface."""

from fastapi.testclient import TestClient

from kantaq_runtime.app import app

client = TestClient(app)


def test_healthz_ok() -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == "0.2.0"


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


def test_unknown_api_path_is_json_404_not_spa() -> None:
    # The SPA fallback must never swallow the API namespace: an agent calling a
    # typo'd /v1 path needs a machine-readable 404, not index.html with a 200.
    for path in ("/v1/nope", "/v1", "/v1/members/x/unknown-action"):
        response = client.get(path)
        assert response.status_code == 404, path
        assert response.headers["content-type"].startswith("application/json"), path


def test_v1_prefixed_spa_route_still_deep_links() -> None:
    # Only the exact /v1 namespace is reserved; a client route that merely
    # starts with "v1" (e.g. /v1ctory) still falls back to the SPA.
    response = client.get("/v1ctory")
    assert response.status_code == 200
