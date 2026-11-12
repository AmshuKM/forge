"""Local dashboard: use the app, review proposed changes, watch health. Listens on 127.0.0.1 only."""
import ast
import hmac
import html
import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import builder, improve, release, router, runtime, sandbox, store
from .store import q, x

PORT = 8765
TOKEN = secrets.token_hex(16)  # in every form, so another website cannot press these buttons for you
e = html.escape

# One tonal ramp (n0-n9) feeds both themes; navy rail and signal yellow are the board's own colours.
CSS = """
:root{--n0:#F7F9FB;--n1:#E8ECF1;--n2:#D5DCE5;--n3:#AEB8C6;--n4:#7D8896;--n5:#56606E;--n6:#3B4450;--n7:#2B323D;--n8:#1C2129;--n9:#12161C;
--rail:#0B2545;--signal:#FFC629;--danger:#B3261E;
--ground:var(--n1);--surface:var(--n0);--text:var(--n8);--muted:var(--n5);--line:var(--n2);--field:var(--n3);
--board:var(--n8);--tile:var(--n7);--board-line:var(--n7);--board-text:var(--n0);--board-muted:var(--n3);
--pass:#1E7A45;--fail:#B3261E;--pass-b:#7FE0A4;--fail-b:#FF8A80;
--act:var(--rail);--act-text:var(--n0);--focus:var(--rail);
--cond:"Barlow Condensed",Bahnschrift,"DIN Alternate","Avenir Next Condensed","Arial Narrow",system-ui,sans-serif;
--body:"Segoe UI",system-ui,-apple-system,"Helvetica Neue",sans-serif;
--mono:"Cascadia Mono",Consolas,"SF Mono",ui-monospace,monospace;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--ground:var(--n9);--surface:var(--n8);--text:var(--n1);--muted:var(--n3);--line:var(--n7);--field:var(--n6);
--board:var(--n7);--tile:var(--n6);--board-line:var(--n6);--pass:#7FE0A4;--fail:#FF8A80;--act:var(--n1);--act-text:var(--n9);--focus:var(--signal);color-scheme:dark}}
@font-face{font-family:"Barlow Condensed";src:url(/font/signage.ttf) format("truetype");font-weight:700;font-display:swap}
*{box-sizing:border-box}
body{margin:0;background:var(--ground);color:var(--text);font:16px/1.5 var(--body);font-variant-numeric:tabular-nums;caret-color:var(--text);scrollbar-color:var(--n4) transparent}
::selection{background:var(--signal);color:var(--n9)}
:focus-visible{outline:3px solid var(--focus);outline-offset:2px}
a{color:inherit;text-decoration-thickness:1px;text-underline-offset:3px}
.rail{background:var(--rail);display:flex;flex-wrap:wrap;column-gap:32px;padding:0 max(16px,calc((100% - 1120px) / 2))}
.rail .brand{font:700 1.125rem/1 var(--cond);letter-spacing:.2em;color:var(--n0);text-decoration:none;display:flex;align-items:center;margin-right:16px}
.rail a{color:var(--n2);font-weight:600;text-decoration:none;display:flex;align-items:center;gap:8px;padding:19px 0 16px;border-bottom:3px solid transparent}
.rail a:hover{color:var(--n0)}.rail a[aria-current]{color:var(--n0);border-bottom-color:var(--signal)}
.rail :focus-visible{outline-color:var(--signal)}
.badge{background:var(--signal);color:var(--n9);border-radius:3px;padding:1px 7px;font:700 .8125rem/1.4 var(--cond)}
main{max-width:1120px;margin:0 auto;padding:40px 16px 72px}
h1{font:700 2.25rem/1.1 var(--cond);margin:0 0 8px;text-wrap:balance}
h2{font:700 .875rem/1.2 var(--cond);letter-spacing:.14em;text-transform:uppercase;color:var(--muted);margin:40px 0 12px}
p,blockquote{max-width:70ch}
.plat{display:inline-grid;place-items:center;min-width:1.5em;padding:0 .18em;margin-right:.3em;background:var(--signal);color:var(--n9);border-radius:6px;line-height:1.25}
.meta{color:var(--muted);margin:0 0 20px}
blockquote.ask{margin:0 0 28px;padding:0;font-size:1rem;color:var(--text)}
blockquote.ask::before{content:"You asked for: ";font-weight:700}
label{display:block;font-weight:600;margin:24px 0 6px}
.hint{display:block;color:var(--muted);font-weight:400}
textarea,input[type=text]{width:100%;padding:10px 12px;font:inherit;color:var(--text);background:var(--surface);border:1px solid var(--field);border-radius:6px}
textarea{resize:vertical}::placeholder{color:var(--muted);opacity:1}
button,.button{display:inline-block;font:600 1rem/1 var(--body);padding:12px 20px;margin:16px 12px 0 0;border-radius:6px;border:1px solid var(--act);background:var(--act);color:var(--act-text);cursor:pointer;text-decoration:none}
button:hover,.button:hover{background:var(--n6);border-color:var(--n6);color:var(--n0)}
button.quiet{background:transparent;color:var(--text);border-color:var(--field)}
button.quiet:hover{background:var(--surface);color:var(--text)}
button.danger{background:var(--danger);border-color:var(--danger);color:var(--n0)}
pre{white-space:pre-wrap;overflow-wrap:anywhere;background:var(--surface);border:1px solid var(--line);padding:16px;border-radius:6px;font:.875rem/1.55 var(--mono);max-height:30rem;overflow:auto;margin:8px 0 16px}
code{font:.9em var(--mono)}
.tw{overflow-x:auto;margin:0 0 8px}
table{border-collapse:separate;border-spacing:0;width:100%;background:var(--surface);border:1px solid var(--line);border-radius:8px}
th{font:700 .8125rem/1.2 var(--cond);letter-spacing:.12em;text-transform:uppercase;color:var(--muted);text-align:left;padding:12px 16px;border-bottom:1px solid var(--line)}
td{padding:12px 16px;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:0}
section.card,.panel{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:4px 24px 24px;margin:24px 0}
.panel h2{margin-top:20px}
details{margin:4px 0}summary{cursor:pointer;font-weight:600;padding:8px 0}
.ok{color:var(--pass)}.bad{color:var(--fail)}.muted{color:var(--muted)}form.inline{display:inline}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset(50%);white-space:nowrap}
.split{display:grid;grid-template-columns:minmax(0,1fr) 320px;gap:40px;align-items:start}
.split aside .panel:first-child{margin-top:0}
.board{background:var(--board);color:var(--board-text);border:0}
.board th{color:var(--board-muted);border-bottom-color:var(--board-line);padding:14px 20px}
.board td{border-bottom-color:var(--board-line);padding:16px 20px;vertical-align:middle}
.board .t{font:700 1rem var(--mono);color:var(--signal)}
.board .what{font-weight:600}.board small{display:block;color:var(--board-muted);font-weight:400;font-size:1rem}
.board .dur{text-align:right;font-family:var(--mono);color:var(--board-muted);white-space:nowrap}
.flaps{display:inline-flex;gap:3px;white-space:nowrap}
.flaps i{font-style:normal;display:inline-grid;place-items:center;width:1.3em;height:2em;background:var(--tile);border-radius:3px;font:700 1.125rem/1 var(--cond);position:relative;isolation:isolate}
.flaps i::after{content:"";position:absolute;left:0;right:0;top:50%;height:1px;background:var(--board);z-index:-1}
.flaps.sm i{width:1.2em;height:1.65em;font-size:.875rem;background:var(--n8)}
.flaps.sm i::after{background:var(--n9)}
.s-pass i{color:var(--pass-b)}.s-fail i{color:var(--fail-b)}.s-run i{color:var(--signal)}.s-wait i{color:var(--board-muted)}
.flaps.sm.s-wait i{color:var(--n3)}
.flip i{animation:flip .55s cubic-bezier(.16,1,.3,1) both;animation-delay:calc(var(--i) * 45ms)}
@keyframes flip{from{transform:perspective(240px) rotateX(-75deg);filter:brightness(.6)}to{transform:none;filter:none}}
@media (prefers-reduced-motion:reduce){.flip i{animation:none}}
.done{display:flex;flex-wrap:wrap;align-items:center;gap:8px 16px;margin:0 0 24px}.done p{margin:0;font-weight:600}.done .button{margin:0}
.examples td{font-family:var(--mono)}
@media (max-width:900px){.split{grid-template-columns:1fr}}
@media (max-width:640px){h1{font-size:1.75rem}main{padding-top:28px}.rail{column-gap:16px}.rail .brand{width:100%;padding-top:16px}.rail a{padding:12px 0 10px;font-size:.875rem}
.board .tc,.board .dur,.board th.dc{display:none}.board td,.board th{padding:14px 12px}.flaps i{width:1.15em;height:1.7em;font-size:.875rem}}
@media (max-width:400px){.rail{column-gap:12px}}
"""


