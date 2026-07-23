# Research Stream

Triage Google Scholar alerts on a tablet, and file the papers you want into
Google Drive with a consistent name.

Scholar alerts arrive as email: a title, a byline, and two lines of snippet.
That is not enough to decide whether a paper is worth reading, and after a week
there are two hundred of them. Research Stream turns that mail into a
reviewable queue — with real abstracts, DOIs, and open-access links — then
downloads what you accept and files it where you choose.

```
Gmail/IMAP → parse → dedupe → enrich → [ you decide ] → fetch → Drive
```

Everything runs on your own machine. Nothing is sent anywhere except the
metadata APIs it queries and the Drive folder you pick.

## What it does

- **Reads Scholar alert mail** over the Gmail API or IMAP, and remembers which
  messages it has already handled.
- **Deduplicates** across alerts — the same paper commonly arrives from three
  different saved searches.
- **Enriches** each paper with a DOI, abstract, and open-access PDF location
  from OpenAlex, falling back to Crossref and Unpaywall when OpenAlex's daily
  budget runs out.
- **Recovers missing abstracts** from arXiv, Semantic Scholar, and publisher
  page metadata. On a real queue this took abstract coverage from 13 of 67 to
  46 of 67.
- **Triage UI** for phone or tablet: title, authors, abstract, and links, with
  keep and drop (swipe, keyboard, or buttons).
- **Reading list** that tracks what you've kept — unread, reading, read — with a
  pinned queue and a mark-read control in the reader; reading is a separate axis
  from filing, so a paper can be in Drive but unread, or read but not yet filed.
- **Filing** into any Drive folder, with a suggested destination based on your
  folder names and past choices.
- **Downloads** open-access PDFs, validating them by content rather than by
  `Content-Type`, and names them `YYYY Title.pdf`.
- **Uploads to Drive** and deletes the local copy only after Drive confirms a
  matching size and MD5.
- **Notes** written while filing become the file's description in Drive.

## Requirements

- Python 3.12+
- A Google account with Drive
- Optional but recommended: a free [OpenAlex](https://openalex.org) API key

## Install

```bash
git clone https://github.com/snunezcr/paper-grabber
cd paper-grabber
uv venv && uv pip install -e ".[dev]"
```

## Setup

Full instructions are in [SETUP.md](SETUP.md). In short:

1. Create a Google Cloud project, enable the **Gmail** and **Drive** APIs, and
   download an OAuth client of type *Web application*.
2. Start the app and press **Sign in with Google**.
3. In **Filing**, choose the base Drive folder your papers live under.

Mail can also be read over IMAP with an app password, which needs no Google
Cloud project at all — see SETUP.md.

## Use

```bash
paper-grabber serve          # the app: triage, filing, upload
```

Then open `http://localhost:8823`, or your machine's LAN address from a
tablet. **Check now** pulls new alerts; everything else happens in the UI.

The same steps are available from a terminal:

```bash
paper-grabber sync                # pull new alerts into the ledger
paper-grabber enrich-pending      # DOIs, abstracts, open-access locations
paper-grabber backfill-abstracts  # recover abstracts that enrichment missed
paper-grabber pending             # what is waiting
paper-grabber fetch               # download PDFs for accepted papers
paper-grabber upload              # send them to their chosen folders
```

Run `paper-grabber --help` for the rest.

There is no scheduler. The pipeline runs when you ask it to.

## How it works

| Module | Responsibility |
|---|---|
| `parse.py` | Scholar alert HTML → structured records |
| `clean.py` | Repairs LaTeX escapes, stray quotes, truncated venues |
| `imap_source.py`, `gmail.py` | Two ways to read the same mail |
| `ledger.py` | SQLite: every paper seen, its decision, its progress |
| `enrich.py`, `providers.py`, `chain.py` | OpenAlex, Crossref, Unpaywall |
| `abstracts.py` | arXiv, Semantic Scholar, publisher metadata |
| `fetch.py` | Downloads, validated by magic bytes |
| `staging.py` | Local files, deleted only against proof of upload |
| `drive.py` | Folder browsing and verified upload |
| `server.py`, `static/` | The web app |

Some deliberate choices, each of which cost a bug to learn:

- **PDFs are identified by their leading bytes, not their `Content-Type`.**
  Publishers answer PDF requests with `200 OK`, `application/pdf`, and an HTML
  cookie wall.
- **A landing page is not a PDF location.** `open_access.oa_url` is frequently
  a DOI resolver; fetching it stores HTML as though it were the paper.
- **Title matching is symmetric and year-checked.** An overlap coefficient
  scored a *different* paper at a perfect 1.0 and attached its DOI.
- **The local copy is deleted only after Drive returns a matching MD5.** Size
  alone passes for a truncated upload.
- **Rejections are permanent and recorded**, so a paper rejected once is not
  offered again when another alert surfaces it.

## Limitations

This is a personal tool that happens to be readable. Before using it for
anything that matters:

- **Single user.** One ledger, one token, no accounts. The web API has **no
  authentication** — anyone who can reach the port can triage and file. Bind to
  `127.0.0.1` or keep it on a private network.
- **Single-machine.** Staging is local disk and job state is in-process, so it
  runs as one server, not a horizontally-scaled service. For an always-on
  personal deployment behind Tailscale, see [deploy/DEPLOY.md](deploy/DEPLOY.md).
- **OpenAlex is metered** at $0.001/request against a $0.10 daily allowance.
  The free fallbacks cover the gap, with poorer abstract coverage.
- **ResearchGate blocks automated requests**, so those papers get neither a PDF
  nor an abstract.
- **No institutional proxy.** Papers with no open-access copy are recorded and
  linked, not downloaded.
- **Only keyword and author alerts have been tested.** Citation alerts may
  parse differently.
- **Scholar's alert markup is undocumented** and may change without notice. The
  parser is pinned by tests against real messages.

## Development

```bash
pytest                       # 758 tests, no network required
./scripts/demo.sh            # drive the pipeline against bundled fixtures
```

The suite mocks every network call. Three real Scholar alert emails are
included as fixtures, and the parser is tested against them rather than against
invented markup.

## Licence

Not yet chosen. Until a licence is added, no permission is granted to use,
copy, modify, or distribute this code.
