"""Entry point: python -m forge.cli serve | setup [--auto] | eval <app> [--json] | improve"""
import json
import sys


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "serve"
    if cmd == "serve":
        from . import dashboard, models, router
        if not router.local_path().exists():
            print("First run: looking for model servers on this computer...")
            if models.setup(interactive=False):
                print("Run `python -m forge.cli setup` any time to choose different models.\n")
        dashboard.serve()
    elif cmd == "setup":
        from . import models
        return 0 if models.setup(interactive=sys.stdin.isatty() and "--auto" not in argv) else 1
    elif cmd == "eval" and len(argv) > 2:
        from . import evals
        card = evals.run(argv[2])
        if "--json" in argv:
            print(json.dumps(card))
        else:
            print(f"{argv[2]}: {card['success']:.0%} of {card['n']} cases pass, slowest {card['p95_ms']} ms, "
                  f"${card['usd_per_req']:.4f} per request")
            for f in card["failed"]:
                print(f"  FAIL {f['id']}: {str(f['got'])[:160]!r}")
    elif cmd == "improve":
        from . import improve
        print(improve.cycle())
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
