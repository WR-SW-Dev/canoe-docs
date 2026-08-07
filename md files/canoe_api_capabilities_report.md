# Canoe Intelligence API -- Capabilities & Leverage Report

**Prepared for:** Investment Operations / Portfolio Management Team  
**Source:** Canoe Intelligence Help Center (as of July 2026; reference articles: API Overview, Best Practices, Bulk Categorization, Fetching Access Token via Python, Service Client OAuth Client Credentials Flow)  
**Bottom line up front:** The Canoe API is a real automation lever -- it eliminates manual download trips to the platform, creates a structured file archive, and opens the door to AI enrichment and team-wide reporting. Fully executable on a private repo at near-zero operational cost.

---

## 1. What the API Actually Does

Canoe's API lets your team programmatically access everything you currently do inside the platform -- pulling documents, reading their metadata, uploading new files, and reclassifying documents at scale. The API sits on `api.canoesoftware.com` over HTTPS (ports 443/9443). Authentication is OAuth 2.0 client credentials. Every endpoint returns JSON; document payloads are returned as binary.

### Core capabilities

| Domain | What the API does |
|---|---|
| **Document discovery** | List documents with metadata: status, fund, entity, allocation, data date, document type, tags, upload time, modification time. Filter by date range, status, and tag. |
| **Document download** | Download the actual file bytes by ID. Pagination is supported on the download endpoints. |
| **Document upload** | Upload a new document with optional metadata set at upload time (fund, entity, allocation, data date, document type). |
| **Bulk metadata updates** | Update approvals, tags, comments, and client document ID on up to 100 documents in a single call. |
| **Classification / categorization** | Reassign fund, entity, allocation, data date, and document type on existing documents. Canoe's own AI makes predictions; the API lets you override or set explicitly. |
| **Ownership management** | Query and assign Funds, Organizations (entities), and Allocations. |
| **Change detection** | Poll for new documents since your last fetch using `file_upload_time_start` or `last_modified_time_start`. |

### Rate limits

Standard: **60 calls per minute per HTTP method** (GET, POST, PUT, DELETE). The limit is per-method, not per-endpoint, so sustained GET polling plus the occasional POST/upload are both viable without special arrangements.

> **Practical impact:** An hourly poll using a delta filter returns only new documents since the last run. Even a busy portfolio sits well below the limit. Unpaginated endpoints will be deprecated -- always include `page` and `limit`.

---

## 2. How to Think About Leveraging This (for Your Team)

### The mental model shift

Most teams treat Canoe as a human-facing portal: file in → categorized → reviewed → found later. The API fundamentally changes that because it turns Canoe into a *machine-readable data layer* -- the same one your ops people already use, but now accessible to scripts, schedulers, and language models.

Think of Canoe as:

- **The canonical inbox** -- every PDF that arrives from a GP or manager lands here first. Your downstream systems shouldn't copy this data; they should query it.
- **The structured metadata source** -- Canoe pre-classifies documents. That classification (fund, entity, allocation, type, date) is available in the API before you read a single byte of the file.
- **The audit trail** -- every tag, status change, and ownership assignment is trackable, so compliance and legal work flows naturally out of the platform's own data model.

### The three automation tiers

1. **Mechanical automation (low effort, high reliability)**  
   Hourly polling for new documents → auto-download → archive to structured file paths → tag as ingested. No AI required. Eliminates the manual "trip to the portal" entirely.

2. **Workflow automation (medium effort)**  
   Document type detection → role-based routing → templated email → status tracking. E.g., the moment a capital call lands, Legal, Finance, and the RM all get notified with the relevant details. K-1s go to the Tax team with a receipt checklist. No human reading required to decide "who needs this."

3. **Intelligence layer (higher value, cheap cost)**  
   Run a lightweight LLM pass on the documents that passed tier 2. Extract structured fields (amount, due date, IRR, state allocations) and feed them into:
   - Weekly portfolio digest emails
   - Cross-quarter trend detection
   - Cross-fund manager comparisons
   - Exception flagging (fees rising, NAV dropping, distributions slowing)

