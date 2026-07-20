"""Shared Google OAuth for Gmail and Drive.

One consent flow covers both APIs: the user authorises once, the refresh token
is cached, and later runs are non-interactive. This matters because the daily
job runs unattended -- a flow that needed a browser every morning would be
useless.

Scopes are deliberately narrow. The service reads Scholar alert mail and writes
PDFs; it never needs to send mail, modify the inbox, or read files it did not
create.
"""

from __future__ import annotations

from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

# Read-only on mail: the service parses alerts and must never be able to send,
# delete, or alter anything in the mailbox.
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Writes stay confined to drive.file: the app can only modify files it created
# itself, never anything else in the Drive. metadata.readonly is added purely so
# the destination browser can see the existing folder tree -- it grants no
# ability to read file *contents*, only names and hierarchy.
DRIVE_SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive.metadata.readonly",
]

# Mail now arrives over IMAP with an app password, so OAuth covers Drive alone.
DEFAULT_SCOPES = DRIVE_SCOPES

DEFAULT_CREDENTIALS = Path("credentials.json")
DEFAULT_TOKEN = Path.home() / ".config" / "paper-grabber" / "token.json"


class AuthError(Exception):
    """Authorisation could not be established without user action."""


def load_credentials(
    *,
    credentials_path: Path = DEFAULT_CREDENTIALS,
    token_path: Path = DEFAULT_TOKEN,
    scopes: list[str] | None = None,
    allow_interactive: bool = True,
) -> Credentials:
    """Return usable credentials, refreshing or prompting as needed.

    ``allow_interactive=False`` is for the unattended daily run: it will use or
    refresh an existing token but never try to open a browser, failing loudly
    instead so the problem surfaces as an error rather than a hung process.
    """
    scopes = scopes or DEFAULT_SCOPES
    creds: Credentials | None = None

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), scopes)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _save(creds, token_path)
        return creds

    if not allow_interactive:
        raise AuthError(
            f"no usable token at {token_path}; run an interactive command once "
            "to authorise"
        )

    if not credentials_path.exists():
        raise AuthError(
            f"{credentials_path} not found. In a Google Cloud project with the "
            'Drive API enabled, create an OAuth client ID of type "Desktop app" '
            "and download it to this path. The Gmail API is not needed -- mail "
            "arrives over IMAP."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), scopes)
    creds = flow.run_local_server(port=0)
    _save(creds, token_path)
    return creds


def _save(creds: Credentials, token_path: Path) -> None:
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json())
    # The token grants mailbox and Drive access; keep it owner-only.
    token_path.chmod(0o600)
