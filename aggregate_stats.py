"""
aggregate_stats.py — Consolidation des statistiques d'extraction.

Parcourt les fichiers _run_report.json et _all_tables.json de chaque
datasheet extraite, et génère un fichier unique global_extraction_stats.json
contenant toutes les métadonnées d'extraction (méthode, confiance, taux de
vide, warnings) séparées des données RAG pures.

Ce fichier est destiné au monitoring/debug, PAS à l'indexation vectorielle.

Usage:
    python aggregate_stats.py
"""
import json
from pathlib import Path


def main():
    out_dir = Path("outJason")
    if not out_dir.exists():
        print("Erreur : le dossier outJason/ n'existe pas. Lancez d'abord l'extraction.")
        return

    global_stats = []

    # ── Parcourir chaque rapport d'exécution (un par PDF traité) ──────────────
    for report_file in sorted(out_dir.rglob("_run_report.json")):
        try:
            report_data = json.loads(report_file.read_text(encoding="utf-8"))

            # Chercher le fichier _all_tables.json correspondant dans le même dossier
            all_tables_file = report_file.parent / "_all_tables.json"
            tables_stats = []

            if all_tables_file.exists():
                tables_data = json.loads(all_tables_file.read_text(encoding="utf-8"))
                for t in tables_data:
                    # Extraire uniquement les métadonnées d'extraction de chaque table
                    t_stats = {
                        "table_id": t.get("table_id"),
                        "extraction_method": t.get("extraction_method"),
                        "extraction_confidence": t.get("extraction_confidence"),
                        "empty_cell_ratio": t.get("empty_cell_ratio"),
                        "warnings": t.get("warnings", []),
                    }
                    # Ajouter les infos de datasheet_metaData si présentes
                    meta = t.get("datasheet_metaData", {})
                    if meta:
                        t_stats["rows_count"] = meta.get("rows_count")
                        t_stats["cols_count"] = meta.get("cols_count")
                        t_stats["is_continued"] = meta.get("is_continued")
                        t_stats["pages"] = meta.get("pages")

                    tables_stats.append(t_stats)

            # Combiner le rapport global du PDF et les stats par table
            pdf_global = {
                "summary": report_data,
                "tables": tables_stats,
            }
            global_stats.append(pdf_global)

        except Exception as e:
            print(f"Erreur lors du traitement de {report_file}: {e}")

    # ── Sauvegarde du rapport global ─────────────────────────────────────────
    out_file = Path("global_extraction_stats.json")
    out_file.write_text(
        json.dumps(global_stats, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Statistiques agrégées sauvegardées dans {out_file} ({len(global_stats)} PDFs traités).")


if __name__ == "__main__":
    main()
