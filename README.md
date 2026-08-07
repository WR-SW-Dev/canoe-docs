# Canoe Document Automation

Local-first tooling to pull documents from **Canoe Intelligence** via its API and
keep a clean, foldered archive in OneDrive/SharePoint. Runs on an always-on Mac
(a Mac Mini), on a weekly schedule, with no cloud middleman — your fund documents
never leave the licensed Microsoft environment.

> **Data & compliance — read first.** The documents are LP account statements,
> K-1s, and portfolio detail — restricted financial data under Wake Robin's Gen AI
> / data policy. This tooling only **downloads and files** them (no AI reads their
> contents). Never commit documents or credentials to Git (the `.gitignore`
> enforces this). Any future step that sends document *contents* to an AI model
> requires IT Lead/CTO sign-off and must stay in-tenant (see *Roadmap*).

---

## Repository layout

```
Canoe API/
├── py files/
│   ├── canoe_auth.py            # OAuth (client-credentials, with password-grant fallback)
│   ├── credentials_check.py     # Verify credentials — fetches a token, downloads nothing
│   ├── canoe_bulk_download.py   # Main downloader: full + weekly-incremental, foldered, deduped
│   ├── canoe_reclassify.py      # No-AI cleanup: resolve Undated/Unknown by reading text locally
│   ├── canoe_route.py           # Route Merrill/BofA + news out to dedicated folders
│   ├── canoe_downloader.py      # DEPRECATED — early version; superseded by canoe_bulk_download.py
│   └── .env                     # Credentials (gitignored — never commit)
├── run_weekly.sh                # Wrapper the scheduler runs each Monday
├── co.wakerobin.canoe.weekly.plist  # launchd schedule (Mondays 7am)
├── requirements.txt
├── logs/                        # Run logs (gitignored)
└── README.md
```

The document archive itself lives **outside** this folder, at
`Canoe/Private Fund Reporting/` — so it is never inside the Git repo.

---

## Setup

```bash
cd "Canoe API"
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### Credentials
Store Canoe credentials in `py files/.env` (gitignored) — never in source:

```bash
CANOE_CLIENT_ID=...
CANOE_CLIENT_SECRET=...
# Fallback (password grant), used automatically if client-credentials is disabled for the tenant:
CANOE_USERNAME=service_account_email
CANOE_PASSWORD=service_account_password
# CANOE_ORGANIZATION_ID=only_if_multiple_orgs
```
More secure alternative: macOS Keychain (`security add-generic-password -s canoe-api -a client_id -w '<value>'`). Resolution order is env var → Keychain → `.env`.

### Verify it works
```bash
cd "py files" && ../.venv/bin/python credentials_check.py
```
Expect a token preview and `Credentials are valid`. (A `404` means the tenant hasn't enabled client-credentials — the password-grant fallback covers it.)

---

## Scripts

### `canoe_bulk_download.py` — download & file documents
Canoe's `GET /v1/documents` returns a **ZIP bundle** of files (paged, already
foldered by manager). This tool pages through them, extracts, and sub-organizes.

```bash
# Full pull of everything, organized Manager/Year/Category:
../.venv/bin/python canoe_bulk_download.py \
    --dest "/Users/<you>/Library/CloudStorage/OneDrive-WakeRobin/Canoe/Private Fund Reporting" \
    --organize year-category

# Weekly incremental — only documents uploaded since the last run:
../.venv/bin/python canoe_bulk_download.py --dest "<archive>" --organize year-category \
    --since auto --state "./.canoe_last_run.json"
```
- `--organize`: `none | category | type | year | category-year | year-category`
- Placement is **content-aware** (keyed by each file's CRC): distinct files that
  share a name get `__2`/`__3` suffixes so none is lost; identical duplicates are
  collapsed; files already on disk are left in place. Safe to re-run.
- `--since auto` reads/writes the state file so scheduled runs only fetch new docs.

### `canoe_reclassify.py` — resolve Undated / Unknown (no AI)
Reads each PDF's text **locally** to recover a year (for `Undated` files) or the
fund (for `Unknown Investment` files), then refiles the high-confidence ones and
lists the rest for review. Emits metadata only — no document text leaves the machine.

```bash
../.venv/bin/python canoe_reclassify.py            # dry run -> writes _reclassify_review.csv
../.venv/bin/python canoe_reclassify.py --apply    # apply high-confidence moves (+ undo log)
../.venv/bin/python canoe_reclassify.py --undo _reclassify_moves_<stamp>.json
```

### `canoe_route.py` — route non-fund docs
Moves Merrill/BofA custodian statements to `Merrill/` (and, optionally, news to
`News Articles/`). Dry-run by default; `--apply` writes an undo log.

```bash
../.venv/bin/python canoe_route.py --rules merrill            # dry run
../.venv/bin/python canoe_route.py --rules merrill --apply
```

---

## Archive structure

```
Private Fund Reporting/
├── <Manager>/<Year>/<Canoe Category>/<file>.pdf
├── Merrill/<Year>/<Category>/...          # custodian statements (via canoe_route.py)
└── Unknown Investment/...                 # docs Canoe couldn't map to a fund
```
- **Year** comes from the document's data date (the period-end date in the filename), not the upload date. Files with no detectable date sit under `Undated/`.
- **Category** is Canoe's own document category (Financial Statements & Performance, Tax, Legal/Compliance, Investor Administration & Communication, Capital Activity).

---

## Weekly automation (always-on Mac)

The pull runs every **Monday at 7:00am** via macOS `launchd`, so no one has to
remember — and because it's on an always-on Mac Mini, no laptop needs to be awake.

**Deploy on the Mac Mini:**
1. Get the code there: `git clone <repo>` (or copy `Canoe API/`), then `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`.
2. Make sure **OneDrive is installed and syncing** the `Private Fund Reporting` folder on the Mini.
3. Put the credentials in `py files/.env` (or Keychain) on the Mini.
4. If the Mini's username/paths differ from this machine, update the absolute paths in `run_weekly.sh` and the `.plist`.
5. Activate:
   ```bash
   cp co.wakerobin.canoe.weekly.plist ~/Library/LaunchAgents/
   launchctl load -w ~/Library/LaunchAgents/co.wakerobin.canoe.weekly.plist
   ```

**Operate:**
```bash
launchctl start co.wakerobin.canoe.weekly    # run now (test)
tail -25 logs/weekly.log                      # see what happened
launchctl unload ~/Library/LaunchAgents/co.wakerobin.canoe.weekly.plist   # disable
```
launchd fires the job only while the Mac is on/awake; if it was asleep at 7am it
runs at next wake. Keep the Mini powered on.

---

## Roadmap / known limits

The download & filing are solved. What deterministic (no-AI) tooling **cannot**
fully resolve — and which is the business case for an approved, in-tenant
document-intelligence (LLM) step — includes:

- **Image-only scans** (no text layer) — need OCR / vision AI.
- **Ambiguous / unmapped funds** in `Unknown Investment/` — need content-level ID.
- **News vs. financial-statement** classification — unreliable by rules alone.
- **Data-date glitches** from Canoe (e.g., a stray `2082/` folder) — would be caught
  by reading the document's own stated period date.

Any such step must run on an **in-tenant model** (M365 Copilot / Azure OpenAI) and
be signed off by the IT Lead/CTO, per the Gen AI policy — never an external API on
this data without an approved, zero-retention agreement.
