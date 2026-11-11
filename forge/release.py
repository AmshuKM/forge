"""Human decisions and release: approve -> trial on part of the traffic -> keep (merge + tag) or discard."""
import json
import threading
import time

from . import evals, router, store
from .store import ROOT, q, x

_lock = threading.Lock()


def _git(*args):
    from .improve import git
    return git(*args)


def _proposal(pid, status):
    rows = q("select * from proposals where id=?", int(pid))
    if not rows or rows[0]["status"] != status:
        raise ValueError(f"Change {pid} is not {status}.")
    return rows[0]


def cleanup(pid):
    for args in (("worktree", "remove", "--force", str(ROOT / ".forge" / "wt" / f"p{pid}")),
                 ("branch", "-D", f"proposal/{pid}")):
        try:
            _git(*args)
        except RuntimeError:
            pass  # already gone


def close(p, status, issue_status, reason):
    """Record a final outcome so later proposals can learn from it."""
    x("update proposals set status=?, reason=? where id=?", status, reason, p["id"])
    if p["issue_id"]:
        x("update issues set status=?, seen_count=count where id=?", issue_status, p["issue_id"])
    store.remember(status, f"{p['app']} {p['channel']}", f"{p['summary']} -> {status}. {reason}")


def approve(pid):
    p = _proposal(pid, "awaiting")
    if not (ROOT / "apps" / p["app"] / "app.py").exists():  # a brand-new app has no live traffic to trial against
        with _lock:
            return _promote(p, "New app: released without a trial.")
    if store.kv_get("canary"):
        raise ValueError("Another change is still in its trial. Wait for it to finish first.")
    cfg = router.config()["release"]
    store.kv_set("canary", {"proposal": p["id"], "path": str(ROOT / ".forge" / "wt" / f"p{p['id']}"),
                            "pct": cfg["canary_percent"], "need": cfg["canary_requests"], "since": time.time()})
    x("update proposals set status='canary', reason=null where id=?", p["id"])


def reject(pid, reason):
    p = _proposal(pid, "awaiting")
    close(p, "rejected", "rejected", f"Rejected by the owner: {reason or 'no reason given'}")
    cleanup(p["id"])


def check():
    """Called after each trial request: keep the change, discard it, or keep watching."""
    with _lock:
        c = store.kv_get("canary")
        if not c:
            return
        p = q("select * from proposals where id=?", c["proposal"])[0]
        rows = q("select fingerprint from traces where version=? and ts>=?", f"canary:p{p['id']}", c["since"])
        n, bad = len(rows), sum(r["fingerprint"] is not None for r in rows)
        stable = q("""select avg(fingerprint is not null) r from
                      (select fingerprint from traces where version='stable' order by ts desc limit 50)""")[0]["r"] or 0
        if (bad >= 3 and bad / n > 0.5) or (n >= c["need"] and bad / n > stable + 0.1):
            store.kv_set("canary", None)
            close(p, "rolled_back", "hold", f"Trial failed: {bad} of {n} requests failed. Nothing reached main.")
            cleanup(p["id"])
        elif n >= c["need"]:
            store.kv_set("canary", None)
            _promote(p, f"Trial passed: {bad} of {n} requests failed.")


def _promote(p, note):
    """Merge and tag. If the merge fails nothing changes and the change goes back to awaiting."""
    try:
        if _git("rev-parse", "--abbrev-ref", "HEAD").strip() != "main":
            raise RuntimeError("the project folder is not on the main branch")
        _git("merge", "--no-ff", "-m", f"release: change {p['id']}: {p['summary']}", p["branch"])
        _git("tag", "-f", f"release-{p['id']}")
    except RuntimeError as e:
        try:
            _git("merge", "--abort")
        except RuntimeError:
            pass
        x("update proposals set status='awaiting', reason=? where id=?",
          f"The merge failed, so nothing changed: {e}", p["id"])
        return
    after = json.loads(p["gate"] or "{}").get("after")
    if after:
        evals.save(p["app"], "stable", after)
    close(p, "shipped", "fixed", note)
    cleanup(p["id"])


def undo(pid):
    """Manual rollback of a shipped change: a revert commit, so history stays intact."""
    p = _proposal(pid, "shipped")
    try:
        _git("revert", "-m", "1", "--no-edit", f"release-{p['id']}")
    except RuntimeError as e:
        raise ValueError(f"Could not undo change {p['id']}: {e}") from None
    close(p, "rolled_back", "hold", "Undone by the owner after release.")