def page(title, body, current="/", refresh=0, refresh_url="", heading=None):
    waiting = q("select count(*) n from proposals where status='awaiting'")[0]["n"]
    badge = f' <span class="badge" aria-label="{waiting} waiting">{waiting}</span>' if waiting else ""
    links = [("/", "Build an app", ""), ("/apps", "My apps", ""), ("/review", "Review changes", badge), ("/health", "Health", "")]
    nav = "".join(f'<a href="{h}"{" aria-current=page" if h == current else ""}>{e(t)}{b}</a>' for h, t, b in links)
    meta = f'<meta http-equiv="refresh" content="{refresh}{";url=" + refresh_url if refresh_url else ""}">' if refresh else ""
    body = body.replace("<table", '<div class="tw"><table').replace("</table>", "</table></div>")  # tables scroll sideways on phones
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">{meta}'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(title)} - Forge</title>'
            f'<style>{CSS}</style></head><body><nav class="rail" aria-label="Main"><a class="brand" href="/">FORGE</a>{nav}</nav>'
            f'<main><h1>{heading or e(title)}</h1>{body}</main></body></html>')


def flaps(word, state, small=False, flip=False):
    """A status word set in split-flap tiles. The tiles are decoration; screen readers get the word once."""
    tiles = "".join(f'<i style="--i:{n}">{e(ch)}</i>' for n, ch in enumerate(word))
    cls = f'flaps s-{state}{" sm" if small else ""}{" flip" if flip else ""}'
    return f'<span class="{cls}" aria-hidden="true">{tiles}</span><span class="sr">{e(word.lower())}</span>'


