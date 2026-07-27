"""Reconstruct batch report from existing outJason directories (no extraction)."""

import io
import json
import sys
from pathlib import Path

# Force UTF-8 sur stdout (évite cp1252 errors avec caractères Unicode)
try:
    if sys.stdout.encoding != "utf-8":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass

OUT = Path(__file__).resolve().parent.parent / "outJason"

def _pretty_pct(v):
    return f"{v * 100:.1f}%"

by_family = {}

for fam_dir in sorted(OUT.iterdir()):
    if not fam_dir.is_dir() or fam_dir.name.startswith("_"):
        continue
    for pdf_dir in sorted(fam_dir.iterdir()):
        if not pdf_dir.is_dir():
            continue
        all_path = pdf_dir / "_all_tables.json"
        if not all_path.exists():
            continue
        data = json.loads(all_path.read_text(encoding="utf-8"))

        features = {}
        if data and isinstance(data[0], dict) and "features" in data[0]:
            features = data[0]
            data = data[1:]

        pdf_name = data[0].get("pdf_name", pdf_dir.name)
        n_high = n_med = n_low = n_fail = 0
        worst = []
        for t in data:
            conf = t.get("extraction_confidence", "?")
            if conf == "high":
                n_high += 1
            elif conf == "medium":
                n_med += 1
            elif conf == "low":
                n_low += 1
            elif conf == "failed":
                n_fail += 1

            empty = t.get("empty_cell_ratio", 0)
            has_empty = t.get("has_empty_cells", False)
            warns = t.get("warnings", [])
            if conf != "high" or has_empty or warns:
                worst.append({
                    "table_id": t.get("table_id", "?"),
                    "page": t.get("page", "?"),
                    "confidence": conf,
                    "empty_cell_ratio": empty,
                    "has_empty_cells": has_empty,
                    "warnings": warns,
                    "caption": (t.get("caption") or "")[:80],
                    "rows_count": len(t.get("rows", [])),
                })

        r = {
            "pdf_name": pdf_name,
            "family": fam_dir.name,
            "tables_found": len(data),
            "tables_extracted": len(data),
            "high": n_high,
            "medium": n_med,
            "low": n_low,
            "failed": n_fail,
            "errors": [],
            "worst_tables": worst,
        }
        by_family.setdefault(fam_dir.name, []).append(r)

total_pdfs = sum(len(v) for v in by_family.values())
total_high = total_medium = total_low = total_failed = 0
total_issues = 0

for fam in sorted(by_family):
    fam_results = by_family[fam]
    fam_high = fam_med = fam_low = fam_fail = 0
    fam_pdfs_ok = fam_pdfs_fail = 0
    print()
    print(f"FAMILLE {fam} ({len(fam_results)} pdfs)")
    print("=" * 60)
    for r in sorted(fam_results, key=lambda x: x.get("pdf_name", "")):
        name = r.get("pdf_name", "?")
        n_found = r.get("tables_found", 0)
        n_high = r.get("high", 0)
        n_med = r.get("medium", 0)
        n_low = r.get("low", 0)
        n_fail = r.get("failed", 0)
        worst = r.get("worst_tables", [])
        ok = n_fail == 0
        status = "OK" if ok else "!!"
        print(f"  {name:<30s}  {n_found}/{n_found}  high={n_high} med={n_med} low={n_low} fail={n_fail}  [{status}]")
        fam_high += n_high; fam_med += n_med; fam_low += n_low; fam_fail += n_fail
        if ok: fam_pdfs_ok += 1
        else: fam_pdfs_fail += 1
        for w in worst:
            tid = w.get("table_id", "?")
            conf = w.get("confidence", "?")
            empty = w.get("empty_cell_ratio", 0)
            has_empty = w.get("has_empty_cells", False)
            warns = w.get("warnings", [])
            cap = w.get("caption", "")[:80]
            rows = w.get("rows_count", 0)
            parts = []
            if conf != "high": parts.append(f"conf={conf}")
            if has_empty: parts.append(f"empty={_pretty_pct(empty)}")
            if warns: parts.append(f"warns={warns}")
            print(f"    -> {tid:<12s} pg={w.get('page','?')} rows={rows}  {' | '.join(parts)}")
            if cap: print(f'       "{cap}"')
            total_issues += 1
    print(f"  total: high={fam_high} med={fam_med} low={fam_low} fail={fam_fail}  pdfs: {fam_pdfs_ok} OK / {fam_pdfs_fail} FAIL")
    total_high += fam_high; total_medium += fam_med; total_low += fam_low; total_failed += fam_fail

print()
print("=" * 60)
print(f"  BATCH COMPLETE: {total_pdfs} PDFs")
print(f"  Tables: {total_high} HIGH / {total_medium} MED / {total_low} LOW / {total_failed} FAILED")
print(f"  Tables with issues: {total_issues}")
print()
print("--- TABLES AVEC PROBLEMES ---")
for fam in sorted(by_family):
    for r in by_family[fam]:
        for w in r.get("worst_tables", []):
            tid = w.get("table_id", "?")
            conf = w.get("confidence", "?")
            if conf == "high" and not w.get("has_empty_cells") and not w.get("warnings"):
                continue
            empty = w.get("empty_cell_ratio", 0)
            has_empty = w.get("has_empty_cells", False)
            warns = w.get("warnings", [])
            parts = []
            if conf != "high": parts.append(conf)
            if has_empty: parts.append(f"empty={_pretty_pct(empty)}")
            if warns: parts.append(str(warns))
            print(f"  {fam}/{r['pdf_name']} {tid} page={w.get('page','?')} rows={w.get('rows_count',0)}  {', '.join(parts)}")
print("=" * 60)
