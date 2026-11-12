"""Idea in, working app out: Forge drives the chosen builder (Claude Code or the local model) through its gates.

A new app built from the owner's own idea goes live as soon as it passes every gate.
"""
import json
import re
import shutil
import subprocess
import threading
import time

from . import improve, mcp, release, router, runtime, sandbox, store
from .store import ROOT, q

_lock = threading.Lock()  # one build at a time: they compete for the GPU and the sandbox
ARROW = re.compile(r"\s*(?:→|=>|->)\s*")
CLAUDE_TOOLS = "mcp__forge__guide,mcp__forge__read_file,mcp__forge__write_file,mcp__forge__check_change"
STOP_WORDS = {"a", "an", "the", "app", "that", "which", "make", "build", "create", "me", "for", "to", "and",
              "with", "i", "want", "please", "can", "you", "of", "my", "is", "it", "this", "some", "any",
              "tool", "program", "simple", "given"}
# Words that start the details ("count words IN a text"): a name with two words already ends before one.
JOINERS = {"in", "on", "at", "by", "from", "into", "between", "using", "as", "when", "if", "so", "then", "or",
           "but", "per", "via", "where"}


def name_for(idea, wanted=""):
    """A free app name: the one asked for, or one made from the idea's first words."""
    if wanted.strip():
        name = wanted.strip().lower()
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,30}", name):
            raise ValueError("Names use lowercase letters, digits and underscores, start with a letter, 2 to 31 characters.")
        if name in runtime.apps():
            raise ValueError(f"An app called {name} already exists. Pick another name.")
        return name
    words = []
    for w in re.findall(r"[a-z][a-z0-9]*", idea.lower()):
        if w in JOINERS:
            if len(words) >= 2:
                break
        elif w not in STOP_WORDS:
            words.append(w)
            if len(words) == 3:
                break
    words = words or ["app"]
    base = words[0][:24]
    for w in words[1:]:  # whole words only, so names never end mid-word
        if len(base) + 1 + len(w) > 24:
            break
        base += "_" + w
    if len(base) < 2:
        base = "app_" + base
    name, n = base, 2
    while name in runtime.apps() or q("select 1 from proposals where app=? and status='testing'", name):
        name, n = f"{base}_{n}", n + 1
    return name


def parse_examples(text):
    """'input -> expected answer' per line. These become golden cases the builder may not change."""
    cases = []
    for line in (l.strip() for l in text.splitlines()):
        if not line:
            continue
        arrows = list(ARROW.finditer(line))
        inp = line[:arrows[-1].start()].strip() if arrows else ""
        answer = line[arrows[-1].end():].strip() if arrows else ""
        if not inp or not answer:
            raise ValueError(f"Example '{line[:60]}' needs an arrow between input and answer, like: 2+3*4 -> 14")
        cases.append({"id": f"you-{len(cases) + 1}", "input": {"input": inp}, "answer": answer})
    if len(cases) > 50:
        raise ValueError("Please give at most 50 examples.")
    return cases


def restore_examples(wt, name, cases):
    """Put the owner's examples back exactly as given, whatever the builder did to them."""
    path = wt / "apps" / name / "evals" / "golden.jsonl"
    keep = []
    if path.exists():
        for line in path.read_text("utf-8").splitlines():
            try:
                if not str(json.loads(line).get("id", "")).startswith("you-"):
                    keep.append(line)
            except ValueError:
                keep.append(line)  # a malformed line fails the evals and is reported to the builder
    lines = [json.dumps(c) for c in cases] + [line for line in keep if line.strip()]
    if lines:
        improve.write(wt, f"apps/{name}/evals/golden.jsonl", "\n".join(lines) + "\n")


def kind():
    chosen = router.config().get("builder", {}).get("kind")
    return chosen if chosen in ("claude", "local") else ("claude" if shutil.which("claude") else "local")


def log(pid, text):
    store.kv_set(f"build:{pid}", store.kv_get(f"build:{pid}", []) + [f"{time.strftime('%H:%M:%S')}  {text}"])


def _examples_note(name, cases):
    if not cases:
        return ""
    shown = "\n".join(f"  {c['input']['input']}  ->  {c['answer']}" for c in cases)
    return (f"The owner gave examples the app must get exactly right:\n{shown}\n"
            f"They are already in apps/{name}/evals/golden.jsonl with ids starting 'you-'; keep them unchanged. "
            f"The app's main input field must be named \"input\", and output[\"answer\"] must be exactly the "
            f"answer text shown.")


