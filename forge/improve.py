"""Improvement plane: detect -> triage -> propose on a branch -> gate.

Its only output is a Git branch waiting for approval. It never changes main or the running app.
"""
import hashlib
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from . import evals, router, runtime, sandbox, store
from .store import ROOT, q, x

TRANSIENT = ("RouterError", "TimeoutError", "URLError", "ConnectionError")  # no code fix needed
WEIGHT = {"error": 3, "eval": 2, "feedback": 1}
_lock = threading.Lock()


def git(*args, cwd=ROOT):
    p = subprocess.run(["git", "-c", "user.name=forge", "-c", "user.email=forge@localhost", *args],
                       cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode:
        raise RuntimeError(f"git {args[0]} failed: {(p.stderr or p.stdout).strip()}")
    return p.stdout


# ---------- detect and de-duplicate ----------

def upsert(fp, kind, app, title, detail, n=1):
    """One issue per fingerprint. Fixed issues reopen; rejected ones reopen only once occurrences double."""
    now = time.time()
    x("""insert into issues(fingerprint,kind,app,title,detail,count,first_ts,last_ts,status)
         values(?,?,?,?,?,?,?,?,?)
         on conflict(fingerprint) do update set count=count+excluded.count, last_ts=excluded.last_ts,
           detail=excluded.detail,
           status=case when status='fixed' or (status='rejected' and count+excluded.count>=2*seen_count)
                       then 'open' else status end""",
      fp, kind, app, title[:200], detail[:4000], n, now, now, "closed" if kind == "transient" else "open")


def detect():
    since, now = store.kv_get("detect_ts", 0), time.time()
    for r in q("""select fingerprint, app, count(*) n, max(error) e from traces
                  where ts>? and fingerprint is not null and version!='eval' group by fingerprint, app""", since):
        kind = "transient" if r["fingerprint"].startswith(TRANSIENT) else "error"
        upsert(r["fingerprint"], kind, r["app"], r["fingerprint"], r["e"] or "", r["n"])
    for f in q("""select f.id, f.text, f.rating, t.app, t.input, t.output from feedback f
                  join traces t on t.id=f.trace_id where f.seen=0"""):
        text = (f["text"] or "").strip()
        if f["rating"] < 0:
            words = " ".join(re.findall(r"\w+", text.lower()))
            fp = "feedback:" + (hashlib.sha1(words.encode()).hexdigest()[:10] if words else f"thumbsdown:{f['app']}")
            detail = json.dumps({"user_comment": text, "input": f["input"], "output": f["output"]})
            upsert(fp, "feedback", f["app"], text or "Answers were rated 'not good'", detail)
        x("update feedback set seen=1 where id=?", f["id"])
    store.kv_set("detect_ts", now)


def eval_check(app):
    card = evals.run(app)
    evals.save(app, "stable", card)
    failing = {f"eval:{app}:{f['id']}" for f in card["failed"]}
    for r in q("select id, fingerprint from issues where kind='eval' and app=? and status='open'", app):
        if r["fingerprint"] not in failing:  # the case passes again: nothing left to fix
            x("update issues set status='fixed' where id=?", r["id"])
    for f in card["failed"]:
        upsert(f"eval:{app}:{f['id']}", "eval", app, f"Golden case '{f['id']}' fails",
               json.dumps({"case": f["case"], "got": f["got"]}))
    return card


def triage(limit):
    """Open issues ranked by frequency x severity; one per app; apps with a change in flight wait."""
    busy = {r["app"] for r in q("select app from proposals where status in ('testing','awaiting','canary')")}
    rows = [r for r in q("select * from issues where status='open' and kind!='transient'") if r["app"] not in busy]
    rows.sort(key=lambda r: -r["count"] * WEIGHT.get(r["kind"], 1))
    picked = []
    for r in rows:
        if r["app"] not in {p["app"] for p in picked}:
            picked.append(r)
    return picked[:max(limit, 0)]


# ---------- propose ----------

def allowed(channel, app, paths, issue_id):
    """Allow-list per channel. Tests, evals, gates, config and forge itself are never writable here."""
    for p in paths:
        ok = {"prompt": bool(re.fullmatch(rf"prompts/{app}_[\w-]+\.md", p)),
              "routing": p == "config/routing.toml",
              "code": p in (f"apps/{app}/app.py", f"apps/{app}/tests/test_issue_{issue_id}.py")}[channel]
        if not ok:
            return f"changing {p} is not allowed in a {channel} proposal"
    return ""


def parse(reply):
    reply = reply.replace("\r", "")
    cause = re.search(r"^CAUSE:\s*(.+)$", reply, re.M)
    files = re.findall(r"^FILE:\s*(\S+)\s*\n<<<\n(.*?)\n>>>", reply, re.S | re.M)
    if not cause or not files:
        raise ValueError("the reply did not follow the required CAUSE / FILE format")
    return cause[1].strip(), {path.replace("\\", "/"): text + "\n" for path, text in files}


def write(wt, rel, text):
    path = (Path(wt) / rel).resolve()
    if not path.is_relative_to(Path(wt).resolve()):
        raise ValueError(f"{rel} is outside the project")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, "utf-8", newline="\n")


