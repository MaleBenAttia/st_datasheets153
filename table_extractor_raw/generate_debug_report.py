"""
generate_debug_report.py — Consolidates all _run_report.json into a global debug report.

Usage:
    python generate_debug_report.py

Output:
    outJason/_debug_report_all.json  (structured data)
    outJason/_debug_report_all.txt   (human-readable)
"""

import json
import os
import sys
from pathlib import Path
from datetime import datetime

try:
    from config import OUTPUT_DIR
except ImportError:
    OUTPUT_DIR = Path(__file__).resolve().parent.parent / "outJason"


def _safe_read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception as e:
        return None


def _short_caption(cap: str, max_len: int = 80) -> str:
    if not cap:
        return ""
    return cap[:max_len] + ("..." if len(cap) > max_len else "")


def _fmt_warnings(warns: list) -> str:
    if not warns:
        return ""
    return ", ".join(str(w) for w in warns)


def main():
    t0 = datetime.now()

    if not OUTPUT_DIR.exists():
        print(f"ERROR: {OUTPUT_DIR} does not exist. Run the extraction pipeline first.")
        sys.exit(1)

    # Walk all <family>/<pdf_name>/_run_report.json
    family_map: dict[str, list[dict]] = {}

    for family_dir in sorted(OUTPUT_DIR.iterdir()):
        if not family_dir.is_dir():
            continue
        family = family_dir.name
        family_map.setdefault(family, [])

        for pdf_dir in sorted(family_dir.iterdir()):
            if not pdf_dir.is_dir():
                continue
            report_path = pdf_dir / "_run_report.json"
            if not report_path.exists():
                continue

            summary = _safe_read_json(report_path)
            if summary is None:
                continue

            pdf_name = summary.get("pdf_name", pdf_dir.name)
            worst_raw = summary.get("worst_tables", [])

            # Build per-datasheet entry
            entry = {
                "pdf_name": pdf_name,
                "family": family,
                "tables_found": summary.get("tables_found", 0),
                "tables_extracted": summary.get("tables_extracted", 0),
                "high": summary.get("high", 0),
                "medium": summary.get("medium", 0),
                "low": summary.get("low", 0),
                "failed": summary.get("failed", 0),
                "errors": summary.get("errors", []),
                "worst_tables": worst_raw,
            }
            family_map[family].append(entry)

    if not family_map:
        print(f"No _run_report.json found under {OUTPUT_DIR}")
        sys.exit(0)

    # ── Aggregate ────────────────────────────────────────────────────────────
    all_datasheets = []
    for fam, entries in family_map.items():
        all_datasheets.extend(entries)

    # Global stats
    total_found = sum(e["tables_found"] for e in all_datasheets)
    total_extracted = sum(e["tables_extracted"] for e in all_datasheets)
    total_high = sum(e["high"] for e in all_datasheets)
    total_medium = sum(e["medium"] for e in all_datasheets)
    total_low = sum(e["low"] for e in all_datasheets)
    total_failed = sum(e["failed"] for e in all_datasheets)
    total_errors = sum(len(e["errors"]) for e in all_datasheets)

    # All worst tables across all datasheets
    all_worst = []
    for e in all_datasheets:
        for w in e["worst_tables"]:
            w_copy = dict(w)
            w_copy["_pdf"] = e["pdf_name"]
            w_copy["_family"] = e["family"]
            all_worst.append(w_copy)

    all_worst.sort(key=lambda x: x.get("empty_cell_ratio", 0), reverse=True)

    global_report = {
        "generated_at": t0.isoformat(),
        "total_datasheets": len(all_datasheets),
        "total_families": len(family_map),
        "families": list(family_map.keys()),
        "global_stats": {
            "tables_found": total_found,
            "tables_extracted": total_extracted,
            "high": total_high,
            "medium": total_medium,
            "low": total_low,
            "failed": total_failed,
            "errors": total_errors,
            "extraction_rate_pct": round(total_extracted / total_found * 100, 2) if total_found else 0,
            "high_rate_pct": round(total_high / total_found * 100, 2) if total_found else 0,
        },
        "global_worst_tables_count": len(all_worst),
        "global_worst_tables": all_worst,
        "by_family": {},
        "by_datasheet": all_datasheets,
    }

    # Per-family aggregation
    for fam, entries in family_map.items():
        f_found = sum(e["tables_found"] for e in entries)
        f_extracted = sum(e["tables_extracted"] for e in entries)
        f_high = sum(e["high"] for e in entries)
        f_medium = sum(e["medium"] for e in entries)
        f_low = sum(e["low"] for e in entries)
        f_failed = sum(e["failed"] for e in entries)
        f_worst = [w for e in entries for w in e["worst_tables"]]
        global_report["by_family"][fam] = {
            "datasheets": len(entries),
            "tables_found": f_found,
            "tables_extracted": f_extracted,
            "high": f_high,
            "medium": f_medium,
            "low": f_low,
            "failed": f_failed,
            "worst_tables_count": len(f_worst),
            "worst_tables": sorted(f_worst, key=lambda x: x.get("empty_cell_ratio", 0), reverse=True),
        }

    # ── Write JSON ───────────────────────────────────────────────────────────
    json_path = OUTPUT_DIR / "_debug_report_all.json"
    json_path.write_text(
        json.dumps(global_report, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    print(f"JSON report: {json_path}")

    # ── Write human-readable TXT ─────────────────────────────────────────────
    lines = []
    _w = lines.append
    _w("=" * 70)
    _w("  DEBUG REPORT — Tables Extraction")
    _w(f"  Generated: {t0.strftime('%Y-%m-%d %H:%M:%S')}")
    _w("=" * 70)
    _w("")

    # Global summary
    g = global_report["global_stats"]
    _w("── GLOBAL ─────────────────────────────────────────────────────")
    _w(f"  Datasheets:     {global_report['total_datasheets']}")
    _w(f"  Families:       {global_report['total_families']}")
    _w(f"  Tables found:   {g['tables_found']}")
    _w(f"  Tables extract: {g['tables_extracted']}")
    _w(f"  High:           {g['high']}")
    _w(f"  Medium:         {g['medium']}")
    _w(f"  Low:            {g['low']}")
    _w(f"  Failed:         {g['failed']}")
    _w(f"  Errors:         {g['errors']}")
    _w(f"  Extraction rate: {g['extraction_rate_pct']}%")
    _w(f"  High rate:       {g['high_rate_pct']}%")
    _w(f"  Worst tables:    {global_report['global_worst_tables_count']}")
    _w("")

    # Global worst tables (top 50)
    if all_worst:
        _w("── GLOBAL WORST TABLES ───────────────────────────────────────")
        _w(f"  (showing top {min(50, len(all_worst))} of {len(all_worst)})")
        _w("")
        for i, w in enumerate(all_worst[:50], 1):
            _w(f"  [{i:2d}] {w.get('_family','')}/{w.get('_pdf','')} | {w.get('table_id','')}")
            _w(f"        caption:   {_short_caption(w.get('caption',''))}")
            _w(f"        method:    {w.get('extraction_method','')}")
            _w(f"        conf:      {w.get('confidence','')}")
            _w(f"        ratio:     {w.get('empty_cell_ratio','')}")
            _w(f"        empty:     {w.get('has_empty_cells','')}")
            _w(f"        warnings:  {_fmt_warnings(w.get('warnings',[]))}")
            _w(f"        status:    {w.get('status','')}")
            _w(f"        cols:      {w.get('col_count','')}")
            _w(f"        rows:      {w.get('rows_count','')}")
            _w(f"        headers:   {w.get('headers_preview','')[:120]}")
            heur = w.get("heuristics", {})
            if heur:
                _w(f"        heuristics: {json.dumps(heur, ensure_ascii=False)}")
            _w("")

    # Per-family
    _w("── BY FAMILY ──────────────────────────────────────────────────")
    _w("")
    for fam in sorted(global_report["by_family"]):
        f = global_report["by_family"][fam]
        _w(f"  FAMILY: {fam}")
        _w(f"    Datasheets: {f['datasheets']}  found={f['tables_found']}  extracted={f['tables_extracted']}")
        _w(f"    high={f['high']}  medium={f['medium']}  low={f['low']}  failed={f['failed']}")
        _w(f"    worst_tables: {f['worst_tables_count']}")
        for w in f["worst_tables"][:10]:
            _w(f"      [{w.get('table_id','')}] {_short_caption(w.get('caption',''), 60)}")
            _w(f"        conf={w.get('confidence','')} ratio={w.get('empty_cell_ratio','')} method={w.get('extraction_method','')}")
            if w.get("warnings"):
                _w(f"        warnings: {_fmt_warnings(w['warnings'])}")
        if f["worst_tables_count"] > 10:
            _w(f"        ... and {f['worst_tables_count'] - 10} more")
        _w("")

    # Per-datasheet one-liner
    _w("── BY DATASHEET ───────────────────────────────────────────────")
    _w("")
    _w(f"  {'FAMILY':<10} {'PDF':<30} {'Fd':>4} {'Ext':>4} {'H':>4} {'M':>4} {'L':>4} {'F':>4} {'Worst':>5}")
    _w("  " + "-" * 75)
    for e in sorted(all_datasheets, key=lambda x: (x["family"], x["pdf_name"])):
        n_worst = len(e["worst_tables"])
        _w(f"  {e['family']:<10} {e['pdf_name']:<30} {e['tables_found']:>4d} {e['tables_extracted']:>4d} {e['high']:>4d} {e['medium']:>4d} {e['low']:>4d} {e['failed']:>4d} {n_worst:>5d}")
    _w("")

    # Datasheets with failed/errors
    has_issues = [e for e in all_datasheets if e["failed"] > 0 or e["errors"]]
    if has_issues:
        _w("── DATASHEETS WITH FAILURES ─────────────────────────────────")
        for e in sorted(has_issues, key=lambda x: x["failed"], reverse=True):
            _w(f"  {e['family']}/{e['pdf_name']} — failed={e['failed']} errors={len(e['errors'])}")
            for err in e["errors"][:5]:
                _w(f"    ✗ {err}")
            if len(e["errors"]) > 5:
                _w(f"    ... and {len(e['errors']) - 5} more")
        _w("")

    _w("=" * 70)
    _w(f"  END — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    _w("=" * 70)

    txt_path = OUTPUT_DIR / "_debug_report_all.txt"
    txt_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"TXT  report: {txt_path}")


if __name__ == "__main__":
    main()
