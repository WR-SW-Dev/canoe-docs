# Canoe Intelligence -- Document Automation

Local-first tooling for downloading and archiving Canoe Intelligence documents
on a Mac Mini. Built for private fund ops: entity-aware folder structure,
delta polling, append-only state, and secure OAuth client-credentials auth.

---

## Setup

### 1. Create a Canoe service client

1. In Canoe, open **Settings** (gear icon) > **API Configuration**
2. Click **Create New Client**
3. Enter any Name and a Redirect URL (required but unused for client-credentials)
4. Copy the generated `client_id` and `client_secret`

### 2. Configure credentials

Fill in `.env` (already present in the folder):

```bash
export CANOE_CLIENT_ID=
export CANOE_CLIENT_SECRET=
```

Recommended alternative: store in macOS Keychain so the values are not plain-text
on disk:

```bash
security add-generic-password -a canoe-api -s client_id -w "<your client_id>"
security add-generic-password -a canoe-api -s client_secret -w "<your client_secret>"
```

### 3. Install dependencies

```bash
source .venv/bin/activate
# requests is already installed in the bundled venv
```

### 4. Verify credentials

```bash
python credentials_check.py
```

Expected output: a token preview, `expires_in` time, and `Credentials are valid.`

### Fallback: password grant (stopgap)

If `client_credentials` returns a **404**, Canoe has not enabled that grant
type for your tenant yet -- contact Canoe support to enable it. In the
meantime, `canoe_auth.py` automatically falls back to the password grant
(`/v1/tokens`) if these are set:

```bash
CANOE_USERNAME=
CANOE_PASSWORD=
CANOE_ORGANIZATION_ID=   # only needed if the account has access to multiple orgs
```

Prefer a dedicated service-account user for this, not a personal login --
the whole point of client-credentials is to avoid auth breaking when
someone leaves the team, and password grant loses that property.

---

## Usage

### Dry run

Show what would be downloaded without writing files:

```bash
python canoe_downloader.py --dry-run
```

### First pull (backfill)

Download history for the last N years:

```bash
python canoe_downloader.py --backfill 3
```

Recommended: smoke-test with limits before running unbounded:

```bash
python canoe_downloader.py --backfill 3 --limit 50
```

### Daily run

```bash
python canoe_downloader.py
```

The tool uses `.state/downloaded_ids.json` to detect new documents since the
last successful poll.

### Advanced

```bash
# Limit to a single fund (pass Canoe fund_id)
python canoe_downloader.py --fund-id FUND-123

# Force from a specific date
python canoe_downloader.py --since 2025-01-01

# Combine backfill with a limit for testing
python canoe_downloader.py --backfill 1 --limit 20
```

---

## Folder structure

```
archive/
  <Entity or Fund>/
    <Year>/
      <Document Type>/
        <DocumentID>_<YYYY-MM-DD>.<ext>
```

The top-level splits on **Entity** first. If no entity is present, the tool
falls back to **Fund Name**, then to the client-provided document ID. There is
no quarter subfolder: the document's own `data_date` already encodes quarter
info.

---

## State

- `.state/downloaded_ids.json` tracks processed documents and the last poll
  timestamp
- This file is append-only; never overwrite or delete entries
- Do not commit `.state/`, `.env`, `archive/`, or `.venv/` to Git

---

## Security

- OAuth client-credentials with a **service account** only
- Credentials resolve from: env vars > macOS Keychain > `.env`
- Tokens are cached in memory only and never written to disk
- A service account prevents breakage when people leave the team

---

## Future

- Weekly digest email with new docs and insights
- SharePoint archive upload
- LLM extraction on new documents
- K-1 tracking dashboard
- GitHub private repo for collaboration
