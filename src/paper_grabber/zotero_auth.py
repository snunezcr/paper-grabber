"""Zotero account sign-in for the web app.

Two ways in, both ending at the same place -- a verified API key stored
server-side with the user id it belongs to:

* **Connect Zotero** runs OAuth 1.0a through the app, the way the Google
  button does. Zotero mints the key itself and hands it back, so nothing is
  typed. Needs a client registered once at <https://www.zotero.org/oauth/apps>.
* **Paste a key** takes a key the user made by hand at
  <https://www.zotero.org/settings/keys/new>. No client registration, and it
  is the only route when the app has no OAuth client yet.

Zotero's OAuth is 1.0a and its access-token response *is* the API key: the
token and secret both carry it, and there is nothing to refresh later. That
makes the stored credential a plain string rather than Google's refresh-token
dance, and it means a key keeps working until the user revokes it at
<https://www.zotero.org/settings/keys>.

Unlike Google, Zotero accepts a plain-http callback on any host, so the
in-page flow works from the tablet over the LAN without a certificate -- the
one thing the Google sign-in cannot do.

Single user by design, like oauth_web: one credential file, no sessions, and
the pending-request-token store is a dict in memory.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode

import httpx
from oauthlib.oauth1 import SIGNATURE_HMAC_SHA1, Client as OAuth1Client

OAUTH_REQUEST_URL = "https://www.zotero.org/oauth/request"
OAUTH_AUTHORIZE_URL = "https://www.zotero.org/oauth/authorize"
OAUTH_ACCESS_URL = "https://www.zotero.org/oauth/access"

API_BASE = "https://api.zotero.org"
API_VERSION = "3"

NEW_KEY_URL = "https://www.zotero.org/settings/keys/new"
REVOKE_KEYS_URL = "https://www.zotero.org/settings/keys"
REGISTER_APP_URL = "https://www.zotero.org/oauth/apps"

CALLBACK_PATH = "/auth/zotero/callback"

DEFAULT_CLIENT = Path("zotero_client.json")
DEFAULT_CREDENTIAL = Path.home() / ".config" / "paper-grabber" / "zotero.json"

# What the app needs of a library: read it to know what is already there, write
# to add papers, and notes so a filing note can travel with the item. Group
# libraries are deliberately not requested -- nothing here files into one.
KEY_PERMISSIONS = {
    "name": "Research Stream",
    "library_access": "1",
    "notes_access": "1",
    "write_access": "1",
    "all_groups": "none",
}

# An abandoned tab should not pin its request token forever.
_PENDING_TTL_SECONDS = 15 * 60

_TIMEOUT = httpx.Timeout(20.0)


class ZoteroAuthError(Exception):
    """The connection could not be started, completed, or verified."""


@dataclass(frozen=True)
class ZoteroIdentity:
    """Who a key belongs to and what it may do, as Zotero reports it."""

    user_id: str
    username: str
    library: bool
    write: bool
    notes: bool

    @classmethod
    def from_key_info(cls, info: dict) -> ZoteroIdentity:
        user = (info.get("access") or {}).get("user") or {}
        user_id = info.get("userID")
        if user_id in (None, ""):
            raise ZoteroAuthError("Zotero did not say which account this key belongs to")
        return cls(
            user_id=str(user_id),
            username=str(info.get("username") or ""),
            library=bool(user.get("library")),
            write=bool(user.get("write")),
            notes=bool(user.get("notes")),
        )


@dataclass
class ZoteroCredential:
    """A verified key, plus who it belongs to at the time it was stored."""

    api_key: str
    identity: ZoteroIdentity
    linked_at: float

    def to_json(self) -> str:
        return json.dumps(
            {
                "api_key": self.api_key,
                "user_id": self.identity.user_id,
                "username": self.identity.username,
                "library_access": self.identity.library,
                "write_access": self.identity.write,
                "notes_access": self.identity.notes,
                "linked_at": self.linked_at,
            },
            indent=2,
        )

    @classmethod
    def from_dict(cls, data: dict) -> ZoteroCredential:
        key = data.get("api_key")
        if not key:
            raise ValueError("no api_key in stored Zotero credential")
        return cls(
            api_key=str(key),
            identity=ZoteroIdentity(
                user_id=str(data.get("user_id") or ""),
                username=str(data.get("username") or ""),
                library=bool(data.get("library_access")),
                write=bool(data.get("write_access")),
                notes=bool(data.get("notes_access")),
            ),
            linked_at=float(data.get("linked_at") or 0.0),
        )


@dataclass
class PendingConnect:
    """A request token waiting for the user to come back from Zotero."""

    token: str
    token_secret: str
    callback_uri: str
    created_at: float


@dataclass(frozen=True)
class ClientRegistration:
    """The consumer key and secret for this installation's Zotero app."""

    client_key: str
    client_secret: str


