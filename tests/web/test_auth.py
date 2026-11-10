"""API-key authentication: every endpoint requires "Authorization: Bearer <key>".

The key comes from the deployment environment (SINAPIS_API_KEY) and is enforced
at WebSettings.from_env() with fail-closed semantics; create_app() turns on the
global dependency whenever the key is non-empty. Tests build settings directly,
so both halves are covered separately here.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from server.controller.web_controller import WebSettings, create_app

KEY = "test-key-abc123"


def bearer(value: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {value}"}


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app(WebSettings(api_key=KEY)))


# -- configuration resolution -------------------------------------------------
class TestFromEnvKeyResolution:
    def test_missing_key_refuses_to_start(self):
        """Fail closed: no key and no explicit opt-out must abort the launch."""
        with pytest.raises(ValueError, match="SINAPIS_API_KEY"):
            WebSettings.from_env({})

    def test_key_is_loaded_and_stripped(self):
        settings = WebSettings.from_env({"SINAPIS_API_KEY": f"  {KEY}  "})
        assert settings.api_key == KEY

    def test_explicit_disable_yields_no_key(self):
        settings = WebSettings.from_env({"SINAPIS_AUTH_DISABLED": "true"})
        assert settings.api_key == ""

    def test_key_and_disable_are_mutually_exclusive(self):
        with pytest.raises(ValueError, match="mutually exclusive"):
            WebSettings.from_env({"SINAPIS_API_KEY": KEY, "SINAPIS_AUTH_DISABLED": "true"})

    def test_invalid_disable_value_still_requires_a_key(self):
        with pytest.raises(ValueError, match="SINAPIS_API_KEY"):
            WebSettings.from_env({"SINAPIS_AUTH_DISABLED": "probably"})


# -- request gating ------------------------------------------------------------
class TestRequestGating:
    def test_every_endpoint_rejects_a_missing_key(self, client):
        for path in ("/api/healthz", "/api/overview", "/api/docs", "/api/openapi.json"):
            response = client.get(path)
            assert response.status_code == 401, path
            assert response.headers["www-authenticate"] == "Bearer"

    def test_post_endpoints_reject_a_missing_key(self, client):
        body = {"source": "t", "metrics": [{"name": "m", "value": 1}]}
        assert client.post("/api/user-system/metrics", json=body).status_code == 401
        assert client.post(
            "/api/user-system/events",
            json={"source": "t", "events": [{"name": "e", "labels": {}, "fields": {}}]},
        ).status_code == 401

    def test_wrong_key_is_rejected(self, client):
        assert client.get("/api/healthz", headers=bearer("not-the-key")).status_code == 401

    def test_malformed_header_is_rejected(self, client):
        for value in ("Basic dXNlcjpwYXNz", "Bearer", "Bearer    ", "bearer-without-space",
                      f"Token {KEY}"):
            response = client.get("/api/healthz", headers={"Authorization": value})
            assert response.status_code == 401, value

    def test_rejection_message_does_not_leak_why(self, client):
        """Missing, malformed, and wrong keys all answer identically, so a caller
        cannot probe which check failed."""
        messages = {
            client.get("/api/healthz").json()["detail"],
            client.get("/api/healthz", headers=bearer("nope")).json()["detail"],
            client.get(
                "/api/healthz", headers={"Authorization": "garbage"}
            ).json()["detail"],
        }
        assert messages == {"missing or invalid API key"}

    def test_correct_key_passes_on_get_and_post(self, client):
        assert client.get("/api/healthz", headers=bearer(KEY)).status_code == 200
        assert client.get("/api/overview", headers=bearer(KEY)).status_code == 200
        assert client.get("/api/docs", headers=bearer(KEY)).status_code == 200
        # Auth passed: the request reaches the business layer (200 accepted or
        # 503 config-unavailable), never the auth gate's 401.
        response = client.post(
            "/api/user-system/metrics",
            json={"source": "t", "metrics": [{"name": "m", "value": 1}]},
            headers=bearer(KEY),
        )
        assert response.status_code in (200, 503)

    def test_scheme_match_is_case_insensitive(self, client):
        response = client.get(
            "/api/healthz", headers={"Authorization": f"bearer {KEY}"}
        )
        assert response.status_code == 200

    def test_no_key_settings_serve_openly(self):
        """Programmatic settings without a key keep auth off (tests/dev tooling);
        production cannot reach this state because from_env() fails closed."""
        open_client = TestClient(create_app(WebSettings()))
        assert open_client.get("/api/healthz").status_code == 200
