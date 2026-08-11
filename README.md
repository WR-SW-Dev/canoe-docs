# Canoe Document Automation

Pulls documents from **Canoe Intelligence** via its API and keeps a clean, foldered
archive in the team's **SharePoint** library. Designed to run unattended on an
always-on Mac (a Mac Mini), once a week, entirely inside the licensed Microsoft
environment — the documents never pass through a third-party cloud.

> **Data & compliance — read first.** The documents are LP account statements,
> K-1s, and portfolio detail: restricted financial data under Wake Robin's Gen AI /
> data policy. This tooling only **downloads and files** them — no AI reads their
> contents. Never commit documents or credentials to Git (the `.gitignore` enforces
> this). Any future step that sends document *contents* to an AI model requires IT
> Lead/CTO sign-off and must stay in-tenant (see [Roadmap](#roadmap)).

---

## How it runs

Once installed on an always-on Mac, a `launchd` job fires **every Monday at 7:00 AM**:

```
Canoe API  ──GET /v1/documents (ZIP, only docs uploaded since last run)──▶  run_weekly.sh
      │                                                                          │
      │                                                        canoe_bulk_download.py
      ▼                                                                          ▼
  new documents ───────────────▶  synced SharePoint "Canoe" library  ──OneDrive──▶  team
                                   (Manager / Year / Category)
      │                                                                          │
      └──▶ _download_activity.csv (per-file, in SharePoint)      run_history.csv (timestamps, in repo)
```

- Only **new** documents are fetched each week (tracked via a last-run marker), so runs are quick.
- Files are placed by fund, then year, then Canoe document category, and de-duplicated by content.
- Two logs are written: a per-file activity log next to the archive, and a git-safe run history (timestamps + counts, no file names). See [Logs](#logs).

No laptop needs to be awake — the Mac Mini handles it.

---

## How files reach SharePoint

The code has **no SharePoint credentials and makes no SharePoint API calls.** It
writes documents to a local folder (`CANOE_ARCHIVE_DIR`); the **OneDrive desktop
client** syncs that folder to SharePoint, authenticated by the account signed into
OneDrive on the device. SharePoint write access is therefore governed entirely by
that signed-in account — nothing in this repo.

**Recommended:** sign the device's OneDrive into a **dedicated, licensed M365 service
account** with write access to the `Canoe` library (rather than a person's account),
so the pipeline doesn't break when someone changes roles. The only credentials the
code itself holds are the Canoe API credentials in `.env` (entered via `setup.py`).

---

## Repository layout

```
canoe-docs/
├── install.sh                   # One-shot installer for a new machine
├── setup.py                     # Credential wizard: terminal prompts -> writes .env secrets
├── run_weekly.sh                # What the scheduler runs each Monday (self-locating)
├── co.wakerobin.canoe.weekly.plist  # launchd job (reference; install.sh generates a per-machine copy)
├── requirements.txt
├── run_history.csv              # Run audit trail: timestamps + counts, NO file names
├── py files/
│   ├── canoe_auth.py            # OAuth (client-credentials, with password-grant fallback)
│   ├── credentials_check.py     # Verify credentials — fetches a token, downloads nothing
│   ├── canoe_bulk_download.py   # Downloader: full + weekly-incremental, foldered, deduped, logged
│   ├── canoe_reclassify.py      # No-AI cleanup: resolve Undated/Unknown by reading text locally
│   ├── canoe_route.py           # Route Merrill/BofA + news out to dedicated folders
│   ├── statement_tracker.py     # Statement tracker: received/pending/overdue per fund per period
│   └── canoe_downloader.py      # DEPRECATED — early version; superseded by canoe_bulk_download.py
├── Canoe Docs/                  # Canoe API reference (text + OpenAPI spec)
├── Claude Output/               # Architecture & proposal write-ups (HTML)
└── README.md
```

The **document archive is not in this repo** — it lives in the SharePoint `Canoe`
library, synced locally. Credentials, the virtualenv, logs, and the per-file
activity log are all gitignored.

---

## Install on a new machine

### Prerequisites
- **macOS** with **Python 3.9+** and **git**.
- The **OneDrive client** on the device, **signed in to a dedicated service account**
  (recommended) with write access to the **Investment → `Canoe` SharePoint library**,
  and that library **synced locally** (right-click → *Always keep on this device*, so
  the archive is present for de-duplication rather than cloud-only). See
  [How files reach SharePoint](#how-files-reach-sharepoint).
- **Canoe API credentials** (client id/secret, and/or a service-account username/password).

### Steps

1. **Clone the repo:**
   ```bash
   git clone git@github.com:WR-SW-Dev/canoe-docs.git && cd canoe-docs
   ```

2. **Find the local path of the synced `Canoe` library** and set it as an env var.
   To locate it:
   ```bash
   ls ~/Library/CloudStorage/*/Investment*/ 2>/dev/null
   ```
   Then (adjust to what you see — the exact path can vary per machine):
   ```bash
   export CANOE_ARCHIVE_DIR="$HOME/Library/CloudStorage/OneDrive-SharedLibraries-WakeRobin/Investment - Documents/Canoe"
   ```

3. **Run the installer** (creates the virtualenv, installs dependencies, generates the weekly launchd job for this machine):
   ```bash
   ./install.sh
   ```

4. **Add Canoe credentials** using the guided wizard:
   ```bash
   python setup.py
   ```
   It prompts in the terminal (secret fields are hidden as you type) and writes them
   to `py files/.env` with owner-only permissions — nothing is transmitted. Provide
   the Client ID + Secret and/or the service-account username + password.

   *Alternatively*, hand-edit `py files/.env` (gitignored):
   ```bash
   CANOE_CLIENT_ID=...
   CANOE_CLIENT_SECRET=...
   CANOE_USERNAME=service_account_email        # optional fallback auth
   CANOE_PASSWORD=service_account_password     # optional fallback auth
   # CANOE_ORGANIZATION_ID=only_if_multiple_orgs
   ```

   > **Receiving the credentials:** have them sent to whoever deploys this over a
   > secure channel (encrypted email, or better, a password manager / one-time secret
   > link) and entered directly into the setup form. They should never be pasted into
   > chat or code, or committed to Git.

5. **Verify credentials:**
   ```bash
   cd "py files" && ../.venv/bin/python credentials_check.py
   ```
   Expect a token preview and `Credentials are valid`.

6. **Schedule the weekly job:**
   ```bash
   launchctl load -w ~/Library/LaunchAgents/co.wakerobin.canoe.weekly.plist
   ```
   Optional — run it once now and watch the log:
   ```bash
   launchctl start co.wakerobin.canoe.weekly && tail -20 logs/weekly.log
   ```

> **The archive is not re-downloaded.** Because the ~9,000-doc archive already lives
> in SharePoint (and syncs down via OneDrive), the tool only ever pulls *new*
> documents and de-duplicates against what's there. The first run simply catches up
> the last several days.

To disable later: `launchctl unload ~/Library/LaunchAgents/co.wakerobin.canoe.weekly.plist`.
The job only fires while the Mac is on/awake; if asleep at 7 AM it runs at next wake —
keep the Mini powered on.

---

## Manual usage

All commands run from `py files/` using the project venv.

```bash
# One-off incremental pull (same as the weekly job):
../.venv/bin/python canoe_bulk_download.py --dest "$CANOE_ARCHIVE_DIR" \
    --organize year-category --since auto --state "./.canoe_last_run.json"

# Full re-pull of everything (rarely needed — the archive already exists):
../.venv/bin/python canoe_bulk_download.py --dest "$CANOE_ARCHIVE_DIR" --organize year-category

# Resolve Undated/Unknown docs by reading text locally (no AI). Dry-run first:
../.venv/bin/python canoe_reclassify.py            # writes _reclassify_review.csv
../.venv/bin/python canoe_reclassify.py --apply    # applies high-confidence moves (+ undo log)

# Route Merrill/BofA custodian statements to a Merrill/ folder:
../.venv/bin/python canoe_route.py --rules merrill --apply
```

Placement is **content-aware** (keyed by each file's CRC): distinct files that share
a name get `__2`/`__3` suffixes so none is lost; identical duplicates are collapsed;
files already on disk are left in place. Every download tool is safe to re-run.

---

## Archive structure

```
Canoe/                                     # team SharePoint library (Investment site)
├── <Manager>/<Year>/<Canoe Category>/<file>.pdf
├── Merrill/<Year>/<Category>/...          # custodian statements (via canoe_route.py)
└── Unknown Investment/...                 # docs Canoe couldn't map to a fund
```
- **Year** comes from the document's data date (the period-end date in the filename), not the upload date. Undated docs sit under `Undated/`.
- **Category** is Canoe's own document category (Financial Statements & Performance, Tax, Legal/Compliance, Investor Administration & Communication, Capital Activity).

---

## Statement tracker

`statement_tracker.py` answers *"which managers have — and haven't — sent their
statement for each period?"* It is **Architecture A** from the proposal packet:
metadata-and-rules only. It reads Canoe's structured fields (fund, sponsor,
data date, document type, validation status) via `GET /v1/documents/data` and
**never opens a document body** — no Gen AI, nothing new to approve.

It pulls by **document type across all Canoe categories** (Account Statement,
Capital Account Statement, Monthly/Quarterly/Annual Report, Financials) — Canoe
files the same "Account Statement" type under Capital Activity, Investment
Reporting, *or* Financial Statements & Performance depending on the document,
so a category-scoped pull silently misses real statements.

In the grid, **green cells hyperlink to the statement file in the archive**
(relative links, so they work on any machine syncing the library); a legend at
the top of each sheet explains green / red / blank.

The weekly job runs it automatically after each Monday pull. The team-facing
grid is the only workbook at the top of `_statement_tracker/`; everything
supporting it lives in `backend/`, and prior runs in `Archive/`:

```
Canoe/_statement_tracker/
├── Statement Tracker <date>.xlsx   # THE grid: green = received (click "Link"),
│                                   # red = not; one sheet per cadence, one row
│                                   # per fund (+ entity sub-rows), one column
│                                   # per period. A NEW dated file each run.
├── Archive/                        # prior runs, kept for records
└── backend/
    ├── statement_schedule.xlsx     # EDITABLE config — the expected schedule
    │                               # (dropdowns for frequency/track; "How to use" tab)
    ├── Statement Tracker.html      # detail dashboard: action-needed list + 5-status grids
    ├── statement_status.csv        # flat fund x period status table
    ├── statement_received_log.csv  # every statement seen (data date, upload date, status)
    ├── Statement Digest.html       # statements that arrived since the last run
    └── statement_metadata_cache.json  # metadata cache (auto-managed)
```

**Why a new dated file each run:** rewriting one workbook in place wedges
OneDrive's Office-file sync whenever someone has it open in Excel — the update
then silently never reaches SharePoint. A fresh file is a fresh OneDrive item,
so the weekly refresh always lands; older runs are swept into `Archive/`.
(The "Link" cells in archived copies point one folder level off and won't
resolve — archives keep the color record; use the current file for links.)

**Email digest.** Each run builds `backend/Statement Digest.html` — the
statements that arrived since the previous run (each document is announced
exactly once). To have it emailed, run `python setup.py` (safe to re-run —
existing values are kept when you press Enter) and fill in the digest prompts,
or add the SMTP settings to `py files/.env` by hand:

```bash
CANOE_DIGEST_TO=ops@wakerobin.co,jbyrne@wakerobin.co
CANOE_SMTP_USER=service_account@wakerobin.co     # mailbox with SMTP AUTH enabled
CANOE_SMTP_PASS=...
# optional: CANOE_SMTP_HOST (default smtp.office365.com), CANOE_SMTP_PORT (587),
#           CANOE_DIGEST_FROM (defaults to CANOE_SMTP_USER)
```

Unconfigured, the digest is still written to the backend folder — the run log
notes that email is off. (M365: the mailbox needs *Authenticated SMTP* enabled
in the admin center.)

(Older layouts — the grid at the archive root, csv schedule, flat files — are
migrated automatically on the first run of the new version.)

**How a period is judged.** For each tracked fund, every monthly/quarterly/annual
period from its `start_date` gets one status:

| Status | Meaning |
|---|---|
| Received | A statement-type document with a data date in the period, uploaded by the due date |
| Received late | Same, but it arrived after the due date |
| Pending | Period has ended; still inside the grace window |
| **OVERDUE** | No statement and the grace window has passed |
| Review | Only flagged documents cover the period (Awaiting Confirmation / Anomaly / Potential Discrepancy) — Canoe's review flags never auto-confirm a period |

The due date is period end + `grace_days` (defaults: monthly 45, quarterly 90,
annual 180; December period-ends get +30 for audit-season lag).

**The schedule is the source of truth — edit it.** `backend/statement_schedule.xlsx`
is auto-seeded on first run (frequency inferred from 12 months of history, with
Canoe's own reporting-frequency field as a tie-breaker) and safe to edit in Excel:
change `frequency`, `grace_days`, set `track=no` for wind-downs, or list `doc_types`
overrides for funds whose "statement" arrives under a different label. Funds that
appear in Canoe later are appended automatically with a `NEW` note.

```bash
# Manual run (same as the weekly job):
../.venv/bin/python statement_tracker.py --dest "$CANOE_ARCHIVE_DIR"

# Force a full metadata re-pull (picks up re-categorized documents):
../.venv/bin/python statement_tracker.py --dest "$CANOE_ARCHIVE_DIR" --refresh full
```

By default only document types that actually evidence a statement satisfy a
period (Account Statement, Capital Account Statement, Monthly/Quarterly/Annual
Report, Financials); fact sheets, performance estimates and the like are logged
but never mark a period received.

---

## Logs

Two logs, deliberately split so **file names never reach Git**:

| Log | Location | Contents | In Git? |
|---|---|---|---|
| `run_history.csv` | repo root | One row per run — timestamp, mode, docs seen, new, duplicates, elapsed. **No file names.** | ✅ yes |
| `_download_activity.csv` | beside the archive (SharePoint) | One row per downloaded file — timestamp, path, manager, year, category. | ❌ never (gitignored) |
| `logs/weekly.log` | repo `logs/` (local) | Verbose per-run console output. | ❌ no (gitignored) |

Every run is recorded in `run_history.csv` — including weeks with zero new documents.

---

## Roadmap

The download & filing are solved and running. What deterministic (no-AI) tooling
**cannot** fully resolve — and which is the business case for an approved, in-tenant
document-intelligence (LLM/OCR) step — includes:

- **Image-only scans** (no text layer) — need OCR / vision AI.
- **Ambiguous / unmapped funds** in `Unknown Investment/` — need content-level ID.
  (Docs re-tagged by hand in Canoe can instead be refiled deterministically — a
  planned no-AI "sync re-tags" step.)
- **News vs. financial-statement** classification — unreliable by rules alone.
- **Data-date glitches** from Canoe — would be caught by reading the document's own stated period date.

Any such step must run on an **in-tenant model** (M365 Copilot / Azure OpenAI) with
IT Lead/CTO sign-off, per the Gen AI policy — never an external API on this data
without an approved, zero-retention agreement.
