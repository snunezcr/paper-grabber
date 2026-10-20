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
import os
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

# One user, one sign-in at a time. The slack is for retries and a second tab.
_MAX_PENDING = 8

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


def missing_access(identity: ZoteroIdentity) -> list[str]:
    """Permissions this app asks for that a key does not carry.

    Checked as a set rather than write alone: notes are requested so a filing
    note can travel with the item, and a key without them would connect
    cleanly and then fail at the first note.
    """
    return [
        name
        for name, held in (
            ("library", identity.library),
            ("write", identity.write),
            ("notes", identity.notes),
        )
        if not held
    ]


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
            # A key missing any permission this app asks for is connected but
            # cannot do the job, and the fix is a new key -- the same shape as
            # Google's needs_reauth, and measured the same way: against the
            # whole required set, not one favoured member of it.
            "needs_reconnect": bool(cred) and bool(missing_access(cred.identity)),
            "missing_access": missing_access(ident) if ident else [],
            "has_client": self.has_client(),
            "new_key_url": new_key_url(),
            "revoke_url": REVOKE_KEYS_URL,
        }

    def disconnect(self) -> bool:
        """Forget the stored key. Does not revoke it at Zotero.

        Revoking is deliberately left to the user: this app did not necessarily
        create the key, and deleting someone's hand-made key behind their back
        would break any other tool sharing it.
        """
        try:
            self.credential_path.unlink()
        except FileNotFoundError:
            # Already gone, whether it never existed or a second disconnect
            # beat us to it. Not an error either way.
            return False
        return True

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
                # httpx strips Authorization across a redirect but not a custom
                # header, so following one would hand the live key to whatever
                # host it pointed at. A redirect here is a bug to surface, not
                # to chase.
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise ZoteroAuthError(f"could not reach Zotero: {exc}") from exc

        if response.is_redirect:
            raise ZoteroAuthError(
                "Zotero redirected the key check; the endpoint has moved and "
                "this app needs updating"
            )
        if response.status_code in (401, 403):
            raise ZoteroAuthError(
                "Zotero rejected that key. Check it was copied whole, and that "
                "it has not been revoked."
            )
        if response.status_code in (429, 503):
            # Reported apart from the rest because the generic wording -- and
            # certainly "rejected that key" -- would blame the key, which is
            # the one diagnosis that is definitely wrong here.
            wait = response.headers.get("Retry-After") or response.headers.get("Backoff")
            when = f" Try again in {wait} seconds." if wait else " Try again shortly."
            raise ZoteroAuthError(f"Zotero is busy and would not answer.{when}")
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
        self.credential_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Created owner-only rather than written and then chmod'ed: under the
        # usual umask the latter leaves the key world-readable for the length
        # of the write.
        fd = os.open(self.credential_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(cred.to_json())
        return cred

    # --- the OAuth flow -------------------------------------------------------

    def _expire_pending(self) -> None:
        cutoff = time.time() - _PENDING_TTL_SECONDS
        for token in [t for t, p in self._pending.items() if p.created_at < cutoff]:
            del self._pending[token]

    def _trim_pending(self) -> None:
        """Hold the pending store to a ceiling, oldest evicted first.

        /auth/zotero/start needs no authentication, so without this anyone who
        can reach the port can grow the dict for as long as the TTL lasts. Run
        after the insert, so the ceiling is the real number held.
        """
        while len(self._pending) > _MAX_PENDING:
            del self._pending[min(self._pending, key=lambda t: self._pending[t].created_at)]

    def _signed(self, url: str, oauth_client: OAuth1Client) -> dict[str, str]:
        """POST a signed OAuth 1.0a request and parse the form-encoded reply.

        oauthlib's default puts the parameters in an Authorization header,
        which RFC 5849 requires providers to accept. Zotero's own example
        instead signs them into the form body, and no test here has reached
        the live endpoint: if /oauth/request answers with a signature error,
        pass signature_type=SIGNATURE_TYPE_BODY with a form Content-Type
        before looking anywhere else.
        """
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
        # The one signal that Zotero took the callback we asked for rather than
        # substituting the one registered with the client. Only checked when
        # present: its absence cannot be distinguished from a provider that
        # simply does not send it, and failing on that would be a guess.
        confirmed = fields.get("oauth_callback_confirmed")
        if confirmed is not None and confirmed.lower() != "true":
            raise ZoteroAuthError(
                "Zotero would not accept this app's callback address. Check the "
                f"callback registered at {REGISTER_APP_URL} matches {callback_uri}"
            )

        self._pending[token] = PendingConnect(
            token=token,
            token_secret=secret,
            callback_uri=callback_uri,
            created_at=time.time(),
        )
        self._trim_pending()
        params = {"oauth_token": token, **KEY_PERMISSIONS}
        return f"{OAUTH_AUTHORIZE_URL}?{urlencode(params)}"

    def finish(
        self,
        *,
        oauth_token: str,
        oauth_verifier: str,
        callback_uri: str | None = None,
    ) -> ZoteroCredential:
        """Exchange the callback for an API key, verify it, and store it.

        The pending entry is consumed only once a key is in hand. Popping it up
        front would mean a Zotero hiccup mid-exchange forced the user to
        approve a second key, orphaning the first -- and would let anyone who
        learned the request token (it rides in the URL bar on zotero.org, so it
        reaches history and any Referer) burn a legitimate sign-in by replaying
        the callback.
        """
        # Checked before anything is consumed, so a replay without the verifier
        # cannot destroy a sign-in that is still good.
        if not oauth_verifier:
            raise ZoteroAuthError("Zotero did not return a verifier")

        pending = self._pending.get(oauth_token)
        expired = pending is not None and (
            pending.created_at < time.time() - _PENDING_TTL_SECONDS
        )
        if pending is None or expired:
            # Unknown or stale: a closed tab, a restarted server, or a forged
            # callback. All of them want the same answer -- start again. The
            # TTL is enforced here and not only swept in start(), or a pending
            # entry from days ago would still complete.
            self._pending.pop(oauth_token, None)
            raise ZoteroAuthError("this sign-in is no longer pending; start again")

        if callback_uri is not None and callback_uri != pending.callback_uri:
            # The callback landed on a different origin than the one the
            # request token was signed for. Terminal: the flow is no longer the
            # one that was started.
            del self._pending[oauth_token]
            raise ZoteroAuthError(
                "this sign-in came back to a different address than it left "
                "from; start again from the app"
            )

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
        identity = self.key_info(api_key)
        # The handshake is complete only now, so this is where the request
        # token is spent. Anything that raised above left it intact and
        # retryable.
        self._pending.pop(oauth_token, None)
        return self._store(api_key, identity)


def callback_url(request_base: str) -> str:
    """Build the callback from the URL the browser actually used.

    Derived rather than configured, for the same reason the Google flow does
    it: the value then matches whichever origin the user is on.
    """
    return request_base.rstrip("/") + CALLBACK_PATH
