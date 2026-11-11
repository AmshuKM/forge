"""Sandbox: runs generated code in a locked Docker container. Fails closed when Docker is missing."""
import json
import os
import shutil
import subprocess
from pathlib import Path

from . import router


def available():
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _run(args, timeout=900):
    try:
        p = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "timed out"
    return p.returncode == 0, (p.stdout + p.stderr)[-6000:]


def _docker(root, image, cmd, network=False, env=(), cache=False, mounts=()):
    if not available():
        return False, "Docker is not running, so nothing was executed."
    cfg = router.config()["sandbox"]
    args = ["docker", "run", "--rm", "--memory", cfg["memory"], "--cpus", "2", "--pids-limit", "256",
            "--read-only", "--tmpfs", "/tmp", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "-v", f"{Path(root).resolve()}:/src:ro", "-w", "/src", "-e", "PYTHONDONTWRITEBYTECODE=1"]
    args += ["--add-host", "host.docker.internal:host-gateway"] if network else ["--network", "none"]
    if cache:  # scanners need a writable home, and re-downloading their rule databases every run is wasteful
        args += ["-v", "forge-scan-cache:/cache", "-e", "HOME=/cache", "-e", "XDG_CACHE_HOME=/cache"]
    for src, dst in mounts:
        args += ["-v", f"{src}:{dst}:ro"]
    for e in env:
        args += ["-e", e]
    return _run(args + [image] + list(cmd))


def tests(root, app, pattern="test*.py"):
    """Unit tests: no network, read-only files, memory-capped."""
    image = router.config()["sandbox"]["image"]
    return _docker(root, image, ["python", "-m", "unittest", "discover", "-s", f"apps/{app}/tests", "-p", pattern])


def scans(root):
    ok_all, report = True, []
    for image, *cmd in router.config()["sandbox"]["scans"]:
        ok, out = _docker(root, image, cmd, network=True, cache=True)  # scanners download their own rule sets
        ok_all &= ok
        report.append(f"[{'pass' if ok else 'FAIL'}] {image}\n{out[-1200:]}")
    return ok_all, "\n\n".join(report)


def evals(root, app):
    """Golden set against candidate code, inside the container. Returns the scorecard."""
    # shortcut: the container gets network to reach the local model server and no API keys,
    # so candidate code is scored on local models only; upgrade to a model proxy if you add cloud-only tasks
    cfg = router.config()
    env = ["FORGE_DB=/tmp/forge.db"] + (["FORGE_MOCK=1"] if os.environ.get("FORGE_MOCK") else [])
    for name, ep in cfg["endpoints"].items():
        base = ep["base"].replace("localhost", "host.docker.internal").replace("127.0.0.1", "host.docker.internal")
        env.append(f"FORGE_BASE_{name.upper()}={base}")
    mounts = []
    if router.local_path().is_file():  # the change's copy has no local.toml, so lend it this computer's models
        mounts.append((router.local_path().resolve(), "/forge/local.toml"))
        env.append("FORGE_LOCAL_CONFIG=/forge/local.toml")
    ok, out = _docker(root, cfg["sandbox"]["image"], ["python", "-m", "forge.cli", "eval", app, "--json"],
                      network=True, env=env, mounts=mounts)
    if not ok:
        raise RuntimeError("evaluation in the sandbox failed:\n" + out[-1500:])
    return json.loads(out.strip().splitlines()[-1])