STATUS_WORDS = {"testing": ("BUILDING", "run"), "awaiting": ("WAITING", "run"), "canary": ("TRIAL", "run"),
                "shipped": ("LIVE", "pass"), "escalated": ("STOPPED", "fail"), "rejected": ("REJECTED", "fail"),
                "rolled_back": ("UNDONE", "wait")}


def clock(ts):
    return time.strftime("%H:%M", time.localtime(ts)) if ts else "--:--"


def took(seconds):
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}"


def form(action, inner, cls=""):
    return f'<form method="post" action="{action}" class="{cls}"><input type="hidden" name="t" value="{TOKEN}">{inner}</form>'


def hidden(name, value):
    return f'<input type="hidden" name="{name}" value="{e(str(value))}">'


def when(ts):
    return time.strftime("%d %b %H:%M", time.localtime(ts)) if ts else "never"


# ---------- pages ----------

def build_home(values=None, error=""):
    v = values or {}
    who = "Claude Code" if builder.kind() == "claude" else "your local model"
    body = (f'<p>Describe an app. Forge has {who} build it, tests it in a locked sandbox, and makes it live '
            f'once it passes every check. To change who builds, run <code>python -m forge.cli setup</code>.</p>')
    if error:
        body += f'<p class="bad" role="alert">{e(error)}</p>'
    body += form("/build", (
        '<label for="idea">What should the app do?</label>'
        f'<textarea id="idea" name="idea" rows="6" required minlength="10" maxlength="4000" '
        f'placeholder="A scientific calculator with sin, cos, log and powers">{e(v.get("idea", ""))}</textarea>'
        '<label for="examples">Examples it must get right <span class="hint">Optional. One per line: what you type, an arrow, '
        'and the exact answer. The builder cannot change these.</span></label>'
        f'<textarea id="examples" name="examples" rows="4" placeholder="2+3*4 -> 14">{e(v.get("examples", ""))}</textarea>'
        '<label for="name">Name <span class="hint">Optional. Made from your description if left empty.</span></label>'
        f'<input type="text" id="name" name="name" maxlength="31" value="{e(v.get("name", ""))}">'
        '<button>Build it</button> <span class="muted">Usually a few minutes. You can leave the page while it builds.</span>'))
    builds = q("select * from proposals where channel='agent' order by id desc limit 8")
    if builds:
        body += "<h2>Recent builds</h2><table><tr><th>Build</th><th>App</th><th>Status</th><th>Started</th></tr>"
        for p in builds:
            word, state = STATUS_WORDS.get(p["status"], (p["status"].upper(), "wait"))
            body += (f'<tr><td><a href="/build?id={p["id"]}">Build {p["id"]}</a></td><td>{e(p["app"])}</td>'
                     f'<td>{flaps(word, state, small=True)}</td><td>{when(p["ts"])}</td></tr>')
        body += "</table>"
    return page("Build an app", body, "/")


