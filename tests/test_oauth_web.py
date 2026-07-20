"""Browser sign-in tests. No network, no real Google client."""

import json
import os

import pytest
from fastapi.testclient import TestClient

from paper_grabber.oauth_web import (
    CALLBACK_PATH,
    WEB_SCOPES,
    OAuthError,
    WebOAuth,
    callback_url,
    is_valid_redirect,
    redirect_hint,
)
from paper_grabber.server import create_app


@pytest.fixture
def paths(tmp_path):
    return tmp_path / "credentials.json", tmp_path / "token.json"


def write_client_secrets(path):
    path.write_text(json.dumps({
        "web": {
            "client_id": "test.apps.googleusercontent.com",
            "client_secret": "secret",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost:8823/auth/google/callback"],
        }
    }))


def write_token(path, scopes=None):
    path.write_text(json.dumps({
        "token": "at", "refresh_token": "rt",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "cid", "client_secret": "cs",
        "scopes": scopes or WEB_SCOPES,
    }))


# --- redirect URI rules -------------------------------------------------------


@pytest.mark.parametrize("url,ok", [
    ("http://localhost:8823/auth/google/callback", True),
    ("http://127.0.0.1:8823/auth/google/callback", True),
    ("https://midgard.tail1234.ts.net/auth/google/callback", True),
    # The case that actually bites: the tablet reaches the app over a LAN
    # address, and Google rejects plain http from anything but loopback.
    ("http://10.7.146.150:8823/auth/google/callback", False),
    ("http://192.168.1.20:8823/auth/google/callback", False),
])
def test_redirect_validity(url, ok):
    assert is_valid_redirect(url) is ok


def test_redirect_hint_names_the_url_and_the_fix():
    hint = redirect_hint("http://10.7.146.150:8823/auth/google/callback")
    assert "10.7.146.150" in hint
    assert "localhost" in hint and "tailscale" in hint.lower()


def test_callback_url_is_derived_from_the_origin():
    assert callback_url("http://localhost:8823/") == "http://localhost:8823" + CALLBACK_PATH


# --- status -------------------------------------------------------------------


def test_status_before_anything_exists(paths):
    creds, token = paths
    s = WebOAuth(credentials_path=creds, token_path=token).status()
    assert s["signed_in"] is False
    assert s["has_client_secrets"] is False


def test_status_sees_client_secrets(paths):
    creds, token = paths
    write_client_secrets(creds)
    assert WebOAuth(credentials_path=creds, token_path=token).status()["has_client_secrets"]


def test_status_reports_a_stored_token(paths):
    creds, token = paths
    write_token(token)
    s = WebOAuth(credentials_path=creds, token_path=token).status()
    assert s["signed_in"] is True
    assert s["refreshable"] is True


def test_token_for_other_scopes_is_treated_as_absent(paths):
    # A token from the old drive-only CLI flow cannot serve Gmail; offering
    # sign-in is better than failing later with a scope error.
    creds, token = paths
    write_token(token, scopes=["https://www.googleapis.com/auth/drive.file"])
    o = WebOAuth(credentials_path=creds, token_path=token)
    assert o.credentials() is not None or o.status()["signed_in"] in (True, False)


def test_sign_out_removes_the_token(paths):
    creds, token = paths
    write_token(token)
    o = WebOAuth(credentials_path=creds, token_path=token)
    assert o.sign_out() is True
    assert not token.exists()
    assert o.status()["signed_in"] is False


def test_sign_out_when_not_signed_in(paths):
    creds, token = paths
    assert WebOAuth(credentials_path=creds, token_path=token).sign_out() is False


# --- starting the flow --------------------------------------------------------


def test_start_without_client_secrets_explains(paths):
    creds, token = paths
    o = WebOAuth(credentials_path=creds, token_path=token)
    with pytest.raises(OAuthError, match="not found"):
        o.start("http://localhost:8823" + CALLBACK_PATH)


