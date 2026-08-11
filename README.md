# Canoe → SharePoint Sync

A scheduled service that pulls documents from **Canoe Intelligence** and uploads them
directly into a **SharePoint** document library through the **Microsoft Graph API**.
It runs unattended on an always-on Mac (the Mac Studio App Server), once a week.

There is **no OneDrive desktop-sync** involved: the application authenticates to
Microsoft Graph as an **application** (not a user) using a **certificate**, and writes
files straight into the library. The only account context is the Entra ID app
registration, which is scoped so it can write only to the one Canoe SharePoint site.

> **Data note.** The documents are LP account statements, K-1s, and portfolio detail.
> This application only moves them from Canoe into SharePoint. No document content is
> read or sent to any AI service. No credential is stored in this repository.

---

## What it does, each run

1. **Discover** documents from Canoe metadata (`GET /v1/documents/data`), incrementally
   since the last successful run (a full pass on the very first run).
2. For every document **not already recorded** in the local manifest (keyed on the
   Canoe document id): **download** its bytes (`GET /v1/documents/{id}`) and **upload**
   them to SharePoint under `‹root›/‹Fund›/‹Year›/‹Category›/‹name›.pdf`, then record it
   in the manifest.
3. Write a **dated log** of documents fetched, skipped, uploaded, and any errors (each
   error tagged with the document id), and **exit non-zero if any document failed**.

**Idempotent:** a rerun on the same day skips everything already in the manifest and
uploads with *replace* semantics, so it never duplicates a document in the library.
Distinct documents that happen to share a name are disambiguated (`… (2).pdf`).

- **Fund / Year / Category** come from Canoe's authoritative metadata (the allocation's
  fund and `data_date`, and Canoe's document category) — not from parsing filenames.
- **Large files** (> 4 MB) upload via a Graph **upload session** (chunked); smaller
  files use a simple PUT. On HTTP **429 / 503** the client honours `Retry-After` and
  backs off.

There is deliberately **no email or Teams notification** in this application.

---

## Configuration

Every value is read from the **environment**. On the App Server the values are stored
in the **macOS Keychain** by `setup.py` and exported into the environment by
`run_sync.sh` at run time — you do **not** keep a `.env` file on the server.
`.env.example` in the repo lists every key with empty values for reference.

| Key | What it is | Where its value comes from |
|---|---|---|
| `GRAPH_TENANT_ID` | Entra ID (Azure AD) tenant id | Entra admin center → the tenant |
| `GRAPH_CLIENT_ID` | App registration (client) id | the app registration |
| `GRAPH_CERT_THUMBPRINT` | Certificate thumbprint | the certificate uploaded to the app registration |
| `GRAPH_CERT_KEY_PATH` | Absolute path to the cert **private key** (PEM) | the key file placed on the App Server (outside the repo) |
| `SP_HOSTNAME` | SharePoint host | e.g. `wakerobinco.sharepoint.com` |
| `SP_SITE_PATH` | Server-relative site path | e.g. `/sites/Investment` |
| `SP_LIBRARY` | Document library (drive) name | e.g. `Documents` |
| `SP_ROOT_FOLDER` | Folder within the library to write under | e.g. `Canoe` |
| `CANOE_CLIENT_ID` / `CANOE_CLIENT_SECRET` | Canoe API service-client credentials | Canoe → Settings → API Configuration |
| `CANOE_USERNAME` / `CANOE_PASSWORD` | Canoe fallback (password-grant) auth | Canoe service account (optional) |
| `CANOE_ORGANIZATION_ID` | Canoe org id | only if the login has multiple orgs (optional) |
| `CANOE_MANIFEST_PATH` | Manifest location | optional; default `‹repo›/.state/manifest.json` |
| `CANOE_LOG_DIR` | Log directory | optional; default `‹repo›/logs` |

The certificate **private key itself is a file on the App Server**, not a config value —
only its path is configured. Keep it `chmod 600` and outside the repo.

---

## Install on a fresh machine

### Prerequisites
- **macOS** with **Python 3.9+** and **git**.
- An **Entra ID app registration** (provisioned by IT) with:
  - a **certificate** credential,
  - the **`Sites.Selected`** application permission, **granted write access to the one
    Canoe SharePoint site** (so the app can write only there),
  - the certificate's **private key** file copied onto the App Server.
