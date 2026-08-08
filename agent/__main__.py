"""`python -m agent <command>` -- one entrypoint for everything.

    python -m agent check                      # is the archive + API ready?
    python -m agent solve <template> -o <out>  # verdicts in, verdicts out
    python -m agent run                        # full run into work/runs/tryN
    python -m agent compare [tryA tryB]        # score / diff attempts
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

USAGE = __doc__


def cmd_check(argv):
    """Verify the archive is complete and the API answers, before a timed run."""
    import llm
    from config import ARCHIVE, LLMConfig

    print(f"archive : {ARCHIVE}")
    ok = True
    for name, what in (("submission_template.json", "which cells to answer"),
                       ("master_ledger_2025.csv", "transactions"),
                       ("documents", "the PDFs")):
        p = ARCHIVE / name
        mark = "OK " if p.exists() else "MISSING"
        if not p.exists():
            ok = False
        extra = ""
        if p.exists() and name == "documents":
            extra = f"  ({len(list(p.glob('*.pdf')))} pdfs)"
        elif p.exists() and name == "submission_template.json":
            import json
            t = json.loads(p.read_text(encoding="utf-8"))["answers"]
            extra = (f"  ({len(t)} borrowers, "
                     f"{sum(len(v) for v in t.values())} cells: "
                     f"{', '.join(list(t)[:6])}{' ...' if len(t) > 6 else ''})")
        print(f"  {mark:8} {name:28} {what}{extra}")
    print(f"  {'OK ' if (ARCHIVE / 'ground_truth.json').exists() else '-- ':8} "
          f"{'ground_truth.json':28} answer key (absent on the private set -- expected)")

    print()
    if not LLMConfig.available():
        print(f"LLM     : disabled -- {LLMConfig.describe()}")
        print("          run with --no-llm; the deterministic core is unaffected")
    else:
        good, msg = llm.preflight()
        print(f"LLM     : {'OK  ' if good else 'FAIL'} {msg}")
        if good:
            models = llm.list_models()
            if models:
                print(f"          {len(models)} models reachable")
        else:
            print("          run with --no-llm")
    print()
    print("ready" if ok else "NOT READY -- fix the MISSING entries above")
    return 0 if ok else 1


def cmd_solve(argv):
    import solve
    sys.argv = ["solve.py"] + argv
    solve.main()
    return 0


def cmd_run(argv):
    import run
    sys.argv = ["run.py"] + argv
    run.main()
    return 0


def cmd_compare(argv):
    import compare
    sys.argv = ["compare.py"] + argv
    if len(argv) == 2:
        compare.diff(argv[0], argv[1])
    else:
        compare.table()
    return 0


COMMANDS = {"check": cmd_check, "solve": cmd_solve, "run": cmd_run,
            "compare": cmd_compare}


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    cmd = sys.argv[1]
    if cmd not in COMMANDS:
        print(f"unknown command: {cmd}\n{USAGE}")
        return 2
    return COMMANDS[cmd](sys.argv[2:])


if __name__ == "__main__":
    raise SystemExit(main())