def slots(text):
    return sorted(set(re.findall(r"\{\{\w+\}\}", text)))


def open_proposal(app, channel, issue_id):
    pid = x("insert into proposals(ts,issue_id,app,channel,status) values(?,?,?,?,'testing')",
            time.time(), issue_id, app, channel)
    wt = ROOT / ".forge" / "wt" / f"p{pid}"
    git("worktree", "add", "-b", f"proposal/{pid}", str(wt), "main")
    x("update proposals set branch=? where id=?", f"proposal/{pid}", pid)
    return pid, wt


def finish(pid, wt, summary, gate):
    git("add", "-A", cwd=wt)
    git("commit", "-m", f"proposal {pid}: {summary}", cwd=wt)
    x("update proposals set status='awaiting', summary=?, diff=?, gate=? where id=?",
      summary, git("diff", "main", f"proposal/{pid}")[:60000], json.dumps(gate), pid)


def attempt(issue, channel, wt, base, failure):
    """One try at a fix. Returns the gate result: {'ok': bool, 'why': str, ...evidence}."""
    app, iid = issue["app"], issue["id"]
    limit_ms = router.config()["gates"]["p95_ms"]
    if channel == "code":
        test = f"apps/{app}/tests/test_issue_{iid}.py"
        targets = [f"apps/{app}/app.py"]
        rules = (f"Change only {targets[0]} and keep its FIELDS, validate_input, compute, validate_output contract.\n"
                 f"Also write a new file {test} using unittest. It must fail on the current code and pass with "
                 "your fix. Load the app in the test exactly like this:\n"
                 "import importlib.util\nfrom pathlib import Path\n"
                 "spec = importlib.util.spec_from_file_location('app', Path(__file__).parents[1] / 'app.py')\n"
                 "app = importlib.util.module_from_spec(spec)\nspec.loader.exec_module(app)\n"
                 "Tests run with no network: pass a fake ctx object with an llm(task, prompt, **values) method.")
    else:
        targets = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "prompts").glob(f"{app}_*.md"))
        rules = "Change only the prompt files shown. Keep every {{placeholder}} exactly as it is and add none."
    files = "\n\n".join(f"FILE: {t}\n<<<\n{(wt / t).read_text('utf-8')}\n>>>" for t in targets)
    memory = "\n".join(f"- [{m['kind']}] {m['text']}" for m in store.recall(issue["title"] + " " + issue["detail"]))
    prompt = runtime.render(ROOT, "forge_fix", kind=issue["kind"], count=issue["count"], title=issue["title"],
                            detail=issue["detail"], memory=memory or "nothing yet", files=files, rules=rules,
                            failure=f"Your previous attempt was rejected: {failure}" if failure else "")
    cause, new = parse(router.call("develop", [{"role": "user", "content": prompt}], cache=False))
    gate = {"cause": cause}
    err = allowed(channel, app, new, iid)
    if err:
        return {**gate, "ok": False, "why": err}
    if channel == "code":
        if set(new) != {targets[0], test}:
            return {**gate, "ok": False, "why": f"the reply must contain exactly {targets[0]} and {test}"}
        write(wt, test, new[test])
        ok, out = sandbox.tests(wt, app, f"test_issue_{iid}.py")
        if "Docker is not running" in out:
            return {**gate, "ok": False, "why": out}
        if ok:
            return {**gate, "ok": False, "why": "the new test passes on the current code, so it does not reproduce the problem"}
        write(wt, targets[0], new[targets[0]])
        ok, gate["tests"] = sandbox.tests(wt, app)
        if not ok:
            return {**gate, "ok": False, "why": "tests failed:\n" + gate["tests"][-1500:]}
        ok, gate["scans"] = sandbox.scans(wt)
        if not ok:
            return {**gate, "ok": False, "why": "a security scan failed:\n" + gate["scans"][-1500:]}
        card = sandbox.evals(wt, app)
    else:
        for path, text in new.items():
            if not (wt / path).exists() or slots((wt / path).read_text("utf-8")) != slots(text):
                return {**gate, "ok": False, "why": f"{path} must exist already and keep the same placeholders"}
            write(wt, path, text)
        card = evals.run(app, root=wt)  # only prompt text differs from main, so no generated code runs here
    gate["before"], gate["after"] = base, card
    why = evals.compare(base, card, must_improve=issue["kind"] == "eval", limit_ms=limit_ms)
    return {**gate, "ok": not why, "why": "; ".join(why)}


