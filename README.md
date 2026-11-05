# Folio — underwriting workspace

Folio keeps loan applications and their review documents together. A reviewer can create or edit an application, upload and categorize files, and update a human-led review status. Claude can create and update the same records through the included Model Context Protocol (MCP) endpoint.

**There is no AI in this app.** It makes no calls to Anthropic, OpenAI, or other AI providers, and needs no AI keys. Claude performs the writing and reasoning on the customer's own plan; Folio stores and serves the result.

## Run locally

Requires Python 3.10 or later; there are no third-party packages.

```powershell
python app.py
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). The first run creates a few fictional demo cases in `data\loan_desk.sqlite3`. Uploaded documents are stored in `data\documents`. Set `LOAN_DESK_DATA_DIR` to use another persistent data directory.

To connect a local Claude Code client, run:

```powershell
claude mcp add --transport http folio http://127.0.0.1:8000/mcp/YOUR_TOKEN
```

For Claude's hosted custom connector, deploy the app over HTTPS and add the URL shown on the Set up MCP page (`https://YOUR-HOST/mcp/<token>`) from Claude Settings → Connectors → Add a custom connector. The server is stateless and supports MCP Streamable HTTP JSON-RPC requests.

On a hosting platform, set `HOST=0.0.0.0` and use the platform-provided `PORT`. Keep `LOAN_DESK_DATA_DIR` on a persistent volume; the default local `data` directory may be ephemeral on hosted services.

## Architecture

- **Backend:** [app.py](./app.py) is the whole backend, built on Python's standard-library `http.server`. It serves the REST API (`/api/...`), the MCP endpoint (`/mcp`) and the static web UI.
- **Database:** SQLite, created automatically at `data/loan_desk.sqlite3` with three tables: `applications`, `documents` and `flags`.
- **File storage:** uploaded files are stored inside the database itself (a BLOB on the `documents` row), so one persistent volume holds everything. Files from older versions on disk stay downloadable.
- **Extraction:** CSV, DOCX and XLSX text is read automatically with the standard library. PDFs and images are read by the customer's Claude through MCP, which writes the result back as editable fields. The app itself contains no AI.
- **Flags:** a case or a single document can carry flags (info / warning / critical) raised by Claude or added by a reviewer, then resolved or deleted in the case drawer.
- **Frontend:** plain HTML, CSS and JavaScript in [static/](./static); no build step.
- **Dependencies:** none beyond Python 3.10+ (see [requirements.txt](./requirements.txt)).

The database lives in `LOAN_DESK_DATA_DIR` (default `./data`), so on a host it needs a persistent volume.

## MCP actions

- `create_loan_application` — create a saved case from applicant and loan details.
- `list_loan_applications` — search cases by name, location, case ID, or status.
- `get_loan_application` — read an application, its documents, extracted data and flags.
- `update_loan_application` — edit the details, auditor note, or manual review status.
- `register_document` — add a categorized checklist record.
- `upload_document` — store an actual file (base64, up to 8 MB) on a case.
- `read_document` — return a stored file (text, image or PDF) for Claude to read.
- `save_document_extraction` — save the fields and text Claude read; editable in the app.
- `flag_application` — raise a flag on a case or document.
- `resolve_flag` — resolve or reopen a flag.

## Login and access

- **Web app:** sign in with the username and password from the `APP_USERNAME` / `APP_PASSWORD` environment variables. Defaults for the demo are `admin` / `folio-demo` — **set your own before deploying**. Sessions use an HttpOnly cookie (12 hours); five failed attempts lock that IP for a minute.
- **MCP endpoint:** Claude can't use a browser login, so it connects with a private URL containing a secret token: `https://YOUR-HOST/mcp/<token>` (a `Authorization: Bearer <token>` header also works on `/mcp`). The token comes from `MCP_TOKEN`, or is generated on first run and stored in `data/mcp_token.txt`. After signing in, the **Set up MCP** page shows the full URL to paste into Claude.

## Scope and data safety

This is a product-assignment demo, not a production lending system. Demo records are fictional. Do not upload real borrower information: the starter app has no user authentication, encryption-at-rest configuration, access controls, or production hosting setup. It does not read or verify document contents, calculate creditworthiness, or make loan decisions. All review statuses are manually controlled.

Before using real customer information, add identity and access management, encrypted persistent storage and backups, audit logging, retention controls, secure deployment configuration, and any required compliance review.

## Verify

```powershell
python -m unittest discover -s tests
```
