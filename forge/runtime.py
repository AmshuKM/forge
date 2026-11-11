"""Runtime plane: input guard -> context -> compute -> validate (bounded retry) -> output -> trace.

Nothing here changes code, prompts or routing.
"""
import importlib.util
import json
import random
import re
import time
import traceback
import uuid
from pathlib import Path

from . import router, store

MAX_INPUT_CHARS = 200_000
RETRIES = 2  # extra attempts after an invalid output, then a safe error
SAFE_ERROR = "Sorry, something went wrong with that request. It has been logged."


class InputError(Exception):
    """The user's input was not acceptable. Shown to the user; not a system fault."""


def render(root, prompt, **values):
    """Fill {{name}} slots in one pass, so text inside a value is never treated as a slot."""
    limit = router.config(root)["budget"].get("context_chars", 24000)
    template = (Path(root) / "prompts" / f"{prompt}.md").read_text("utf-8")
    return re.sub(r"\{\{(\w+)\}\}", lambda m: str(values.get(m[1], ""))[:limit], template)


class Ctx:
    """What an app may use: model calls through the router, and memory lookups."""

    def __init__(self, root, trace_id, cache=True):
        self.root, self.trace_id, self.cache, self.hint = Path(root), trace_id, cache, ""

    def llm(self, task, prompt, **values):
        text = render(self.root, prompt, **values)
        if self.hint:
            text += f"\n\nYour previous answer was rejected because {self.hint}. Correct that."
        return router.call(task, [{"role": "user", "content": text}], root=self.root,
                           trace_id=self.trace_id, cache=self.cache and not self.hint)

    def recall(self, query):
        return store.recall(query)


def apps(root=None):
    return sorted(p.parent.name for p in (Path(root or store.ROOT) / "apps").glob("*/app.py"))


def load_app(root, app):
    if not re.fullmatch(r"[a-z0-9_]+", app or ""):
        raise InputError("Unknown app.")
    path = Path(root) / "apps" / app / "app.py"
    spec = importlib.util.spec_from_file_location(f"forge_app_{app}_{uuid.uuid4().hex}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fingerprint(exc):
    frames = traceback.extract_tb(exc.__traceback__)
    frame = next((f for f in reversed(frames) if "apps" in Path(f.filename).parts), frames[-1] if frames else None)
    where = f"{Path(frame.filename).name}:{frame.name}" if frame else "?"
    return f"{type(exc).__name__}:{where}"


def pick_root():
    canary = store.kv_get("canary")
    if canary and random.random() * 100 < canary["pct"]:
        return Path(canary["path"]), f"canary:p{canary['proposal']}"
    return store.ROOT, "stable"


def handle(app, payload, *, root=None, version=None, cache=True):
    if root is None:
        root, version = pick_root()
    trace_id, t0 = uuid.uuid4().hex[:16], time.time()
    ok, output, error, fp, shown = False, None, None, None, SAFE_ERROR
    try:
        if not isinstance(payload, dict) or len(json.dumps(payload)) > MAX_INPUT_CHARS:
            raise InputError("That input is too large.")
        mod = load_app(root, app)
        try:
            clean = mod.validate_input(payload)
        except ValueError as e:
            raise InputError(str(e)) from None
        ctx = Ctx(root, trace_id, cache)
        for _ in range(RETRIES + 1):
            output = mod.compute(clean, ctx)
            try:
                mod.validate_output(clean, output)
                ok = True
                break
            except ValueError as e:
                ctx.hint = str(e)
        if not ok:
            error = f"OutputInvalid: {ctx.hint}"
            fp = f"OutputInvalid:{app}:" + re.sub(r"\d+", "N", ctx.hint)[:60]
    except InputError as e:
        error = shown = str(e)
    except Exception as e:  # app or model failure: log it, show the user a safe message
        error = "".join(traceback.format_exception(e))[-4000:]
        fp = fingerprint(e)
    ms = int((time.time() - t0) * 1000)
    store.x("insert into traces(id,ts,app,version,ok,error,fingerprint,ms,input,output) values(?,?,?,?,?,?,?,?,?,?)",
            trace_id, time.time(), app, version, int(ok), error, fp, ms,
            json.dumps(payload, default=str)[:4000], json.dumps(output, default=str)[:4000])
    usd = store.q("select coalesce(sum(usd),0) s from calls where trace_id=?", trace_id)[0]["s"]
    if str(version).startswith("canary"):
        from . import release
        release.check()
    return {"trace_id": trace_id, "ok": ok, "output": output if ok else None,
            "error": None if ok else shown, "ms": ms, "usd": usd, "version": version}
