"""
_run_batch_all.py — extracts ALL datasheets, then prints a detailed
per-family / per-datasheet report of all problems in the terminal.

Usage: python _run_batch_all.py [--workers 10]
"""

import sys
import time
import json
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from table_extractor_raw.main import process_pdf

DATASHEETS = REPO_ROOT / "DataSHEET"

# ── helpers ──────────────────────────────────────────────────────────────────

def _pretty_pct(v: float) -> str:
    return f"{v * 100:.1f}%"

def _fmt_badge(conf: str) -> str:
    return {"high": "HIGH", "medium": "MED", "low": "LOW", "failed": "FAIL"}.get(conf, conf.upper())

# ── report ───────────────────────────────────────────────────────────────────

def print_report(results: list[dict]):
    by_family: dict[str, list[dict]] = {}
    for r in results:
        by_family.setdefault(r.get("family", "?"), []).append(r)

    total_pdfs = len(results)
    total_high = total_medium = total_low = total_failed = 0
    total_issues = 0

    for fam in sorted(by_family):
        fam_results = by_family[fam]
        fam_high = fam_medium = fam_low = fam_failed = 0
        fam_pdfs_ok = fam_pdfs_fail = 0

        print()
        print(f"\u2554\u2550\u2550\u2550 FAMILLE {fam} ({len(fam_results)} pdfs) \u2550" + "\u2550" * 20)
        for r in sorted(fam_results, key=lambda x: x.get("pdf_name", "")):
            name = r.get("pdf_name", "?")
            n_found = r.get("tables_found", 0)
            n_extracted = r.get("tables_extracted", 0)
            n_high = r.get("high", 0)
            n_med = r.get("medium", 0)
            n_low = r.get("low", 0)
            n_fail = r.get("failed", 0)
            n_errors = len(r.get("errors", []))
            worst = r.get("worst_tables", [])

            ok = n_fail == 0 and n_extracted == n_found
            status = "OK" if ok else "!!"

            print(
                f"\u255f {name:<30s}  {n_extracted}/{n_found}  "
                f"high={n_high} med={n_med} low={n_low} fail={n_fail}  [{status}]"
            )

            fam_high += n_high
            fam_med += n_med
            fam_low += n_low
            fam_fail += n_fail
            if ok:
                fam_pdfs_ok += 1
            else:
                fam_pdfs_fail += 1

            # indent each problematic table
            for w in worst:
                tid = w.get("table_id", "?")
                conf = w.get("confidence", "?")
                empty = w.get("empty_cell_ratio", 0)
                has_empty = w.get("has_empty_cells", False)
                warns = w.get("warnings", [])
                cap = (w.get("caption") or "")[:80]
                rows = w.get("rows_count", 0)

                parts = []
                if conf != "high":
                    parts.append(f"conf={conf}")
                if has_empty:
                    parts.append(f"empty={_pretty_pct(empty)}")
                if warns:
                    parts.append(f"warns={warns}")
                if n_errors > 0:
                    parts.append(f"errors={n_errors}")

                print(f"  \u2570 {tid:<12s} pg={w.get('page','?')} rows={rows}  {' | '.join(parts)}")
                if cap:
                    print(f"     \"{cap}\"")
                total_issues += 1

        print(
            f"\u255a \u2500\u2500 fam total: high={fam_high} med={fam_med} "
            f"low={fam_low} fail={fam_fail}  pdfs: {fam_pdfs_ok} OK / {fam_pdfs_fail} FAIL"
        )

        total_high += fam_high
        total_medium += fam_med
        total_low += fam_low
        total_failed += fam_fail

    # ── Global summary ──
    print()
    print("\u2550" * 60)
    print(f"  BATCH COMPLETE: {total_pdfs} PDFs")
    print(f"  Tables: {total_high} HIGH / {total_medium} MED / {total_low} LOW / {total_failed} FAILED")
    print(f"  Tables with issues: {total_issues}")
    print(f"  Output: outJason/<fam>/<pdf>/")
    print("\u2550" * 60)


# ── main ─────────────────────────────────────────────────────────────────────

def main(workers: int = 10):
    families = sorted(d.name for d in DATASHEETS.iterdir() if d.is_dir())
    pdfs_to_run: list[tuple[Path, str]] = []
    for fam in families:
        fam_dir = DATASHEETS / fam
        for pdf in sorted(fam_dir.glob("*.pdf")):
            pdfs_to_run.append((pdf, fam))

    total = len(pdfs_to_run)
    print(f"Batch: {total} PDFs across {len(families)} families ({workers} workers)")
    t0 = time.time()

    results: list[dict] = []

    with ProcessPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(process_pdf, pdf, fam): (pdf, fam)
            for pdf, fam in pdfs_to_run
        }
        for future in as_completed(futures):
            pdf, fam = futures[future]
            try:
                summary = future.result()
                summary["family"] = fam
                results.append(summary)
                name = summary.get("pdf_name", pdf.stem)
                n_fail = summary.get("failed", 0)
                n_tot = summary.get("tables_found", 0)
                status = "OK" if (n_fail == 0 and summary.get("tables_extracted") == n_tot) else "FAIL"
                print(f"  [{status}] {fam}/{name}  ({summary.get('tables_extracted',0)}/{n_tot})")
            except Exception as e:
                print(f"  [ERR] {fam}/{pdf.name}: {e}")
                results.append({
                    "pdf_name": pdf.stem,
                    "family": fam,
                    "tables_found": 0,
                    "tables_extracted": 0,
                    "high": 0, "medium": 0, "low": 0, "failed": 0,
                    "errors": [str(e)],
                    "worst_tables": [],
                })

    elapsed = time.time() - t0
    print(f"\nExtraction done in {elapsed:.0f}s. Generating report...\n")

    print_report(results)

    # Save raw summary to JSON for later inspection
    report_path = REPO_ROOT / "outJason" / "_batch_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(results, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8"
    )
    print(f"\nRaw report saved to {report_path}")


if __name__ == "__main__":
    workers = 10
    if len(sys.argv) > 1 and sys.argv[1].startswith("--workers="):
        workers = int(sys.argv[1].split("=")[1])
    elif len(sys.argv) > 2 and sys.argv[1] == "--workers":
        workers = int(sys.argv[2])
    main(workers=workers)