> The key design constraint: only invoke the LLM on *new* documents, not on every poll. Polling is metadata-only and free; enrichment runs per document. This is how you keep inference cost negligible.

---

## 3. Full Use-Case Catalog -- Beyond the Obvious

### Document Operations
- **Auto-archive pipeline**: New Complete doc → download → structured file path → checksum verification → tag as ingested. Repeat on a schedule. Eliminates the manual download step entirely.
- **Discrepancy triage**: New docs with `Potential Discrepancy` status trigger immediate alerts to Ops/Legal with the doc ID and issue type. These should not wait for the next hourly batch.
- **Bulk recategorization**: When a fund ownership restructure occurs, use the bulk metadata endpoint to reassign all affected documents to the new fund in a single batch.
- **Duplicate prevention**: After download, tag the document. On the next poll, exclude documents carrying that tag. This is both dedup logic and a visual audit trail inside Canoe's platform.
- **Pre-categorization at upload**: Include `fund_id` and `document_type_id` in the upload call. Documents skip manual categorization entirely -- they land in the right bucket automatically.

### Capital Calls & Distributions
- **Capital call routing**: Detect → extract fund/amount/due date/GP → email Legal + Finance + RM → optional calendar event.
- **Distribution tracking**: Detect → extract per-share amount, yield, recallable amount → update NAV dashboard → notify LPs as appropriate.
- **Notice period enforcement**: If your fund docs have due dates, build a calendar hold or a pre-due-date reminder (e.g., T-3 days before the capital call deadline).
- **Recallable amount tracker**: If distributions include a recallable component, log this in a running tracker so it reconciles against the next call.

### Tax (K-1s, 1065s)
- **Receipt tracking**: Every K-1 that arrives is logged. Build a per-LP, per-state receipt status table. Flag items outstanding past a configurable window.
- **State allocation comparison**: Layer LLM comparison against prior-year K-1s. Large deltas in state allocations flag potential state-tax implications.
- **Partner count / entity changes**: Track when fund entities change, as this affects who needs K-1s in future periods.
- **CPA handoff**: Auto-bundle K-1s by external CPA and generate a handoff-ready summary.

### Portfolio Assessment & Strategy
- **Quarter-over-quarter NAV tracking**: Extract NAV from each statement; chart trends per fund. Flag drops that exceed a threshold (configurable).
- **Fee escalation detection**: Compare management fees and carried interest across periods. Flag funds where fees grow faster than NAV.
- **Top holdings drift**: For equity-heavy funds, track the top 10 holdings across quarters. Flag significant churn or concentration risk.
- **Cross-fund manager comparison**: When you have multiple fund managers (or GP relationships), normalize performance metrics side-by-side. Identifies underperacing managers early.
- **Distribution yield analysis**: Compare distribution yield against NAV and committed capital. Spot funds that are returning capital vs. generating income.

### Team Enablement
- **Weekly portfolio digest**: Compile all new documents from the past 7 days. Include a narrative summary of what moved, what flagged, and what's coming (upcoming deadlines). Sent to the whole team Monday morning.
- **Audit log**: Every document action (downloaded, categorized, tagged, alerted) is logged. This supports compliance reviews and legal hold requests without extra manual effort.
- **Config-driven routing**: Fund map, team roster, and document type mappings live in version-controlled JSON files. Non-engineers can propose changes via GitHub Issues. Engineers review and merge.

---

## 4. Recommended Tech Stack

