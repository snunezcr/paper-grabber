"""Zotero sign-in tests. No network, no real Zotero client."""

import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from paper_grabber.server import create_app
from paper_grabber.zotero_auth import (
    CALLBACK_PATH,
    OAUTH_ACCESS_URL,
    OAUTH_AUTHORIZE_URL,
    OAUTH_REQUEST_URL,
    ZoteroAuth,
    ZoteroAuthError,
    callback_url,
    load_client,
    new_key_url,
)

KEY = "P9NiFoyLeZu2bZNvvuQPDWsd"
OTHER_KEY = "aaaaBBBBccccDDDDeeeeFFFF"


def key_info(*, key=KEY, library=True, write=True, notes=True, user_id=475425,
             username="snunez"):
    """A trimmed copy of a real /keys/current record."""
    return {
        "key": key,
        "userID": user_id,
        "username": username,
        "displayName": "",
        "access": {
            "user": {"library": library, "files": True, "notes": notes, "write": write},
            "groups": {},
        },
    }


def make_auth(tmp_path, handler, *, client=True):
    """A ZoteroAuth whose HTTP transport is a callable, not a socket."""
    client_path = tmp_path / "zotero_client.json"
    if client:
        client_path.write_text(json.dumps(
            {"client_key": "ck", "client_secret": "cs"}
        ))
    return ZoteroAuth(
        client_path=client_path,
        credential_path=tmp_path / "zotero.json",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def no_http(request):  # pragma: no cover - a call here is the failure
    raise AssertionError(f"unexpected request to {request.url}")


# --- client registration ------------------------------------------------------


def test_no_client_file_is_not_an_error(tmp_path):
    """The paste-a-key route must work with no OAuth client at all."""
    assert load_client(tmp_path / "missing.json") is None


def test_client_file_accepts_a_wrapped_shape(tmp_path):
    path = tmp_path / "zotero_client.json"
    path.write_text(json.dumps({"zotero": {"client_key": "k", "client_secret": "s"}}))
    reg = load_client(path)
    assert (reg.client_key, reg.client_secret) == ("k", "s")


def test_client_file_missing_the_secret_says_where_to_register(tmp_path):
    path = tmp_path / "zotero_client.json"
    path.write_text(json.dumps({"client_key": "k"}))
    with pytest.raises(ZoteroAuthError, match="oauth/apps"):
        load_client(path)


def test_half_written_client_file_reads_as_no_client(tmp_path):
    """status() is what the whole page hangs off; it must not raise."""
    path = tmp_path / "zotero_client.json"
    path.write_text(json.dumps({"client_key": "k"}))
    auth = ZoteroAuth(client_path=path, credential_path=tmp_path / "z.json")
    assert auth.has_client() is False
    assert auth.status()["has_client"] is False


def test_oauth_start_without_a_client_explains_both_ways_out(tmp_path):
    auth = make_auth(tmp_path, no_http, client=False)
    with pytest.raises(ZoteroAuthError) as exc:
        auth.start("http://localhost:8823" + CALLBACK_PATH)
    assert "oauth/apps" in str(exc.value)
    assert "paste an API key" in str(exc.value)


# --- the prefilled key form ---------------------------------------------------


def test_new_key_url_asks_for_write_but_not_groups():
    params = parse_qs(urlparse(new_key_url()).query)
    assert params["library_access"] == ["1"]
    assert params["write_access"] == ["1"]
    assert params["notes_access"] == ["1"]
    # Nothing here files into a group library, so it is not requested.
    assert params["all_groups"] == ["none"]


# --- verifying a pasted key ---------------------------------------------------


def test_connect_with_key_stores_the_identity_zotero_reports(tmp_path):
    def handler(request):
        assert request.url.path == "/keys/current"
        # The key travels in a header, never the path, so it stays out of logs.
        assert request.headers["Zotero-API-Key"] == KEY
        assert request.headers["Zotero-API-Version"] == "3"
        return httpx.Response(200, json=key_info())

    auth = make_auth(tmp_path, handler)
    cred = auth.connect_with_key(f"  {KEY}  ")

    assert cred.api_key == KEY
    assert cred.identity.user_id == "475425"
    assert cred.identity.username == "snunez"
    s = auth.status()
    assert s["connected"] is True
    assert s["username"] == "snunez"
    assert s["write_access"] is True
    assert s["needs_reconnect"] is False


def test_the_stored_file_is_owner_only(tmp_path):
    auth = make_auth(tmp_path, lambda r: httpx.Response(200, json=key_info()))
    auth.connect_with_key(KEY)
    assert auth.credential_path.stat().st_mode & 0o777 == 0o600


def test_a_rejected_key_is_not_stored(tmp_path):
    auth = make_auth(tmp_path, lambda r: httpx.Response(403, text="Invalid key"))
    with pytest.raises(ZoteroAuthError, match="rejected"):
        auth.connect_with_key("nope")
    assert auth.credential() is None


def test_a_key_without_library_access_is_refused_up_front(tmp_path):
    """Caught while the user is looking at the field, not at upload time."""
    auth = make_auth(
        tmp_path,
        lambda r: httpx.Response(200, json=key_info(library=False, write=False)),
    )
    with pytest.raises(ZoteroAuthError, match="library access"):
        auth.connect_with_key(KEY)
    assert auth.credential() is None


def test_a_read_only_key_connects_but_asks_to_be_replaced(tmp_path):
    auth = make_auth(tmp_path, lambda r: httpx.Response(200, json=key_info(write=False)))
    auth.connect_with_key(KEY)
    s = auth.status()
    assert s["connected"] is True
    assert s["write_access"] is False
    assert s["needs_reconnect"] is True


def test_an_empty_key_never_reaches_the_network(tmp_path):
    auth = make_auth(tmp_path, no_http)
    with pytest.raises(ZoteroAuthError, match="no API key"):
        auth.connect_with_key("   ")


def test_a_key_record_without_a_user_id_is_refused(tmp_path):
    info = key_info()
    del info["userID"]
    auth = make_auth(tmp_path, lambda r: httpx.Response(200, json=info))
    with pytest.raises(ZoteroAuthError, match="which account"):
        auth.connect_with_key(KEY)


def test_an_unreachable_zotero_is_reported_as_such(tmp_path):
    def handler(request):
        raise httpx.ConnectError("no route to host", request=request)

    auth = make_auth(tmp_path, handler)
    with pytest.raises(ZoteroAuthError, match="could not reach Zotero"):
        auth.connect_with_key(KEY)


def test_a_corrupt_credential_file_reads_as_not_connected(tmp_path):
    auth = make_auth(tmp_path, no_http)
    auth.credential_path.write_text("{ truncated")
    assert auth.credential() is None
    assert auth.status()["connected"] is False


def test_disconnect_forgets_the_key(tmp_path):
    auth = make_auth(tmp_path, lambda r: httpx.Response(200, json=key_info()))
    auth.connect_with_key(KEY)
    assert auth.disconnect() is True
    assert auth.credential() is None
    # Idempotent: disconnecting twice is not an error.
    assert auth.disconnect() is False


# --- the OAuth flow -----------------------------------------------------------


def oauth_handler(*, access_fields=None, request_tokens=("reqtok",)):
    """Stand in for Zotero's three endpoints.

    Request tokens are handed out in order, as Zotero would: a second sign-in
    gets a token of its own, so a test can tell the two apart.
    """
    issued = iter(request_tokens)
    access_body = access_fields or {
        # Zotero's access response carries the API key in both fields.
        "oauth_token": KEY,
        "oauth_token_secret": KEY,
        "userID": "475425",
        "username": "snunez",
    }
    seen = {}

    def handler(request):
        url = str(request.url)
        if url == OAUTH_REQUEST_URL:
            seen["request_auth"] = request.headers.get("Authorization", "")
            token = next(issued)
            return httpx.Response(200, text=str(httpx.QueryParams({
                "oauth_token": token,
                "oauth_token_secret": f"{token}-secret",
                "oauth_callback_confirmed": "true",
            })))
        if url == OAUTH_ACCESS_URL:
            seen["access_auth"] = request.headers.get("Authorization", "")
            return httpx.Response(200, text=str(httpx.QueryParams(access_body)))
        if request.url.path == "/keys/current":
            seen["verified_key"] = request.headers["Zotero-API-Key"]
            return httpx.Response(200, json=key_info(key=seen["verified_key"]))
        raise AssertionError(f"unexpected request to {url}")

    handler.seen = seen
    return handler


def test_start_signs_the_request_and_returns_the_authorize_url(tmp_path):
    handler = oauth_handler()
    auth = make_auth(tmp_path, handler)
    url = auth.start("http://10.7.146.150:8823" + CALLBACK_PATH)

    assert url.startswith(OAUTH_AUTHORIZE_URL)
    params = parse_qs(urlparse(url).query)
    assert params["oauth_token"] == ["reqtok"]
    # The permissions ride along so Zotero's grant screen is pre-filled.
    assert params["write_access"] == ["1"]

    # HMAC-SHA1 in the Authorization header, with the callback the browser used.
    auth_header = handler.seen["request_auth"]
    assert 'oauth_signature_method="HMAC-SHA1"' in auth_header
    assert "10.7.146.150" in auth_header


def test_finish_takes_the_key_from_the_access_response_and_verifies_it(tmp_path):
    handler = oauth_handler()
    auth = make_auth(tmp_path, handler)
    auth.start("http://localhost:8823" + CALLBACK_PATH)
    cred = auth.finish(oauth_token="reqtok", oauth_verifier="v123")

    assert cred.api_key == KEY
    # Verified against /keys/current rather than trusted from the OAuth body.
    assert handler.seen["verified_key"] == KEY
    assert cred.identity.username == "snunez"
    assert auth.status()["connected"] is True

    # The verifier is single-use: the pending token is gone afterwards.
    with pytest.raises(ZoteroAuthError, match="no longer pending"):
        auth.finish(oauth_token="reqtok", oauth_verifier="v123")


def test_the_key_is_read_from_the_secret_when_the_two_differ(tmp_path):
    """Zotero sends the key in both fields; the secret is the documented one."""
    handler = oauth_handler(access_fields={
        "oauth_token": OTHER_KEY,
        "oauth_token_secret": KEY,
        "userID": "475425",
    })
    auth = make_auth(tmp_path, handler)
    auth.start("http://localhost:8823" + CALLBACK_PATH)
    assert auth.finish(oauth_token="reqtok", oauth_verifier="v").api_key == KEY


def test_the_key_falls_back_to_the_token_when_there_is_no_secret(tmp_path):
    handler = oauth_handler(access_fields={"oauth_token": KEY, "userID": "475425"})
    auth = make_auth(tmp_path, handler)
    auth.start("http://localhost:8823" + CALLBACK_PATH)
    assert auth.finish(oauth_token="reqtok", oauth_verifier="v").api_key == KEY


def test_an_unknown_callback_token_is_refused(tmp_path):
    """A stale tab, a restarted server, or a forged callback -- same answer."""
    auth = make_auth(tmp_path, oauth_handler())
    with pytest.raises(ZoteroAuthError, match="no longer pending"):
        auth.finish(oauth_token="forged", oauth_verifier="v")
    assert auth.credential() is None


def test_a_callback_without_a_verifier_is_refused(tmp_path):
    auth = make_auth(tmp_path, oauth_handler())
    auth.start("http://localhost:8823" + CALLBACK_PATH)
    with pytest.raises(ZoteroAuthError, match="verifier"):
        auth.finish(oauth_token="reqtok", oauth_verifier="")


def test_a_request_token_zotero_will_not_grant_is_reported(tmp_path):
    def handler(request):
        return httpx.Response(401, text="Invalid consumer key")

    auth = make_auth(tmp_path, handler)
    with pytest.raises(ZoteroAuthError, match="Invalid consumer key"):
        auth.start("http://localhost:8823" + CALLBACK_PATH)


def test_an_access_response_without_a_key_is_refused(tmp_path):
    handler = oauth_handler(access_fields={"userID": "475425"})
    auth = make_auth(tmp_path, handler)
    auth.start("http://localhost:8823" + CALLBACK_PATH)
    with pytest.raises(ZoteroAuthError, match="no API key"):
        auth.finish(oauth_token="reqtok", oauth_verifier="v")


def test_pending_sign_ins_expire(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import paper_grabber.zotero_auth as za

    # A clock of our own rather than a patch on the real time module, which
    # httpx and the rest of the stack are also reading.
    clock = [1000.0]
    monkeypatch.setattr(za, "time", SimpleNamespace(time=lambda: clock[0]))

    auth = make_auth(tmp_path, oauth_handler(request_tokens=("first", "second")))
    auth.start("http://localhost:8823" + CALLBACK_PATH)

    clock[0] += 16 * 60
    # Starting another flow sweeps the abandoned one, so a long-lived server
    # does not accumulate request tokens from tabs that were closed.
    auth.start("http://localhost:8823" + CALLBACK_PATH)
    with pytest.raises(ZoteroAuthError, match="no longer pending"):
        auth.finish(oauth_token="first", oauth_verifier="v")
    # The fresh one is untouched.
    assert auth.finish(oauth_token="second", oauth_verifier="v").api_key == KEY


# --- the endpoints ------------------------------------------------------------


@pytest.fixture
def client(tmp_path):
    def make(handler=no_http, *, with_client=True):
        auth = make_auth(tmp_path, handler, client=with_client)
        app = create_app(tmp_path / "ledger.db", zotero=auth)
        return TestClient(app, follow_redirects=False), auth

    return make


def test_status_endpoint_before_anything_is_connected(client):
    c, _ = client()
    s = c.get("/api/zotero/status").json()
    assert s["connected"] is False
    assert s["has_client"] is True
    # Reported so the user can register it with their Zotero app. Unlike the
    # Google callback, a plain-http LAN address is perfectly acceptable here.
    assert s["callback_uri"].endswith(CALLBACK_PATH)
    assert "library_access=1" in s["new_key_url"]


def test_status_endpoint_is_declared_in_capabilities(client):
    c, _ = client()
    assert "zotero-auth" in c.get("/api/version").json()["capabilities"]


def test_connecting_with_a_pasted_key_over_http(client):
    c, auth = client(lambda r: httpx.Response(200, json=key_info()))
    r = c.post("/api/zotero/key", json={"api_key": KEY})
    assert r.status_code == 200
    assert r.json()["username"] == "snunez"
    assert auth.credential().api_key == KEY


def test_a_bad_pasted_key_comes_back_as_a_readable_400(client):
    c, _ = client(lambda r: httpx.Response(403, text="Invalid key"))
    r = c.post("/api/zotero/key", json={"api_key": "wrong"})
    assert r.status_code == 400
    assert "rejected" in r.json()["detail"]


def test_disconnect_endpoint(client):
    c, auth = client(lambda r: httpx.Response(200, json=key_info()))
    c.post("/api/zotero/key", json={"api_key": KEY})
    assert c.post("/api/zotero/disconnect").json()["disconnected"] is True
    assert auth.credential() is None


def test_start_endpoint_redirects_to_zotero(client):
    c, _ = client(oauth_handler())
    r = c.get("/auth/zotero/start")
    assert r.status_code == 307
    assert r.headers["location"].startswith(OAUTH_AUTHORIZE_URL)


def test_start_endpoint_without_a_client_is_a_503_not_a_crash(client):
    c, _ = client(no_http, with_client=False)
    r = c.get("/auth/zotero/start")
    assert r.status_code == 503
    assert "paste an API key" in r.json()["detail"]


def test_callback_lands_back_in_the_app(client):
    c, auth = client(oauth_handler())
    c.get("/auth/zotero/start")
    r = c.get(CALLBACK_PATH, params={"oauth_token": "reqtok", "oauth_verifier": "v"})
    # 303, so a reload does not replay a single-use verifier.
    assert r.status_code == 303
    assert r.headers["location"] == "/?zotero=1"
    assert auth.credential().api_key == KEY


def test_a_denied_callback_gets_a_page_rather_than_a_redirect(client):
    c, auth = client(oauth_handler())
    r = c.get(CALLBACK_PATH, params={"denied": "reqtok"})
    assert r.status_code == 200
    assert "did not authorise" in r.text
    assert auth.credential() is None


def test_a_failed_exchange_explains_itself_on_a_page(client):
    c, _ = client(oauth_handler())
    r = c.get(CALLBACK_PATH, params={"oauth_token": "unknown", "oauth_verifier": "v"})
    assert r.status_code == 200
    assert "no longer pending" in r.text


def test_callback_url_is_derived_from_the_origin():
    assert callback_url("http://10.7.146.150:8823/") == (
        "http://10.7.146.150:8823" + CALLBACK_PATH
    )


def test_a_redirected_oauth_endpoint_is_reported_not_followed(tmp_path):
    """A followed redirect would arrive unsigned, and as a GET."""
    def handler(request):
        return httpx.Response(302, headers={"location": "https://www.zotero.org/moved"})

    auth = make_auth(tmp_path, handler)
    with pytest.raises(ZoteroAuthError, match="redirected"):
        auth.start("http://localhost:8823" + CALLBACK_PATH)