def propose(issue, base):
    from . import release
    channel = "code" if issue["kind"] == "error" else "prompt"
    pid, wt = open_proposal(issue["app"], channel, issue["id"])
    x("update issues set status='proposed' where id=?", issue["id"])
    failure, gate = "", {}
    for n in range(1, router.config()["budget"]["max_attempts"] + 1):  # bounded: then a human decides
        if store.kv_get("kill", False):
            failure = "stopped by the kill switch"
            break
        x("update proposals set attempts=? where id=?", n, pid)
        try:
            gate = attempt(issue, channel, wt, base, failure)
        except Exception as e:  # a bad model reply or tool failure counts as a failed attempt
            gate = {"ok": False, "why": f"{type(e).__name__}: {e}"}
        if gate["ok"]:
            finish(pid, wt, gate["cause"], gate)
            return pid
        failure = gate["why"]
        git("checkout", "--", ".", cwd=wt)
        git("clean", "-fd", cwd=wt)
    p = q("select * from proposals where id=?", pid)[0]
    x("update proposals set summary=?, gate=? where id=?", f"Could not fix: {issue['title']}", json.dumps(gate), pid)
    release.close({**dict(p), "summary": issue["title"]}, "escalated", "hold", f"Gave up after {p['attempts']} attempts: {failure}")
    release.cleanup(pid)
    return pid


