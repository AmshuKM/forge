"""Model router: task type -> model chain, with fallback, a daily spend cap, a cache and call logging.

Talks to any OpenAI-compatible endpoint (Ollama, Unsloth, LM Studio, cloud APIs).
"""
import hashlib
import json
import os
import time
import tomllib
import urllib.request
from pathlib import Path

from . import store


class RouterError(Exception):
    pass


def local_path():
    """This computer's models. Never in Git, so it always comes from the main folder, not a change's copy."""
    return Path(os.environ.get("FORGE_LOCAL_CONFIG") or store.ROOT / "config" / "local.toml")


def config(root=None):
    cfg = tomllib.loads((Path(root or store.ROOT) / "config" / "routing.toml").read_text("utf-8"))
    if local_path().is_file():
        for key, value in tomllib.loads(local_path().read_text("utf-8")).items():
            cfg[key] = {**cfg.get(key, {}), **value} if isinstance(value, dict) else value
    cfg.setdefault("endpoints", {})
    cfg.setdefault("models", {})
    return cfg


def spent_today():
    return store.q("select coalesce(sum(usd),0) s from calls where ts>=?", store.today())[0]["s"]


def _post(url, body, key, timeout):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, json.dumps(body).encode(), headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def call(task, messages, *, root=None, trace_id=None, cache=True):
    """Return the reply text from the first model in the task's chain that answers."""
    if os.environ.get("FORGE_MOCK"):  # no model installed: a fixed stand-in so the whole pipeline can be tried
        return "MOCK REPLY: " + messages[-1]["content"][-200:]
    cfg = config(root)
    chain = cfg["tasks"][task]
    if not cfg["models"]:
        raise RouterError("No models are set up on this computer. Run: python -m forge.cli setup")
    # shortcut: exact-match cache, not semantic; upgrade when near-duplicate requests are common
    ck = hashlib.sha256(json.dumps([[cfg["models"].get(n) for n in chain], messages], sort_keys=True).encode()).hexdigest()
    if cache:
        hit = store.q("select v from cache where k=?", ck)
        if hit:
            return hit[0]["v"]
    errors = []
    for name in chain:  # each model is tried once, so this loop always ends
        m = cfg["models"].get(name)
        if not m:
            errors.append(f"{name}: not set up (run python -m forge.cli setup)")
            continue
        ep = cfg["endpoints"][m["endpoint"]]
        usd_in, usd_out = m.get("usd_in", 0), m.get("usd_out", 0)
        if (usd_in or usd_out) and spent_today() >= cfg["budget"]["daily_usd"]:
            errors.append(f"{name}: daily budget reached")
            continue
        key = os.environ.get(ep["key_env"]) if ep.get("key_env") else None
        if ep.get("key_env") and not key:
            errors.append(f"{name}: no API key set in {ep['key_env']}")
            continue
        base = os.environ.get("FORGE_BASE_" + m["endpoint"].upper(), ep["base"])
        body = {"model": m["model"], "messages": messages, "temperature": 0, **m.get("extra", {})}
        t0 = time.time()
        try:
            r = _post(base.rstrip("/") + "/chat/completions", body, key, m.get("timeout_s", cfg["budget"].get("timeout_s", 240)))
            text = r["choices"][0]["message"].get("content") or ""
            if not text.strip():
                raise ValueError("empty reply")
        except Exception as e:  # any failure means: try the next model
            err = f"{type(e).__name__}: {e}"
            errors.append(f"{name}: {err}")
            _log(trace_id, task, name, t0, 0, 0, 0, 0, err)
            continue
        usage = r.get("usage") or {}
        tin, tout = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
        _log(trace_id, task, name, t0, tin, tout, (tin * usd_in + tout * usd_out) / 1e6, 1, None)
        if cache:
            store.x("insert or replace into cache(k,v,ts) values(?,?,?)", ck, text, time.time())
        return text
    raise RouterError("; ".join(errors) or f"no models configured for {task}")


def _log(trace_id, task, model, t0, tin, tout, usd, ok, error):
    store.x("insert into calls(ts,trace_id,task,model,ms,tin,tout,usd,ok,error) values(?,?,?,?,?,?,?,?,?,?)",
            time.time(), trace_id, task, model, int((time.time() - t0) * 1000), tin, tout, usd, ok, error)
