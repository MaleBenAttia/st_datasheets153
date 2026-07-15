"""
continuation.py — Gestion des tables multi-pages.

Implémente la logique pour suivre une table sur les pages suivantes.
"""
from __future__ import annotations
import logging
from typing import Optional

import pdfplumber
from pdfplumber.page import Page

from core.toc_detector import TableRef
from config import PDFPLUMBER_TABLE_SETTINGS

logger = logging.getLogger(__name__)


def _is_continuation_page(
    page: Page,
    expected_col_count: int,
    current_table_id: str,
) -> tuple[bool, Optional[list]]:
    """
    Vérifie si la page contient la suite de la table en cours.

    Critères :
    1. Le texte de la page mentionne "Table X ... (continued)" avec le bon numéro
       OU la table démarre en haut de la page sans autre légende avant elle.
    2. Une table est présente sur la page.
    3. Le nombre de colonnes est cohérent (tolérance ±2 pour les cellules fusionnées
       dans les en-têtes multi-niveaux, ex: Table 2).
    """
    current_table_num = current_table_id.split("_")[1] if "_" in current_table_id else ""

    # ── Extraire les mots de la page ──────────────────────────────────────────
    words = page.extract_words()

    # ── Détecter "(continued)" explicite ──────────────────────────────────────
    # STM32 écrit "Table X. ... (continued)" en titre de page de continuation.
    page_text = " ".join(w["text"] for w in words).lower()
    has_continued_title = (
        current_table_num
        and f"table {current_table_num}" in page_text
        and ("continued" in page_text or "(suite)" in page_text)
    )

    # ── Extraire les tables ───────────────────────────────────────────────────
    tables = page.extract_tables(PDFPLUMBER_TABLE_SETTINGS)
    finder = page.debug_tablefinder(PDFPLUMBER_TABLE_SETTINGS)

    if not tables or not finder.tables:
        return False, None

    # Prendre la table la plus haute sur la page
    candidates = [(t, ft) for t, ft in zip(tables, finder.tables) if t and len(t) >= 1]
    if not candidates:
        return False, None

    top_table, top_ft = min(candidates, key=lambda x: x[1].bbox[1])

    if has_continued_title:
        # Si "(continued)" est détecté, on fait confiance au titre : c'est la bonne table.
        # On vérifie juste qu'une table existe, sans contrainte de position.
        col_count = len(top_table[0])
        # Tolérance plus large : les en-têtes multi-niveaux peuvent avoir moins de cols
        # (ex: 10 cols réelles mais 5 cellules physiques à cause des fusions)
        if col_count < max(1, expected_col_count // 2):
            return False, None
        return True, top_table

    # ── Sans "(continued)" : heuristique de position ──────────────────────────
    # La table doit être proche du haut de la page.
    # Seuil élevé (300) car certaines pages ont un en-tête de ~2 cm.
    if top_ft.bbox[1] > 300:
        return False, None

    # Vérifier qu'il n'y a pas une AUTRE légende de table avant celle-ci
    for w in words:
        if "Table" in w["text"] and w["top"] < top_ft.bbox[1]:
            line_words = [ow for ow in words if abs(ow["top"] - w["top"]) < 3]
            line_text = " ".join(ow["text"] for ow in line_words).lower()

            # Si la légende contient notre numéro de table → c'est la continuation
            if current_table_num and f"table {current_table_num}" in line_text:
                pass  # OK, c'est notre table qui continue
            else:
                # C'est une nouvelle table différente → stopper
                return False, None

    # Vérifier le nombre de colonnes (tolérance ±2)
    col_count = len(top_table[0])
    if abs(col_count - expected_col_count) > 2:
        return False, None

    return True, top_table


def _expand_cont_row(row: list, expected_col_count: int) -> list:
    """
    Expands a row from a continuation page that has fewer physical columns
    (due to merged/spanned cells) to match the expected column count.

    Strategy: distribute None/empty cells after each real value so that the
    total reaches expected_col_count. Real values with None neighbours are
    assumed to span all remaining columns uniformly.

    Example: ['Bootloader', 'USART1, I2C1', None, None, None] (5 cols physical)
             → ['Bootloader', 'Bootloader', 'USART1, I2C1', 'USART1, I2C1', ...]
               padded out to 10 cols.
    """
    if len(row) >= expected_col_count:
        return row  # Already the right size

    # Count real (non-None) segments and None spans
    # Simple approach: repeat each real value to fill the gap
    real_values = []
    for cell in row:
        if cell is not None:
            real_values.append(str(cell))
        # None = merged from left → will be filled by propagation later

    if not real_values:
        return [""] * expected_col_count

    # Distribute real values as evenly as possible across expected_col_count
    result = []
    slots_per_value = expected_col_count // len(real_values)
    remainder = expected_col_count % len(real_values)

    for i, val in enumerate(real_values):
        count = slots_per_value + (1 if i < remainder else 0)
        result.extend([val] * count)

    return result[:expected_col_count]


def find_continuations(
    pdf: pdfplumber.PDF,
    start_page_num: int,
    expected_col_count: int,
    all_refs: list[TableRef],
    current_table_id: str = "",
    header_depth: int = 1,
    first_cell_text: str = "",
    max_pages: int = 10,
) -> tuple[list[int], list[list[str]]]:
    """
    Cherche les pages suivantes contenant la suite de la table.
    Retourne (liste_des_pages_fusionnees, lignes_supplementaires_brutes).
    """
    merged_pages = [start_page_num]
    extra_rows = []

    current_page = start_page_num + 1

    # Trouver la prochaine légende de table différente (limite de scan)
    next_refs = [r for r in all_refs if r.page >= current_page and r.table_id != current_table_id]
    next_ref = min(next_refs, key=lambda r: r.page) if next_refs else None
    next_table_page = next_ref.page if next_ref else float('inf')

    while current_page <= len(pdf.pages) and len(merged_pages) < max_pages:
        # Ne pas dépasser la page de la table suivante
        if current_page >= next_table_page:
            break

        is_cont, table_data = _is_continuation_page(
            pdf.pages[current_page - 1], expected_col_count, current_table_id
        )
        if not is_cont:
            break

        merged_pages.append(current_page)
        logger.info(f"    -> found continuation on page {current_page} ({len(table_data)} rows)")

        # ── Supprimer l'en-tête répété ────────────────────────────────────────
        if len(table_data) > 0:
            row0_cell0 = str(table_data[0][0] or "").strip()

            header_keywords = {
                "symbol", "parameter", "pin", "name", "peripheral",
                "features", "condition", "conditions", "min", "max",
                "unit", "typ", "value", "speed"
            }

            # Méthode 1 : la 1ère cellule correspond au premier texte de la page 1
            # → auto-détecter combien de lignes forment l'en-tête répété sur
            #   cette page de continuation (peut différer de header_depth si
            #   pdfplumber compresse les sous-lignes en une seule sur la cont.).
            if row0_cell0 and first_cell_text and row0_cell0 == first_cell_text:
                skip = 0
                for row in table_data:
                    c0 = str(row[0] or "").strip()
                    # C'est encore une ligne d'en-tête si :
                    # - sa première cellule est identique au premier texte (répétition directe)
                    # - ou sa première cellule est vide/None (ligne de sous-titres fusionnés)
                    if c0 == first_cell_text or c0 == "":
                        skip += 1
                    else:
                        break  # première ligne de données réelles
                data_rows = table_data[skip:]

            else:
                # Méthode 2 : heuristique sur les noms de colonnes standard
                row0 = [str(c or "").lower() for c in table_data[0]]
                if any(cell in header_keywords for cell in row0):
                    data_rows = table_data[1:]
                else:
                    data_rows = table_data

            # ── Expansion des cellules fusionnées ─────────────────────────────
            # Les pages de continuation ont souvent moins de colonnes physiques
            # que la page originale (ex: 5 cols physiques → 10 cols réelles).
            expanded = []
            for row in data_rows:
                if len(row) < expected_col_count:
                    row = _expand_cont_row(row, expected_col_count)
                expanded.append(row)

            extra_rows.extend(expanded)

        current_page += 1

    return merged_pages, extra_rows
