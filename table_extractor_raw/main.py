"""
main.py — CLI : traite un PDF ou un dossier entier.

Usage:
    python main.py --pdf ../C0/stm32c011d6.pdf
    python main.py --family C0
    python main.py --all
"""
from __future__ import annotations
import argparse
import json
import logging
import sys
import io

# Force UTF-8 sur stdout Windows (cp1252 ne supporte pas µ, Ω, ✓, →, etc.)
if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr.encoding != "utf-8":
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
import time
from pathlib import Path

# ── Setup path ─────────────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))

from config import OUTPUT_DIR, LOG_DIR
from core.toc_detector import detect_tables
from core.grid_extractor import extract_table_grid
from core.schema import RawTable

# ── Logging JSON simple ────────────────────────────────────────────────────────
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger("main")


def process_pdf(pdf_path: Path, family: str) -> dict:
    """
    Traite un PDF complet :
    1. Détecte les tables (TOC ou scan)
    2. Extrait chaque table
    3. Valide via Pydantic
    4. Sauvegarde en JSON

    Retourne un résumé du run pour ce PDF.
    """
    pdf_name = pdf_path.stem
    logger.info(f"=== START {family}/{pdf_name} ===")
    t0 = time.time()

    out_dir = OUTPUT_DIR / family / pdf_name
    out_dir.mkdir(parents=True, exist_ok=True)

    summary = {
        "pdf": str(pdf_path),
        "family": family,
        "pdf_name": pdf_name,
        "tables_found": 0,
        "tables_extracted": 0,
        "high": 0,
        "medium": 0,
        "low": 0,
        "failed": 0,
        "errors": [],
        "worst_tables": [],  # trié par empty_cell_ratio desc
    }

    # ── Étape 1 : détection ────────────────────────────────────────────────────
    try:
        refs = detect_tables(str(pdf_path))
    except Exception as e:
        logger.error(f"TOC detection failed: {e}")
        summary["errors"].append(f"toc_detection:{e}")
        return summary

    summary["tables_found"] = len(refs)
    logger.info(f"Detected {len(refs)} tables")

    if not refs:
        logger.warning("No tables detected — PDF may have no table index")
        return summary

    # ── Étape 2 : extraction ───────────────────────────────────────────────────
    extracted_tables = []
    all_tables_json = []
    for ref in refs:
        try:
            raw_dict = extract_table_grid(
                pdf_path=str(pdf_path),
                ref=ref,
                family=family,
                pdf_name=pdf_name,
                output_base=OUTPUT_DIR,
                all_refs=refs,
            )

            # Validation Pydantic
            table_obj = RawTable(**raw_dict)
            table_json = table_obj.model_dump()
            
            # Ajout des métadonnées spécifiques demandées à la toute fin du JSON
            table_json["datasheet_metaData"] = {
                "pdf_name": table_json["pdf_name"],
                "table_id": table_json["table_id"],
                "is_continued": len(table_json["merged_pages"]) > 1,
                "pages": table_json["merged_pages"],
                "rows_count": len(table_json["rows"]),
                "cols_count": len(table_json["headers"]),
                "confidence": table_json["extraction_confidence"],
                "empty_cell_ratio": round(table_json["empty_cell_ratio"], 4)
            }

            # Sauvegarde JSON individuelle
            out_file = out_dir / f"{ref.table_id}.json"
            out_file.write_text(
                json.dumps(table_json, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )

            all_tables_json.append(table_json)

            # Stats
            conf = table_json["extraction_confidence"]
            summary[conf] = summary.get(conf, 0) + 1
            summary["tables_extracted"] += 1

            extracted_tables.append({
                "table_id": ref.table_id,
                "confidence": conf,
                "empty_cell_ratio": table_json["empty_cell_ratio"],
            })

            logger.info(f"  [OK] {ref.table_id} -> {out_file.name} [{conf}]")

        except Exception as e:
            logger.error(f"  ✗ {ref.table_id}: {e}", exc_info=True)
            summary["errors"].append(f"{ref.table_id}:{e}")
            summary["failed"] += 1

    # ── Sauvegarde du fichier global _all_tables.json ──────────────────────────
    all_path = out_dir / "_all_tables.json"
    all_path.write_text(
        json.dumps(all_tables_json, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    # ── Rapport de synthèse ────────────────────────────────────────────────────
    # Top 5 des tables les plus problématiques (pour prioriser le debug)
    worst = sorted(
        [t for t in extracted_tables if t["confidence"] != "high"],
        key=lambda t: t["empty_cell_ratio"],
        reverse=True
    )[:5]
    summary["worst_tables"] = worst

    elapsed = time.time() - t0
    logger.info(
        f"=== DONE {pdf_name} | {summary['tables_extracted']}/{summary['tables_found']} tables "
        f"| high={summary['high']} medium={summary['medium']} "
        f"low={summary['low']} failed={summary['failed']} "
        f"| {elapsed:.1f}s ==="
    )

    # Sauvegarde du rapport de synthèse
    report_path = out_dir / "_run_report.json"
    report_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    return summary


def main():
    parser = argparse.ArgumentParser(
        description="STM32 PDF Table Extractor — sortie JSON brute sans classification"
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--pdf",    type=Path, help="Chemin vers un PDF spécifique")
    group.add_argument("--family", type=str,  help="Ex: C0 → traite tous les PDFs de cette famille")
    group.add_argument("--all",    action="store_true", help="Traite tous les PDFs du projet")

    # Chemin racine des PDFs (relatif au dossier parent de table_extractor_raw)
    DATASHEETS_ROOT = Path(__file__).parent.parent

    args = parser.parse_args()

    if args.pdf:
        # Un seul PDF — déduire la famille depuis le dossier parent
        pdf_path = args.pdf.resolve()
        family = pdf_path.parent.name
        process_pdf(pdf_path, family)

    elif args.family:
        family_dir = DATASHEETS_ROOT / args.family
        if not family_dir.exists():
            logger.error(f"Family directory not found: {family_dir}")
            sys.exit(1)
        pdfs = sorted(family_dir.glob("*.pdf"))
        logger.info(f"Processing {len(pdfs)} PDFs from family {args.family}")
        for pdf in pdfs:
            process_pdf(pdf, args.family)

    elif args.all:
        all_pdfs = sorted(DATASHEETS_ROOT.glob("*/*.pdf"))
        logger.info(f"Processing {len(all_pdfs)} PDFs total")
        for pdf in all_pdfs:
            family = pdf.parent.name
            process_pdf(pdf, family)


if __name__ == "__main__":
    main()