| Layer | Tool | Why |
|---|---|---|
| **Language** | Python 3.11+ | Standard library covers most needs (`smtplib`, `json`, `logging`, `schedule`). `requests` for the API. No heavy framework required. |
| **Auth** | OAuth client credentials + service account | Cost: $0. Prevents credential breakage on departures. |
| **Scheduling** | macOS LaunchAgent or Hermes cron | OS-native, survives reboots, writes logs. No external cron service needed. |
| **Secrets** | macOS Keychain + .env (symlinked, `.gitignore`d) | No hardcoded credentials. Accessible to scripts via `security find-generic-password` or `python-dotenv` pointing at a symlink outside the repo. |
| **Version control** | GitHub Private repo | Free for individual/team. Full audit history. Branch protection for `main`. GitHub Actions for CI lint runs. |
| **LLM inference** | GitHub Models or equivalent free-tier endpoint | Only on new documents. Token cost is proportional to doc count, not poll count. With 10-20 new docs/week, inference is essentially free. |
| **Notifications** | SMTP (`smtplib`) + Microsoft Teams webhook | Most teams have an SMTP relay. Teams webhook costs $0. |
| **Storage** | SharePoint document library | Folder convention: `[Fund Name]/[Year]/[Document Type]/[ID]_[Date].[ext]`. Permissions managed through Microsoft 365 groups. SharePoint version history gives built-in revision tracking and restore.
| **Observability** | Rotating JSON log files + Ops Teams channel | Log every poll cycle: docs checked, docs new, errors, LLM latency. Alert on auth failures and rate-limit 429s. |

### GitHub Access Model

| Role | Access | Use |
|---|---|---|
| Owner (you) | Admin | Merge to `main`, manage secrets, deploy |
| Engineers / Ops | Write (via PR) | Feature branches. Branch protection enforces review before merge. |
| Finance / Legal | Triage (Issues, Comments) | Propose routing/template changes. Read access for digest. No direct push. |
| Read-only stakeholders (RM, Portfolio, Tax) | Read | Consume digests and documentation. Can open Issues for requests. |

### Extension Points

- **Webhooks (future):** If Canoe adds push/notification webhooks, you can replace polling entirely with push. The rest of the architecture is webhook-agnostic -- the scheduler layer can run in push or pull mode with the same outputs.
- **Data warehouse (future):** Currently archiving to SharePoint. Adding a structured data warehouse (Postgres, even SQLite) alongside the SharePoint archive unlocks BI dashboards, pivot tables, and year-over-year comparisons without re-running LLM passes.
- **External system sync:** If you use Addepar, BlackRock Aladdin, or similar, use the same structured data to push NAV and allocation updates back to those systems -- turning Canoe into your single source of truth.

---

## 5. Implementation / Team Access

For the full implementation sequence, team access model, open questions, and setup requirements, see the companion file `architecture.html` in this folder. That document covers:
- GitHub repo access roles (Owner, Engineer, Finance, Legal, RM, Tax, Portfolio)
- Ten-step setup sequence from service account creation through monitoring
- Open technical and business questions to resolve before launch
- Cross-functional team interaction design


---

## 6. Cost & Risk Summary

**Cost:** The only recurring spend beyond Canoe itself is LLM inference on new documents. Using free-tier endpoints, a portfolio receiving 20-50 new documents per week sits comfortably under $1-3/month. If volume grows into the hundreds, switch to a usage-tier model -- still typically under $5/month.

**Risk:**
- Credential management (mitigated by service account + OS keychain)
- Rate limiting (mitigated by hourly polling + delta filter; monitor 429s in logs)
- Duplicate processing (mitigated by tagging post-download + exclusion filter)
- LLM hallucination on structured extraction (mitigated by confidence thresholds; fall back to regex rules or mark as "needs human review" when confidence is low)
- Document type changes at Canoe (Canoe auto-predicts; explicit API override handles the rest)

**What's not covered in the reference docs:**
- The complete endpoint reference (Postman collection referenced but not included in our docs; ask Canoe support for the latest version)
- Webhook/push notification support (not surfaced in reference materials; polling is the reliable documented path)
- Exact document type IDs and custom fields (client-specific; query from your live instance)

---

*This report is based on the Canoe Intelligence help articles current as of the attached reference set. For the latest API surface, contact `support@canoeintelligence.com`.*
