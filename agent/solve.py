"""Take a template JSON in, write a verdicts JSON out.

    python agent/solve.py <template.json> -o <verdicts.json>

The template tells the agent WHICH borrowers and clauses to answer -- it derives
its entire target list from that file and assumes no scenario ids of its own. The
documents and the ledger are read from the template's own directory unless
--archive says otherwise, because in the contest archive they sit side by side:

    <archive>/submission_template.json     <- what you pass in
    <archive>/master_ledger_2025.csv
    <archive>/documents/*.pdf

Examples
--------
    # simplest: everything lives next to the template
    python agent/solve.py agentic-bank-public/submission_template.json -o verdicts.json

    # template somewhere else
    python agent/solve.py ~/Downloads/template.json -o out.json --archive ~/Downloads/private

    # deterministic only (12 s, no API needed)
    python agent/solve.py agentic-bank-public/submission_template.json -o out.json --no-llm

    # emit ground_truth.json shape instead of the submission shape
    python agent/solve.py ... -o key.json --format key
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main():
    ap = argparse.ArgumentParser(
        description="Read a covenant answer template, emit filled-in verdicts.")
    ap.add_argument("template", help="path to submission_template.json")
    ap.add_argument("-o", "--output", required=True, help="path to write verdicts JSON")
    ap.add_argument("--archive", default=None,
                    help="directory holding documents/ and master_ledger_2025.csv "
                         "(default: the template's own directory)")
    ap.add_argument("--format", choices=["submission", "key"], default="submission",
                    help="submission = the contest template shape (default); "
                         "key = ground_truth.json shape, for diffing")
    ap.add_argument("--no-llm", action="store_true", help="deterministic core only")
    ap.add_argument("--review", action="store_true",
                    help="also run the advisory LLM review pass (slow)")
    ap.add_argument("--work", default=None, help="scratch directory for intermediates")
    a = ap.parse_args()

    template = Path(a.template).expanduser().resolve()
    if not template.is_file():
        raise SystemExit(f"template not found: {template}")
    archive = Path(a.archive).expanduser().resolve() if a.archive else template.parent
    output = Path(a.output).expanduser().resolve()

    missing = [n for n in ("master_ledger_2025.csv", "documents")
               if not (archive / n).exists()]
    if missing:
        raise SystemExit(
            f"archive {archive} is missing: {', '.join(missing)}\n"
            f"pass --archive <dir> pointing at the folder that holds them")

    # If the template is not the one inside the archive, stage a copy so every
    # phase reads the same file. The agent answers exactly the cells it lists.
    staged = archive / "submission_template.json"
    if template != staged:
        staged.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")

    work = Path(a.work).expanduser().resolve() if a.work else HERE / "work"
    os.environ["HALYK_ARCHIVE"] = str(archive)
    os.environ["AGENT_WORK"] = str(work)
    sys.path.insert(0, str(HERE))

    t0 = time.time()
    import run as agent_run                                    # noqa: E402

    argv = sys.argv
    sys.argv = ["run.py"] + (["--no-llm"] if a.no_llm else []) \
        + ([] if a.review else ["--no-review"])
    try:
        agent_run.main()
    finally:
        sys.argv = argv

    latest = work / "submission.json"
    sub = json.loads(latest.read_text(encoding="utf-8"))

    tpl = json.loads(template.read_text(encoding="utf-8"))
    want = {(s, c) for s, cs in tpl["answers"].items() for c in cs}
    got = {(s, c) for s, cs in sub["answers"].items() for c in cs}
    if want != got:
        print(f"!! answered cells differ from the template: "
              f"missing {sorted(want - got)}, extra {sorted(got - want)}")

    payload = sub if a.format == "submission" else agent_run.as_key_shape(sub)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print(f"\ntemplate in  : {template}")
    print(f"archive      : {archive}")
    print(f"verdicts out : {output}   [{a.format} shape, {len(got)} cells]")
    print(f"elapsed      : {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
