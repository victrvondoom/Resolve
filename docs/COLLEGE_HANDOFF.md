# College branch handoff — `college/cloud-sprint`

Base: `master` at `553e9c9`. This branch adds a working local Document studio; it has not been merged with any unpublished home branch.

## Changes

- `src/document_studio.py`: tenant and workspace scoped PDF store with SHA-256 version checks, upload, rotate, remove pages, merge, report generation, read and purge. PDF bytes and SQLite metadata are in `data/documents/` by default, or `RESOLVE_DOCUMENTS_DIR` for an isolated test environment.
- `src/resolve_app.py`: `/api/documents`, `/api/documents/upload`, `/api/documents/transform`, `/api/documents/report`, and authenticated PDF download. Upload limit: 5 MiB. Existing text-only voice endpoint now labels its source honestly and does not invent confidence.
- `src/privacy_engine.py`: includes document sizes and metadata in summary/export and purges PDF bytes on workspace/account deletion. Export lists metadata; download the PDF bytes separately before deletion.
- `web/index.html`, `web/static/app.js`, `web/static/app.css`: Document studio, warm cream styling, real PDF preview, download, and browser print dialog.
- `requirements-dev.txt`: `pypdf`, `reportlab`. `.gitignore` excludes generated PDFs and their database.

## API contract

`POST /api/documents/upload` accepts `{name, content_base64}`. `POST /api/documents/transform` accepts `{operation, document_ids, expected_sha256, degrees?, pages?}` with one-based page numbers. Responses contain `{document}` with `id`, `sha256`, `size_bytes`, and metadata. `GET /api/documents/{id}/download` returns PDF bytes. `POST /api/documents/report` accepts `{title, text}`. Private routes require the existing `Authorization: Bearer` or `X-Resolve-Token` header.

## Verification and limits

Run `python -m unittest tests.test_document_studio tests.test_document_api tests.test_cloud_api -v` and `node --check web/static/app.js`. An actual physical print result remains unverified. The current server binds to loopback, has a shared local demo token, and lacks deployed remote sandbox isolation, production OAuth, and real audio transcription. Do not present it as a public multi-user SaaS.

Focused checks: 6 passed (PDF engine, HTTP workflow, prior cloud API). The repository-wide `pytest` command in this workspace reported 47 failed, 133 passed, 10 skipped and 11 errors; sync Playwright tests encountered an active asyncio event loop from the installed pytest environment. Chromium installation from the Playwright CDN failed here, so browser visual checks remain unverified. Re-run in a clean virtual environment with a working Chromium install before integration.

## Integration with unpublished home work

Push the home work to its own branch first. Compare changes to `src/resolve_app.py`, `src/privacy_engine.py`, `web/index.html`, and `web/static/app.js` before merging. Resolve overlapping routes and UI sections deliberately, then run the full suite and a browser walk-through.