def board_rows(p):
    """The departure board: one row per check, with its state worked out from the recorded progress."""
    stages, live = store.kv_get(f"stages:{p['id']}", {}), p["status"] == "testing"
    finished = p["status"] in ("awaiting", "canary", "shipped") and p["gate"]
    rows = [("write", "The builder writes the app", "Claude Code or your local model", "DONE"),
            ("tests", "Unit tests", "Run with no network", "PASSED"),
            ("scans", "Security scans", "gitleaks, semgrep and trivy", "PASSED"),
            ("examples", "Golden cases", "Your examples, then the builder's own", "PASSED"),
            ("live", "Goes live", "Appears under My apps" if p["channel"] == "agent" else "After your approval", "LIVE")]
    out = []
    for key, name, note, done_word in rows:
        s = stages.get(key)
        if s is None and finished and key != "live":  # builds recorded before per-check progress existed
            s = {"ok": True}
        if s is None and key == "live" and p["status"] == "shipped":
            s = {"ok": True}
        if s is None:
            word, state, code = "WAIT", "wait", "w"
        elif "ok" in s:
            word, state, code = (done_word, "pass", "p") if s["ok"] else ("FAILED", "fail", "f")
        else:
            word, state, code = ("RUNNING", "run", "r") if live else ("STOPPED", "fail", "x")
        start, end = (s or {}).get("start"), (s or {}).get("end") or (time.time() if code == "r" else None)
        out.append({"key": key, "name": name, "note": note, "word": word, "state": state, "code": code,
                    "time": clock(start), "took": took(end - start) if start and end else "–"})
    return out


def build_page(pid, seen=""):
    rows = q("select * from proposals where id=?", pid)
    if not rows:
        return page("Not found", '<p>There is no build with that number. <a href="/">Start a new one</a>.</p>')
    p = rows[0]
    running, board = p["status"] == "testing", board_rows(p)
    code = "".join(r["code"] for r in board)
    if p["status"] == "shipped":
        top = (f'<div class="done" role="status"><p class="ok">Your app is live.</p>'
               f'<a class="button" href="/use?app={e(p["app"])}">Open {e(p["app"])}</a></div>')
    elif running:
        top = '<p class="meta" role="status">Building. You can leave this page; it carries on and this board updates itself.</p>'
    elif p["status"] == "awaiting":
        top = '<p role="status">It passed every check and is waiting for your approval on <a href="/review">Review changes</a>.</p>'
    else:
        top = (f'<p class="bad" role="status">It did not pass the checks. {e(p["reason"] or "")}</p>'
               '<p>Try again with a clearer description or a few examples. <a href="/">Start a new build</a>.</p>')
    attempt = store.kv_get(f"attempt:{pid}", 1)
    tries = f" · attempt {attempt} of {router.config()['budget']['max_attempts']}" if attempt > 1 else ""
    idea = store.kv_get(f"idea:{pid}", "")
    body = f'<p class="meta">Started {when(p["ts"])}{tries}</p>' + top
    body += f'<blockquote class="ask">{e(idea)}</blockquote>' if idea else ""
    table = ('<table class="board"><caption class="sr">Checks for this build</caption>'
             '<tr><th class="tc" scope="col">Time</th><th scope="col">Check</th><th scope="col">Status</th>'
             '<th class="dc" scope="col" style="text-align:right">Took</th></tr>')
    for n, r in enumerate(board):
        changed = len(seen) == len(board) and seen[n] != r["code"]  # flip only what changed since the last look
        table += (f'<tr><td class="t tc">{r["time"]}</td><td class="what">{e(r["name"])}<small>{e(r["note"])}</small></td>'
                  f'<td>{flaps(r["word"], r["state"], flip=changed)}</td><td class="dur">{r["took"]}</td></tr>')
    table += "</table>"
    side = '<div class="panel"><h2>Your examples</h2>'
    golden = store.ROOT / "apps" / p["app"] / "evals" / "golden.jsonl"
    if not golden.exists():
        golden = store.ROOT / ".forge" / "wt" / f"p{pid}" / "apps" / p["app"] / "evals" / "golden.jsonl"
    mine = []
    for line in golden.read_text("utf-8").splitlines() if golden.exists() else []:
        try:
            c = json.loads(line)
        except ValueError:
            continue
        if str(c.get("id", "")).startswith("you-"):
            mine.append(c)
    side += ('<table class="examples"><tr><th scope="col">You type</th><th scope="col">Answer</th></tr>'
             + "".join(f'<tr><td>{e(str(c["input"].get("input", "")))}</td><td>{e(str(c.get("answer", "")))}</td></tr>' for c in mine)
             + "</table>" if mine else "<p class=muted>None given, so the builder's own checks decide.</p>")
    side += "</div>"
    g = json.loads(p["gate"] or "{}")
    record = (details("Build log", "\n".join(store.kv_get(f"build:{pid}", [])))
              + details("Test output", g.get("tests")) + details("Security scans", g.get("scans"))
              + details("The code", p["diff"]))
    side += f'<div class="panel"><h2>Technical record</h2>{record or "<p class=muted>Appears as the checks run.</p>"}</div>'
    body += f'<div class="split"><section aria-label="Checks">{table}</section><aside>{side}</aside></div>'
    heading = f'<span class="plat">{p["id"]}</span>{e(p["app"])}'
    return page(f"Build {p['id']}: {p['app']}", body, "/", refresh=5 if running else 0,
                refresh_url=f"/build?id={pid}&seen={code}", heading=heading)


