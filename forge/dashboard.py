"""Local dashboard: use the app, review proposed changes, watch health. Listens on 127.0.0.1 only."""
import hmac
import html
import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import improve, release, router, runtime, sandbox, store
from .store import q, x

PORT = 8765
TOKEN = secrets.token_hex(16)  # in every form, so another website cannot press these buttons for you
e = html.escape

CSS = """
:root{color-scheme:light dark;--line:#8884;--accent:#2563eb;--bad:#b91c1c;--ok:#15803d}
body{font:16px/1.5 system-ui,sans-serif;margin:0}
nav{display:flex;gap:4px;padding:8px 16px;border-bottom:1px solid var(--line);flex-wrap:wrap}
nav a{padding:6px 12px;border-radius:6px;text-decoration:none;color:inherit}
nav a[aria-current]{background:var(--accent);color:#fff}
main{max-width:860px;margin:0 auto;padding:16px}
label{display:block;font-weight:600;margin:12px 0 4px}
textarea,input[type=text]{width:100%;box-sizing:border-box;padding:8px;font:inherit;border:1px solid var(--line);border-radius:6px}
button{font:inherit;padding:8px 16px;margin:12px 8px 0 0;border-radius:6px;border:1px solid var(--accent);background:var(--accent);color:#fff;cursor:pointer}
button.quiet{background:transparent;color:inherit;border-color:var(--line)}
button.danger{background:var(--bad);border-color:var(--bad)}
pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#8881;padding:12px;border-radius:6px;font-size:14px}
table{border-collapse:collapse;width:100%}td,th{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line)}
section.card{border:1px solid var(--line);border-radius:8px;padding:4px 16px 16px;margin:16px 0}
.bad{color:var(--bad)}.ok{color:var(--ok)}.muted{opacity:.7}form.inline{display:inline}
"""


def page(title, body, current="/"):
    waiting = q("select count(*) n from proposals where status='awaiting'")[0]["n"]
    links = [("/", "Use the app"), ("/review", f"Review changes ({waiting})"), ("/health", "Health")]
    nav = "".join(f'<a href="{h}"{" aria-current=page" if h == current else ""}>{e(t)}</a>' for h, t in links)
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(title)} - Forge</title>'
            f'<style>{CSS}</style></head><body><nav aria-label="Main">{nav}</nav><main><h1>{e(title)}</h1>{body}</main></body></html>')


def form(action, inner, cls=""):
    return f'<form method="post" action="{action}" class="{cls}"><input type="hidden" name="t" value="{TOKEN}">{inner}</form>'


def hidden(name, value):
    return f'<input type="hidden" name="{name}" value="{e(str(value))}">'


def when(ts):
    return time.strftime("%d %b %H:%M", time.localtime(ts)) if ts else "never"


# ---------- pages ----------

def app_page(app, values=None, result=None, note=""):
    mod = runtime.load_app(store.ROOT, app)
    fields = hidden("app", app)
    for name, label, kind in mod.FIELDS:
        v = e((values or {}).get(name, ""))
        fields += f'<label for="{name}">{e(label)}</label>'
        fields += (f'<textarea id="{name}" name="{name}" rows="10">{v}</textarea>' if kind == "textarea"
                   else f'<input type="text" id="{name}" name="{name}" value="{v}">')
    others = [a for a in runtime.apps() if a != app]
    if others:
        note += "<p>Other apps: " + ", ".join(f'<a href="/?app={a}">{e(a)}</a>' for a in others) + "</p>"
    body = note + form("/ask", fields + "<button>Run</button> <span class=muted>Local models can take up to a minute.</span>")
    if result and result["ok"]:
        out = result["output"]
        text = out.get("answer", json.dumps(out, indent=2)) if isinstance(out, dict) else str(out)
        body += (f'<section class="card" aria-live="polite"><h2>Result</h2><pre>{e(text)}</pre>'
                 f'<p class="muted">{result["ms"] / 1000:.1f} s, cost ${result["usd"]:.4f}</p>'
                 + form("/feedback", hidden("trace", result["trace_id"]) + hidden("app", app)
                        + '<label for="fb">Was this good? If not, say what was wrong (optional)</label>'
                        '<input type="text" id="fb" name="text" maxlength="500">'
                        '<button name="rating" value="1">Good</button>'
                        '<button name="rating" value="-1" class="quiet">Not good</button>') + "</section>")
    elif result:
        body += f'<p class="bad" role="alert">{e(result["error"])}</p>'
    return page(app, body, "/")