def load_client(path: Path = DEFAULT_CLIENT) -> ClientRegistration | None:
    """Read the registered OAuth client, or None if there isn't one.

    Absent is a normal state, not an error: the paste-a-key route needs no
    client at all, so the app must stay usable without this file.
    """
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ZoteroAuthError(f"{path} is not valid JSON: {exc}") from exc

    # Accept the shape Google's client_secrets uses too, so a file written by
    # habit under an "installed" or "web" key still works.
    for wrapper in ("zotero", "installed", "web"):
        if isinstance(data.get(wrapper), dict):
            data = data[wrapper]
            break

    key = data.get("client_key") or data.get("client_id") or data.get("key")
    secret = data.get("client_secret") or data.get("secret")
    if not key or not secret:
        raise ZoteroAuthError(
            f"{path} needs both client_key and client_secret; register the app "
            f"at {REGISTER_APP_URL}"
        )
    return ClientRegistration(client_key=str(key), client_secret=str(secret))


def new_key_url(permissions: dict[str, str] | None = None) -> str:
    """Zotero's new-key form, pre-filled with the permissions this app wants.

    The user still reviews and confirms; pre-filling only spares them guessing
    which boxes matter, which is the step most likely to produce a key that
    cannot write.
    """
    return f"{NEW_KEY_URL}?{urlencode(permissions or KEY_PERMISSIONS)}"


