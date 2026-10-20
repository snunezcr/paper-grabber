# Setup

Two credentials are needed: an **app password** for reading Scholar alert mail,
and an **OAuth client** for writing PDFs to Drive. Mail deliberately does not
use OAuth — see the note at the end.

## 1. Sign in with Google (recommended)

One browser sign-in covers both mail and Drive — no app password, no
`credentials.json` juggling on the command line.

1. <https://console.cloud.google.com> → **New Project**.
2. *APIs & Services → Library* → enable **Gmail API** and **Google Drive API**.
3. *OAuth consent screen* → **External**, add yourself as a test user.
4. **Set publishing status to "In production".** `gmail.readonly` is a
   *restricted* scope; while the app is in *Testing*, Google expires refresh
   tokens after 7 days and the scheduled run breaks every week. You will click
   through an "unverified app" warning once — expected for a personal app.
5. *Credentials → Create Credentials → OAuth client ID →
   **Web application***.
6. Under **Authorised redirect URIs**, add the callback for every origin you
   will open the app from:

   ```
   http://localhost:8823/auth/google/callback
   ```

   …and, if you want to sign in from the tablet, your HTTPS origin too (see
   below).
7. Download the JSON to `credentials.json` in the repo root (gitignored).

Then:

```bash
paper-grabber serve
```

Open **http://localhost:8823** on the machine running it and press
**Sign in with Google**.

### Signing in from the tablet needs HTTPS

Google accepts plain `http` **only** for `localhost` and `127.0.0.1`. The
tablet reaches the app at something like `http://10.7.146.150:8823`, which
Google rejects outright — the app detects this and says so rather than failing
at the consent screen.

Two options:

- **Sign in once from the laptop** at `http://localhost:8823`. The token is
  stored server-side, so the tablet then works without ever signing in itself.
  Simplest, and enough for a single user.
- **Serve over HTTPS with Tailscale**, which gives a real certificate on a
  `*.ts.net` name that can be registered as a redirect URI and reached from
  the tablet.

## 2. Mail over IMAP (alternative)

Still supported, and it needs no browser at all — useful if a browser on this
machine cannot reach Google. Requires 2-Step Verification.

1. Create an app password: <https://myaccount.google.com/apppasswords>
2. Export it:

   ```bash
   export PAPER_GRABBER_IMAP_USER=snunezcr@gmail.com
   export PAPER_GRABBER_IMAP_PASSWORD='xxxx xxxx xxxx xxxx'
   paper-grabber check-mail
   ```

`sync` prefers the Google sign-in when a token exists and falls back to IMAP
otherwise; `--force-imap` overrides.

## 3. Destination folder

Open the app, go to **Filing**, and press **Change base folder** to browse your
Drive and pick one. Papers are then filed into subfolders you choose from
there.

## 4. Connect a Zotero account (optional)

Open the app, go to **Filing**, and use the Zotero row under the base folder.
Either route ends at the same place: an API key, verified against Zotero and
stored at `~/.config/paper-grabber/zotero.json` (mode 0600).

### Paste a key — no setup

1. Press **Paste a key**, then follow **Create a key on zotero.org**. The link
   pre-ticks the permissions this app asks for — library read, write, and
   notes — so you only need to confirm and copy.
2. Paste it into the field and press **Connect**. The key is checked against
   Zotero before it is saved, so a truncated paste fails there and then.

Revoke a key any time at <https://www.zotero.org/settings/keys>. Disconnecting
in the app only forgets the key; it deliberately does not delete it at Zotero,
since you may have made it by hand or be using it elsewhere.

### Connect Zotero — one-time registration, then no typing

Nicer on a tablet, because nothing has to be copied between devices.

1. Register the app once at <https://www.zotero.org/oauth/apps>. For the
   callback, use the URL the app shows in the Zotero row, e.g.
   `http://localhost:8823/auth/zotero/callback`.
2. Save the client key and secret to `zotero_client.json` in the repo root
   (gitignored):

   ```json
   {"client_key": "...", "client_secret": "..."}
   ```

   Like `credentials.json`, this path is resolved against the working
   directory, so the service must run from inside the repo.
3. Restart the app. A **Connect Zotero** button appears next to **Paste a
   key**; pressing it takes you to Zotero and back.

**This one works from the tablet.** Zotero accepts a plain-`http` callback on
any host, so unlike the Google sign-in there is no certificate to arrange — the
LAN address the tablet already uses is a valid callback. Register that origin
too if you want to connect from there.

## 5. Running commands

```bash
paper-grabber sync          # pull new alerts
paper-grabber enrich-pending
paper-grabber fetch         # download accepted papers
paper-grabber upload        # send to Drive, delete local only once verified
paper-grabber serve         # triage UI
```

## 6. Running it

Everything is driven by hand. **Check now** in the app fetches new alerts and
enriches them; the Upload button on a filed card fetches the PDF and sends it
to Drive.

```bash
paper-grabber serve          # the app: triage, filing, check, upload
```

The equivalent commands, if you prefer a terminal:

```bash
paper-grabber sync           # pull new alerts into the ledger
paper-grabber enrich-pending # DOIs, abstracts, open-access locations
paper-grabber fetch          # download PDFs for accepted papers
paper-grabber upload         # send staged PDFs to their chosen folders
```

Credentials live in `~/.config/paper-grabber/env` (mode 0600). Load them with:

```bash
export UV_ENV_FILE=~/.config/paper-grabber/env   # then: uv run paper-grabber ...
# or
set -a; source ~/.config/paper-grabber/env; set +a
```

## Why mail uses IMAP and not OAuth

Reading mail via the Gmail API needs `gmail.readonly`, which Google classes as
a *restricted* scope. For an unverified personal app that means either living
with 7-day token expiry or going through verification. An app password
sidesteps all of it, and the IMAP session is opened **read-only** with
`BODY.PEEK`, so the service cannot mark, move, or delete anything in the
mailbox even by accident.

The tradeoff is a password in your environment rather than a scoped token. It
grants full mailbox access if leaked, so treat it accordingly — and revoke it
at <https://myaccount.google.com/apppasswords> if you ever suspect it has been.