def tune_routing(app, base):
    """Model-selection channel: try each model first for this app's tasks, keep the best that beats baseline."""
    from . import release
    cfg = router.config()
    tasks = cfg.get("apps", {}).get(app, {}).get("tasks", [])
    if not tasks:  # the app uses no model, so there is no model order to tune
        return None
    text = (ROOT / "config" / "routing.toml").read_text("utf-8")
    golden = (ROOT / "apps" / app / "evals" / "golden.jsonl").read_text("utf-8")
    key = hashlib.sha1((text + golden).encode()).hexdigest()
    if store.kv_get(f"tuned:{app}") == key:  # nothing changed since the last search: do not repeat it
        return None
    store.kv_set(f"tuned:{app}", key)
    best = None
    pid, wt = open_proposal(app, "routing", None)
    for name, m in cfg["models"].items():
        ep = cfg["endpoints"][m["endpoint"]]
        if (ep.get("key_env") and not os.environ.get(ep["key_env"])) or all(cfg["tasks"][t][0] == name for t in tasks):
            continue
        cand = text
        for t in tasks:
            chain = json.dumps([name] + [n for n in cfg["tasks"][t] if n != name])
            cand = re.sub(rf"^({re.escape(t)}\s*=\s*)\[[^\]]*\]", lambda mm: mm[1] + chain, cand, flags=re.M)
        write(wt, "config/routing.toml", cand)
        card = evals.run(app, root=wt)
        rank = (card["success"], -card["usd_per_req"], -card["p95_ms"])
        if not evals.compare(base, card, True, cfg["gates"]["p95_ms"]) and (best is None or rank > best[0]):
            best = (rank, name, cand, card)
    if not best:
        x("delete from proposals where id=?", pid)
        release.cleanup(pid)
        store.remember("routing", app, "Tried every configured model first; none beat the current order.")
        return None
    _, name, cand, card = best
    write(wt, "config/routing.toml", cand)
    summary = f"Use {name} first for {', '.join(tasks)}"
    finish(pid, wt, summary, {"cause": summary + " (better score, cost or speed on the golden set)",
                              "before": base, "after": card})
    return pid


# ---------- changes made by a connected agent (Claude Code or any MCP client) ----------

def start_change(app, issue_id=None):
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,30}", app or ""):
        raise ValueError("App names use lowercase letters, digits and underscores, starting with a letter.")
    if store.kv_get("kill", False):
        raise ValueError("The owner has stopped all changes (kill switch is on).")
    busy = q("select * from proposals where app=? and status in ('testing','awaiting','canary')", app)
    if busy and busy[0]["status"] == "testing" and busy[0]["channel"] == "agent":
        return busy[0]["id"], ROOT / ".forge" / "wt" / f"p{busy[0]['id']}"  # resume the open change
    if busy:
        raise ValueError(f"Change {busy[0]['id']} for {app} is still {busy[0]['status']}. Wait for the owner's decision.")
    # no daily cap here: a person asked for this change. It is still bounded by one change per app and limited submits.
    pid, wt = open_proposal(app, "agent", issue_id)
    if issue_id:
        x("update issues set status='proposed' where id=? and app=?", issue_id, app)
    return pid, wt


def agent_change(pid):
    rows = q("select * from proposals where id=? and channel='agent' and status='testing'", int(pid))
    if not rows:
        raise ValueError(f"Change {pid} is not open for editing.")
    return rows[0], ROOT / ".forge" / "wt" / f"p{rows[0]['id']}"


def agent_path(p, rel):
    """An agent may touch its own app and that app's prompts. Nothing else, ever."""
    rel, app = rel.replace("\\", "/"), p["app"]
    ok = re.fullmatch(rf"apps/{app}/[\w./-]+", rel) or re.fullmatch(rf"prompts/{app}_[\w-]+\.md", rel)
    if not ok or ".." in rel:
        raise ValueError(f"{rel} is not allowed. Only apps/{app}/... and prompts/{app}_*.md can be changed.")
    return rel


