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
4. Refresh the **statement tracker** (`statement_tracker.py --graph`) and upload the
   received grid to `‹root›/_statement_tracker/`. Metadata only — it never opens a
   document body. See [The statement tracker](#the-statement-tracker).
5. Refresh the **K-1 tracker** (`k1_tracker.py --graph`) and upload its grid to
   `‹root›/_k1_tracker/`. Also metadata only. See [The K-1 tracker](#the-k-1-tracker).

**Idempotent:** a rerun on the same day skips everything already in the manifest and
uploads with *replace* semantics, so it never duplicates a document in the library.
Distinct documents that happen to share a name are disambiguated (`… (2).pdf`).

- **Fund / Year / Category** come from Canoe's authoritative metadata (the allocation's
  fund and `data_date`, and Canoe's document category) — not from parsing filenames.
- **Large files** (> 4 MB) upload via a Graph **upload session** (chunked); smaller
  files use a simple PUT. On HTTP **429 / 503** the client honours `Retry-After` and
  backs off.

The document sync itself sends **no email or Teams notification** — its output is the
library, the log, and the exit code. The one exception is the trackers' optional
digests, sent only when `CANOE_DIGEST_TO` (or `CANOE_K1_DIGEST_TO` for K-1s) and the
`CANOE_SMTP_*` keys are set; leave them unset and nothing is emailed.

---

## Configuration

Every value is read from the **environment**. On the App Server the values are stored
in a **local secrets file** (`~/.config/wr-canoe-sync/secrets.env`, mode 600) by
`setup.py` and exported into the environment by `run_sync.sh` at run time — you do
**not** keep a `.env` file on the server. `.env.example` in the repo lists every key
with empty values for reference.

> The macOS **Keychain is deliberately not used**: the login keychain is locked after
> an unattended reboot, so a `launchd` job with no interactive session would silently
> read empty secrets. A mode-600 file avoids that failure mode.

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
| `CANOE_DATA_DIR` | Local dir for all runtime state | optional; default `~/Library/Application Support/canoe-sync` |
| `CANOE_MANIFEST_PATH` | Manifest location | optional; default `‹CANOE_DATA_DIR›/manifest.json` |
| `CANOE_LOG_DIR` | Log directory | optional; default `‹CANOE_DATA_DIR›/logs` |
| `CANOE_STATE_PATH` | Incremental last-run marker | optional; default `‹CANOE_DATA_DIR›/last_sync.json` |

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
1. **Clone the repo to a LOCAL path — not inside OneDrive or any synced folder** (e.g. a
   service-account home or `/usr/local/canoe-sync`):
   ```bash
   git clone git@github.com:WR-SW-Dev/canoe-docs.git ~/canoe-sync && cd ~/canoe-sync
   ```
   Both the code and its runtime state must live on local disk. Runtime state defaults to
   `~/Library/Application Support/canoe-sync`; keep the checkout out of OneDrive too.
2. **Place the certificate private key** on the machine (e.g. `~/secrets/canoe-graph.pem`),
   readable only by this user (`chmod 600`). Note its absolute path.
3. **Run the installer** (creates the virtualenv, installs dependencies, and installs the
   weekly system LaunchDaemon; `sudo` is requested only for the plist installation):
   ```bash
   ./install.sh
   ```
4. **Configure credentials** — prompts in the terminal, writes to the **local secrets
   file** (`~/.config/wr-canoe-sync/secrets.env`, mode 600), and **validates Graph
   access** with one harmless call (so a bad setup fails now, not next Monday). It also
   generates and prints the dashboard Resync secret; save that value in 1Password:
   ```bash
   python setup.py
   ```
5. **Verify Canoe auth:**
   ```bash
   cd "py files" && ../.venv/bin/python credentials_check.py
   ```
6. **Schedule the weekly sync:**
   ```bash
   sudo launchctl load -w /Library/LaunchDaemons/co.wakerobin.canoe.sync.plist
   ```
   Run it once now to confirm (optional):
   ```bash
   sudo launchctl start co.wakerobin.canoe.sync
   tail -30 "$HOME/Library/Application Support/canoe-sync/logs/run_sync.log"
   ```

---

## First-run migration (when the library already has documents)

If the SharePoint library was **already populated** (e.g. an existing archive copied in
by hand), do **not** let the first sync re-upload everything. Seed the manifest so the
sync knows those documents are already present, then reconcile against the live library
to catch any gap. Two steps:

1. **Seed** the manifest + last-run marker from a full Canoe discovery (no upload):
   ```bash
   cd "py files"
   ../.venv/bin/python canoe_sync.py --seed --seed-out ~/canoe-seed
   ```
   Copy the resulting `manifest.json` and `last_sync.json` into `CANOE_DATA_DIR`
   (default `~/Library/Application Support/canoe-sync`). The next sync will now **skip**
   every document already recorded, and only pick up genuinely new ones.

   > The seed marks all discovered documents as present. That is accurate **only if the
   > existing copy is complete through the seed time.** Anything uploaded to Canoe after
   > the copy was taken but before the seed would be in the manifest yet possibly not in
   > SharePoint — which the next step catches.

2. **Reconcile** against the live library once Graph is configured, to verify the seed
   and surface anything missing:
   ```bash
   ../.venv/bin/python canoe_sync.py --export live_inventory.csv
   ```
   or use the dashboard's **Reconcile** view. Either lists what is *actually* in
   SharePoint (via Graph) and flags any manifest entry as `MISSING_IN_SHAREPOINT`. Remove
   those doc ids from the manifest (or just run a normal sync) so they get uploaded.

After that, the weekly incremental sync carries on from `last_sync.json` as usual.

---

## Admin dashboard

A lightweight **local** dashboard so nobody has to read JSON or log files to see the
sync's state. It reads the same runtime state the sync writes (`manifest.json`,
`runs.jsonl`) and talks to Graph for live verification.

```bash
./run_dashboard.sh          # loads secrets -> env, serves on http://127.0.0.1:8765
```

It binds to **localhost by default**. To reach it from another machine without changing
the bind address, SSH-tunnel to the App Server:
```bash
ssh -L 8765:127.0.0.1:8765 «app-server»    # then open http://127.0.0.1:8765 locally
```

Four views:

| View | What it shows |
|---|---|
| **Manifest** | Searchable/filterable table of `manifest.json` — doc id, destination path, uploaded-at, size. The source of truth for "what's been synced". |
| **Run history** | Recent runs from `runs.jsonl` — mode (full/incremental), fetched/uploaded/skipped/error counts, duration, result. Structured, not scraped from log lines. |
| **Reconcile** | Lists what is *actually* in SharePoint (live Graph crawl) and compares it to the manifest: flags entries recorded as uploaded but **missing in SharePoint**, and files present in the library but **absent from the manifest**. This is the verification underneath the manifest — don't trust the manifest blindly. |
| **Resync** | A guarded action (below). |

### The Resync action

Behind a typed confirmation and a shared-secret challenge, **Resync** does a clean full
rebuild of the library:

1. **Archives** the current SharePoint root folder in place — an in-place Graph rename to
   `‹root›_archive_‹YYYY-MM-DD›`. Nothing is deleted; every existing file moves under the
   archived name.
2. **Clears** `manifest.json` and `last_sync.json`.
3. **Launches** `canoe_sync.py --full` in the background and shows **live status**
   (documents uploaded so far, elapsed time, a tail of the run log), refreshing while it
   runs.

A full rebuild re-downloads and re-uploads every document; at Canoe's ~60 calls/min rate
limit a full library (~9,800 docs) takes **a few hours**. The confirmation guard is there
because this is a real, consequential action — not a casual toggle.

Viewing the dashboard remains unauthenticated. Immediately before Resync, the browser
must submit `CANOE_RESYNC_SECRET`; a successful challenge creates a two-minute,
one-use action token that is consumed by the next Resync POST. The secret is never
placed in the page or token, and the password field is cleared after the challenge.
Run `python setup.py --rotate-resync-secret` to rotate it and save the printed value in
1Password.

The dashboard needs the Graph configuration for **Reconcile** and **Resync**; the
**Manifest** and **Run history** views work without it.

---

## The weekly schedule

`install.sh` installs a system `launchd` job at
`/Library/LaunchDaemons/co.wakerobin.canoe.sync.plist`, owned by `root:wheel`, that runs
**`run_sync.sh` as `dev` every Monday at 07:00 local time**. A LaunchDaemon is used so
the service does not depend on any user having an active GUI/console session.
`run_sync.sh` reads the configuration from the local secrets file, exports it to the
environment (line-by-line, never `source`/eval`), runs `canoe_sync.py`, and then
refreshes the statement tracker and the K-1 tracker.

Both tracker steps run **regardless of the sync's exit code**, and their exit codes are
logged separately without changing the job's: they read Canoe metadata rather than the
library, so they still produce correct grids when an upload failed, and a tracker
problem must not be reported as a broken document sync. Neither tracker can stop the
other from running. All three codes appear in `run_sync.log`. Both steps are skipped for
modes that upload nothing (`--dry-run`, `--seed`, `--local-dest`, `--export`).

- Disable: `sudo launchctl unload /Library/LaunchDaemons/co.wakerobin.canoe.sync.plist`
- Run now: `sudo launchctl start co.wakerobin.canoe.sync`
- The job fires only while the Mac is on/awake; if asleep at 07:00 it runs at next wake.
  Keep the App Server powered on.

> Because secrets are in a mode-600 file (not the login keychain), the job runs
> correctly after an unattended reboot with no interactive login — no keychain unlock
> is required.

---

## The statement tracker

`statement_tracker.py` answers a different question from the sync: not "did we file this
document" but "**which statements have not arrived yet**". It reads Canoe's structured
metadata (fund, sponsor, data date, type, status) and reconciles it against a per-fund
schedule of expected frequency and grace period. It never opens a document body, so it
adds no GenAI dependency and no new data-handling surface.

It runs as the second step of the weekly job and writes into the **same SharePoint
library as the documents**, under `‹root›/_statement_tracker/`:

| Path | What it is |
| --- | --- |
| `Statement Tracker ‹date›.xlsx` | **The team grid.** Green = received (click to open the statement), red = expected but missing, amber = arrived but not tagged to an entity in Canoe. One sheet per cadence. |
| `Archive/` | Previous grids. Each run writes a **new dated workbook** and moves the old one here — a fresh item always syncs, whereas rewriting a workbook someone has open in Excel wedges it. Moves preserve the item id, so saved links keep working. |
| `backend/statement_schedule.xlsx` | **Editable by the team.** One row per fund: frequency, grace days, whether to track it, optional `doc_types` override. Auto-seeded from history on the first run — review it. |
| `backend/` (rest) | Supporting detail: HTML status dashboard, digest, status/received CSVs. |

**The schedule round-trips.** Each run downloads the workbook from SharePoint before
reconciling, so edits made there always win, and re-uploads it **only if that run
changed it** (seeded it, or appended newly-discovered funds flagged `NEW --`). An
unchanged schedule is never re-uploaded, so a concurrent edit is not overwritten.

Outputs are built in a local staging dir (`CANOE_TRACKER_DIR`, default
`‹data›/statement_tracker`) and then uploaded. The **metadata cache and digest state stay
there** — like the manifest, they are runtime state and do not belong in a synced library.

The tracker folder is excluded from the document inventories used by the dashboard's
**Reconcile** and `canoe_sync --export`; otherwise every grid would be reported as an
orphan with no manifest entry.

Two destinations:

```bash
cd "py files"
../.venv/bin/python statement_tracker.py --graph                  # what the weekly job runs
../.venv/bin/python statement_tracker.py --graph --refresh full   # re-pull all metadata
../.venv/bin/python statement_tracker.py --dest /path/to/Canoe    # a LOCAL synced archive
```

`--graph` needs no synced folder, which is why the scheduled job uses it. `--dest` writes
into a local OneDrive-synced `Canoe` folder instead, and is kept for ad-hoc local runs;
grid hyperlinks are relative paths in that mode and absolute SharePoint URLs in `--graph`.

Both trackers resolve a hyperlink by matching Canoe's document name against the library
listing, and that match **sanitizes the name first** — `canoe_sync` strips `/`, `:` and
friends out of a name before filing it, so a fund like `P/E Investments` is stored as
`P-E Investments-…`. Without sanitizing, those documents silently missed and the cell
fell back to linking the fund *folder*, which reads as a broken link to whoever clicks
it. A document genuinely absent from the library still falls back to the folder — most
often one filed under `Unknown Investment` before Canoe finished classifying it, which
the manifest then stops the sync from ever re-filing.

---

## The K-1 tracker

`k1_tracker.py` is the tax-document sibling of the statement tracker, built to the same
shape (same two destinations, dated workbook plus `backend/`, auto-seeded editable
schedule, digest, `Archive/` rotation) and answering the same kind of question:
**which K-1s have not arrived yet**. It covers `K-1` and `K-3` — K-3 is the
foreign-activity companion and arrives on the same cycle from the same manager. Like the
statement tracker it reads metadata only, never a document body.

It runs as the third step of the weekly job and writes under `‹root›/_k1_tracker/`:

| Path | What it is |
| --- | --- |
| `K-1 Tracker ‹date›.xlsx` | **The team grid.** One row per fund/entity, one column per tax year, in four colours: received / not in Canoe / not yet due / needs a fix. Sheet 2 is the **Chase list** — a flat worklist, most-actionable first, whose *Action* column says what to do about each row. Sheet 3 is **By entity** — expected / received / outstanding per investing entity, per tax year. |
| `Archive/` | Previous grids, same rotation and rationale as the statement tracker. |
| `backend/k1_schedule.xlsx` | **Editable by the team.** One row per fund: `track`, `first_tax_year`, `due_month_day`, `exclude_entities`, `contact`. Auto-seeded on the first run — review it. A second sheet, **Entity aliases**, folds two Canoe entity names into one owner (see below). |
| `backend/` (rest) | HTML dashboard, digest, status/received CSVs. |

### What is different from the statement tracker

K-1s are annual by definition, so there is no cadence to infer. The question becomes
which fund × entity × tax year combinations are *owed* a K-1, and that is derived from
**holdings, not from K-1 history**: an entity is expected to receive a K-1 for tax year
Y when Canoe shows it held that fund during Y — an Account Statement or Capital Call
Notice tagged to that fund and entity, or a K-1 itself. Holding is treated as a
contiguous span, so a gap in Canoe's coverage does not punch a hole in the expectation,
and an exited position stops being expected after its final year. **A row exists only
where a K-1 is owed**, so an empty cell means nothing is expected — there is no third
reading. Deriving from holdings rather than from past K-1s is what lets the grid flag a
fund that has *never* delivered one.

The deadline is a **date, not a grace period**: `June 30` of the year following the tax
year by default, editable per fund. That is deliberately ahead of the September 15
extended filing deadline, to leave a window for chasing.

Statuses, beyond received / late / pending / OVERDUE:

| Status | Meaning |
| --- | --- |
| `review` | Arrived, but Canoe flags it (`Awaiting Confirmation`, `Anomaly Detected`, `Potential Discrepancy`, `Configuration Required`) — internal Canoe work. |
| `locked` | Arrived but **password protected**. Encrypted tax PDFs are routine; this is a different action (ask the manager for the password) from a Canoe review, so it is not counted as received. |
| `retag` | A K-1 for the fund and year is in Canoe with **no entity assigned** — a tagging job, not a missing document. |

**Entity-less K-1s, and why a cell sometimes has no link.** Canoe leaves the entity blank
when its extraction is unsure, but the manager almost always names the partner in the
filename, so the tracker matches that back against the entities Canoe knows hold the fund
(`&`/`and` and punctuation normalised). Exactly one match attributes the document; the
cell shows `Tag` and links to it. Anything else leaves it unattributed — a filename like
`2023 K1 - FVP Opportunity Fund IV LP - 66332.pdf` identifies the investor only by account
number, so no attribution is possible.

An unattributed document is offered to **no** entity row. Earlier revisions handed the
same untagged K-1 to every entity missing that fund-year, which pointed one beneficiary
at another beneficiary's tax return — the rows read `Tag?` and carry **no hyperlink**, and
the document is instead listed once, per fund-year, at the bottom of the Chase list where
nothing claims to own it. Rows with no candidate at all stay plain `OVERDUE`.

> The matcher deliberately keeps *"Scott and Amanda Brooks Family Trust"* and *"Scott J
> Brooks and Amanda T Brooks"* apart. They are different Canoe entities, and conflating
> them is exactly how a K-1 gets mis-filed.

**Mis-tag detection.** Within one fund and tax year, an entity holding **two final K-1s**
while a sibling entity holds **none** is the signature of a mis-tag: managers send one
K-1 per partner, so the surplus one probably belongs to the entity showing nothing. Both
sides are flagged, the missing cell reads **`Check?`**, and its Chase list action becomes
**"CHECK CANOE TAGGING first"** instead of "Chase the manager" — the whole point being not
to email a manager for a document Canoe already holds under the wrong name.

It is a suspicion, not a verdict: an amended K-1 also arrives as a second final, so the
tracker asks for a check rather than reassigning anything. Drafts never count toward the
surplus, and a fund-year with nobody missing is never flagged.

**Entity aliases.** Canoe sometimes files one owner under two names — the same position
tagged to a trust in one year and to its trustees personally in another — which splits it
into two grid rows, one showing a K-1 nobody expected and the other showing a gap that is
not real. The **Entity aliases** sheet in `k1_schedule.xlsx` maps `alias` →
`canonical_entity`, and the rename is applied where rows are first built, so holdings,
expectations, attribution and every sheet all agree. Matching ignores case; an alias
pointing at itself is ignored.

> Only for names that are genuinely the same owner. Two entities that file separate tax
> returns must stay separate rows, or the grid will call one complete while the other's
> K-1 is still outstanding.
| `draft` | Only a **draft** K-1 has arrived; the final is still owed. |
| `unverified` | Holdings say a K-1 is owed, but this fund has **never delivered one for any year**, so it may simply not issue them (a 1099 payer, an offshore feeder, a position held through a blocker). |

**Drafts.** Canoe gives a draft K-1 the document type `K-1` and the status `Complete`,
exactly like the real thing — `document_approval` reads `Pending` on all 454 documents
and `document_tags` carry only Canoe-internal flags, so neither helps. The **only**
signal is the manager's own filename (`2025 DRAFT K-1 - RMWC DLF, LP.pdf`), which the
tracker matches on `draft` / `preliminary` / `estimated`. Where a draft and a final both
cover a year, the final wins and the cell is simply `received` — and the cell's link
points at the final even when the draft arrived first. A draft **alone** never counts as
received: a return cannot be filed on it, so the year stays open and the Chase list says
*"Chase the manager for the FINAL"*.

> `amended` is deliberately not treated as a marker. Every occurrence in this library is
> the phrase *"as amended"* inside a trust's legal name, so matching it would flag dozens
> of perfectly good K-1s.

`unverified` is painted the **same red as OVERDUE**. Canoe genuinely cannot tell the two
apart — `investment_structure` reads `drawdown_fund` for 11 of the 13 funds that have
never filed a K-1, exactly as it does for the 50 that have — so giving it a softer colour
only hid real chases. The distinction is real but it is a *human* judgement, so it
surfaces where a human acts on it: the Chase list's **Action** column reads either
*"Chase the manager"* or *"Confirm this fund issues a K-1"*. Once you have confirmed a
fund genuinely does not issue one, `track=no` retires its rows for good.

Canoe-flagged `Duplicate` documents are ignored outright, so a re-send never satisfies a
year on its own. K-1s sitting in Canoe's **`unknown` investment bucket** cannot be placed
on the grid at all — there is no fund to file them under — so they are listed separately
in the dashboard's *Action needed* table rather than silently dropped. Canoe already
knows they are K-1s; assigning the investment in Canoe is all it takes to pull them onto
the grid.

Four colours, not eight, is deliberate: an earlier revision gave every status its own
swatch and the legend read as noise. The finer statuses still drive the Chase list and
the CSVs, where the difference changes what you do.

Two further notes. Canoe returns `fund_sponsor: null` on K-1 allocations, so the
schedule's **`contact`** column is the only way the Chase list can tell you who to
email — worth filling in for the funds you actually chase. And the **first metadata pull
takes several minutes** (it covers the holdings-evidence types as well as the K-1s);
delta refreshes afterwards take seconds.

```bash
cd "py files"
../.venv/bin/python k1_tracker.py --graph                     # what the weekly job runs
../.venv/bin/python k1_tracker.py --graph --dry-run           # build in staging, upload nothing
../.venv/bin/python k1_tracker.py --graph --refresh full      # re-pull all metadata
../.venv/bin/python k1_tracker.py --graph --reuse-inventory   # fast re-publish (see below)
../.venv/bin/python k1_tracker.py --dest /path/to/Canoe       # a LOCAL synced archive
```

**Re-publishing after a Canoe correction.** Metadata is already incremental — a delta
pull by `last_modified` takes seconds and picks up re-tags made inside Canoe. What makes
a run slow is walking the library (~10k files) to resolve the grid's hyperlinks, and that
listing only changes when the sync uploads documents. `--reuse-inventory[=MINUTES]`
(default 360) reuses the previous walk, turning a re-publish from minutes into seconds.

It is **off by default and not used by the weekly job**, deliberately: that job runs
immediately after the sync has added documents, which is precisely when a stale listing
would mislink the newest K-1s.

---

## Logs and state

All runtime state lives under **`CANOE_DATA_DIR`** (default
`~/Library/Application Support/canoe-sync`), on **local disk — never in the repo or a
synced/OneDrive folder**, so the idempotency manifest cannot be corrupted by a sync client.

- **Per-run log:** `‹data›/logs/canoe_sync_YYYY-MM-DD.log` — documents fetched / skipped /
  uploaded, and any errors with the document id. This is the file to read after a run.
- **Wrapper log:** `‹data›/logs/run_sync.log` — start/finish and exit code of each scheduled run.
- **launchd stdout/stderr:** `‹data›/logs/launchd.out.log`, `‹data›/logs/launchd.err.log`.
- **Manifest:** `‹data›/manifest.json` — the idempotency record (Canoe doc id →
  uploaded path). Must stay on local disk, out of any synced folder.
- **Last-run marker:** `‹data›/last_sync.json` — the incremental window.
- **Run history:** `‹data›/runs.jsonl` — one structured JSON record per real sync run
  (start/end, mode, fetched/uploaded/skipped/error counts, duration, exit code). Written
  automatically by each run; read by the dashboard's **Run history** view. This replaces
  the old hand-maintained `run_history.csv`.
- **Statement-tracker staging:** `‹data›/statement_tracker/_statement_tracker/` — where
  the grid is built before upload, and the permanent home of the tracker's metadata cache
  and digest state. Safe to delete: the next run re-pulls (a full metadata pass) and
  re-baselines the digest to the last 7 days.
- **K-1-tracker staging:** `‹data›/k1_tracker/_k1_tracker/` (`CANOE_K1_TRACKER_DIR`) — the
  same arrangement for the K-1 tracker, with its own independent metadata cache and digest
  state. Equally safe to delete, at the cost of one full metadata pass.

A non-zero exit code from `canoe_sync.py` means at least one document failed — the log
names each failure with its document id.

### One source of truth

"What's been synced" has exactly one authority: **`manifest.json`** (Canoe doc id →
uploaded path), **verified against what is actually in SharePoint** via a live Graph
listing (`--export`, or the dashboard's **Reconcile** view). There is deliberately **no
parallel spreadsheet or local-filesystem/OneDrive-mirror scan** — those drift from the
live site (a mirror once listed a document as present that was not actually there). If
you want to know whether a document is in the library, ask the manifest and reconcile it
against Graph; do not scan a folder.

---

## Repository layout

```
canoe-docs/
├── install.sh                # Installer: venv + deps + launchd job
├── setup.py                  # Credential wizard: prompts -> secrets file, validates Graph
├── run_sync.sh               # What the scheduler runs weekly (canoe_sync, then both trackers)
├── run_dashboard.sh          # Launch the local admin dashboard (secrets file -> env -> dashboard)
├── .env.example              # Every config key, empty (reference only)
├── requirements.txt
└── py files/
    ├── canoe_sync.py         # The sync pipeline (discover -> download -> upload -> manifest)
    ├── graph_client.py       # Microsoft Graph client: MSAL cert auth + chunked upload + backoff
    ├── dashboard.py          # Local admin dashboard (manifest / runs / reconcile / resync)
    ├── config.py             # Reads all configuration from the environment
    ├── manifest.py           # Idempotency record, keyed on the Canoe document id
    ├── statement_tracker.py  # Which statements have not arrived: grid -> _statement_tracker/
    ├── k1_tracker.py         # Which K-1s have not arrived: grid -> _k1_tracker/
    ├── canoe_auth.py         # Canoe API OAuth (client-credentials / password fallback)
    └── credentials_check.py  # Verify Canoe auth (fetches a token, downloads nothing)
```

`statement_tracker.py` is the weekly job's second step — see
[The statement tracker](#the-statement-tracker). The remaining scripts in `py files/`
(`canoe_reclassify.py`, `canoe_route.py`, `canoe_bulk_download.py`) are utilities from
earlier phases and are **not** part of the scheduled sync; `run_weekly.sh` is the
superseded pre-Graph pipeline, kept for reference and not installed by `install.sh`.

---

## Manual operation

```bash
cd "py files"
../.venv/bin/python canoe_sync.py --dry-run   # discover + report, upload nothing
../.venv/bin/python canoe_sync.py             # incremental sync (same as the weekly job)
../.venv/bin/python canoe_sync.py --full      # reconsider all documents (skips those in the manifest)
../.venv/bin/python canoe_sync.py --local-dest ~/Desktop/Canoe --full   # backfill to a LOCAL folder
../.venv/bin/python canoe_sync.py --export inventory.csv                # live SharePoint inventory (with doc_id)
../.venv/bin/python canoe_sync.py --seed --seed-out DIR                 # seed manifest+last_sync for a fresh install
```

**Discovery is chunked.** Canoe's `/v1/documents/data` times out on multi-year spans, so
`discover()` walks month-by-month (halving any month that still times out down to days).
A first-run full discovery works without manual chunking.

**Inventory (`--export`).** Lists what is *actually* in the SharePoint library via Graph
(not a local mirror, which can diverge) and annotates each row with the Canoe `doc_id`
from the manifest, flagging any manifest entry missing from the live library.
(For a manual run outside `run_sync.sh`, the environment must carry the config — either
export it, or run via `run_sync.sh` which loads it from the secrets file.)

**Local backfill (`--local-dest`).** Writes files to a local folder in the same
`Fund/Year/Category` structure instead of uploading — useful for a first backfill or a
review copy, and it needs **no Graph certificate**. It keeps its own manifest
(`_sync_manifest.json`) inside that folder and does **not** touch the SharePoint
incremental state, so it never blocks or interferes with the real weekly sync.
A full backfill downloads documents one at a time and self-throttles to Canoe's rate
limit, so expect a few thousand documents to take a couple of hours.