def score_table(before, after):
    if not before or not after:
        return ""
    rows = [("Golden cases passing", f"{before['success']:.0%}", f"{after['success']:.0%}"),
            ("Slowest request", f"{before['p95_ms'] / 1000:.1f} s", f"{after['p95_ms'] / 1000:.1f} s"),
            ("Cost per request", f"${before['usd_per_req']:.4f}", f"${after['usd_per_req']:.4f}")]
    return ("<table><tr><th>Measure</th><th>Now</th><th>With this change</th></tr>"
            + "".join(f"<tr><td>{a}</td><td>{b}</td><td>{c}</td></tr>" for a, b, c in rows) + "</table>")


def details(title, text):
    return f"<details><summary>{e(title)}</summary><pre>{e(text)}</pre></details>" if text else ""


def proposal_card(p):
    g = json.loads(p["gate"] or "{}")
    issue = q("select * from issues where id=?", p["issue_id"]) if p["issue_id"] else []
    risk = ("Code change: read the diff carefully." if p["channel"] in ("code", "agent")
            else "Low risk: only wording or model order changes. No code.")
    if g.get("warning"):
        risk += f' <strong class="bad">{e(g["warning"])}</strong>'
    body = f'<section class="card"><h2>Change {p["id"]}: {e(p["summary"] or "")}</h2>'
    body += f'<p><strong>{e(p["app"])}</strong>, {e(p["channel"])} change, {p["attempts"] or 1} attempt(s). {risk}</p>'
    if issue:
        body += f'<p>Why: {e(issue[0]["title"])} (seen {issue[0]["count"]} times).</p>'
    if p["reason"]:
        body += f'<p class="bad">{e(p["reason"])}</p>'
    body += score_table(g.get("before"), g.get("after"))
    body += f'<details open><summary>What changes</summary><pre>{e(p["diff"] or "")}</pre></details>'
    body += details("Test output", g.get("tests")) + details("Security scans", g.get("scans"))
    if p["status"] == "awaiting":
        body += form("/approve", hidden("id", p["id"]) + "<button>Approve and start trial</button>", "inline")
        body += form("/reject", hidden("id", p["id"])
                     + f'<label for="r{p["id"]}">Reason for rejecting (it will be remembered)</label>'
                     f'<input type="text" id="r{p["id"]}" name="reason" maxlength="300">'
                     '<button class="danger">Reject</button>')
    return body + "</section>"


def review_page():
    body = ""
    c = store.kv_get("canary")
    if c:
        n = q("select count(*) n from traces where version=? and ts>=?", f"canary:p{c['proposal']}", c["since"])[0]["n"]
        body += (f'<p role="status">Change {c["proposal"]} is in its trial: {n} of {c["need"]} trial requests so far. '
                 f'It handles about {c["pct"]}% of requests and is discarded automatically if they fail.</p>')
    waiting = q("select * from proposals where status='awaiting' order by id")
    low = [p for p in waiting if p["channel"] != "code"]
    body += "".join(proposal_card(p) for p in waiting) or "<p>Nothing is waiting for your approval.</p>"
    if len(low) > 1:
        body += "<p class=muted>Low-risk changes still go through the trial one at a time.</p>"
    stuck = q("select * from proposals where status='escalated' order by id desc limit 5")
    if stuck:
        body += "<h2>Needs your help</h2>" + "".join(
            f'<section class="card"><h3>{e(p["summary"] or "")}</h3><p>{e(p["reason"] or "")}</p>'
            + form("/reopen", hidden("id", p["issue_id"]) + '<button class="quiet">Let it try again</button>', "inline")
            + "</section>" for p in stuck)
    done = q("select * from proposals where status in ('shipped','rejected','rolled_back') order by id desc limit 10")
    if done:
        body += "<h2>History</h2><table><tr><th>Change</th><th>Outcome</th><th>Note</th><th></th></tr>"
        for p in done:
            undo = form("/undo", hidden("id", p["id"]) + '<button class="quiet">Undo</button>', "inline") if p["status"] == "shipped" else ""
            body += (f'<tr><td>{p["id"]}: {e(p["summary"] or "")}</td><td>{e(p["status"].replace("_", " "))}</td>'
                     f'<td>{e(p["reason"] or "")}</td><td>{undo}</td></tr>')
        body += "</table>"
    return page("Review changes", body, "/review")


