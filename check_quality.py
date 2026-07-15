"""
check_quality.py — Audit rapide de tous les JSON générés dans outJason/.

Parcourt récursivement les fichiers JSON de sortie et génère un rapport
de qualité en console : nombre total de tables, tables en continuation,
tables à confiance moyenne/basse, tables vides, et taux de cellules vides.

Les fichiers internes (préfixés par _) sont automatiquement ignorés.

Usage:
    python check_quality.py              (analyse outJason/ par défaut)
    python check_quality.py mon_dossier  (analyse un autre dossier)
"""
import json
import sys
from pathlib import Path

# ── Dossier cible (argument CLI ou outJason par défaut) ──────────────────────
out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("outJason")

# ── Compteurs globaux ────────────────────────────────────────────────────────
problems = []
stats = {"total": 0, "continued": 0, "medium": 0, "low": 0, "empty": 0}

for json_file in sorted(out_dir.rglob("*.json")):
    # Ignorer les fichiers internes (_all_tables.json, _run_report.json, etc.)
    if json_file.name.startswith("_"):
        continue
    try:
        data = json.loads(json_file.read_text(encoding="utf-8"))
        meta = data.get("datasheet_metaData", {})
        stats["total"] += 1

        conf  = meta.get("confidence", "?")
        ratio = meta.get("empty_cell_ratio", 0)
        cont  = meta.get("is_continued", False)
        rows  = meta.get("rows_count", 0)

        if cont:
            stats["continued"] += 1
        if conf == "medium":
            stats["medium"] += 1
        if conf == "low":
            stats["low"] += 1
        if rows == 0:
            stats["empty"] += 1
            problems.append(f"[EMPTY]  {json_file.relative_to(out_dir)}")
        if ratio > 0.3:
            problems.append(f"[HIGH_EMPTY {ratio:.0%}] {json_file.relative_to(out_dir)}")
        if conf in ("medium", "low"):
            problems.append(f"[{conf.upper()}] {json_file.relative_to(out_dir)}")

    except Exception as e:
        problems.append(f"[JSON_ERROR] {json_file}: {e}")

# ── Affichage du rapport ─────────────────────────────────────────────────────
print(f"\n=== RAPPORT QUALITÉ ===")
print(f"Total tables    : {stats['total']}")
print(f"  is_continued  : {stats['continued']}")
print(f"  confidence=medium : {stats['medium']}")
print(f"  confidence=low    : {stats['low']}")
print(f"  rows=0 (vides)    : {stats['empty']}")
print(f"\n=== PROBLÈMES DÉTECTÉS ({len(problems)}) ===")
for p in problems:
    print(" ", p)
if not problems:
    print("  Aucun probleme detecte [OK]")