def agent_gate(p, wt):
    app = p["app"]
    git("add", "-A", cwd=wt)
    paths = git("diff", "--cached", "--name-only", "main", cwd=wt).split("\n")[:-1]
    if not paths:
        return {"ok": False, "why": "nothing was changed"}
    for path in paths:
        agent_path(p, path)
    for need in ("app.py", "evals/golden.jsonl"):
        if not (wt / "apps" / app / need).is_file():
            return {"ok": False, "why": f"apps/{app}/{need} is missing"}
    if not list((wt / "apps" / app / "tests").glob("test*.py")):
        return {"ok": False, "why": f"apps/{app}/tests/ needs at least one test_*.py file"}
    new_app = not (ROOT / "apps" / app / "app.py").exists()
    gate = {"cause": "Built by a connected agent"}
    touched = [f for f in paths if re.match(rf"apps/{app}/(tests|evals)/", f) and (ROOT / f).exists()]
    if touched:
        gate["warning"] = "This change edits existing tests or golden cases: " + ", ".join(touched)
    ok, gate["tests"] = sandbox.tests(wt, app)
    if not ok:
        return {**gate, "ok": False, "why": "unit tests failed:\n" + gate["tests"][-1500:]}
    ok, gate["scans"] = sandbox.scans(wt)
    if not ok:
        return {**gate, "ok": False, "why": "a security scan failed:\n" + gate["scans"][-1500:]}
    card = gate["after"] = sandbox.evals(wt, app)
    limit_ms = router.config()["gates"]["p95_ms"]
    if new_app:  # no baseline yet: the app's own golden set defines "done"
        why = [f"golden cases fail: {', '.join(f['id'] for f in card['failed'])}"] if card["failed"] else []
        if card["p95_ms"] > limit_ms:
            why.append(f"slowest request took {card['p95_ms']} ms, over the {limit_ms} ms limit")
    else:
        gate["before"] = evals.latest(app) or evals.run(app)
        why = evals.compare(gate["before"], card, must_improve=False, limit_ms=limit_ms)
    return {**gate, "ok": not why, "why": "; ".join(why)}


def submit_change(pid, summary):
    from . import release
    p, wt = agent_change(pid)
    n, limit = p["attempts"] + 1, router.config()["budget"]["max_attempts"]
    x("update proposals set attempts=? where id=?", n, p["id"])
    try:
        gate = agent_gate(p, wt)
    except Exception as e:
        gate = {"ok": False, "why": f"{type(e).__name__}: {e}"}
    if gate["ok"]:
        finish(p["id"], wt, summary.strip()[:200] or f"Change to {p['app']}", gate)
        return f"Change {p['id']} passed every gate. It now waits for the owner's approval in the dashboard."
    if n >= limit:  # bounded: the owner decides what happens next; the work stays in the folder for them
        x("update proposals set summary=?, gate=? where id=?", summary.strip()[:200], json.dumps(gate), p["id"])
        release.close({**dict(p), "summary": summary}, "escalated", "hold",
                      f"Gave up after {n} submits: {gate['why'][:500]}. Work is kept in {wt}")
        return f"Gate failed and the submit limit ({limit}) is reached. The change is escalated to the owner.\n{gate['why']}"
    return f"Gate failed (submit {n} of {limit}). Fix it, run check_change, then submit again.\n{gate['why']}"


# ---------- the scheduled cycle ----------

def cycle():
    if not _lock.acquire(blocking=False):
        return "A cycle is already running."
    try:
        if store.kv_get("kill", False):
            return "Stopped: the kill switch is on."
        cfg = router.config()
        detect()
        notes = []
        cards = {app: eval_check(app) for app in runtime.apps()}
        room = cfg["budget"]["daily_proposals"] - q("select count(*) n from proposals where ts>=? and channel!='agent'", store.today())[0]["n"]
        for issue in triage(room):
            if issue["kind"] == "error" and not sandbox.available():
                notes.append(f"Skipped '{issue['title']}': Docker is not running, so code fixes are paused.")
                continue
            pid = propose(issue, cards[issue["app"]])
            room -= 1
            notes.append(f"Change {pid} for '{issue['title']}'.")
        for app in cards:
            busy = q("select 1 from proposals where app=? and status in ('testing','awaiting','canary')", app)
            if room > 0 and not busy and not store.kv_get("kill", False):
                pid = tune_routing(app, cards[app])
                if pid:
                    room -= 1
                    notes.append(f"Change {pid}: a better model order for {app}.")
        scores = ", ".join(f"{a} {c['success']:.0%}" for a, c in cards.items())
        return f"Golden set: {scores}. " + (" ".join(notes) or "Nothing to propose.")
    finally:
        _lock.release()
