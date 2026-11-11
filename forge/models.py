"""Find local model servers and their models, pick one per role, and write config/local.toml."""
import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request

from . import router

SERVERS = [  # the usual local addresses of OpenAI-compatible model servers
    ("ollama", "http://localhost:11434"),
    ("lmstudio", "http://localhost:1234"),
    ("llamacpp", "http://localhost:8080"),
    ("vllm", "http://localhost:8000"),
    ("unsloth", "http://localhost:8888"),
]
KEY_ENV = {"unsloth": "FORGE_UNSLOTH_KEY"}  # servers that need a key; the key itself stays in the environment
SKIP = re.compile(r"embed|rerank|whisper|tts|cloud", re.I)
LONG = 16384  # prompts carry whole documents, so a short context window silently cuts them off
ROLES = {
    "main": "everyday answers",
    "think": "writing fixes and judging answers (a reasoning model helps)",
    "fast": "quick small jobs",
    "big": "backup when the main model fails",
}


def _get(url, key=None, body=None):
    headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {key}"} if key else {})}
    data = json.dumps(body).encode() if body is not None else None
    with urllib.request.urlopen(urllib.request.Request(url, data, headers), timeout=5) as r:
        return json.load(r)


def _ollama(base):
    found = []
    for t in _get(base + "/api/tags")["models"]:
        if SKIP.search(t["name"]) or t.get("size", 0) < 100_000_000:  # cloud stubs are tiny
            continue
        info = _get(base + "/api/show", body={"model": t["name"]})
        caps = info.get("capabilities") or t.get("capabilities") or ["completion"]
        ctx = re.search(r"num_ctx\s+(\d+)", info.get("parameters") or "")
        if "completion" in caps:
            found.append({"model": t["name"], "size": t["size"], "thinking": "thinking" in caps,
                          "ctx": int(ctx[1]) if ctx else 0})
    return found


def detect():
    """Return (models, notes). Each model: endpoint, base, model, size (bytes, 0 if unknown), thinking, ctx."""
    found, notes = [], []
    for name, base in SERVERS:
        key_env = KEY_ENV.get(name)
        try:
            if name == "ollama":
                models = _ollama(base)
            else:
                ids = [m["id"] for m in _get(base + "/v1/models", os.environ.get(key_env or ""))["data"]]
                models = [{"model": i, "size": 0, "thinking": False, "ctx": 0} for i in ids if not SKIP.search(i)]
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                notes.append(f"Found {name} at {base}, but it needs an API key. Set {key_env or 'its key'} and run setup again.")
            continue
        except (OSError, ValueError, KeyError):
            continue  # nothing listening there, or not a model server
        for m in models:
            found.append({**m, "endpoint": name, "base": base + "/v1", "key_env": key_env})
    return found, notes


def vram_gb():
    if not shutil.which("nvidia-smi"):
        return None
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10).stdout
        return max(int(x) for x in out.split()) / 1024
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def pick(models, vram=None):
    """Choose a model for each role: long context first, then the largest that fits in GPU memory."""
    limit = (vram or 8) * 0.9e9
    fits = [m for m in models if m["size"] <= limit] or models
    rank = lambda m: (m["ctx"] >= LONG, m["size"])  # noqa: E731
    main = max(fits, key=rank)
    by_size = sorted(fits, key=lambda m: m["size"])
    return {"main": main, "think": max([m for m in fits if m["thinking"]] or [main], key=rank),
            "fast": by_size[0], "big": by_size[-1]}


def builders():
    """Who can build apps from an idea, best first: Claude Code if installed, then the local model."""
    return (["claude"] if shutil.which("claude") else []) + ["local"]


def render(roles, builder="local"):
    lines = ["# This computer's models. Written by: python -m forge.cli setup",
             "# Not shared through Git. Edit a model line by hand, or run setup again to choose.", "",
             "[builder]", f"kind = {json.dumps(builder)}   # claude = Claude Code, local = the think model below", ""]
    for name in dict.fromkeys(m["endpoint"] for m in roles.values()):
        m = next(r for r in roles.values() if r["endpoint"] == name)
        lines += [f"[endpoints.{name}]", f"base = {json.dumps(m['base'])}"]
        lines += [f"key_env = {json.dumps(m['key_env'])}"] if m["key_env"] else []
        lines.append("")
    for role, m in roles.items():
        lines += [f"[models.{role}]", f"endpoint = {json.dumps(m['endpoint'])}", f"model = {json.dumps(m['model'])}"]
        if m["thinking"]:  # reasoning is slow: only the role that writes fixes and judges gets it
            lines.append('extra = { reasoning_effort = "%s" }' % ("medium" if role == "think" else "none"))
        if role == "think":
            lines.append("timeout_s = 900")
        lines.append("")
    return "\n".join(lines)


def describe(m):
    size = f", {m['size'] / 1e9:.1f} GB" if m["size"] else ""
    ctx = f", {m['ctx'] // 1024}k context" if m["ctx"] else ""
    return f"{m['model']} ({m['endpoint']}{size}{ctx})"


def choose(models, roles):
    print("\nModels found:")
    for i, m in enumerate(models, 1):
        print(f"  {i}. {describe(m)}")
    print("\nPress Enter to keep the suggestion, or type a number.")
    for role, why in ROLES.items():
        default = models.index(roles[role]) + 1
        answer = input(f"  {role}: {why} [{default}]: ").strip()
        if answer:
            if answer.isdigit() and 1 <= int(answer) <= len(models):
                roles[role] = models[int(answer) - 1]
            else:
                print(f"    Not a number from the list, so keeping {default}.")
    return roles


def choose_builder(options):
    names = {"claude": "Claude Code (best results; uses your Claude plan or API credit)",
             "local": "your local model (free; slower, and small models fail more builds)"}
    print("\nWho should build apps from your ideas?")
    for i, b in enumerate(options, 1):
        print(f"  {i}. {names[b]}")
    answer = input("  Builder [1]: ").strip()
    return options[int(answer) - 1] if answer.isdigit() and 1 <= int(answer) <= len(options) else options[0]


def setup(interactive=True):
    models, notes = detect()
    for n in notes:
        print(n)
    options = builders()
    if not models and options == ["local"]:
        print("No local model server and no Claude Code were found. Install either one (Ollama, LM Studio,\n"
              "llama.cpp, vLLM, Unsloth, or Claude Code) and run python -m forge.cli setup again.\n"
              "Apps that use no model still work without one.")
        return False
    if not models:
        print("No local model server was found, so apps that call a model will not work. Claude Code can still build apps.")
    roles = pick(models, vram_gb()) if models else {}
    if interactive and models:
        roles = choose(models, roles)
    builder = choose_builder(options) if interactive and len(options) > 1 else options[0]
    router.local_path().parent.mkdir(parents=True, exist_ok=True)
    router.local_path().write_text(render(roles, builder), "utf-8")
    print(f"\nSaved to {router.local_path()}:")
    print(f"  builder {'Claude Code' if builder == 'claude' else 'local model'}")
    for role, m in roles.items():
        print(f"  {role:<7} {describe(m)}")
    return True