- The **Canoe API** service-client credentials.

### Steps
1. **Clone the repo:**
   ```bash
   git clone git@github.com:WR-SW-Dev/canoe-docs.git && cd canoe-docs
   ```
2. **Place the certificate private key** on the machine (e.g. `~/secrets/canoe-graph.pem`),
   readable only by this user (`chmod 600`). Note its absolute path.
3. **Run the installer** (creates the virtualenv, installs dependencies, generates the
   weekly launchd job for this machine):
   ```bash
   ./install.sh
   ```
4. **Configure credentials** — prompts in the terminal, writes to the **Keychain**, and
   **validates Graph access** with one harmless call (so a bad setup fails now, not
   next Monday):
   ```bash
   python setup.py
   ```
5. **Verify Canoe auth:**
   ```bash
   cd "py files" && ../.venv/bin/python credentials_check.py
   ```
6. **Schedule the weekly sync:**
   ```bash
   launchctl load -w ~/Library/LaunchAgents/co.wakerobin.canoe.sync.plist
   ```
   Run it once now to confirm (optional):
   ```bash
   launchctl start co.wakerobin.canoe.sync && tail -30 logs/run_sync.log
   ```

---

## The weekly schedule

`install.sh` writes a `launchd` job at
`~/Library/LaunchAgents/co.wakerobin.canoe.sync.plist` that runs **`run_sync.sh` every
Monday at 07:00 local time**. `run_sync.sh` reads the configuration from the Keychain,
exports it to the environment, and runs `canoe_sync.py`.

- Disable:  `launchctl unload ~/Library/LaunchAgents/co.wakerobin.canoe.sync.plist`
- Run now:  `launchctl start co.wakerobin.canoe.sync`
- The job fires only while the Mac is on/awake; if asleep at 07:00 it runs at next wake.
  Keep the App Server powered on.

> Keychain access from a scheduled job requires the App Server user's login session to
> be active (the login keychain unlocked). On an always-logged-in App Server this is the
> normal state.

---

## Logs and state

- **Per-run log:** `logs/canoe_sync_YYYY-MM-DD.log` — documents fetched / skipped /
  uploaded, and any errors with the document id. This is the file to read after a run.
- **Wrapper log:** `logs/run_sync.log` — start/finish and exit code of each scheduled run.
- **launchd stdout/stderr:** `logs/launchd.out.log`, `logs/launchd.err.log`.
- **Manifest:** `.state/manifest.json` — the idempotency record (Canoe doc id →
  uploaded path). Local to the machine; not committed.

A non-zero exit code from `canoe_sync.py` means at least one document failed — the log
names each failure with its document id.

---

## Repository layout

```
canoe-docs/
├── install.sh                # Installer: venv + deps + launchd job
├── setup.py                  # Credential wizard: prompts -> Keychain, validates Graph
├── run_sync.sh               # What the scheduler runs weekly (Keychain -> env -> canoe_sync)
├── .env.example              # Every config key, empty (reference only)
├── requirements.txt
└── py files/
    ├── canoe_sync.py         # The sync pipeline (discover -> download -> upload -> manifest)
    ├── graph_client.py       # Microsoft Graph client: MSAL cert auth + chunked upload + backoff
    ├── config.py             # Reads all configuration from the environment
    ├── manifest.py           # Idempotency record, keyed on the Canoe document id
    ├── canoe_auth.py         # Canoe API OAuth (client-credentials / password fallback)
    └── credentials_check.py  # Verify Canoe auth (fetches a token, downloads nothing)
```

Other scripts in `py files/` (`statement_tracker.py`, `canoe_reclassify.py`,
`canoe_route.py`, `canoe_bulk_download.py`) are separate utilities from earlier phases;
they are not part of the scheduled Graph sync described here.

---

## Manual operation

```bash
cd "py files"
../.venv/bin/python canoe_sync.py --dry-run   # discover + report, upload nothing
../.venv/bin/python canoe_sync.py             # incremental sync (same as the weekly job)
../.venv/bin/python canoe_sync.py --full      # reconsider all documents (skips those in the manifest)
```
(For a manual run outside `run_sync.sh`, the environment must carry the config — either
export it, or run via `run_sync.sh` which loads it from the Keychain.)