def claude_round(pid, wt, name, idea, cases, failure):
    prompt = (f"Build a new app for Forge. Change {pid} is already open for the app '{name}'. Call the forge `guide` "
              f"tool first, then use only read_file, write_file and check_change with change_id {pid}. "
              f"Run check_change until it passes; Forge submits the change itself when you finish.\n\n"
              f"The owner's request:\n<request>\n{idea}\n</request>\n\n{_examples_note(name, cases)}\n\n"
              + (f"Your previous attempt failed Forge's gates:\n{failure}\nFix that.\n\n" if failure else "")
              + "Begin your final reply with one sentence saying what you built.")
    try:
        p = subprocess.run([shutil.which("claude"), "-p", prompt, "--mcp-config", str(ROOT / ".mcp.json"),
                            "--strict-mcp-config", "--allowedTools", CLAUDE_TOOLS,
                            "--disallowedTools", "Bash,Edit,Write,NotebookEdit", "--output-format", "json"],
                           cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800)
        result = json.loads(p.stdout or "{}")
    except (subprocess.TimeoutExpired, OSError, ValueError, TypeError) as e:
        raise RuntimeError(f"Claude Code did not finish: {type(e).__name__}: {e}") from None
    text = (result.get("result") or "").strip()
    if result.get("is_error") or not text or "authenticate" in text.lower():
        raise RuntimeError(f"Claude Code failed: {text[:300] or p.stderr[-300:]}")
    log(pid, f"Claude Code finished ({result.get('num_turns', '?')} steps, "
             f"about ${result.get('total_cost_usd') or 0:.2f} of your Claude plan or credit).")
    return next(line for line in text.splitlines() if line.strip()).strip(" #*")[:200]  # the opening line says what was built


def local_round(pid, wt, name, idea, cases, failure):
    summary = f"New app {name}"
    for n in range(2):  # two drafts per round, each checked in the sandbox
        files = "\n\n".join(f"FILE: {p.relative_to(wt).as_posix()}\n<<<\n{p.read_text('utf-8')}\n>>>"
                            for p in sorted((wt / "apps" / name).rglob("*")) if p.is_file() and p.suffix in (".py", ".jsonl"))
        prompt = runtime.render(ROOT, "forge_build", name=name, idea=idea, guide=mcp.GUIDE,
                                examples=_examples_note(name, cases), files=files or "none yet",
                                failure=f"The last draft failed:\n{failure}\nFix that." if failure else "")
        log(pid, f"Local model is writing draft {n + 1}...")
        summary, written = improve.parse(router.call("develop", [{"role": "user", "content": prompt}], cache=False))
        for path, text in written.items():
            improve.write(wt, improve.agent_path({"app": name}, path), text)
        restore_examples(wt, name, cases)
        ok, out = sandbox.tests(wt, name)
        if ok:
            card = sandbox.evals(wt, name)
            if not card["failed"]:
                return summary
            failure = "golden cases fail: " + json.dumps([{"id": f["id"], "got": f["got"]} for f in card["failed"]])[:2000]
        else:
            failure = "unit tests failed:\n" + out[-2000:]
        log(pid, "Draft failed the checks; trying again." if n == 0 else "Draft failed the checks.")
    return summary


def run(pid, name, idea, cases):
    wt = ROOT / ".forge" / "wt" / f"p{pid}"
    failure, builder = "", kind()
    try:
        for attempt in range(1, router.config()["budget"]["max_attempts"] + 1):  # bounded: then the owner decides
            log(pid, f"Attempt {attempt}: {'Claude Code' if builder == 'claude' else 'the local model'} is building...")
            store.kv_set(f"stages:{pid}", {})  # a fresh board for each attempt
            store.kv_set(f"attempt:{pid}", attempt)
            store.stage(pid, "write")
            try:
                summary = (claude_round if builder == "claude" else local_round)(pid, wt, name, idea, cases, failure)
            except RuntimeError as e:
                if builder != "claude":
                    store.stage(pid, "write", False)
                    raise
                log(pid, f"{e} Switching to the local model.")
                builder = "local"
                summary = local_round(pid, wt, name, idea, cases, failure)
            store.stage(pid, "write", True)
            restore_examples(wt, name, cases)
            log(pid, "Running every gate: unit tests, security scans, golden cases...")
            message = improve.submit_change(pid, summary)
            status = q("select status from proposals where id=?", pid)[0]["status"]
            if status == "awaiting":
                store.stage(pid, "live")
                release.approve(pid)  # the owner's own idea and every gate passed: it goes live
                store.stage(pid, "live", q("select status from proposals where id=?", pid)[0]["status"] == "shipped")
                log(pid, "Passed every gate. Your app is live.")
                return
            log(pid, message.splitlines()[0])
            if status != "testing":
                return
            failure = message
    except Exception as e:  # a crash must leave a clear record, never a half-open change
        log(pid, f"Stopped: {type(e).__name__}: {e}")
        p = q("select * from proposals where id=?", pid)[0]
        if p["status"] == "testing":
            release.close({**dict(p), "summary": f"Build of {name}"}, "escalated", "hold", f"Build stopped: {e}")
    finally:
        _lock.release()


def start(idea, wanted_name="", examples=""):
    """Validate, open the change, and build in the background. Returns the change number."""
    idea = idea.strip()
    if len(idea) < 10:
        raise ValueError("Describe the app in a sentence or more.")
    if len(idea) > 4000:
        raise ValueError("Please keep the description under 4,000 characters.")
    cases = parse_examples(examples)
    name = name_for(idea, wanted_name)
    if not sandbox.available():
        raise ValueError("Docker is not running, so nothing can be tested. Start Docker Desktop and try again.")
    if not _lock.acquire(blocking=False):
        raise ValueError("Another app is being built. Wait for it to finish.")
    try:
        pid, wt = improve.start_change(name)
        restore_examples(wt, name, cases)
        store.kv_set(f"idea:{pid}", idea)
        log(pid, f"Building '{name}'" + (f" with {len(cases)} example(s) it must get right." if cases else "."))
    except Exception:
        _lock.release()
        raise
    threading.Thread(target=run, args=(pid, name, idea, cases), daemon=True).start()
    return pid