def health_page():
    cfg = router.config()
    kill = store.kv_get("kill", False)
    last = store.kv_get("last_cycle", {"ts": 0, "result": "not run yet"})
    body = f'<p>Paid model spend today: <strong>${router.spent_today():.2f}</strong> of ${cfg["budget"]["daily_usd"]:.2f} allowed. Local models are free.</p>'
    roles = cfg["models"]
    body += ("<p>Models: " + ", ".join(f"{e(r)} = {e(m['model'])}" for r, m in roles.items()) + ".</p>" if roles else
             '<p class="bad">No models are set up, so apps that use a model will not work. '
             "Apps that use no model are fine. Run <code>python -m forge.cli setup</code> to choose models.</p>")
    body += f'<p>Sandbox (Docker): {"<span class=ok>running</span>" if sandbox.available() else "<span class=bad>not running</span>. Code fixes are paused until it is; prompt and model-order improvements still work."}</p>'
    body += f'<p>Improvement engine: {"<span class=bad>stopped by you</span>" if kill else "on"}. Last run {when(last["ts"])}: {e(str(last["result"]))}</p>'
    body += form("/run", '<button class="quiet">Run an improvement cycle now</button>', "inline")
    body += form("/kill", hidden("on", "0" if kill else "1")
                 + (f'<button class="quiet">Turn the improvement engine back on</button>' if kill
                    else '<button class="danger">Stop all self-improvement</button>'), "inline")
    body += "<h2>Scorecard</h2><table><tr><th>When</th><th>App</th><th>Golden cases passing</th><th>Slowest</th><th>Cost per request</th></tr>"
    for r in q("select * from scorecards where version='stable' order by id desc limit 8"):
        c = json.loads(r["card"])
        body += f'<tr><td>{when(r["ts"])}</td><td>{e(r["app"])}</td><td>{c["success"]:.0%} of {c["n"]}</td><td>{c["p95_ms"] / 1000:.1f} s</td><td>${c["usd_per_req"]:.4f}</td></tr>'
    body += "</table><h2>Problems found</h2><table><tr><th>Problem</th><th>Type</th><th>Seen</th><th>Last</th><th>Status</th></tr>"
    for r in q("select * from issues order by last_ts desc limit 20"):
        body += f'<tr><td>{e(r["title"])}</td><td>{e(r["kind"])}</td><td>{r["count"]}</td><td>{when(r["last_ts"])}</td><td>{e(r["status"])}</td></tr>'
    body += "</table><h2>Recent requests</h2><table><tr><th>When</th><th>App</th><th>Version</th><th>Result</th><th>Time</th></tr>"
    for r in q("select * from traces where version!='eval' order by ts desc limit 15"):
        body += f'<tr><td>{when(r["ts"])}</td><td>{e(r["app"])}</td><td>{e(r["version"])}</td><td>{"ok" if r["ok"] else e((r["fingerprint"] or r["error"] or "")[:80])}</td><td>{r["ms"] / 1000:.1f} s</td></tr>'
    return page("Health", body + "</table>", "/health")


# ---------- background work ----------