class ZoteroAuth:
    """Connects one Zotero account to this app and remembers the key."""

    def __init__(
        self,
        *,
        client_path: Path = DEFAULT_CLIENT,
        credential_path: Path = DEFAULT_CREDENTIAL,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.client_path = Path(client_path)
        self.credential_path = Path(credential_path)
        self._http = http_client
        self._pending: dict[str, PendingConnect] = {}

    # --- plumbing -------------------------------------------------------------

    def _request(self, method: str, url: str, **kw) -> httpx.Response:
        if self._http is not None:
            return self._http.request(method, url, **kw)
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as client:
            return client.request(method, url, **kw)

    def _client(self) -> ClientRegistration:
        reg = load_client(self.client_path)
        if reg is None:
            raise ZoteroAuthError(
                f"No Zotero OAuth client on this server. Register one at "
                f"{REGISTER_APP_URL} and save its key and secret to "
                f"{self.client_path}, or paste an API key instead."
            )
        return reg

    # --- status ---------------------------------------------------------------

    def credential(self) -> ZoteroCredential | None:
        """The stored credential, or None if the account is not connected."""
        if not self.credential_path.exists():
            return None
        try:
            return ZoteroCredential.from_dict(json.loads(self.credential_path.read_text()))
        except (OSError, ValueError):
            # A truncated or hand-edited file reads as "not connected" rather
            # than crashing the status endpoint the whole page depends on.
            return None

    def has_client(self) -> bool:
        try:
            return load_client(self.client_path) is not None
        except ZoteroAuthError:
            # Present but unusable. The in-page button would only fail later,
            # so report it as no client and let the paste route carry the user.
            return False

    def status(self) -> dict[str, object]:
        cred = self.credential()
        ident = cred.identity if cred else None
        return {
            "connected": bool(cred),
            "username": ident.username if ident else None,
            "user_id": ident.user_id if ident else None,
            "library_access": bool(ident and ident.library),
            "write_access": bool(ident and ident.write),
            "notes_access": bool(ident and ident.notes),
            # A key that cannot write is connected but useless for filing, and
            # the fix is a new key -- the same shape as Google's needs_reauth.
            "needs_reconnect": bool(cred) and not cred.identity.write,
            "has_client": self.has_client(),
            "linked_at": cred.linked_at if cred else None,
            "new_key_url": new_key_url(),
            "revoke_url": REVOKE_KEYS_URL,
            "register_app_url": REGISTER_APP_URL,
        }

    def disconnect(self) -> bool:
        """Forget the stored key. Does not revoke it at Zotero.

        Revoking is deliberately left to the user: this app did not necessarily
        create the key, and deleting someone's hand-made key behind their back
        would break any other tool sharing it.
        """
        if self.credential_path.exists():
            self.credential_path.unlink()
            return True
        return False

    # --- verifying and storing a key -----------------------------------------

    def key_info(self, api_key: str) -> ZoteroIdentity:
        """Ask Zotero who a key belongs to and what it may do.

        /keys/current takes the key in a header rather than the path, so the
        secret stays out of URLs, logs, and error messages.
        """
        key = api_key.strip()
        if not key:
            raise ZoteroAuthError("no API key given")
        try:
            response = self._request(
                "GET",
                f"{API_BASE}/keys/current",
                headers={
                    "Zotero-API-Key": key,
                    "Zotero-API-Version": API_VERSION,
                },
            )
        except httpx.HTTPError as exc:
            raise ZoteroAuthError(f"could not reach Zotero: {exc}") from exc

        if response.status_code in (401, 403):
            raise ZoteroAuthError(
                "Zotero rejected that key. Check it was copied whole, and that "
                "it has not been revoked."
            )
        if response.status_code >= 400:
            raise ZoteroAuthError(
                f"Zotero answered {response.status_code} when checking the key"
            )
        try:
            info = response.json()
        except ValueError as exc:
            raise ZoteroAuthError(f"Zotero's reply was not JSON: {exc}") from exc
        if not isinstance(info, dict):
            raise ZoteroAuthError("Zotero's reply was not a key record")
        return ZoteroIdentity.from_key_info(info)

    def connect_with_key(self, api_key: str) -> ZoteroCredential:
        """Store a hand-made key, after confirming Zotero accepts it.

        Verified before saving so a typo surfaces here, at the one moment the
        user is looking at the field, rather than as a failed upload later.
        """
        key = api_key.strip()
        identity = self.key_info(key)
        if not identity.library:
            raise ZoteroAuthError(
                "that key cannot read the library, so it is no use here -- "
                "create one with library access"
            )
        return self._store(key, identity)

    def _store(self, api_key: str, identity: ZoteroIdentity) -> ZoteroCredential:
        cred = ZoteroCredential(api_key=api_key, identity=identity, linked_at=time.time())
        self.credential_path.parent.mkdir(parents=True, exist_ok=True)
        self.credential_path.write_text(cred.to_json())
        # The key grants library access; keep it owner-only, like token.json.
        self.credential_path.chmod(0o600)
        return cred

    # --- the OAuth flow -------------------------------------------------------

    def _expire_pending(self) -> None:
        cutoff = time.time() - _PENDING_TTL_SECONDS
        for token in [t for t, p in self._pending.items() if p.created_at < cutoff]:
            del self._pending[token]

    def _signed(self, url: str, oauth_client: OAuth1Client) -> dict[str, str]:
        """POST a signed OAuth 1.0a request and parse the form-encoded reply."""
        uri, headers, body = oauth_client.sign(url, http_method="POST")
        try:
            response = self._request(
                "POST",
                uri,
                headers=headers,
                content=body or None,
                # A signature covers one exact URL and method, so a followed
                # redirect would arrive unsigned -- and as a GET. Better a
                # legible error here than a baffling signature rejection.
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise ZoteroAuthError(f"could not reach Zotero: {exc}") from exc
        if response.is_redirect:
            raise ZoteroAuthError(
                f"Zotero redirected {url} to "
                f"{response.headers.get('location', 'elsewhere')}; the endpoint "
                "has moved and this app needs updating"
            )
        if response.status_code >= 400:
            # Zotero's body here is short and says what was wrong (bad
            # signature, unknown consumer), so it is worth passing through.
            detail = response.text.strip()[:200] or f"HTTP {response.status_code}"
            raise ZoteroAuthError(f"Zotero refused the request: {detail}")
        return dict(parse_qsl(response.text))

    def start(self, callback_uri: str) -> str:
        """Get a request token and return the Zotero URL to send the user to."""
        self._expire_pending()
        reg = self._client()
        fields = self._signed(
            OAUTH_REQUEST_URL,
            OAuth1Client(
                reg.client_key,
                client_secret=reg.client_secret,
                callback_uri=callback_uri,
                signature_method=SIGNATURE_HMAC_SHA1,
            ),
        )
        token = fields.get("oauth_token")
        secret = fields.get("oauth_token_secret")
        if not token or not secret:
            raise ZoteroAuthError("Zotero did not return a request token")

        self._pending[token] = PendingConnect(
            token=token,
            token_secret=secret,
            callback_uri=callback_uri,
            created_at=time.time(),
        )
        params = {"oauth_token": token, **KEY_PERMISSIONS}
        return f"{OAUTH_AUTHORIZE_URL}?{urlencode(params)}"

    def finish(self, *, oauth_token: str, oauth_verifier: str) -> ZoteroCredential:
        """Exchange the callback for an API key, verify it, and store it."""
        pending = self._pending.pop(oauth_token, None)
        if pending is None:
            # Unknown token: a stale tab, a restarted server, or a forged
            # callback. All three want the same answer -- start again.
            raise ZoteroAuthError("this sign-in is no longer pending; start again")
        if not oauth_verifier:
            raise ZoteroAuthError("Zotero did not return a verifier")

        reg = self._client()
        fields = self._signed(
            OAUTH_ACCESS_URL,
            OAuth1Client(
                reg.client_key,
                client_secret=reg.client_secret,
                resource_owner_key=pending.token,
                resource_owner_secret=pending.token_secret,
                verifier=oauth_verifier,
                signature_method=SIGNATURE_HMAC_SHA1,
            ),
        )
        # Zotero puts the API key in the access token: the token and the secret
        # are the same string. Prefer the secret, which is what Zotero's own
        # example reads, and fall back for the sake of either changing.
        api_key = (fields.get("oauth_token_secret") or fields.get("oauth_token") or "").strip()
        if not api_key:
            raise ZoteroAuthError("Zotero returned no API key")

        # Asked rather than taken from the response: /keys/current is the
        # authoritative account of what the key may do, it proves the key works
        # before anything is stored, and it makes both routes into this module
        # end identically.
        return self._store(api_key, self.key_info(api_key))


def callback_url(request_base: str) -> str:
    """Build the callback from the URL the browser actually used.

    Derived rather than configured, for the same reason the Google flow does
    it: the value then matches whichever origin the user is on.
    """
    return request_base.rstrip("/") + CALLBACK_PATH
