"""Evaluation engine: runs the golden set, produces the scorecard, compares to baseline. It only scores."""
import json
import time
from pathlib import Path

from . import router, runtime, store

NOISE = 0.05  # a success-rate change smaller than this is treated as run-to-run noise


def run(app, root=None):
    root = Path(root or store.ROOT)
    lines = (root / "apps" / app / "evals" / "golden.jsonl").read_text("utf-8").splitlines()
    rows = []
    for case in (json.loads(line) for line in lines if line.strip()):
        r = runtime.handle(app, case["input"], root=root, version="eval", cache=False)
        out = r["output"]
        text = " ".join(str(v) for v in out.values()) if isinstance(out, dict) else str(out or "")
        low = text.lower()
        passed = (r["ok"]
                  and ("expect" not in case or out == case["expect"])
                  and all(s.lower() in low for s in case.get("must_include", []))
                  and not any(s.lower() in low for s in case.get("must_not_include", []))
                  and len(text) <= case.get("max_chars", 10**9))
        if passed and case.get("rubric"):
            prompt = runtime.render(root, "forge_judge", input=json.dumps(case["input"]), answer=text,
                                    rubric=case["rubric"])
            verdict = router.call("judge", [{"role": "user", "content": prompt}], root=root, cache=False)
            passed = verdict.strip().upper().startswith("PASS")
        rows.append({"id": case["id"], "pass": passed, "ms": r["ms"], "usd": r["usd"],
                     "got": text[:500] or r["error"], "case": case})
    ms = sorted(r["ms"] for r in rows)
    return {
        "n": len(rows),
        "success": sum(r["pass"] for r in rows) / len(rows),
        "p95_ms": ms[min(len(ms) - 1, int(0.95 * len(ms)))],
        "usd_per_req": sum(r["usd"] for r in rows) / len(rows),
        "failed": [{"id": r["id"], "got": r["got"], "case": r["case"]} for r in rows if not r["pass"]],
    }


def compare(base, cand, must_improve, limit_ms):
    """Return the reasons the candidate fails the gate; an empty list means it passes."""
    why = []
    if cand["success"] < base["success"] - 1e-9:
        why.append(f"success rate fell from {base['success']:.0%} to {cand['success']:.0%}")
    if cand["p95_ms"] > limit_ms:
        why.append(f"slowest request took {cand['p95_ms']} ms, over the {limit_ms} ms limit")
    if cand["usd_per_req"] > base["usd_per_req"] * 1.2 + 1e-9:
        why.append("cost per request rose by more than 20%")
    better = (cand["success"] > base["success"] + NOISE
              or cand["usd_per_req"] < base["usd_per_req"] * 0.9
              or cand["p95_ms"] < base["p95_ms"] * 0.7)
    if must_improve and not why and not better:
        why.append("no measurable improvement over the current version")
    return why


def save(app, version, card):
    store.x("insert into scorecards(ts,app,version,card) values(?,?,?,?)", time.time(), app, version, json.dumps(card))


def latest(app):
    rows = store.q("select card from scorecards where app=? and version='stable' order by id desc limit 1", app)
    return json.loads(rows[0]["card"]) if rows else None