def run_cycle():
    store.kv_set("last_cycle", {"ts": time.time(), "result": "running now"})
    try:
        result = improve.cycle()
    except Exception as err:
        result = f"failed: {type(err).__name__}: {err}"
    store.kv_set("last_cycle", {"ts": time.time(), "result": result})


def scheduler():
    if not store.kv_get("last_cycle"):
        store.kv_set("last_cycle", {"ts": time.time(), "result": "not run yet"})  # first automatic run is a full interval away
    while True:
        time.sleep(60)
        try:
            due = time.time() - store.kv_get("last_cycle")["ts"] > router.config()["improve"]["interval_hours"] * 3600
            if due and not store.kv_get("kill", False):
                run_cycle()
        except Exception:
            pass  # a broken cycle must never take the app down; the next tick retries


# ---------- http ----------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, body, code=200):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, to):
        self.send_response(303)
        self.send_header("Location", to)
        self.end_headers()

    def local(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def do_GET(self):
        path = urlparse(self.path).path
        if not self.local():
            return self.reply("Forbidden", 403)
        if path == "/":
            apps = runtime.apps()
            app = parse_qs(urlparse(self.path).query).get("app", apps[:1] or [""])[0]
            self.reply(app_page(app) if app in apps else page(
                "No apps yet", "<p>Connect Claude Code or another agent to build one. See the README.</p>"))
        elif path == "/review":
            self.reply(review_page())
        elif path == "/health":
            self.reply(health_page())
        else:
            self.reply(page("Not found", '<p>That page does not exist. <a href="/">Go to the app</a>.</p>'), 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not self.local() or n > 400_000:
            return self.reply(page("Could not do that", '<p class="bad" role="alert">That request was too large or not from this computer.</p>'), 400)
        f = {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode("utf-8", "replace"), keep_blank_values=True).items()}
        if not hmac.compare_digest(f.pop("t", ""), TOKEN):
            return self.reply(page("Page out of date", '<p role="alert">This page is from before the last restart. <a href="/">Reload it</a> and try again.</p>'), 403)
        path = urlparse(self.path).path
        try:
            if path == "/ask":
                app = f.pop("app", "")
                if app not in runtime.apps():
                    raise ValueError("Unknown app.")
                self.reply(app_page(app, f, runtime.handle(app, f)))
            elif path == "/feedback":
                x("insert into feedback(ts,trace_id,rating,text) values(?,?,?,?)", time.time(), f.get("trace", ""),
                  1 if f.get("rating") == "1" else -1, f.get("text", "")[:500])
                self.reply(app_page(f.get("app", ""), note='<p class="ok" role="status">Thank you. Your feedback was saved.</p>'))
            elif path == "/approve":
                release.approve(f["id"])
                self.redirect("/review")
            elif path == "/reject":
                release.reject(f["id"], f.get("reason", ""))
                self.redirect("/review")
            elif path == "/undo":
                release.undo(f["id"])
                self.redirect("/review")
            elif path == "/reopen":
                x("update issues set status='open' where id=? and status='hold'", f.get("id"))
                self.redirect("/review")
            elif path == "/kill":
                store.kv_set("kill", f.get("on") == "1")
                self.redirect("/health")
            elif path == "/run":
                threading.Thread(target=run_cycle, daemon=True).start()
                self.redirect("/health")
            else:
                self.reply(page("Not found", "<p>That action does not exist.</p>"), 404)
        except (ValueError, KeyError, runtime.InputError) as err:
            self.reply(page("Could not do that", f'<p class="bad" role="alert">{e(str(err))}</p><p><a href="/review">Back</a></p>'), 400)


def serve():
    print(f"Forge is running. Open http://127.0.0.1:{PORT} in your browser. Press Ctrl+C to stop.")
    ThreadingHTTPServer.allow_reuse_address = False  # on Windows, reuse lets a second copy share the port silently
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        print(f"Forge is already running at http://127.0.0.1:{PORT}.")
        return
    threading.Thread(target=scheduler, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")