def app_note(app):
    """The first line of the app's docstring, read as text: the app's code is never run to describe it."""
    try:
        doc = ast.get_docstring(ast.parse((store.ROOT / "apps" / app / "app.py").read_text("utf-8"))) or ""
    except (OSError, SyntaxError, ValueError):
        doc = ""
    return doc.strip().splitlines()[0] if doc.strip() else "No description."


def apps_page():
    rows = ""
    for a in runtime.apps():
        n = q("select count(*) n from traces where app=? and version!='eval'", a)[0]["n"]
        rel = q("select id, ts from proposals where app=? and status='shipped' order by id", a)
        no = f'<span class="plat">{rel[-1]["id"]}</span>' if rel else ""
        built = when(rel[0]["ts"]) if rel else "with Forge"
        rows += (f'<tr><td><a href="/use?app={e(a)}"><strong>{no}{e(a)}</strong></a></td><td>{e(app_note(a))}</td>'
                 f'<td>{built}</td><td>{n}</td></tr>')
    body = ('<table><tr><th scope="col">App</th><th scope="col">What it does</th><th scope="col">Built</th>'
            f'<th scope="col">Used</th></tr>{rows}</table>' if rows else '<p>No apps yet. <a href="/">Build one</a>.</p>')
    return page("My apps", body, "/apps")


def app_page(app, values=None, result=None, note=""):
    mod = runtime.load_app(store.ROOT, app)
    fields = hidden("app", app)
    for name, label, kind in mod.FIELDS:
        v = e((values or {}).get(name, ""))
        fields += f'<label for="{name}">{e(label)}</label>'
        fields += (f'<textarea id="{name}" name="{name}" rows="10">{v}</textarea>' if kind == "textarea"
                   else f'<input type="text" id="{name}" name="{name}" value="{v}">')
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
    return page(app, body, "/apps")


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
        query = parse_qs(urlparse(self.path).query)
        if path == "/font/signage.ttf":  # Barlow Condensed Bold, bundled so the look holds on every system, offline
            data = (store.ROOT / "forge" / "static" / "BarlowCondensed-Bold.ttf").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "font/ttf")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            self.wfile.write(data)
        elif path == "/":
            self.reply(build_home())
        elif path == "/apps":
            self.reply(apps_page())
        elif path == "/use":
            app = query.get("app", [""])[0]
            self.reply(app_page(app) if app in runtime.apps() else page("Not found", '<p>No such app. <a href="/apps">See your apps</a>.</p>', "/apps"), 200 if app in runtime.apps() else 404)
        elif path == "/build":
            pid = query.get("id", [""])[0]
            seen = query.get("seen", [""])[0]
            self.reply(build_page(int(pid), seen if seen.isalpha() else "") if pid.isdigit() else build_home())  # no build number: the form that starts one
        elif path == "/review":
            self.reply(review_page())
        elif path == "/health":
            self.reply(health_page())
        else:
            self.reply(page("Not found", '<p>That page does not exist. <a href="/">Go to Forge</a>.</p>'), 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not self.local() or n > 400_000:
            return self.reply(page("Could not do that", '<p class="bad" role="alert">That request was too large or not from this computer.</p>'), 400)
        f = {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode("utf-8", "replace"), keep_blank_values=True).items()}
        if not hmac.compare_digest(f.pop("t", ""), TOKEN):
            return self.reply(page("Page out of date", '<p role="alert">This page is from before the last restart. <a href="/">Reload it</a> and try again.</p>'), 403)
        path = urlparse(self.path).path
        try:
            if path == "/build":
                try:
                    pid = builder.start(f.get("idea", ""), f.get("name", ""), f.get("examples", ""))
                except ValueError as err:
                    return self.reply(build_home(f, str(err)), 400)
                self.redirect(f"/build?id={pid}")
            elif path == "/ask":
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