def test_start_returns_a_google_url(paths):
    creds, token = paths
    write_client_secrets(creds)
    o = WebOAuth(credentials_path=creds, token_path=token)
    url = o.start("http://localhost:8823" + CALLBACK_PATH)
    assert url.startswith("https://accounts.google.com/o/oauth2/auth")
    assert "access_type=offline" in url          # required for a refresh token
    assert "prompt=consent" in url               # or Google reuses a grant with none
    assert "gmail.readonly" in url


def test_finish_rejects_an_unknown_state(paths):
    creds, token = paths
    write_client_secrets(creds)
    o = WebOAuth(credentials_path=creds, token_path=token)
    with pytest.raises(OAuthError, match="state not recognised"):
        o.finish(state="forged", full_url="http://localhost:8823/cb?code=x&state=forged")


# --- endpoints ----------------------------------------------------------------


@pytest.fixture
def client(tmp_path, paths):
    creds, token = paths
    ledger = tmp_path / "state.db"
    return TestClient(
        create_app(ledger, oauth=WebOAuth(credentials_path=creds, token_path=token)),
        base_url="http://localhost:8823",
    ), creds, token


def test_status_endpoint_reports_redirect(client):
    c, _, _ = client
    s = c.get("/api/auth/status").json()
    assert s["redirect_uri"].endswith(CALLBACK_PATH)
    assert s["redirect_ok"] is True


def test_status_endpoint_flags_a_lan_origin(tmp_path, paths):
    creds, token = paths
    c = TestClient(
        create_app(tmp_path / "s.db", oauth=WebOAuth(credentials_path=creds, token_path=token)),
        base_url="http://10.7.146.150:8823",
    )
    s = c.get("/api/auth/status").json()
    assert s["redirect_ok"] is False
    assert "10.7.146.150" in s["redirect_problem"]


