"""
rag_transformer.py — Transformation des tables JSON brutes en chunks RAG.

Ce script lit les fichiers _all_tables.json générés par l'extracteur et les
convertit en objets "chunk" optimisés pour l'indexation dans une base de
données vectorielle (ChromaDB, Qdrant, Pinecone, etc.).

Architecture de sortie :
    RagJason/<Family>/
        stm32c011d6.json   <- un fichier par datasheet
        stm32f103rc.json
        stm32g081cb.json

Chaque fichier contient un array JSON d'objets avec :
    - id       : identifiant unique du chunk (ex: stm32g081cb_table_12_part1)
    - document : texte dense en mots-clés pour l'embedding vectoriel
    - metadata : champs de pré-filtrage (family, category, pins, etc.)
                 + raw_json (le JSON brut pur, sans métadonnées d'extraction)

Règles de catégorisation (par regex sur le caption) :
    - "alternate function"              -> pinout_af_mapping
    - "assignment and description"      -> pinout_description
    - "characteristics/consumption/..." -> electrical_spec
    - "mechanical data/package"         -> mechanical_package
    - "device features/peripheral..."   -> device_features
    - "revision history"                -> changelog
    - sinon                             -> general

Règles de chunking :
    - Tables <= 30 lignes : un seul chunk
    - Tables > 30 lignes  : découpées en groupes de 20 lignes (_part1, _part2...)

Règles de mots-clés (champ "document") :
    - Maximum 40 termes uniques, priorisés : Pins > Signaux > Symboles électriques
    - Correction des textes inversés (ex: "sremiT" -> "Timers" ajouté en plus)
    - Aucune hallucination : seules les valeurs présentes dans les rows sont utilisées

Usage:
    python rag_transformer.py
"""
import json
import re
import sys
from pathlib import Path
import copy


# ═════════════════════════════════════════════════════════════════════════════
# CATÉGORISATION
# ═════════════════════════════════════════════════════════════════════════════

def get_category(caption: str) -> str:
    """Catégorise une table à partir de sa légende (caption) par regex."""
    caption = caption.lower()
    if "alternate function" in caption:
        return "pinout_af_mapping"
    if "assignment and description" in caption:
        return "pinout_description"
    if any(kw in caption for kw in ["characteristics", "consumption", "conditions", "accuracy"]):
        return "electrical_spec"
    if "mechanical data" in caption or "package" in caption:
        return "mechanical_package"
    if "device features" in caption or "peripheral counts" in caption:
        return "device_features"
    if "revision history" in caption:
        return "changelog"
    return "general"


# ═════════════════════════════════════════════════════════════════════════════
# EXTRACTION DE MOTS-CLÉS
# ═════════════════════════════════════════════════════════════════════════════

def extract_keywords(rows: list[list[str]], headers: list[str]) -> list[str]:
    """
    Extrait et priorise les 40 mots-clés les plus pertinents des données.

    Priorité :
        1. Noms de pins      (regex ^P[A-F][0-9]+)
        2. Noms de signaux   (contient '_')
        3. Symboles élec.    (regex ^[VIfRCt][a-z0-9])
        4. Tout le reste

    Gère aussi la correction des textes inversés (sremiT, secafretni .mmoC)
    en ajoutant la version corrigée EN PLUS de l'originale.
    """
    words = set()

    # Correction des headers inversés connus
    for h in headers:
        if not h:
            continue
        if "sremiT" in h:
            words.add("Timers")
        if "secafretni .mmoC" in h:
            words.add("Comm. interfaces")

    # Extraction depuis les cellules des rows
    for row in rows:
        for cell in row:
            if not cell:
                continue
            cell = str(cell).strip()
            if cell == "" or cell == "-":
                continue

            # Tokeniser et nettoyer
            tokens = cell.split()
            for t in tokens:
                t_clean = re.sub(r'[,;.()\[\]]', '', t)
                if t_clean and t_clean != "-":
                    words.add(t_clean)

            # Correction des textes inversés dans les cellules
            if "sremiT" in cell:
                words.add("Timers")
            if "secafretni .mmoC" in cell:
                words.add("Comm. interfaces")

    # Tri par priorité
    words_list = list(words)

    def priority_score(w: str) -> int:
        if re.match(r"^P[A-F][0-9]+", w):
            return 1  # Pins en premier
        if "_" in w:
            return 2  # Signaux ensuite
        if re.match(r"^[VIfRCt][a-z0-9]", w):
            return 3  # Symboles électriques
        return 4      # Tout le reste

    words_list.sort(key=lambda w: (priority_score(w), w))
    return words_list[:40]


def extract_pins(keywords: list[str]) -> str:
    """Extrait les noms de pins (PA0, PB5, etc.) en CSV depuis les mots-clés."""
    pins = [w for w in keywords if re.match(r"^P[A-F][0-9]+", w)]
    return ",".join(pins)


