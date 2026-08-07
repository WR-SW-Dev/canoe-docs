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

## Repository layout

```
canoe-docs/
├── install.sh                   # One-shot installer for a new machine
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
- The **OneDrive client** signed in to Wake Robin, with the **Investment → `Canoe`
  SharePoint library synced locally**. Right-click the folder → *Always keep on this
  device* (so the archive is present for de-duplication rather than cloud-only).
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

4. **Add Canoe credentials** to `py files/.env` (gitignored). Most secure alternative: macOS Keychain.
   ```bash
   CANOE_CLIENT_ID=...
   CANOE_CLIENT_SECRET=...
   # Fallback (used automatically if client-credentials is disabled for the tenant):
   CANOE_USERNAME=service_account_email
   CANOE_PASSWORD=service_account_password
   # CANOE_ORGANIZATION_ID=only_if_multiple_orgs
   ```

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
