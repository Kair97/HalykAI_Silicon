"""Phase 0: pdftotext -layout every PDF into txt/<hash>.txt. Cached.

Layout mode is required: covenant thresholds and KYC ownership percentages live
in table cells that collapse into unusable word soup without it.
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(os.environ.get("HALYK_ROOT",
                           Path(__file__).resolve().parent.parent))
ARCHIVE = Path(os.environ.get("HALYK_ARCHIVE", ROOT / "agentic-bank-public"))
SRC = ARCHIVE / "documents"
DST = ROOT / "txt"


def main(force=False):
    DST.mkdir(parents=True, exist_ok=True)
    made = skipped = 0
    for pdf in sorted(SRC.glob("*.pdf")):
        out = DST / f"{pdf.stem}.txt"
        if out.exists() and not force:
            skipped += 1
            continue
        subprocess.run(["pdftotext", "-layout", "-enc", "UTF-8", str(pdf), str(out)],
                       check=True)
        made += 1
    print(f"extracted {made}, cached {skipped}, total {len(list(DST.glob('*.txt')))}")


if __name__ == "__main__":
    main(force="--force" in sys.argv)
