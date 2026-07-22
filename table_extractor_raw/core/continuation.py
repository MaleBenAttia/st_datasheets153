"""
continuation.py — Gestion des tables multi-pages.

Détecte la suite d'une table sur les pages suivantes en reconnaissant
les titres "Table X. ... (continued)", filtre les fausses pages de titre,
et adapte le nombre de colonnes quand une colonne se scinde en sous-colonnes.

Exporte : find_continuations(), _get_col_x0s()
"""
from __future__ import annotations
import logging
from typing import Any, Optional

import pdfplumber
from pdfplumber.page import Page

from core.toc_detector import TableRef
from config import (
    PDFPLUMBER_TABLE_SETTINGS,
    PDFPLUMBER_TABLE_SETTINGS_TYPE2,
    MIN_TABLE_WIDTH,
    MAX_CONTINUATION_PAGES,
    MAX_CONT_COL_DRIFT,
)

logger = logging.getLogger(__name__)


def _get_col_x0s(table_obj) -> list[float]:
    """
    Retourne les abscisses gauches (x0) médianes de chaque colonne,
    dans l'ordre des colonnes du finder table pdfplumber.
    """
    if not hasattr(table_obj, 'rows') or not table_obj.rows:
        return []
    maxc = max(len(r.cells) for r in table_obj.rows) if table_obj.rows else 0
    col_x0s: dict[int, list[float]] = {j: [] for j in range(maxc)}
    for r in table_obj.rows:
        for j, c in enumerate(r.cells):
            if c:
                col_x0s[j].append(round(c[0], 1))
    result = []
    for j in sorted(col_x0s.keys()):
        vals = col_x0s[j]
        if vals:
            vals.sort()
            result.append(vals[len(vals) // 2])
    return result


def _is_continuation_page(
    page: Page,
    expected_col_count: int,
    current_table_id: str,
    pdf_type: int = 1,
) -> tuple[bool, Optional[list], Optional[list[float]], Optional[Any]]:
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
    settings = PDFPLUMBER_TABLE_SETTINGS_TYPE2 if pdf_type == 2 else PDFPLUMBER_TABLE_SETTINGS
    tables = page.extract_tables(settings)
    finder = page.debug_tablefinder(settings)

    if not tables or not finder.tables:
        return False, None, None, None

    # Filtrer les bandeaux décoratifs (Type 2)
    if pdf_type == 2:
        filtered = [
            (t, ft) for t, ft in zip(tables, finder.tables)
            if ft.bbox[2] - ft.bbox[0] >= MIN_TABLE_WIDTH
        ]
        if filtered:
            tables = [t for t, ft in filtered]
            finder.tables = [ft for t, ft in filtered]

    # Prendre la table la plus haute sur la page
    candidates = [(t, ft) for t, ft in zip(tables, finder.tables) if t and len(t) >= 1]
    if not candidates:
        return False, None, None, None

    top_table, top_ft = min(candidates, key=lambda x: x[1].bbox[1])

    if has_continued_title:
        # Si "(continued)" est détecté, on fait confiance au titre : c'est la bonne table.
        col_count = len(top_table[0])
        # Si la table a beaucoup plus de colonnes qu'attendu → pas la bonne table
        if col_count > expected_col_count + 2:
            return False, None, None, None
        # Doit avoir au moins expected - 2 colonnes (évite les petites tables
        # de note de bas de page qui auraient aussi "(continued)")
        if col_count < max(2, expected_col_count - 2):
            return False, None, None, None
        return True, top_table, _get_col_x0s(top_ft), top_ft

    # ── Sans "(continued)" : heuristique de position ──────────────────────────
    # La table doit être proche du haut de la page.
    # Seuil élevé (300) car certaines pages ont un en-tête de ~2 cm.
    if top_ft.bbox[1] > 300:
        return False, None, None, None

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
                return False, None, None, None

    # Vérifier le nombre de colonnes (tolérance ±2)
    col_count = len(top_table[0])
    if abs(col_count - expected_col_count) > 2:
        return False, None, None, None

    return True, top_table, _get_col_x0s(top_ft), top_ft


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


def _reduce_cont_row(row: list, expected_col_count: int) -> list:
    """
    Reduce a row to expected_col_count by dropping columns that are likely
    pdfplumber-injected separators (mostly None/empty).

    Strategy: try all combinations of columns to drop; pick the one that
    preserves the most real values, then favours dropping None/empty
    columns (no data loss), then columns near the center (separator
        heuristic), then rightmost columns.

    ATTENTION : O(C(n,k)) — pour une ligne de 20 colonnes avec n_drop=10,
    cela évalue C(20,10) = 184 756 combinaisons. Appels répétés par ligne
    de continuation. Monitorer si ralentissement sur tables larges.
    """
    from itertools import combinations

    n_drop = len(row) - expected_col_count
    center = (len(row) - 1) / 2

    best_criteria = None
    best_row = None

    for drop_set in combinations(range(len(row)), n_drop):
        kept = [row[i] for i in range(len(row)) if i not in drop_set]
        real_count = sum(
            1 for c in kept if c is not None and str(c).strip() != ""
        )
        none_dropped = sum(
            1
            for i in drop_set
            if row[i] is None or str(row[i]).strip() == ""
        )
        avg_dist = sum(abs(i - center) for i in drop_set) / n_drop

        crit = (real_count, none_dropped, -avg_dist, sum(drop_set))
        if best_criteria is None or crit > best_criteria:
            best_criteria = crit
            best_row = kept

    return best_row


def _min_drift(cols_a: list[float], cols_b: list[float]) -> float:
    """
    Calcule le drift minimum entre deux listes de x0 de colonnes en essayant
    d'ignorer toute position dans la plus longue liste. Gère les colonnes
    fantômes à n'importe quelle position (début, milieu ou fin).

    Ex: base=[42.5,185.1,233.2,...] cont=[42.5,116.7,185.1,233.2,...]
        skip position 1 (116.7) → base aligné avec cont privé de l'index 1
        → drift=0.0
    """
    shorter, longer = sorted([cols_a, cols_b], key=len)
    n_diff = len(longer) - len(shorter)
    if n_diff == 0:
        return max(abs(longer[i] - shorter[i]) for i in range(len(shorter)))
    best = float("inf")
    for skip in range(len(longer)):
        aligned = list(longer[:skip]) + list(longer[skip+1:])
        if len(aligned) != len(shorter):
            continue
        drift = max(abs(aligned[i] - shorter[i]) for i in range(len(shorter)))
        if drift < best:
            best = drift
    return best


def find_continuations(
    pdf: pdfplumber.PDF,
    start_page_num: int,
    expected_col_count: int,
    all_refs: list[TableRef],
    current_table_id: str = "",
    header_depth: int = 1,
    first_cell_text: str = "",
    max_pages: int = MAX_CONTINUATION_PAGES,
    pdf_type: int = 1,
    base_col_x0s: list[float] | None = None,
) -> tuple[list[int], list[list[str]], int, list[list[float]]]:
    """
    Cherche les pages suivantes contenant la suite de la table.

    Stratégie :
    1. Pour chaque page suivante, vérifier si c'est une continuation via
       _is_continuation_page (titre "Table X (continued)" ou position en haut)
    2. Vérifier la dérive géométrique des colonnes (drift des x0)
    3. Réduire/étendre les lignes de continuation au nombre de colonnes cible
    4. Arrêter si : page de la table suivante atteinte, ou max_pages, ou
       colonnes structurellement différentes

    Retourne (pages_fusionnees, lignes_supplementaires, target_cols, all_col_x0s).
    target_cols = max(expected_col_count, max cols trouvées dans les continuations).
    """
    merged_pages = [start_page_num]
    all_data_rows: list[list] = []
    all_col_x0s: list[list[float]] = []
    max_cont_cols = 0

    current_page = start_page_num + 1

    # Trouver la prochaine légende de table différente (limite de scan)
    next_refs = [r for r in all_refs if r.page >= current_page and r.table_id != current_table_id]
    next_ref = min(next_refs, key=lambda r: r.page) if next_refs else None
    next_table_page = next_ref.page if next_ref else float('inf')

    while current_page <= len(pdf.pages) and len(merged_pages) < max_pages:
        # Ne pas dépasser la page de la table suivante
        if current_page > next_table_page:
            break

        is_cont, table_data, cont_x0s, top_ft = _is_continuation_page(
            pdf.pages[current_page - 1], expected_col_count, current_table_id, pdf_type
        )
        if not is_cont:
            break

        # ── Vérification de la dérive des colonnes ─────────────────────────────
        # Utilise un alignement glissant pour tolérer les colonnes fantômes
        # (pdfplumber détecte parfois une colonne None supplémentaire).
        if base_col_x0s and cont_x0s and len(base_col_x0s) >= 2:
            if abs(len(cont_x0s) - len(base_col_x0s)) <= 1:
                drift = _min_drift(cont_x0s, base_col_x0s)
            else:
                drift = float("inf")

            if drift > MAX_CONT_COL_DRIFT:
                logger.warning(
                    f"  -> page {current_page}: column drift {drift:.1f}px "
                    f"({len(cont_x0s)} cols vs {len(base_col_x0s)} base), skipping"
                )
                break

        merged_pages.append(current_page)
        logger.info(f"    -> found continuation on page {current_page} ({len(table_data)} rows)")

        # ── Détection vectorielle des dashs sur la page de continuation ──────
        from core.grid_extractor import _detect_vector_dashes
        table_data = _detect_vector_dashes(table_data, top_ft, pdf.pages[current_page - 1])

        # Collecter les x0s des colonnes de cette page de continuation
        if cont_x0s:
            all_col_x0s.append(cont_x0s)

        # ── Supprimer l'en-tête répété ────────────────────────────────────────
        if len(table_data) > 0:
            row0_cell0 = str(table_data[0][0] or "").strip()

            header_keywords = {
                "symbol", "parameter", "pin", "name", "peripheral",
                "features", "condition", "conditions", "min", "max",
                "unit", "typ", "value", "speed"
            }

            # Méthode 1 : la 1ère cellule correspond au premier texte de la page 1
            if row0_cell0 and first_cell_text and row0_cell0 == first_cell_text:
                skip = 0
                for row in table_data:
                    c0 = str(row[0] or "").strip()
                    if c0 == first_cell_text or c0 == "":
                        skip += 1
                    else:
                        break
                data_rows = table_data[skip:]

            else:
                row0 = [str(c or "").lower() for c in table_data[0]]
                if any(cell in header_keywords for cell in row0):
                    data_rows = table_data[1:]
                else:
                    data_rows = table_data

            # Mettre à jour max_cont_cols
            for row in data_rows:
                max_cont_cols = max(max_cont_cols, len(row))

            # Collecter les lignes brutes (sans expand/reduce) pour les traiter
            # toutes avec le même target_cols final
            all_data_rows.extend(data_rows)

        current_page += 1

    # ── Traiter toutes les lignes collectées avec le même target_cols ────
    target_cols = max(expected_col_count, min(max_cont_cols, expected_col_count + 1))
    extra_rows = []
    for row in all_data_rows:
        if len(row) < target_cols:
            row = _expand_cont_row(row, target_cols)
        elif len(row) > target_cols:
            row = _reduce_cont_row(row, target_cols)
        extra_rows.append(row)

    return merged_pages, extra_rows, target_cols, all_col_x0s