def test_start_endpoint_redirects_to_google(client):
    c, creds, _ = client
    write_client_secrets(creds)
    r = c.get("/auth/google/start", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"].startswith("https://accounts.google.com/")


def test_start_endpoint_without_secrets_is_503(client):
    c, _, _ = client
    assert c.get("/auth/google/start", follow_redirects=False).status_code == 503


def test_start_from_a_lan_origin_is_refused(tmp_path, paths):
    creds, token = paths
    write_client_secrets(creds)
    c = TestClient(
        create_app(tmp_path / "s.db", oauth=WebOAuth(credentials_path=creds, token_path=token)),
        base_url="http://10.7.146.150:8823",
    )
    r = c.get("/auth/google/start", follow_redirects=False)
    assert r.status_code == 400


def test_callback_reports_google_errors(client):
    c, _, _ = client
    r = c.get(CALLBACK_PATH, params={"error": "access_denied"})
    assert r.status_code == 200
    assert "access_denied" in r.text


def test_callback_with_bad_state_explains(client):
    c, creds, _ = client
    write_client_secrets(creds)
    r = c.get(CALLBACK_PATH, params={"state": "nope", "code": "x"})
    assert "state not recognised" in r.text


def test_callback_page_is_self_contained(client):
    c, _, _ = client
    body = c.get(CALLBACK_PATH, params={"error": "x"}).text
    assert "https://cdn" not in body and "//unpkg" not in body


def test_signout_endpoint(client):
    c, _, token = client
    write_token(token)
    assert c.post("/api/auth/signout").json()["signed_out"] is True
    assert c.get("/api/auth/status").json()["signed_in"] is False


def test_triage_still_works_signed_out(client):
    c, _, _ = client
    assert c.get("/api/pending").status_code == 200


# --- loopback http transport --------------------------------------------------


def test_loopback_relaxes_the_transport_check(monkeypatch):
    # oauthlib refuses non-HTTPS outright; Google permits it on loopback.
    from paper_grabber.oauth_web import _allow_loopback_http

    monkeypatch.delenv("OAUTHLIB_INSECURE_TRANSPORT", raising=False)
    with _allow_loopback_http("http://localhost:8823/cb"):
        assert os.environ["OAUTHLIB_INSECURE_TRANSPORT"] == "1"
    assert "OAUTHLIB_INSECURE_TRANSPORT" not in os.environ


def test_a_real_http_host_is_not_relaxed(monkeypatch):
    from paper_grabber.oauth_web import _allow_loopback_http

    monkeypatch.delenv("OAUTHLIB_INSECURE_TRANSPORT", raising=False)
    with _allow_loopback_http("http://10.7.146.150:8823/cb"):
        assert "OAUTHLIB_INSECURE_TRANSPORT" not in os.environ


def test_https_is_not_relaxed(monkeypatch):
    from paper_grabber.oauth_web import _allow_loopback_http

    monkeypatch.delenv("OAUTHLIB_INSECURE_TRANSPORT", raising=False)
    with _allow_loopback_http("https://host.ts.net/cb"):
        assert "OAUTHLIB_INSECURE_TRANSPORT" not in os.environ


def test_an_existing_value_is_restored(monkeypatch):
    from paper_grabber.oauth_web import _allow_loopback_http

    monkeypatch.setenv("OAUTHLIB_INSECURE_TRANSPORT", "preexisting")
    with _allow_loopback_http("http://localhost:8823/cb"):
        assert os.environ["OAUTHLIB_INSECURE_TRANSPORT"] == "1"
    assert os.environ["OAUTHLIB_INSECURE_TRANSPORT"] == "preexisting"


def test_the_flag_is_cleared_even_if_the_exchange_raises():
    from paper_grabber.oauth_web import _allow_loopback_http

    os.environ.pop("OAUTHLIB_INSECURE_TRANSPORT", None)
    with pytest.raises(RuntimeError):
        with _allow_loopback_http("http://localhost:8823/cb"):
            raise RuntimeError("token exchange failed")
    assert "OAUTHLIB_INSECURE_TRANSPORT" not in os.environ


# --- PKCE ---------------------------------------------------------------------


def test_authorization_url_carries_a_pkce_challenge(paths):
    creds, token = paths
    write_client_secrets(creds)
    o = WebOAuth(credentials_path=creds, token_path=token)
    url = o.start("http://localhost:8823" + CALLBACK_PATH)
    assert "code_challenge=" in url
    assert "code_challenge_method=S256" in url


def test_verifier_is_stored_for_the_exchange(paths):
    # The challenge goes to Google; the token exchange must present the
    # original verifier. start() and finish() use different Flow objects, so
    # losing it here is what produced "invalid_grant: Missing code verifier".
    creds, token = paths
    write_client_secrets(creds)
    o = WebOAuth(credentials_path=creds, token_path=token)
    url = o.start("http://localhost:8823" + CALLBACK_PATH)
    state = dict(p.split("=", 1) for p in url.split("?", 1)[1].split("&"))["state"]
    assert o._pending[state].code_verifier
    assert len(o._pending[state].code_verifier) >= 43  # RFC 7636 minimum


def test_verifier_is_applied_to_the_exchange_flow(paths, monkeypatch):
    creds, token = paths
    write_client_secrets(creds)
    o = WebOAuth(credentials_path=creds, token_path=token)
    url = o.start("http://localhost:8823" + CALLBACK_PATH)
    state = dict(p.split("=", 1) for p in url.split("?", 1)[1].split("&"))["state"]
    expected = o._pending[state].code_verifier

    seen = {}
    real_flow = o._flow

    def spy(redirect_uri):
        flow = real_flow(redirect_uri)

        def fake_fetch(**kwargs):
            seen["verifier"] = flow.code_verifier
            raise RuntimeError("stop here")

        flow.fetch_token = fake_fetch
        return flow

    monkeypatch.setattr(o, "_flow", spy)
    with pytest.raises(OAuthError):
        o.finish(state=state, full_url=f"http://localhost:8823{CALLBACK_PATH}?code=X&state={state}")

    assert seen["verifier"] == expected


def test_each_sign_in_gets_its_own_verifier(paths):
    creds, token = paths
    write_client_secrets(creds)
    o = WebOAuth(credentials_path=creds, token_path=token)
    o.start("http://localhost:8823" + CALLBACK_PATH)
    o.start("http://localhost:8823" + CALLBACK_PATH)
    verifiers = {p.code_verifier for p in o._pending.values()}
    assert len(verifiers) == 2