# ═════════════════════════════════════════════════════════════════════════════
# TRAITEMENT D'UNE TABLE -> CHUNKS
# ═════════════════════════════════════════════════════════════════════════════

def process_table(table: dict) -> list[dict]:
    """
    Transforme un objet table JSON brut en un ou plusieurs chunks RAG.

    Gère le découpage automatique :
        - Tables <= 30 lignes : un seul chunk
        - Tables > 30 lignes  : groupes de 20 lignes avec suffixe _partN
    """
    table_id = table.get("table_id", "")
    pdf_name = table.get("pdf_name", "")
    family   = table.get("family", "")
    caption  = table.get("caption", "")
    page     = table.get("page", 0)
    headers  = table.get("headers", [])
    rows     = table.get("rows", [])

    category  = get_category(caption)
    row_count = len(rows)
    col_count = len(headers)

    # ── Chunking : découper si > 30 lignes ───────────────────────────────────
    row_chunks = []
    if row_count <= 30:
        row_chunks = [rows]
    else:
        for i in range(0, row_count, 20):
            row_chunks.append(rows[i:i + 20])

    chunks = []
    for i, r_chunk in enumerate(row_chunks):
        part_suffix = f"_part{i + 1}" if len(row_chunks) > 1 else ""
        chunk_id = f"{pdf_name}_{table_id}{part_suffix}"

        # Construire le raw_json PUR (sans métadonnées d'extraction)
        chunk_raw = copy.deepcopy(table)
        chunk_raw["rows"] = r_chunk
        for key in ["extraction_method", "extraction_confidence", "empty_cell_ratio",
                     "warnings", "datasheet_metaData", "col_count"]:
            chunk_raw.pop(key, None)

        # Mots-clés pour le champ "document"
        keywords = extract_keywords(r_chunk, headers)

        # Enrichissement pour les petites tables (Min/Typ/Max)
        if row_count <= 3:
            for h in headers:
                if h in ["Min", "Typ", "Max", "Unit"] and h not in keywords:
                    keywords.append(h)

        # Texte dense pour l'embedding vectoriel
        doc_text = f"{caption} (page {page}). " + ", ".join(keywords)

        # CSV des pins détectés (pour filtrage)
        pins_csv = extract_pins(keywords)

        # Construction des métadonnées
        metadata = {
            "pdf_name": pdf_name,
            "table_id": table_id,
            "family": family,
            "page": page,
            "category": category,
            "row_count": len(r_chunk),
            "col_count": col_count,
            "raw_json": json.dumps(chunk_raw, ensure_ascii=False),
        }
        if pins_csv:
            metadata["pins"] = pins_csv

        chunks.append({
            "id": chunk_id,
            "document": doc_text,
            "metadata": metadata,
        })

    return chunks


# ═════════════════════════════════════════════════════════════════════════════
# FONCTION RÉUTILISABLE — appelée depuis main.py
# ═════════════════════════════════════════════════════════════════════════════

def generate_rag_for_pdf(
    tables: list[dict],
    family: str,
    pdf_name: str,
    out_base: Path,
) -> int:
    """
    Construit les chunks RAG pour un datasheet et écrit
    out_base/<family>/<pdf_name>.json.

    Retourne le nombre de chunks écrits (0 si empty).
    Sûr à appeler depuis main.py (gère les dossiers manquants).
    """
    if not tables:
        return 0
    chunks = []
    for t in tables:
        chunks.extend(process_table(t))
    if not chunks:
        return 0
    out_dir = Path(out_base) / family
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{pdf_name}.json"
    out_file.write_text(
        json.dumps(chunks, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return len(chunks)


# ═════════════════════════════════════════════════════════════════════════════
# MAIN — Génération des fichiers RAG par datasheet (standalone)
# ═════════════════════════════════════════════════════════════════════════════

def main():
    in_dir = Path("outJason")
    if not in_dir.exists():
        print("Erreur : le dossier outJason/ n'existe pas. Lancez d'abord l'extraction.")
        sys.exit(1)

    total_chunks = 0

    # Un fichier RAG par datasheet (dans RagJason/<Family>/)
    for json_file in sorted(in_dir.rglob("_all_tables.json")):
        try:
            data = json.loads(json_file.read_text(encoding="utf-8"))
            if not isinstance(data, list) or not data:
                continue

            pdf_name = data[0].get("pdf_name", "unknown")
            family = json_file.parent.parent.name  # outJason/<Family>/<pdf>/_all_tables.json

            n = generate_rag_for_pdf(data, family, pdf_name, Path("RagJason"))
            total_chunks += n

        except Exception as e:
            print(f"Erreur lors du traitement de {json_file}: {e}")

    print(f"Génération terminée : {total_chunks} chunks RAG dans RagJason/")


if __name__ == "__main__":
    main()
