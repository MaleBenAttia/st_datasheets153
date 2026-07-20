"""
bug_report.py — Rapport complet des erreurs, bugs et problèmes d'extraction.

Usage:
    python bug_report.py

Output:
    outJason/_bug_report_complet.txt
"""
from __future__ import annotations
import json
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
    except Exception:
        return None


def _short(s: str, n: int = 80) -> str:
    if not s:
        return ""
    return s[:n] + ("..." if len(s) > n else "")


def main():
    t0 = datetime.now()
    if not OUTPUT_DIR.exists():
        print(f"ERREUR: {OUTPUT_DIR} n'existe pas. Lancez d'abord l'extraction.")
        sys.exit(1)

    reports: list[dict] = []

    for family_dir in sorted(OUTPUT_DIR.iterdir()):
        if not family_dir.is_dir():
            continue
        for pdf_dir in sorted(family_dir.iterdir()):
            if not pdf_dir.is_dir():
                continue
            rp = pdf_dir / "_run_report.json"
            if not rp.exists():
                continue
            s = _safe_read_json(rp)
            if s:
                s["_family"] = family_dir.name
                reports.append(s)

    if not reports:
        print(f"Aucun rapport trouvé sous {OUTPUT_DIR}")
        sys.exit(0)

    # ── Aggrégation ──────────────────────────────────────────────────
    total_pdf = len(reports)
    total_found = sum(r.get("tables_found", 0) for r in reports)
    total_extracted = sum(r.get("tables_extracted", 0) for r in reports)
    total_high = sum(r.get("high", 0) for r in reports)
    total_medium = sum(r.get("medium", 0) for r in reports)
    total_low = sum(r.get("low", 0) for r in reports)
    total_failed = sum(r.get("failed", 0) for r in reports)
    total_drawings = sum(r.get("drawing_failed", 0) for r in reports)
    total_bugs = sum(r.get("bug_suspected", 0) for r in reports)

    all_errors: list[dict] = []
    all_worst: list[dict] = []
    all_warnings: list[dict] = []
    zero_extracted: list[dict] = []

    for r in reports:
        fam = r["_family"]
        name = r.get("pdf_name", "")
        for err in r.get("errors", []):
            all_errors.append({"family": fam, "pdf": name, "error": err})
        for w in r.get("worst_tables", []):
            w["_family"] = fam
            w["_pdf"] = name
            all_worst.append(w)
        warns = [t for t in r.get("worst_tables", []) if t.get("warnings")]
        for w in warns:
            all_warnings.append({"family": fam, "pdf": name, "table": w.get("table_id", ""), "warnings": w.get("warnings", [])})
        if r.get("tables_found", 0) > 0 and r.get("tables_extracted", 0) == 0:
            zero_extracted.append({"family": fam, "pdf": name, "found": r["tables_found"], "errors": r.get("errors", [])})

    all_worst.sort(key=lambda x: x.get("empty_cell_ratio", 0), reverse=True)

    lines = []
    W = lines.append

    W("=" * 80)
    W("  RAPPORT COMPLET — BUGS / ERREURS / PROBLÈMES D'EXTRACTION")
    W(f"  Généré le {t0.strftime('%Y-%m-%d %H:%M:%S')}")
    W("=" * 80)
    W("")

    # ── 1. RÉSUMÉ GLOBAL ──────────────────────────────────────────────
    W("─" * 80)
    W("  1. RÉSUMÉ GLOBAL")
    W("─" * 80)
    W(f"  PDFs traités :            {total_pdf}")
    W(f"  Tables trouvées :         {total_found}")
    W(f"  Tables extraites :        {total_extracted}")
    if total_found:
        W(f"  Taux d'extraction :       {total_extracted/total_found*100:.2f}%")
    W(f"  ─ high :                  {total_high}")
    W(f"  ─ medium :                {total_medium}")
    W(f"  ─ low :                   {total_low}")
    W(f"  ─ failed :                {total_failed}")
    W(f"  ─ dessins mécaniques :    {total_drawings}")
    W(f"  ─ bugs suspects :         {total_bugs}")
    W(f"  Erreurs totales :         {len(all_errors)}")
    W(f"  Tables problématiques :   {len(all_worst)}")
    W(f"  PDFs sans extraction :    {len(zero_extracted)}")
    W("")

    # ── 2. PDFs SANS AUCUNE EXTRACTION ────────────────────────────────
    W("─" * 80)
    W("  2. PDFs AVEC 0 TABLE EXTRAITE (ÉCHEC TOTAL)")
    W("─" * 80)
    if zero_extracted:
        for z in sorted(zero_extracted, key=lambda x: x["pdf"]):
            W(f"  [{z['family']}/{z['pdf']}]")
            W(f"      tables trouvées: {z['found']}")
            for e in z["errors"][:5]:
                W(f"      ✗ {e}")
            if len(z["errors"]) > 5:
                W(f"      ... et {len(z['errors'])-5} autres")
        W("")
    else:
        W("  (aucun)")
        W("")

    # ── 3. TOUTES LES ERREURS ─────────────────────────────────────────
    W("─" * 80)
    W("  3. LISTE COMPLÈTE DES ERREURS")
    W("─" * 80)
    if all_errors:
        for e in sorted(all_errors, key=lambda x: (x["family"], x["pdf"])):
            W(f"  [{e['family']}/{e['pdf']}]")
            W(f"      ✗ {e['error']}")
        W("")
    else:
        W("  (aucune)")
        W("")

    # ── 4. PROBLÈMES DE CHAÎNES VIDES / "" ──────────────────────────
    W("─" * 80)
    W("  4. PROBLÈMES DE CHAÎNES VIDES / HEADERS VIDES / CAPTIONS VIDES")
    W("─" * 80)
    empty_headers: list[dict] = []
    empty_captions: list[dict] = []
    high_empty_cells: list[dict] = []

    for w in all_worst:
        headers = w.get("headers_preview", "")
        if headers in ("[]", "", '""'):
            empty_headers.append(w)
        cap = w.get("caption", "")
        if not cap or cap.strip() == "":
            empty_captions.append(w)
        if w.get("has_empty_cells") and w.get("empty_cell_ratio", 0) > 0.3:
            high_empty_cells.append(w)

    if empty_headers:
        W(f"  Headers vides : {len(empty_headers)} tables")
        for h in empty_headers[:30]:
            W(f"    [{h['_family']}/{h['_pdf']}] {h.get('table_id','')} — ratio={h.get('empty_cell_ratio','')}")
    else:
        W("  Aucun header vide détecté")
    W("")

    if empty_captions:
        W(f"  Captions vides : {len(empty_captions)} tables")
        for c in empty_captions[:30]:
            W(f"    [{c['_family']}/{c['_pdf']}] {c.get('table_id','')} — statut={c.get('status','')}")
    else:
        W("  Aucune caption vide détectée")
    W("")

    if high_empty_cells:
        W(f"  Cellules vides (ratio > 30%) : {len(high_empty_cells)} tables")
        for c in high_empty_cells[:30]:
            W(f"    [{c['_family']}/{c['_pdf']}] {c.get('table_id','')} — ratio={c.get('empty_cell_ratio','')}")
    else:
        W("  Aucune cellule vide problématique")
    W("")

    # ── 5. TOUS LES WARNINGS ──────────────────────────────────────────
    W("─" * 80)
    W("  5. LISTE COMPLÈTE DES WARNINGS")
    W("─" * 80)
    if all_warnings:
        for aw in sorted(all_warnings, key=lambda x: (x["family"], x["pdf"])):
            W(f"  [{aw['family']}/{aw['pdf']}] {aw['table']}")
            for warn in aw["warnings"]:
                W(f"      ⚠ {warn}")
        W("")
    else:
        W("  (aucun warning)")
        W("")

    # ── 6. TOP 100 PIRE TABLES ────────────────────────────────────────
    W("─" * 80)
    W("  6. TOP 100 — PIRE TABLES (triées par empty_cell_ratio décroissant)")
    W("─" * 80)
    for i, w in enumerate(all_worst[:100], 1):
        W(f"  [{i:3d}] {w['_family']}/{w['_pdf']} | {w.get('table_id','')}")
        W(f"        caption:   {_short(w.get('caption',''), 100)}")
        W(f"        conf:      {w.get('confidence','')}")
        W(f"        ratio:     {w.get('empty_cell_ratio','')}")
        W(f"        empty:     {w.get('has_empty_cells','')}")
        W(f"        method:    {w.get('extraction_method','')}")
        W(f"        status:    {w.get('status','')}")
        W(f"        rows:      {w.get('rows_count','')}")
        W(f"        cols:      {w.get('col_count','')}")
        W(f"        warnings:  {', '.join(str(x) for x in w.get('warnings',[]))}")
        W(f"        headers:   {_short(w.get('headers_preview',''), 120)}")
        heur = w.get("heuristics", {})
        if heur:
            W(f"        heuristics: {json.dumps(heur, ensure_ascii=False)}")
        W("")

    # ── 7. RÉSUMÉ PAR FAMILLE ─────────────────────────────────────────
    W("─" * 80)
    W("  7. RÉSUMÉ PAR FAMILLE")
    W("─" * 80)
    families: dict[str, dict] = {}
    for r in reports:
        f = r["_family"]
        if f not in families:
            families[f] = {"pdfs": 0, "found": 0, "extracted": 0, "high": 0, "medium": 0, "low": 0, "failed": 0, "errors": 0, "drawings": 0, "bugs": 0, "pdfs_zero": 0}
        families[f]["pdfs"] += 1
        families[f]["found"] += r.get("tables_found", 0)
        families[f]["extracted"] += r.get("tables_extracted", 0)
        families[f]["high"] += r.get("high", 0)
        families[f]["medium"] += r.get("medium", 0)
        families[f]["low"] += r.get("low", 0)
        families[f]["failed"] += r.get("failed", 0)
        families[f]["errors"] += len(r.get("errors", []))
        families[f]["drawings"] += r.get("drawing_failed", 0)
        families[f]["bugs"] += r.get("bug_suspected", 0)
        if r.get("tables_found", 0) > 0 and r.get("tables_extracted", 0) == 0:
            families[f]["pdfs_zero"] += 1

    for fam in sorted(families):
        f = families[fam]
        rate = f["extracted"] / f["found"] * 100 if f["found"] else 0
        W(f"  FAMILLE: {fam}")
        W(f"    PDFs: {f['pdfs']} | Tables: {f['found']} trouvées, {f['extracted']} extraites ({rate:.1f}%)")
        W(f"    H={f['high']} M={f['medium']} L={f['low']} Failed={f['failed']}")
        W(f"    Erreurs: {f['errors']} | Dessins: {f['drawings']} | Bugs suspectés: {f['bugs']}")
        if f["pdfs_zero"]:
            W(f"    ⚠ {f['pdfs_zero']} PDF(s) avec 0 extraction")
        W("")

    # ── 8. STATUT DÉTAILLÉ PAR DATASHEET ──────────────────────────────
    W("─" * 80)
    W("  8. STATUT DÉTAILLÉ PAR DATASHEET")
    W("─" * 80)
    headers = f"{'FAMILLE':<10} {'PDF':<35} {'Trv':>4} {'Ext':>4} {'H':>4} {'M':>4} {'L':>4} {'F':>4} {'Bugs':>5} {'Errs':>5}"
    W("  " + headers)
    W("  " + "-" * 85)
    for r in sorted(reports, key=lambda x: (x["_family"], x.get("pdf_name",""))):
        fam = r["_family"]
        name = r.get("pdf_name", "")
        n_bug = r.get("bug_suspected", 0)
        n_err = len(r.get("errors", []))
        flag = " ⚠" if (n_bug > 0 or n_err > 0 or r.get("failed", 0) > 0) else ""
        W(f"  {fam:<10} {name:<35} {r.get('tables_found',0):>4d} {r.get('tables_extracted',0):>4d} {r.get('high',0):>4d} {r.get('medium',0):>4d} {r.get('low',0):>4d} {r.get('failed',0):>4d} {n_bug:>5d} {n_err:>5d}{flag}")
    W("")

    # ── 9. ERREURS FATALES / EXCEPTIONS ──────────────────────────────
    W("─" * 80)
    W("  9. ERREURS FATALES ET EXCEPTIONS")
    W("─" * 80)
    fatal_errors = [e for e in all_errors if "toc_detection" in e["error"] or "Traceback" in e["error"]]
    if fatal_errors:
        for fe in fatal_errors:
            W(f"  ⛔ [{fe['family']}/{fe['pdf']}] {fe['error']}")
    else:
        W("  Aucune erreur fatale détectée")
    W("")

    # ── 10. FIN ──────────────────────────────────────────────────────
    W("=" * 80)
    W(f"  FIN DU RAPPORT — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    W(f"  Total PDFs: {total_pdf} | Erreurs: {len(all_errors)} | Tables problématiques: {len(all_worst)}")
    W("=" * 80)

    out_txt = OUTPUT_DIR / "_bug_report_complet.txt"
    out_txt.write_text("\n".join(lines), encoding="utf-8")
    print(f"Rapport généré : {out_txt}")


if __name__ == "__main__":
    main()
