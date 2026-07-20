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

## 4. Running commands

```bash
paper-grabber sync          # pull new alerts
paper-grabber enrich-pending
paper-grabber fetch         # download accepted papers
paper-grabber upload        # send to Drive, delete local only once verified
paper-grabber serve         # triage UI
```

## 5. Running it daily

The unattended half of the pipeline — sync, enrich, fetch, upload — runs on a
systemd **user** timer. Triage and filing are not in it: those are decisions,
and they happen in the web app whenever you get to them.

Put the credentials in an environment file (the unit deliberately does not
carry them, because unit files are world-readable and `systemctl cat` would
print the password):

```bash
mkdir -p ~/.config/paper-grabber
install -m 600 /dev/null ~/.config/paper-grabber/env
cat > ~/.config/paper-grabber/env <<'EOF'
PAPER_GRABBER_IMAP_USER=snunezcr@gmail.com
PAPER_GRABBER_IMAP_PASSWORD=xxxx xxxx xxxx xxxx
OPENALEX_MAILTO=snunezcr@gmail.com
EOF
```

Install and start the timer:

```bash
cp systemd/research-stream-sync.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now research-stream-sync.timer
```

Check it:

```bash
systemctl --user list-timers research-stream-sync.timer
systemctl --user start research-stream-sync.service   # run it now
journalctl --user -u research-stream-sync.service -n 50
```

`Persistent=true` matters on a laptop: if the machine is asleep at 06:00 the
job runs on the next wake rather than silently skipping the day. Each step is
allowed to fail without killing the run, so a morning where OpenAlex is out of
budget still syncs mail.

To keep the triage UI available, run it as its own long-lived service or just
start it by hand when you want it:

```bash
paper-grabber serve
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
