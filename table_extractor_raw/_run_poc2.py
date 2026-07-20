"""
_run_poc2.py — 2 PDFs from U0, F7, F4, G0 → extraction + debug report.
Usage: python _run_poc2.py
"""

import sys
import time
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from table_extractor_raw.main import process_pdf

DATASHEETS = REPO_ROOT / "DataSHEET"

families = ["U0", "F7", "F4", "G0"]
import random
rng = random.Random(42)

pdfs_to_run = []
for fam in families:
    fam_dir = DATASHEETS / fam
    pdfs = sorted(fam_dir.glob("*.pdf"))
    selected = rng.sample(pdfs, min(2, len(pdfs)))
    pdfs_to_run.extend(selected)

print(f"POC2: {len(pdfs_to_run)} PDFs from {families}")
for p in pdfs_to_run:
    print(f"  {p.parent.name}/{p.name}")

t0 = time.time()
ok = failed = 0
with ProcessPoolExecutor(max_workers=4) as executor:
    futures = {
        executor.submit(process_pdf, pdf, pdf.parent.name): pdf
        for pdf in pdfs_to_run
    }
    for future in as_completed(futures):
        pdf = futures[future]
        try:
            summary = future.result()
            if summary["failed"] == 0 and summary["tables_extracted"] == summary["tables_found"]:
                ok += 1
            else:
                failed += 1
                print(f"  {summary['pdf_name']}: {summary['failed']}/{summary['tables_found']} failed")
        except Exception as e:
            failed += 1
            print(f"  {pdf.name}: {e}")

elapsed = time.time() - t0
print(f"\nPOC2 extraction: {ok} OK / {failed} FAILED in {elapsed:.0f}s")

print(f"\nGenerating debug report...")
import generate_debug_report
generate_debug_report.main()
