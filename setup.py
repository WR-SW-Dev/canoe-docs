#!/usr/bin/env python3
"""
setup.py -- First-run credential setup wizard for Canoe Document Automation.

Starts a LOCAL web server (bound to 127.0.0.1 only), serves a simple form to enter
the Canoe API credentials, and writes them to `py files/.env` with owner-only
permissions (chmod 600). Nothing is transmitted anywhere -- the values are written
to this machine's secrets file and the server shuts down as soon as they're saved.

Run:
    python setup.py
Then open the printed URL (it also tries to open your browser automatically),
fill in the form, and submit.

This does NOT email or send credentials. Getting the credentials onto the machine
(e.g. via encrypted email or a password manager) is a separate, human step.
"""

import html
import os
import threading
import urllib.parse
import webbrowser
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(HERE, "py files", ".env")
HOST, PORT = "127.0.0.1", 8765

# (key, label, required, is_password)
FIELDS = [
    ("CANOE_CLIENT_ID", "Canoe Client ID", True, False),
    ("CANOE_CLIENT_SECRET", "Canoe Client Secret", True, True),
    ("CANOE_USERNAME", "Service-account username (fallback auth, optional)", False, False),
    ("CANOE_PASSWORD", "Service-account password (fallback auth, optional)", False, True),
    ("CANOE_ORGANIZATION_ID", "Organization ID (only if your login has multiple orgs)", False, False),
]

PAGE_CSS = """
  :root{color-scheme:light dark}
  body{font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;max-width:560px;
       margin:6vh auto;padding:0 20px;line-height:1.5;background:#f5f6f2;color:#1b2420}
  @media (prefers-color-scheme:dark){body{background:#12161a;color:#e7e9e2}}
  .card{background:#fff;border:1px solid #d8d9d2;border-radius:12px;padding:26px 28px}
  @media (prefers-color-scheme:dark){.card{background:#1a2024;border-color:#2a3128}}
  h1{font-size:22px;margin:0 0 4px} p.sub{color:#4b554e;margin:0 0 22px;font-size:14px}
  @media (prefers-color-scheme:dark){p.sub{color:#a9b0a5}}
  label{display:block;font-size:13px;font-weight:600;margin:14px 0 4px}
  input{width:100%;box-sizing:border-box;padding:9px 11px;border:1px solid #c9cbc2;
        border-radius:7px;font-size:14px;background:transparent;color:inherit}
  .req{color:#a3432f}
  button{margin-top:22px;width:100%;padding:11px;border:0;border-radius:8px;
         background:#2f6f62;color:#fff;font-size:15px;font-weight:600;cursor:pointer}
  .note{font-size:12px;color:#4b554e;margin-top:16px}
  @media (prefers-color-scheme:dark){.note{color:#a9b0a5}}
"""


def form_html(error: str = "") -> str:
    rows = []
    for key, label, required, is_pw in FIELDS:
        star = ' <span class="req">*</span>' if required else ""
        typ = "password" if is_pw else "text"
        rows.append(
            f'<label for="{key}">{html.escape(label)}{star}</label>'
            f'<input type="{typ}" id="{key}" name="{key}" autocomplete="off" spellcheck="false">'
        )
    err = f'<p style="color:#a3432f;font-size:14px">{html.escape(error)}</p>' if error else ""
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Canoe setup</title>
<style>{PAGE_CSS}</style></head><body><div class="card">
<h1>Canoe API credentials</h1>
<p class="sub">Enter the Canoe credentials. They're written only to this machine's
secrets file — nothing is sent anywhere. Provide the Client ID + Secret, and/or the
service-account username + password.</p>
{err}
<form method="POST" action="/save">
{''.join(rows)}
<button type="submit">Save to .env</button>
</form>
<p class="note">Fields marked <span class="req">*</span> are the recommended pair.
The server shuts down automatically once saved.</p>
</div></body></html>"""


def success_html(saved_keys) -> str:
    items = "".join(f"<li><code>{html.escape(k)}</code></li>" for k in saved_keys)
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Saved</title>
<style>{PAGE_CSS}</style></head><body><div class="card">
<h1>✓ Credentials saved</h1>
<p class="sub">Written to <code>{html.escape(ENV_PATH)}</code> (permissions set to owner-only).</p>
<p>Keys saved:</p><ul>{items}</ul>
<p class="note">You can close this tab. Next: verify with
<code>cd "py files" &amp;&amp; ../.venv/bin/python credentials_check.py</code></p>
</div></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # suppress request logging so no form data leaks to the console

    def _send(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, form_html())
        else:
            self._send(404, "<h1>Not found</h1>")

    def do_POST(self):
        if self.path != "/save":
            self._send(404, "<h1>Not found</h1>")
            return
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8")
        posted = {k: v[0].strip() for k, v in urllib.parse.parse_qs(raw).items()}

        have_client = posted.get("CANOE_CLIENT_ID") and posted.get("CANOE_CLIENT_SECRET")
        have_pw = posted.get("CANOE_USERNAME") and posted.get("CANOE_PASSWORD")
        if not (have_client or have_pw):
            self._send(400, form_html(
                "Please provide either Client ID + Secret, or username + password."))
            return

        lines, saved_keys = [], []
        for key, _, _, _ in FIELDS:
            val = posted.get(key, "")
            if val:
                lines.append(f"{key}={val}")
                saved_keys.append(key)

        os.makedirs(os.path.dirname(ENV_PATH), exist_ok=True)
        # Write with owner-only perms from the start.
        fd = os.open(ENV_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write("# Canoe API credentials -- written by setup.py. Never commit this file.\n")
            f.write("\n".join(lines) + "\n")
        os.chmod(ENV_PATH, 0o600)

        self._send(200, success_html(saved_keys))
        # Shut the server down shortly after responding (from a separate thread).
        threading.Thread(target=self.server.shutdown, daemon=True).start()


def main():
    if os.path.exists(ENV_PATH):
        print(f"Note: {ENV_PATH} already exists and will be OVERWRITTEN when you submit.\n")
    url = f"http://{HOST}:{PORT}/"
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print("Canoe credential setup is running locally (this machine only).")
    print(f"  Open:  {url}")
    print("  Enter the credentials in the page, then submit. The server stops automatically after saving.")
    print("  (Press Ctrl+C to cancel.)\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nCancelled — no changes written.")
    print("Setup server stopped.")


if __name__ == "__main__":
    main()
