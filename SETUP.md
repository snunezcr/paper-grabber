# Setup

Two credentials are needed: an **app password** for reading Scholar alert mail,
and an **OAuth client** for writing PDFs to Drive. Mail deliberately does not
use OAuth — see the note at the end.

## 1. Mail: Gmail app password

Requires 2-Step Verification on the account.

1. Enable 2-Step Verification: <https://myaccount.google.com/signinoptions/two-step-verification>
2. Create an app password: <https://myaccount.google.com/apppasswords>
   Name it `paper-grabber`. Google shows a 16-character string once.
3. Put it in the environment:

   ```bash
   export PAPER_GRABBER_IMAP_USER=snunezcr@gmail.com
   export PAPER_GRABBER_IMAP_PASSWORD='xxxx xxxx xxxx xxxx'
   ```

   Spaces in the password are fine. Keep this out of shell history — put it in
   a file only you can read (`chmod 600`) and source it, or use a systemd
   `EnvironmentFile`.

4. Check it:

   ```bash
   paper-grabber check-mail
   ```

   Prints how many Scholar alerts are in the last 30 days.

IMAP no longer needs enabling in Gmail settings; it is always on.

## 2. Drive: OAuth client

Drive has no app-password equivalent, so this part needs OAuth.

1. <https://console.cloud.google.com> → **New Project** (`paper-grabber`).
2. *APIs & Services → Library* → enable **Google Drive API**.
   (The Gmail API is **not** needed — mail arrives over IMAP.)
3. *OAuth consent screen* → **External** → add yourself under **Test users**.
4. **Set publishing status to "In production".** While the app is in *Testing*,
   Google expires refresh tokens after 7 days, which would break the scheduled
   run every week. You will see an "unverified app" warning at consent; choose
   *Advanced → Go to paper-grabber*. That is expected for a personal app.
5. *Credentials → Create Credentials → OAuth client ID → **Desktop app***.
   Download the JSON to `credentials.json` in the repo root (gitignored).

The only scope requested is `drive.file`, which grants access **solely to files
this app creates**. It cannot read, list, or modify anything else in your Drive.

## 3. Destination folder

Because `drive.file` cannot look folders up by name, give the folder **ID**.
Open the destination folder in Drive and copy the part of the URL after
`/folders/`:

```
https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz
                                       ^^^^^^^^^^^^^^^^^^^^^^^^^^ this
```

## Running

```bash
# pull new alerts into the ledger
paper-grabber sync

# see what is waiting
paper-grabber pending

# triage
paper-grabber decide "Some Paper Title" accepted
paper-grabber decide "Another Paper" rejected

# fetch open-access PDFs into staging
paper-grabber download --dest ~/.local/share/paper-grabber/staging tests/data/*.eml

# upload to Drive; local copies are deleted only after Drive
# confirms a matching size and MD5
paper-grabber upload \
  --staging ~/.local/share/paper-grabber/staging \
  --folder 1AbCdEfGhIjKlMnOpQrStUvWxYz
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
