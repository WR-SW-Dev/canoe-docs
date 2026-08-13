#!/usr/bin/env python3
"""
dashboard.py -- Lightweight local admin dashboard for the Canoe -> SharePoint sync.

A single-purpose Flask app that reads the SAME runtime state the scheduled sync writes
(manifest.json, runs.jsonl) so nobody has to dig through JSON or log files. It binds to
localhost only and is meant to run on the App Server (or via an SSH tunnel to it).

There is deliberately ONE source of truth for "what's been synced": the manifest, keyed
on the Canoe document id, verified against what is *actually* in SharePoint via the live
Graph listing (the Reconcile view). No local-filesystem/OneDrive-mirror scan -- those
drift from the live site.

Views
-----
  Manifest   Searchable table of manifest.json (doc_id, dest_path, uploaded_at, size).
  Runs       Recent sync runs from runs.jsonl (mode, counts, duration) -- structured,
             not scraped from log lines.
  Reconcile  Live Graph listing of the library, compared to the manifest: flags entries
             recorded as uploaded but MISSING_IN_SHAREPOINT, and files present in the
             library but absent from the manifest.
  Resync     Guarded action: archive the current SharePoint root folder in place
             (rename -> <root>_archive_<date>), clear the manifest/last-run marker, and
             launch canoe_sync.py --full in the background, with live status.

Run:
    ./run_dashboard.sh          # loads secrets -> env, then launches this on 127.0.0.1
    # or directly, with the environment already carrying the config:
    ../.venv/bin/python dashboard.py
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from datetime import datetime, timezone

try:
    from flask import Flask, jsonify, request, Response
except ImportError as exc:  # pragma: no cover
    raise SystemExit("flask is required: pip install -r requirements.txt") from exc

import config
from manifest import Manifest

HERE = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__)


# -- data access ---------------------------------------------------------------
def manifest_rows() -> list[dict]:
    try:
        with open(config.manifest_path()) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    rows = []
    for doc_id, e in data.items():
        rows.append({
            "doc_id": doc_id,
            "dest_path": e.get("dest_path", ""),
            "uploaded_at": e.get("uploaded_at", ""),
            "size": e.get("size", 0),
        })
    rows.sort(key=lambda r: r["dest_path"])
    return rows


def manifest_count() -> int:
    try:
        with open(config.manifest_path()) as f:
            return len(json.load(f))
    except (OSError, ValueError):
        return 0


def load_runs() -> list[dict]:
    p = config.runs_path()
    runs = []
    try:
        with open(p) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    runs.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    runs.reverse()  # newest first
    return runs


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


# -- routes: read-only views ---------------------------------------------------
@app.get("/api/manifest")
def api_manifest():
    rows = manifest_rows()
    return jsonify({"count": len(rows), "rows": rows})


@app.get("/api/runs")
def api_runs():
    return jsonify({"runs": load_runs()})


@app.get("/api/reconcile")
def api_reconcile():
    """Compare the manifest against the LIVE SharePoint library via Graph."""
    try:
        from graph_client import GraphClient, GraphError
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"Graph client unavailable: {exc}"}), 500
    try:
        client = GraphClient()
        live = client.list_files()
    except Exception as exc:  # noqa: BLE001 -- includes ConfigError, GraphError
        return jsonify({"error": f"Could not list SharePoint: {exc}"}), 502
    id_by_path = Manifest(config.manifest_path()).doc_id_by_path()
    live_paths = {it["path"] for it in live}
    missing = sorted(set(id_by_path) - live_paths)                 # in manifest, not in library
    orphans = sorted(p for p in live_paths if p not in id_by_path)  # in library, not in manifest
    return jsonify({
        "root_folder": client.root_folder,
        "live_count": len(live),
        "manifest_count": len(id_by_path),
        "matched": len(live_paths & set(id_by_path)),
        "missing_in_sharepoint": [{"doc_id": id_by_path[p], "path": p} for p in missing],
        "orphans_in_sharepoint": [{"path": p} for p in orphans],
    })


# -- routes: resync ------------------------------------------------------------
def _read_resync_status() -> dict | None:
    try:
        with open(config.resync_status_path()) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


@app.get("/api/status")
def api_status():
    st = _read_resync_status()
    if not st:
        return jsonify({"state": "idle"})
    pid = st.get("pid")
    running = bool(pid) and _pid_alive(pid)
    started = st.get("started_iso")
    elapsed = None
    if started:
        try:
            elapsed = round((datetime.now(timezone.utc)
                             - datetime.fromisoformat(started)).total_seconds())
        except ValueError:
            elapsed = None
    tail = ""
    log_path = st.get("log_path")
    if log_path and os.path.exists(log_path):
        try:
            with open(log_path) as f:
                tail = "".join(f.readlines()[-40:])
        except OSError:
            tail = ""
    return jsonify({
        "state": "running" if running else "finished",
        "archive_name": st.get("archive_name"),
        "started_iso": started,
        "elapsed_sec": elapsed,
        "uploaded_so_far": manifest_count(),
        "log_tail": tail,
    })


@app.post("/api/resync")
def api_resync():
    """Guarded full resync: archive the SharePoint root, clear state, launch --full."""
    body = request.get_json(silent=True) or {}
    if body.get("confirm") != "RESYNC":
        return jsonify({"error": "Confirmation required: send {\"confirm\": \"RESYNC\"}."}), 400

    st = _read_resync_status()
    if st and st.get("pid") and _pid_alive(st["pid"]):
        return jsonify({"error": "A resync is already running."}), 409

    # 1. Archive the current library contents by renaming the root folder IN PLACE.
    #    Do this first: if it fails, we have not cleared anything.
    try:
        from graph_client import GraphClient
        client = GraphClient()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        archive_name = client.rename_root_folder(f"{client.root_folder}_archive_{today}")
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"Archive (folder rename) failed; nothing changed: {exc}"}), 502

    # 2. Clear the manifest and last-run marker so the full sync re-considers everything.
    try:
        with open(config.manifest_path(), "w") as f:
            f.write("{}")
        state = config.state_path()
        if os.path.exists(state):
            os.remove(state)
    except OSError as exc:
        return jsonify({"error": f"Root archived to '{archive_name}', but clearing local "
                                 f"state failed: {exc}. Fix, then run canoe_sync.py --full."}), 500

    # 3. Launch canoe_sync.py --full in the background (inherits our environment/secrets).
    os.makedirs(config.log_dir(), exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    log_path = os.path.join(config.log_dir(), f"resync_{stamp}.log")
    logf = open(log_path, "w")
    proc = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "canoe_sync.py"), "--full"],
        cwd=HERE, stdout=logf, stderr=subprocess.STDOUT,
        start_new_session=True, env=os.environ.copy(),
    )
    with open(config.resync_status_path(), "w") as f:
        json.dump({
            "pid": proc.pid,
            "archive_name": archive_name,
            "started_iso": datetime.now(timezone.utc).isoformat(),
            "log_path": log_path,
        }, f, indent=2)
    return jsonify({"state": "running", "archive_name": archive_name, "pid": proc.pid})


# -- the page ------------------------------------------------------------------
@app.get("/")
def index():
    return Response(PAGE, mimetype="text/html")


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Canoe Sync — Admin</title>
<style>
  :root {
    --bg:#0f1115; --panel:#171a21; --line:#2a2f3a; --fg:#e6e8ec; --muted:#9aa2b1;
    --accent:#4f8cff; --warn:#e0533d; --ok:#37c46f; --chip:#222734;
  }
  @media (prefers-color-scheme: light) {
    :root { --bg:#f4f5f7; --panel:#fff; --line:#e2e5ea; --fg:#1a1d22; --muted:#5b6472;
            --accent:#2563eb; --warn:#c53727; --ok:#1f9d57; --chip:#eef1f6; }
  }
  * { box-sizing:border-box; }
  body { margin:0; font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
         background:var(--bg); color:var(--fg); }
  header { padding:16px 20px; border-bottom:1px solid var(--line); display:flex;
           align-items:baseline; gap:14px; flex-wrap:wrap; }
  header h1 { font-size:16px; margin:0; font-weight:650; }
  header .sub { color:var(--muted); font-size:12.5px; }
  nav { display:flex; gap:4px; padding:10px 20px 0; border-bottom:1px solid var(--line); flex-wrap:wrap; }
  nav button { background:none; border:none; color:var(--muted); padding:8px 14px; cursor:pointer;
               font-size:13.5px; border-bottom:2px solid transparent; }
  nav button.active { color:var(--fg); border-bottom-color:var(--accent); }
  main { padding:20px; max-width:1200px; }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:16px; margin-bottom:16px; }
  .row { display:flex; gap:16px; flex-wrap:wrap; }
  .stat { background:var(--chip); border-radius:8px; padding:10px 14px; min-width:120px; }
  .stat b { display:block; font-size:20px; }
  .stat span { color:var(--muted); font-size:12px; }
  input[type=text] { background:var(--bg); border:1px solid var(--line); color:var(--fg);
                     border-radius:8px; padding:8px 12px; width:320px; max-width:100%; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th, td { text-align:left; padding:7px 10px; border-bottom:1px solid var(--line); vertical-align:top; }
  th { color:var(--muted); font-weight:600; position:sticky; top:0; background:var(--panel); cursor:pointer; }
  td.mono, .mono { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12px; }
  .tbl-wrap { overflow:auto; max-height:70vh; border:1px solid var(--line); border-radius:8px; }
  .muted { color:var(--muted); }
  .pill { display:inline-block; padding:1px 8px; border-radius:20px; font-size:11.5px; }
  .pill.full { background:rgba(79,140,255,.16); color:var(--accent); }
  .pill.incremental { background:var(--chip); color:var(--muted); }
  .pill.err { background:rgba(224,83,61,.16); color:var(--warn); }
  .pill.ok { background:rgba(55,196,111,.16); color:var(--ok); }
  button.btn { background:var(--accent); color:#fff; border:none; border-radius:8px; padding:9px 16px;
               font-size:13.5px; cursor:pointer; }
  button.btn.danger { background:var(--warn); }
  button.btn[disabled] { opacity:.5; cursor:not-allowed; }
  .danger-box { border:1px solid var(--warn); border-radius:10px; padding:16px; background:rgba(224,83,61,.06); }
  pre.log { background:var(--bg); border:1px solid var(--line); border-radius:8px; padding:12px;
            font-family:ui-monospace,Menlo,monospace; font-size:12px; max-height:320px; overflow:auto; white-space:pre-wrap; }
  .spinner { display:inline-block; width:14px; height:14px; border:2px solid var(--muted);
             border-top-color:transparent; border-radius:50%; animation:spin .7s linear infinite; vertical-align:-2px; }
  @keyframes spin { to { transform:rotate(360deg); } }
  .hidden { display:none; }
</style>
</head>
<body>
<header>
  <h1>Canoe → SharePoint Sync</h1>
  <span class="sub" id="dataDir"></span>
</header>
<nav>
  <button data-tab="manifest" class="active">Manifest</button>
  <button data-tab="runs">Run history</button>
  <button data-tab="reconcile">Reconcile</button>
  <button data-tab="resync">Resync</button>
</nav>
<main>

  <section id="tab-manifest">
    <div class="card">
      <div class="row" style="align-items:center; justify-content:space-between;">
        <div class="row">
          <div class="stat"><b id="mCount">—</b><span>documents in manifest</span></div>
        </div>
        <input type="text" id="mFilter" placeholder="Filter by path or doc id…">
      </div>
    </div>
    <div class="card">
      <div class="tbl-wrap">
        <table id="mTable">
          <thead><tr><th data-k="dest_path">Destination path</th><th data-k="uploaded_at">Uploaded</th>
          <th data-k="size">Size</th><th data-k="doc_id">Canoe doc id</th></tr></thead>
          <tbody></tbody>
        </table>
      </div>
      <p class="muted" id="mShown" style="margin:10px 2px 0;"></p>
    </div>
  </section>

  <section id="tab-runs" class="hidden">
    <div class="card">
      <div class="tbl-wrap">
        <table id="rTable">
          <thead><tr><th>Started (UTC)</th><th>Mode</th><th>Fetched</th><th>Uploaded</th>
          <th>Skipped</th><th>Errors</th><th>Duration</th><th>Result</th></tr></thead>
          <tbody></tbody>
        </table>
      </div>
      <p class="muted" id="rEmpty" style="margin:10px 2px 0;"></p>
    </div>
  </section>

  <section id="tab-reconcile" class="hidden">
    <div class="card">
      <p class="muted">Lists what is <b>actually</b> in the SharePoint library (live, via Microsoft Graph)
      and compares it to the manifest — the real verification of "what's been synced". This crawls the
      library, so it can take a moment.</p>
      <button class="btn" id="recRun">Run reconcile</button>
      <span id="recBusy" class="hidden"><span class="spinner"></span> listing SharePoint…</span>
    </div>
    <div id="recResult" class="hidden">
      <div class="card"><div class="row">
        <div class="stat"><b id="recLive">—</b><span>files in SharePoint</span></div>
        <div class="stat"><b id="recManifest">—</b><span>in manifest</span></div>
        <div class="stat"><b id="recMatched">—</b><span>matched</span></div>
        <div class="stat"><b id="recMissing">—</b><span>missing in SharePoint</span></div>
        <div class="stat"><b id="recOrphan">—</b><span>in SharePoint, not manifest</span></div>
      </div></div>
      <div class="card" id="recMissingCard">
        <h3 style="margin-top:0;">Missing in SharePoint <span class="pill err">recorded as uploaded, not present</span></h3>
        <div class="tbl-wrap"><table id="recMissTable">
          <thead><tr><th>Path</th><th>Canoe doc id</th></tr></thead><tbody></tbody></table></div>
      </div>
      <div class="card" id="recOrphanCard">
        <h3 style="margin-top:0;">In SharePoint, not in manifest</h3>
        <div class="tbl-wrap"><table id="recOrphanTable">
          <thead><tr><th>Path</th></tr></thead><tbody></tbody></table></div>
      </div>
    </div>
    <div id="recError" class="card hidden" style="border-color:var(--warn);"></div>
  </section>

  <section id="tab-resync" class="hidden">
    <div class="card danger-box">
      <h3 style="margin-top:0; color:var(--warn);">Full resync — consequential</h3>
      <p>This will, in order:</p>
      <ol>
        <li>Rename the current SharePoint root folder to <span class="mono">&lt;root&gt;_archive_&lt;today&gt;</span>
            (in place — nothing is deleted, all existing files move under the archived name).</li>
        <li>Clear <span class="mono">manifest.json</span> and <span class="mono">last_sync.json</span>.</li>
        <li>Launch <span class="mono">canoe_sync.py --full</span> in the background — it re-downloads and
            re-uploads every document. At Canoe's ~60 calls/min limit, a full library (~9,800 docs)
            takes <b>a few hours</b>.</li>
      </ol>
      <p>Type <span class="mono">RESYNC</span> to confirm, then start.</p>
      <div class="row" style="align-items:center;">
        <input type="text" id="confirmBox" placeholder="RESYNC" autocomplete="off">
        <button class="btn danger" id="resyncBtn" disabled>Archive &amp; start full resync</button>
      </div>
      <p class="muted" id="resyncMsg" style="margin-bottom:0;"></p>
    </div>
    <div class="card" id="resyncStatus">
      <h3 style="margin-top:0;">Status</h3>
      <div id="statusBody" class="muted">Checking…</div>
      <pre class="log hidden" id="statusLog"></pre>
    </div>
  </section>

</main>
<script>
const $ = s => document.querySelector(s);
const fmtSize = n => { if (!n) return "—"; const u=["B","KB","MB","GB"]; let i=0,x=n;
  while (x>=1024 && i<u.length-1){x/=1024;i++;} return x.toFixed(x<10&&i>0?1:0)+" "+u[i]; };
const esc = s => (s||"").replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));

// -- tabs --
let statusTimer = null;
document.querySelectorAll('nav button').forEach(b => b.onclick = () => {
  document.querySelectorAll('nav button').forEach(x => x.classList.remove('active'));
  b.classList.add('active');
  ['manifest','runs','reconcile','resync'].forEach(t =>
    $('#tab-'+t).classList.toggle('hidden', t !== b.dataset.tab));
  if (b.dataset.tab === 'runs') loadRuns();
  if (b.dataset.tab === 'resync') { pollStatus(); statusTimer = setInterval(pollStatus, 3000); }
  else if (statusTimer) { clearInterval(statusTimer); statusTimer = null; }
});

// -- manifest --
let manifestRows = [];
async function loadManifest() {
  const d = await (await fetch('api/manifest')).json();
  manifestRows = d.rows; $('#mCount').textContent = d.count.toLocaleString();
  renderManifest();
}
function renderManifest() {
  const q = $('#mFilter').value.toLowerCase();
  const rows = q ? manifestRows.filter(r =>
    r.dest_path.toLowerCase().includes(q) || r.doc_id.toLowerCase().includes(q)) : manifestRows;
  const shown = rows.slice(0, 2000);
  $('#mTable').querySelector('tbody').innerHTML = shown.map(r =>
    `<tr><td class="mono">${esc(r.dest_path)}</td><td>${esc((r.uploaded_at||'').slice(0,19).replace('T',' '))}</td>
     <td>${fmtSize(r.size)}</td><td class="mono muted">${esc(r.doc_id)}</td></tr>`).join('');
  $('#mShown').textContent = `Showing ${shown.length.toLocaleString()} of ${rows.length.toLocaleString()}`
    + (rows.length > shown.length ? ' (refine the filter to see more)' : '');
}
$('#mFilter').oninput = renderManifest;

// -- runs --
async function loadRuns() {
  const d = await (await fetch('api/runs')).json();
  const tb = $('#rTable').querySelector('tbody');
  if (!d.runs.length) { tb.innerHTML=''; $('#rEmpty').textContent = 'No runs recorded yet.'; return; }
  $('#rEmpty').textContent = '';
  tb.innerHTML = d.runs.map(r => {
    const mode = (r.mode||'').replace(/[^a-z]/g,'') || 'incremental';
    const pill = mode === 'full' ? 'full' : (r.errors ? 'err' : 'incremental');
    const dur = r.duration_sec >= 60 ? (r.duration_sec/60).toFixed(1)+' min' : (r.duration_sec||0)+' s';
    const res = r.errors ? `<span class="pill err">${r.errors} error(s)</span>` : `<span class="pill ok">ok</span>`;
    return `<tr><td>${esc((r.run_start||'').slice(0,19).replace('T',' '))}</td>
      <td><span class="pill ${pill}">${esc(r.mode||'')}</span></td>
      <td>${r.fetched??'—'}</td><td>${r.uploaded??'—'}</td><td>${r.skipped??'—'}</td>
      <td>${r.errors??'—'}</td><td>${dur}</td><td>${res}</td></tr>`;
  }).join('');
}

// -- reconcile --
$('#recRun').onclick = async () => {
  $('#recBusy').classList.remove('hidden'); $('#recRun').disabled = true;
  $('#recResult').classList.add('hidden'); $('#recError').classList.add('hidden');
  try {
    const r = await fetch('api/reconcile'); const d = await r.json();
    if (!r.ok) throw new Error(d.error || ('HTTP '+r.status));
    $('#recLive').textContent = d.live_count.toLocaleString();
    $('#recManifest').textContent = d.manifest_count.toLocaleString();
    $('#recMatched').textContent = d.matched.toLocaleString();
    $('#recMissing').textContent = d.missing_in_sharepoint.length.toLocaleString();
    $('#recOrphan').textContent = d.orphans_in_sharepoint.length.toLocaleString();
    $('#recMissTable').querySelector('tbody').innerHTML = d.missing_in_sharepoint.length
      ? d.missing_in_sharepoint.map(m => `<tr><td class="mono">${esc(m.path)}</td><td class="mono muted">${esc(m.doc_id)}</td></tr>`).join('')
      : '<tr><td colspan="2" class="muted">None — every manifest entry is present in SharePoint.</td></tr>';
    $('#recOrphanTable').querySelector('tbody').innerHTML = d.orphans_in_sharepoint.length
      ? d.orphans_in_sharepoint.map(m => `<tr><td class="mono">${esc(m.path)}</td></tr>`).join('')
      : '<tr><td class="muted">None — nothing in SharePoint is missing from the manifest.</td></tr>';
    $('#recResult').classList.remove('hidden');
  } catch (e) {
    $('#recError').textContent = 'Reconcile failed: ' + e.message; $('#recError').classList.remove('hidden');
  } finally { $('#recBusy').classList.add('hidden'); $('#recRun').disabled = false; }
};

// -- resync --
$('#confirmBox').oninput = e => $('#resyncBtn').disabled = (e.target.value !== 'RESYNC');
$('#resyncBtn').onclick = async () => {
  $('#resyncBtn').disabled = true; $('#resyncMsg').textContent = 'Starting…';
  try {
    const r = await fetch('api/resync', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({confirm:'RESYNC'})});
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || ('HTTP '+r.status));
    $('#resyncMsg').textContent = `Archived to "${d.archive_name}". Full resync running (pid ${d.pid}).`;
    $('#confirmBox').value = '';
    pollStatus();
  } catch (e) { $('#resyncMsg').textContent = 'Error: ' + e.message; $('#resyncBtn').disabled = false; }
};
async function pollStatus() {
  let d; try { d = await (await fetch('api/status')).json(); } catch { return; }
  const body = $('#statusBody'), log = $('#statusLog');
  if (d.state === 'idle') { body.innerHTML = '<span class="muted">No resync has been run.</span>'; log.classList.add('hidden'); return; }
  const mins = d.elapsed_sec != null ? (d.elapsed_sec/60).toFixed(1) : '—';
  if (d.state === 'running') {
    body.innerHTML = `<span class="spinner"></span> <b>Running</b> — archived to
      <span class="mono">${esc(d.archive_name||'')}</span>. Uploaded so far:
      <b>${(d.uploaded_so_far||0).toLocaleString()}</b>. Elapsed: ${mins} min.`;
  } else {
    body.innerHTML = `<span class="pill ok">finished</span> Last resync archived
      <span class="mono">${esc(d.archive_name||'')}</span>. Manifest now holds
      <b>${(d.uploaded_so_far||0).toLocaleString()}</b> documents. (See Run history for the result.)`;
  }
  if (d.log_tail) { log.textContent = d.log_tail; log.classList.remove('hidden'); }
}

loadManifest();
</script>
</body>
</html>
"""


def main() -> None:
    port = int(os.environ.get("CANOE_DASHBOARD_PORT", "8765"))
    # Bind to localhost only: this is an admin surface (it can trigger a resync) and must
    # not be exposed on the network. Reach it from another machine via an SSH tunnel.
    host = os.environ.get("CANOE_DASHBOARD_HOST", "127.0.0.1")
    try:
        data_dir = config.data_dir()
    except Exception:  # noqa: BLE001
        data_dir = "(unset)"
    print(f"Canoe sync dashboard on http://{host}:{port}  (data dir: {data_dir})")
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    main()
