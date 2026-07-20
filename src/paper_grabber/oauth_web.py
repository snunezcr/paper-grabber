"""Browser-based Google sign-in for the web app.

The desktop flow in google_auth.py opens a browser *on the machine running the
code*, which is useless when the app is being driven from a tablet. This module
runs the authorization-code flow through the web app itself: the page sends the
user to Google, Google redirects back to an endpoint here, and the refresh
token is stored exactly where the CLI expects to find it.

Single user by design. The token is one file, there are no sessions, and the
pending-state store is a dict in memory. Making this multi-tenant means
per-user tokens and a real session layer, which is a different project.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from .google_auth import DEFAULT_CREDENTIALS, DEFAULT_TOKEN, _save

# Mail over the Gmail API, plus Drive. gmail.readonly is a *restricted* scope:
# the app must be published (not "Testing") or its refresh tokens expire after
# seven days, silently breaking the scheduled run once a week.
WEB_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive.metadata.readonly",
]

CALLBACK_PATH = "/auth/google/callback"

# A sign-in that is never completed should not pin state forever.
_STATE_TTL_SECONDS = 15 * 60


class OAuthError(Exception):
    """The sign-in could not be started or completed."""


@dataclass
class PendingSignIn:
    state: str
    redirect_uri: str
    created_at: float


class WebOAuth:
    """Drives the authorization-code flow for a single user."""

    def __init__(
        self,
        *,
        credentials_path: Path = DEFAULT_CREDENTIALS,
        token_path: Path = DEFAULT_TOKEN,
        scopes: list[str] | None = None,
    ) -> None:
        self.credentials_path = Path(credentials_path)
        self.token_path = Path(token_path)
        self.scopes = scopes or WEB_SCOPES
        self._pending: dict[str, PendingSignIn] = {}

    # --- status ---------------------------------------------------------------

    def credentials(self) -> Credentials | None:
        """Stored credentials, or None if the user has not signed in."""
        if not self.token_path.exists():
            return None
        try:
            return Credentials.from_authorized_user_file(str(self.token_path), self.scopes)
        except ValueError:
            # A token written for a different scope set is not usable here;
            # treat it as absent so the UI offers sign-in rather than erroring.
            return None

    def status(self) -> dict[str, object]:
        creds = self.credentials()
        return {
            "signed_in": bool(creds),
            "valid": bool(creds and creds.valid),
            "refreshable": bool(creds and creds.refresh_token),
            "scopes": list(creds.scopes) if creds and creds.scopes else [],
            "has_client_secrets": self.credentials_path.exists(),
        }

    def sign_out(self) -> bool:
        """Forget the stored token. Does not revoke it at Google."""
        if self.token_path.exists():
            self.token_path.unlink()
            return True
        return False

    # --- the flow -------------------------------------------------------------

    def _expire_pending(self) -> None:
        """Drop sign-ins that were started and never completed.

        Without this an abandoned tab pins its state forever, and a long-lived
        server slowly accumulates them.
        """
        cutoff = time.time() - _STATE_TTL_SECONDS
        for state in [s for s, p in self._pending.items() if p.created_at < cutoff]:
            del self._pending[state]

    def _flow(self, redirect_uri: str) -> Flow:
        if not self.credentials_path.exists():
            raise OAuthError(
                f"{self.credentials_path} not found. In a Google Cloud project "
                'with the Gmail and Drive APIs enabled, create an OAuth client '
                'ID of type "Web application", add this app\'s callback URL as '
                "an authorised redirect URI, and download the JSON here."
            )
        try:
            return Flow.from_client_secrets_file(
                str(self.credentials_path), scopes=self.scopes, redirect_uri=redirect_uri
            )
        except ValueError as exc:
            raise OAuthError(f"{self.credentials_path} is not a valid client secrets file: {exc}") from exc

    def start(self, redirect_uri: str) -> str:
        """Return the Google URL to send the user to."""
        self._expire_pending()
        flow = self._flow(redirect_uri)
        auth_url, state = flow.authorization_url(
            # offline + consent is what actually yields a refresh token; without
            # it the scheduled run has no way to renew and dies in an hour.
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
        )
        self._pending[state] = PendingSignIn(
            state=state, redirect_uri=redirect_uri, created_at=time.time()
        )
        return auth_url

    def finish(self, *, state: str, full_url: str) -> Credentials:
        """Exchange the callback for tokens and store them."""
        pending = self._pending.pop(state, None)
        if pending is None:
            # An unknown state is either a stale tab or a forged callback.
            raise OAuthError("sign-in state not recognised; start again")

        flow = self._flow(pending.redirect_uri)
        try:
            flow.fetch_token(authorization_response=full_url)
        except Exception as exc:  # oauthlib raises a wide variety here
            raise OAuthError(f"Google rejected the sign-in: {exc}") from exc

        creds = flow.credentials
        if not creds.refresh_token:
            raise OAuthError(
                "Google returned no refresh token, so the scheduled run could "
                "not renew access. Revoke the app at "
                "https://myaccount.google.com/permissions and sign in again."
            )
        _save(creds, self.token_path)
        return creds


def callback_url(request_base: str) -> str:
    """Build the redirect URI from the URL the browser actually used.

    Derived rather than configured so the value always matches the origin the
    user is on -- localhost from the laptop, a Tailscale name from the tablet --
    which is exactly what Google compares against its registered list.
    """
    return request_base.rstrip("/") + CALLBACK_PATH


def is_valid_redirect(url: str) -> bool:
    """Whether Google will accept this as a redirect URI.

    Google permits http only for localhost/127.0.0.1; everything else must be
    https. A LAN address like http://10.7.146.150:8823 is rejected, which is
    the single most common reason in-page sign-in fails from a tablet.
    """
    if url.startswith("https://"):
        return True
    return url.startswith(("http://localhost", "http://127.0.0.1"))


def redirect_hint(url: str) -> str:
    """Explain why a redirect URI will not work, and what to do."""
    return (
        f"Google will reject the redirect URI {url!r}: it allows plain http only "
        "for localhost. Either open this app at http://localhost:8823 on the "
        "machine running it, or serve it over HTTPS -- `tailscale cert` gives a "
        "real certificate on a *.ts.net name that can be registered as a "
        "redirect URI and reached from the tablet."
    )
