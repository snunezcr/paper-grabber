# Vendored third-party assets

## PDF.js 4.6.82 — Apache 2.0 (see LICENSE-pdfjs)

`pdf.mjs` and `pdf.worker.mjs`, taken unmodified from the official
`pdfjs-4.6.82-dist.zip` release.

Vendored rather than loaded from a CDN because the app must stay
self-contained: it runs on a tailnet with no guarantee of internet access from
the browser, and a test asserts the page references no external hosts.

Source maps and `pdf.sandbox.mjs` (PDF form JavaScript) are deliberately
omitted -- the reader neither debugs nor executes embedded scripts.

To update: download the release zip, copy those two files, bump this note.
