"""
grid_extractor.py — Étape 2 : extraction de la grille brute via pdfplumber.

Fixes intégrés :
  [Fix 1] Texte vertical inversé (upright=False) → reconstruction depuis page.chars
  [Fix 2] Header multi-niveau → détection géométrique + split lignes
  [Fix 3] Sélection de table la plus proche EN DESSOUS de la légende
  [Fix 4] Newlines internes → espace
  [Fix 5] Rowspan/colspan → propagation géométrique via bboxes
  [Fix 6] Propagation descendante des cellules vides (rowspan) avec détection
          de groupe pour les PDFs Type 2
  [Fix 7] Insertion automatique de colonnes page 1 si continuation en a une
          de plus (split géométrique détecté par x0)

Pipeline : _extract_from_page → _expand_spans_and_headers → (continuation)
           → Fix 6 propagation → glyphe → qualité
"""
from __future__ import annotations
import logging
import re
import sys
from pathlib import Path
from typing import Any, Optional

import pdfplumber
from pdfplumber.page import Page

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import (
    PDFPLUMBER_TABLE_SETTINGS,
    PDFPLUMBER_TABLE_SETTINGS_FALLBACK,
    PDFPLUMBER_TABLE_SETTINGS_TYPE2,
    PDFPLUMBER_TABLE_SETTINGS_FALLBACK_TYPE2,
    MIN_TABLE_WIDTH,
    SAVE_DEBUG_IMAGES,
    SAVE_IMAGES_ONLY_ON_ISSUE,
    DEBUG_IMAGE_DPI,
    OUTPUT_DIR,
)
from core.toc_detector import TableRef, get_section_at
from core.glyph_fixer import CID_PATTERN, FOOTER_PATTERN, fix_headers, fix_rows
from core.quality_flags import evaluate_table
from core.continuation import find_continuations, _get_col_x0s
from core.ordering import extract_ordering_info

logger = logging.getLogger(__name__)

# ── Constantes pour la détection vectorielle des dashs ──────────────────────────
DASH_COL_KEYWORDS = ("parameter", "conditions", "symbol", "ratings",
                     "min", "typ", "max", "unit", "value")
DASH_CHARS = frozenset({"-", "–", "−", "\u2212", "\uf02d"})

# ══════════════════════════════════════════════════════════════════════════════
# FIX 1 — Texte vertical inversé
# ══════════════════════════════════════════════════════════════════════════════

def _get_rotated_text_map(page: Page) -> dict[tuple, str]:
    """
    Construit une map bbox → texte-correct pour les zones de texte rotatif.

    pdfplumber lit les chars upright=False de bas en haut → texte inversé.
    On regroupe ces chars par zone (même x-range arrondi) et on les re-trie
    par y décroissant (bas → haut dans l'espace PDF = ordre naturel de lecture
    pour texte vertical-upward).

    Retourne {(x0_arrondi, x1_arrondi): texte_corrigé}
    """
    rotated_chars = [c for c in page.chars if not c.get("upright", True)]
    if not rotated_chars:
        return {}

    # Grouper par bande x (±5 px de tolérance)
    groups: dict[int, list] = {}
    for c in rotated_chars:
        key = round(c["x0"] / 5) * 5
        groups.setdefault(key, []).append(c)

    result = {}
    for key, chars in groups.items():
        # Trier par top décroissant (top grand = en bas de la page)
        # Pour texte "Timers" vertical : le T est en bas (ex: top=446), le s en haut (top=427)
        # → trier du plus grand top au plus petit donne T-i-m-e-r-s ✓
        chars_sorted = sorted(chars, key=lambda c: c["top"], reverse=True)
        text = "".join(c["text"] for c in chars_sorted).strip()
        if text:
            x0 = min(c["x0"] for c in chars)
            x1 = max(c["x1"] for c in chars)
            y0 = min(c["top"] for c in chars)
            y1 = max(c["bottom"] for c in chars)
            # Use a slightly more generous bbox to match the cells later
            result[(round(x0)-2, round(y0)-2, round(x1)+2, round(y1)+2)] = text

    return result


def _fix_cell_rotated_text(cell_text: str, cell_bbox: Optional[tuple],
                           rotated_map: dict) -> str:
    """
    Si la cellule est dans une zone de texte rotatif connu, retourner
    le texte corrigé. Sinon retourner le texte brut.
    """
    if not cell_bbox or not rotated_map or not cell_text:
        return cell_text

    cx0, cy0, cx1, cy1 = cell_bbox
    for (rx0, ry0, rx1, ry1), corrected in rotated_map.items():
        # Vérifier si la bbox de la cellule overlap avec la zone rotative
        overlap_x = rx0 <= cx1 and rx1 >= cx0
        overlap_y = ry0 <= cy1 and ry1 >= cy0
        if overlap_x and overlap_y:
            return corrected

    return cell_text


# ══════════════════════════════════════════════════════════════════════════════
# FIX 2 — Header multi-niveau (normalisation des \n dans les cellules)
# ══════════════════════════════════════════════════════════════════════════════

def _normalize_newlines_in_cell(text: str) -> str:
    """Remplace les \n internes d'une cellule de data par un espace."""
    return re.sub(r"\s*\n\s*", " ", text).strip()


# ══════════════════════════════════════════════════════════════════════════════
# FIX 3 — Sélection de table par proximité sous la légende
# ══════════════════════════════════════════════════════════════════════════════

def _find_caption_y(page: Page, caption: str) -> Optional[float]:
    """
    Cherche la position y (bord bas) de la légende sur la page.
    Extrait les mots de la page et cherche le début de la légende.
    """
    # Extraire les 6 premiers mots de la légende pour la recherche
    caption_words = caption.lower().split()[:5]
    if not caption_words:
        return None

    words = page.extract_words()
    if not words:
        return None

    # Chercher une séquence de mots consécutifs qui matche le début de la légende
    for idx, word in enumerate(words):
        word_clean = word["text"].lower().split("(")[0].rstrip(".,:;!?")
        caption_word_clean = caption_words[0].lower().split("(")[0].rstrip(".,:;!?")
        if word_clean == caption_word_clean:
            # Vérifier les mots suivants
            match_count = 1
            for k in range(1, len(caption_words)):
                if idx + k < len(words):
                    w_clean = words[idx + k]["text"].lower().split("(")[0].rstrip(".,:;!?")
                    c_clean = caption_words[k].lower().split("(")[0].rstrip(".,:;!?")
                    if w_clean == c_clean:
                        match_count += 1
                    else:
                        break
            if match_count >= min(3, len(caption_words)):
                # Retourner le bord bas du mot de début
                return word["bottom"]

    return None


def _table_quality(table: list) -> float:
    """
    Score de 'qualité' d'un tableau réel (vs fragment d'image CID).
    Retourne le nombre de cellules réelles (non-vides, non-CID), ou -1.0 si vide.
    """
    if not table:
        return -1.0
    n_real = 0
    for row in table:
        for c in row:
            s = str(c or "").strip()
            if not s:
                continue
            if CID_PATTERN.search(s):
                continue
            n_real += 1
    return float(n_real) if n_real > 0 else -1.0


def _is_image_table(raw_table: list) -> bool:
    """Détecte si raw_table est un diagramme MCU (dimensions, broches)
    et non un vrai tableau de données. Se base sur le ratio de cellules
    purement numériques et l'absence de mots réels."""
    if not raw_table or len(raw_table) < 2:
        return False
    numeric = 0
    total = 0
    has_words = False
    for row in raw_table:
        for cell in row:
            s = str(cell or "").strip()
            if not s:
                continue
            total += 1
            if re.match(r'^-?[\d.,\s°\'"µ]+$', s):
                numeric += 1
            elif len(s) >= 3:
                has_words = True
    if total > 0 and numeric / total > 0.8 and not has_words:
        return True
    return False


def _pick_best_table(
    page: Page,
    tables: list,
    finder_tables: list,
    caption: str,
) -> tuple[Optional[list], Optional[Any], Optional[tuple]]:
    """
    [Fix 3] Sélectionne la table la plus proche EN DESSOUS de la légende.

    1. Localise la légende par coordonnées y
    2. Parmi les tables dont le bord supérieur est > y_légende, prend la plus proche
    3. Fallback : la plus grande si aucune position trouvée

    Retourne (raw_table, pdfplumber_table_obj, bbox).
    """
    candidates = [(t, ft) for t, ft in zip(tables, finder_tables)
                  if t and len(t) >= 2]
    if not candidates:
        return None, None, None

    caption_y = _find_caption_y(page, caption)

    if caption_y is not None and len(candidates) > 1:
        below = [(t, ft) for t, ft in candidates
                 if ft.bbox[1] > caption_y - 20]
        if below:
            sorted_candidates = sorted(below, key=lambda x: x[1].bbox[1])
        else:
            sorted_candidates = sorted(candidates, key=lambda x: x[1].bbox[1])
    else:
        sorted_candidates = sorted(candidates, key=lambda x: x[1].bbox[1])
        if _table_quality(sorted_candidates[0][0]) < 0.5:
            best_alt = max(candidates, key=lambda x: _table_quality(x[0]))
            if _table_quality(best_alt[0]) > _table_quality(sorted_candidates[0][0]):
                sorted_candidates = [best_alt] + [c for c in sorted_candidates if c != best_alt]

    # Filtrer les fausses tables images (diagrammes MCU)
    rejected_images = 0
    for best_t, best_ft in sorted_candidates:
        if _is_image_table(best_t):
            rejected_images += 1
            continue
        return best_t, best_ft, best_ft.bbox

    # Toutes les tables sont des images → log + retourner la meilleure quand même
    if rejected_images:
        logger.info(f"_pick_best_table: all {rejected_images}/{len(sorted_candidates)} "
                    f"candidates rejected as image tables (returning first anyway)")
    best_t, best_ft = sorted_candidates[0]
    return best_t, best_ft, best_ft.bbox


# ══════════════════════════════════════════════════════════════════════════════
# Extraction principale
# ══════════════════════════════════════════════════════════════════════════════

def _cell_str(cell) -> str:
    """Convertit une cellule pdfplumber (str ou None) en string propre."""
    if cell is None:
        return ""
    # Fix 4 : normaliser les \n internes dans les cellules de data
    return _normalize_newlines_in_cell(str(cell)).strip()


def _detect_vector_dashes(
    raw_table: list[list[str | None]],
    finder_table: Any,
    page: Page,
) -> list[list[str | None]]:
    """
    Détecte les tirets '-' rendus comme traits vectoriels (non capturés par
    pdfplumber) dans les colonnes à dash attendu (Parameter, Conditions,
    Symbol, Ratings, Min, Typ, Max, Unit, Value).

    Pour chaque cellule vide de ces colonnes :
      1. Vérifie page.chars dans la bbox de la cellule → dash-like char ?
      2. Vérifie page.lines dans la bbox → courte horizontale vectorielle ?
    Si une des deux vérifications trouve un dash → la cellule reçoit "-".
    Sinon elle reste "" (vraiment vide).

    Retourne la table corrigée (modifiée in-place).
    """
    if not raw_table or not finder_table or not hasattr(finder_table, 'rows'):
        return raw_table

    headers_raw = raw_table[0] if raw_table else []
    dash_cols = set()
    for i, h in enumerate(headers_raw):
        if h and isinstance(h, str) and any(kw in h.lower() for kw in DASH_COL_KEYWORDS):
            dash_cols.add(i)

    if not dash_cols:
        return raw_table

    n_rows = min(len(raw_table), len(finder_table.rows))

    for r_idx in range(n_rows):
        finder_row = finder_table.rows[r_idx]
        raw_row = raw_table[r_idx]

        if not hasattr(finder_row, 'cells'):
            continue

        n_cols = min(len(raw_row), len(finder_row.cells))

        for c_idx in range(n_cols):
            if c_idx not in dash_cols:
                continue

            cell_val = raw_row[c_idx]
            if cell_val is not None and cell_val != "":
                continue

            cell_bbox = finder_row.cells[c_idx]
            if cell_bbox is None:
                continue
            x0, top, x1, bottom = cell_bbox
            margin = 3

            found = False
            for ch in page.chars:
                if (ch["x0"] >= x0 - margin and ch["x1"] <= x1 + margin
                        and ch["top"] >= top - margin and ch["bottom"] <= bottom + margin
                        and ch["text"] in DASH_CHARS):
                    found = True
                    break

            if not found:
                for line in page.lines:
                    if (line["x0"] >= x0 - margin and line["x1"] <= x1 + margin
                            and line["top"] >= top - margin and line["bottom"] <= bottom + margin
                            and line.get("height", line["bottom"] - line["top"]) <= 4
                            and (line["x1"] - line["x0"]) > 3
                            and (line["x1"] - line["x0"]) < (x1 - x0) * 0.9):
                        found = True
                        break

            if found:
                raw_table[r_idx][c_idx] = "-"

    return raw_table


def _fill_horizontal(rows: list[list[str]]) -> None:
    """
    Remplit horizontalement les cellules vides : copie depuis le voisin gauche
    non-vide le plus proche dans la même ligne.
    Garantit que toute cellule vide reçoit une valeur.
    """
    for row in rows:
        last_val: str = ""
        for c in range(len(row)):
            if row[c]:
                last_val = row[c]
            elif last_val:
                row[c] = last_val


def _fill_vertical(rows: list[list[str]]) -> None:
    """
    Remplit verticalement les cellules vides : copie depuis la valeur au-dessus
    si celle-ci existe.
    """
    if len(rows) < 2:
        return
    for c in range(min(len(rows[0]), max(len(r) for r in rows))):
        last_val: str = ""
        for r in range(len(rows)):
            if c < len(rows[r]):
                if rows[r][c]:
                    last_val = rows[r][c]
                elif last_val:
                    rows[r][c] = last_val


def _ensure_no_empty_cells(rows: list[list[str]]) -> None:
    """
    Garantit zéro cellule vide : horizontal → vertical → voisin le plus proche.
    Même les cellules vides en première colonne sont remplies depuis la droite.
    Si toute une ligne est vide, copier celle du dessus.
    Itère jusqu'à stabilisation.

    Convergence : boucle while stabilise en ≤3 itérations. La 1ère itération
    remplit horizontal+vertical. La 2nde étend les valeurs nouvellement
    arrivées. La 3ème ne trouve plus de changement. La seule cellule qui
    reste vide est la (0,0) si la 1ère ligne est entièrement vide (artefact
    pdfplumber_text — pas de voisin à propager). Voir table_65.
    """
    changed = True
    while changed:
        old = [list(r) for r in rows]
        _fill_horizontal(rows)
        _fill_vertical(rows)
        for ri, row in enumerate(rows):
            first_val = next((c for c in row if c), "")
            if not first_val and ri > 0:
                above = rows[ri - 1]
                for c in range(len(row)):
                    if c < len(above):
                        row[c] = above[c]
                first_val = next((c for c in row if c), "")
            if first_val:
                for c in range(len(row)):
                    if not row[c]:
                        row[c] = first_val
        changed = any(
            old[r][c] != rows[r][c]
            for r in range(len(rows))
            for c in range(len(rows[r]))
        )


def _save_debug_image(
    page: Page,
    table_bbox: Optional[tuple],
    output_path: Path,
    confidence: str,
    has_empty_cells: bool = False,
) -> None:
    """Sauvegarde un crop de la zone de la table pour debug visuel."""
    try:
        if SAVE_IMAGES_ONLY_ON_ISSUE and confidence == "high" and not has_empty_cells:
            return

        img_dir = output_path.parent / "debug_images"
        img_dir.mkdir(parents=True, exist_ok=True)
        img_path = img_dir / f"{output_path.stem}.png"

        if table_bbox:
            cropped = page.crop(table_bbox)
            img = cropped.to_image(resolution=DEBUG_IMAGE_DPI)
        else:
            img = page.to_image(resolution=DEBUG_IMAGE_DPI)

        img.save(str(img_path))
        logger.debug(f"Debug image saved: {img_path}")
    except Exception as e:
        logger.warning(f"Could not save debug image: {e}")


def _propagate_spans_type2(
    table: list[list],
    raw_table: list[list],
    rows: int,
    cols: int,
    inserted_rows: int,
    ghost_cols: set[int],
    col_centers: list[Optional[float]],
    table_obj: Any,
) -> None:
    """
    Propagation géométrique des cellules fusionnées pour les PDFs Type 2
    (Antenna House / XML-based).
    
    Stratégie : pour chaque cellule vide, chercher la cellule la plus proche
    (même ligne d'abord, puis en remontant) dont la bbox CONTIENT le centre
    de la colonne cible.
    """
    if not table_obj or not hasattr(table_obj, "rows") or not table_obj.rows:
        return

    for r in range(rows):
        r_phys = r - inserted_rows
        if r_phys < 0:
            for c in range(cols):
                if table[r][c] is None:
                    table[r][c] = table[0][c]
            continue
        if r_phys >= len(table_obj.rows):
            break

        target_row = table_obj.rows[r_phys]
        target_row_top = target_row.bbox[1]

        for c in range(cols):
            if c >= len(table[r]) or table[r][c] is not None:
                continue
            if c in ghost_cols:
                table[r][c] = ""
                continue

            target_cx = col_centers[c]
            if target_cx is None:
                table[r][c] = ""
                continue

            master_val = None

            for r_m in range(r, -1, -1):
                r_m_phys = r_m - inserted_rows
                if r_m_phys < 0 or r_m_phys >= len(table_obj.rows):
                    continue
                for c_m in range(c + 1):
                    if c_m >= len(table_obj.rows[r_m_phys].cells):
                        continue
                    cell_bbox = table_obj.rows[r_m_phys].cells[c_m]
                    if cell_bbox is None:
                        continue

                    covers_row = (r_m == r) or (cell_bbox[3] > target_row_top + 2)
                    covers_col = (cell_bbox[0] <= target_cx <= cell_bbox[2])

                    if covers_row and covers_col:
                        val = raw_table[r_m][c_m] if c_m < len(raw_table[r_m]) else None
                        if val is not None:
                            master_val = val
                            break
                if master_val is not None:
                    break

            table[r][c] = master_val


def _count_header_rows_by_color(page, table_obj) -> int:
    """
    Compte les lignes d'en-tête via fond bleu foncé (Type 2 Antenna House).
    Retourne 0 si pas de bleu détecté (fallback vers heuristiques).
    Vérifie que les Y bleus correspondent à des vraies lignes du tableau,
    puis groupe les Y consécutifs (tolérance 2px). Limite à 150px de hauteur.
    """
    if not hasattr(page, 'rects') or not table_obj or not table_obj.bbox:
        return 0
    if not hasattr(table_obj, 'rows') or not table_obj.rows:
        return 0

    table_bbox = table_obj.bbox

    # Collecter les Y des vraies rangées du tableau
    row_y_bottoms = sorted(set(round(r.bbox[3], 1) for r in table_obj.rows if hasattr(r, 'bbox')))

    # Collecter les rectangles remplis en bleu foncé dans la zone table
    header_y_bottoms = []
    for r in page.rects:
        if (r['x0'] >= table_bbox[0] - 5 and r['x1'] <= table_bbox[2] + 5
                and r['top'] >= table_bbox[1] - 5 and r['bottom'] <= table_bbox[3] + 5):
            fill = r.get('non_stroking_color')
            if fill and len(fill) == 3:
                r_norm, g_norm, b_norm = fill
                if r_norm < 0.15 and g_norm < 0.25 and b_norm > 0.25:
                    if r['bottom'] - table_bbox[1] < 150:
                        header_y_bottoms.append(r['bottom'])

    if not header_y_bottoms:
        return 0

    # Filtrer : ne garder que les Y qui correspondent à des lignes réelles
    matching = sorted(set(
        round(y, 1) for y in header_y_bottoms
        if any(abs(round(y, 1) - ry) < 2.0 for ry in row_y_bottoms)
    ))

    if not matching:
        return 0

    # Grouper les Y consécutifs (tolérance 2px)
    groups = []
    for y in matching:
        if not groups or y - groups[-1][-1] > 2.0:
            groups.append([y])
        else:
            groups[-1].append(y)

    return len(groups)


def _expand_spans_and_headers(
    raw_table: list[list],
    table_obj: Optional[Any] = None,
    page: Optional[Any] = None,
    pdf_type: int = 1,
) -> tuple[list[str], list[list[str]], list[str]]:
    """
    Propagation géométrique + détection de profondeur d'en-tête + compression.

    Pipeline interne :
      0. Pré-calcul grille géométrique (centres de colonnes via bboxes)
      1. Division spatiale headers compressés (Type 2 : "STM32G081_/_F4")
      2. Détection colonnes fantômes (absentes de toutes les lignes physiques)
      3. Propagation géométrique des cellules fusionnées (rowspan/colspan)
         - Type 1 : parcours left-to-right, remontée verticale
         - Type 2 : recherche bbox qui CONTIENT le centre de colonne cible
      4. Détection profondeur d'en-tête (géométrique + couleur Type 2)
      5. Construction headers finaux (_build_final_headers avec propagation parent)
      6. Extraction lignes de données (hors header_depth)

    Fix complet gérant :
    - rowspan / colspan réels via bboxes
    - colonnes fantômes (structurellement absentes du PDF)
    - cellules encodant les sous-noms via \\n (Table 2 style)
    - en-têtes multi-niveaux avec texte rotatif
    Retourne (headers_compressés, rows_données_brutes, warnings).
    """
    if not raw_table:
        return [], [], ["empty_raw_table"]

    warnings = []
    table = [list(row) for row in raw_table]
    rows = len(table)
    cols = max(len(r) for r in table) if rows > 0 else 0

    # ── 0. Pré-calcul de la grille géométrique des colonnes ──────────────────
    col_centers: list[Optional[float]] = []
    grid_col_centers: list[float] = []
    if table_obj is not None and hasattr(table_obj, "rows") and len(table_obj.rows) > 0:
        # Extraire toutes les coordonnées X uniques pour reconstituer la grille exacte
        x_coords_set = set()
        for r_obj in table_obj.rows:
            for cell in r_obj.cells:
                if cell is not None:
                    x_coords_set.add(cell[0])
                    x_coords_set.add(cell[2])
        x_coords = sorted(list(x_coords_set))
        if len(x_coords) > 1:
            grid_col_centers = [(x_coords[i] + x_coords[i+1]) / 2.0 for i in range(len(x_coords) - 1)]
            col_centers = list(grid_col_centers)
        else:
            col_centers = [None] * cols
    else:
        col_centers = [None] * cols

    # Pad col_centers to match cols if needed
    while len(col_centers) < cols:
        col_centers.append(None)

    # ── 1. Division spatiale des en-têtes compressés (Table 2 style) ────────
    inserted_rows = 0
    rev_fallback = None
    if page is not None and table_obj is not None and hasattr(table_obj, "rows") and len(table_obj.rows) > 0 and len(grid_col_centers) == cols:
        compressed_c = -1
        cell_bbox = None
        span_cols = []
        for c in range(min(cols, len(raw_table[0]))):
            c_cell_bbox = table_obj.rows[0].cells[c] if c < len(table_obj.rows[0].cells) else None
            if c_cell_bbox is not None:
                c_span_cols = []
                for c2, cx in enumerate(grid_col_centers):
                    if c_cell_bbox[0] - 2 <= cx <= c_cell_bbox[2] + 2:
                        c_span_cols.append(c2)
                if len(c_span_cols) > 1:
                    # Vérification géométrique : la cellule a-t-elle du contenu multi-lignes ?
                    words = page.within_bbox(c_cell_bbox).extract_words(x_tolerance=1, y_tolerance=1)
                    lines_words = []
                    for w in words:
                        placed = False
                        for line in lines_words:
                            if abs(line[0]['top'] - w['top']) < 3:
                                line.append(w)
                                placed = True
                                break
                        if not placed:
                            lines_words.append([w])
                    if len(lines_words) >= 2:
                        compressed_c = c
                        cell_bbox = c_cell_bbox
                        span_cols = c_span_cols
                        break
                    # Fallback : texte inversé avec parties séparées par espace
                    cell_val = str(table[0][c] or "")
                    if ' ' in cell_val and _is_likely_reversed(cell_val):
                        parts = cell_val.split(' ')
                        if len(parts) >= 2 and len(parts) <= len(c_span_cols) and all(p.strip() for p in parts):
                            rev_fallback = (c, c_cell_bbox, c_span_cols, cell_val, parts)
        
        if compressed_c != -1 and cell_bbox is not None:
            c = compressed_c
                    
            # Extraction spatiale exacte
            words = page.within_bbox(cell_bbox).extract_words(x_tolerance=1, y_tolerance=1)
            
            # Regrouper les mots par ligne (tolérance 3pts)
            lines_words = []
            for w in words:
                placed = False
                for line in lines_words:
                    if abs(line[0]['top'] - w['top']) < 3:
                        line.append(w)
                        placed = True
                        break
                if not placed:
                    lines_words.append([w])
            
            lines_words.sort(key=lambda l: l[0]['top'])
            for line in lines_words:
                line.sort(key=lambda w: w['x0'])
            
            if len(lines_words) >= 2:
                # La 1ère ligne est le parent (ex: STM32G081_)
                parent_words = lines_words[0]
                parent_text = " ".join([w['text'] for w in parent_words])
                
                # Les lignes suivantes sont les enfants, projetés sur les centres
                new_row = [None] * cols
                col_text = {c2: [] for c2 in span_cols}
                
                for line in lines_words[1:]:
                    for w in line:
                        wcx = (w['x0'] + w['x1']) / 2.0
                        closest_c = min(span_cols, key=lambda c2: abs(grid_col_centers[c2] - wcx))
                        col_text[closest_c].append(w['text'])
                        
                for c2 in span_cols:
                    new_row[c2] = " ".join(col_text[c2])
                    
                table[0][c] = parent_text
                raw_table[0][c] = parent_text
                table.insert(1, new_row)
                raw_table.insert(1, new_row)
                rows += 1
                inserted_rows += 1
        
        if rev_fallback is not None:
            c, _, c_span_cols, cell_val, parts = rev_fallback
            rev_parts = [p[::-1].strip() for p in parts]
            parent_text = rev_parts[0]
            new_row = [None] * cols
            for idx, c2 in enumerate(c_span_cols):
                if idx == 0:
                    continue
                new_row[c2] = rev_parts[idx] if idx < len(rev_parts) else ""
            table[0][c] = parent_text
            raw_table[0][c] = parent_text
            table.insert(1, new_row)
            raw_table.insert(1, new_row)
            rows += 1
            inserted_rows += 1

    # ── 2. Identifier les colonnes fantômes ────────────────────────────────────
    ghost_cols: set[int] = set()
    if table_obj is not None and hasattr(table_obj, "rows"):
        for c in range(cols):
            # Si le c est hors limite ou si toutes les lignes physiques ont None
            if all(
                c >= len(table_obj.rows[r].cells) or table_obj.rows[r].cells[c] is None
                for r in range(min(rows - inserted_rows, len(table_obj.rows)))
            ):
                ghost_cols.add(c)

    # ── 3. Propagation géométrique des cellules fusionnées ─────────────────────
    if table_obj is not None and hasattr(table_obj, "rows") and len(table_obj.rows) > 0:
        if pdf_type == 2:
            _propagate_spans_type2(table, raw_table, rows, cols, inserted_rows,
                                   ghost_cols, col_centers, table_obj)
        else:
            for r in range(rows):
                r_phys = r - inserted_rows
                if r_phys < 0:
                    for c in range(cols):
                        if table[r][c] is None:
                            table[r][c] = table[0][c]
                    continue
                if r_phys >= len(table_obj.rows):
                    break
                target_row = table_obj.rows[r_phys]
                for c in range(cols):
                    if c >= len(table[r]) or table[r][c] is not None:
                        continue
                    if c in ghost_cols:
                        table[r][c] = ""
                        continue
                    target_cx = col_centers[c]
                    master_val = None
                    for r_m in range(r, -1, -1):
                        r_m_phys = r_m - inserted_rows
                        if r_m_phys < 0 or r_m_phys >= len(table_obj.rows):
                            continue
                        for c_m in range(c, -1, -1):
                            if c_m >= len(table_obj.rows[r_m_phys].cells):
                                continue
                            cell_bbox = table_obj.rows[r_m_phys].cells[c_m]
                            if cell_bbox is None:
                                continue
                            covers_row = (r_m == r) or (cell_bbox[3] > target_row.bbox[1] + 2)
                            covers_col = (target_cx is None) or (cell_bbox[2] > target_cx - 0.5)
                            if covers_row and covers_col:
                                master_val = (raw_table[r_m][c_m]
                                              if c_m < len(raw_table[r_m]) else None)
                                break
                        if master_val is not None:
                            break
                    table[r][c] = master_val
    else:
        # Fallback heuristique sans objet géométrique
        warnings.append("no_table_obj_for_spans")
        for r in range(rows):
            for c in range(cols):
                if c < len(table[r]) and table[r][c] is None:
                    left_val = table[r][c-1] if c > 0 else None
                    top_val = table[r-1][c] if r > 0 else None
                    raw_left = raw_table[r][c-1] if c > 0 and c-1 < len(raw_table[r]) else None
                    raw_top = raw_table[r-1][c] if r > 0 and c < len(raw_table[r-1]) else None
                    if left_val is not None and top_val is not None:
                        table[r][c] = top_val if (raw_top is not None and raw_left is None) else left_val
                    elif left_val is not None:
                        table[r][c] = left_val
                    elif top_val is not None:
                        table[r][c] = top_val

    # ── 3b. Fill-down : propager la dernière valeur non-vide vers le bas ───────
    for c in range(cols):
        carry = None
        for r in range(rows):
            val = table[r][c] if c < len(table[r]) else None
            if val is not None and str(val).strip():
                carry = val
            elif (val is not None and not str(val).strip()) or val is None:
                if carry is not None:
                    table[r][c] = carry

    # ── 4. Détection géométrique de la profondeur d'en-tête ────────────────────
    header_depth = 1 + inserted_rows
    if table_obj is not None and hasattr(table_obj, "rows") and len(table_obj.rows) > 0:
        for r_idx in range(1 + inserted_rows, min(rows, 5)):
            r_phys = r_idx - inserted_rows
            if r_phys >= len(table_obj.rows):
                break
            target_cy = (table_obj.rows[r_phys].bbox[1] + table_obj.rows[r_phys].bbox[3]) / 2.0
            if any(
                cell is not None and cell[3] > target_cy
                for cell in table_obj.rows[0].cells
            ):
                header_depth = r_idx + 1
            else:
                break
    else:
        for r in range(1, min(rows, 5)):
            is_hdr = any(
                c < len(raw_table[r]) and raw_table[r][c] is None
                and c < len(raw_table[r-1]) and raw_table[r-1][c] is not None
                for c in range(cols)
            )
            if is_hdr:
                header_depth = r + 1
            else:
                break

    # ── Détection couleur des lignes d'en-tête (Type 2 Antenna House) ─────
    if pdf_type == 2 and rows >= 2:
        hdr_rows = _count_header_rows_by_color(page, table_obj)
        if hdr_rows > 0:
            header_depth = max(header_depth, hdr_rows)

        # Fallback heuristique si la couleur seule ne suffit pas
        if header_depth == 1:
            non_empty_0 = sum(1 for c in raw_table[0] if c is not None and str(c).strip())
            if non_empty_0 < cols * 0.3:
                header_depth = 2
            elif non_empty_0 > 0:
                empty_prefix = 0
                for c in range(min(3, cols)):
                    val_0 = raw_table[0][c] if c < len(raw_table[0]) else ''
                    val_1 = raw_table[1][c] if c < len(raw_table[1]) else ''
                    empty_0 = val_0 is None or str(val_0).strip() == ''
                    filled_1 = val_1 is not None and str(val_1).strip() != ''
                    if empty_0 and filled_1:
                        empty_prefix += 1
                if empty_prefix >= 1:
                    header_depth = 2

            # Détection des en-têtes spanning via répétitions adjacentes dans Row 0
            # Ex: [A, B, C,C,C, D,D,D,D,D, E,E,E,E,E, F] → header_depth=2
            # On compare uniquement la 1ère ligne (avant \n) car les cellules
            # peuvent contenir des sous-étiquettes (ex: "Conditions\nVDD=1.62V").
            if header_depth == 1 and rows >= 2:
                identical = 0
                for c in range(1, min(cols, len(raw_table[0]))):
                    v0 = (str(raw_table[0][c] or "").strip()).split("\n")[0]
                    v1 = (str(raw_table[0][c-1] or "").strip()).split("\n")[0]
                    if v0 and v0 == v1:
                        identical += 1
                if identical >= cols * 0.25:
                    header_depth = 2

    if header_depth > 1:
        warnings.append(f"dynamic_header_depth:{header_depth}")

    # ── 5. Construction des en-têtes finaux ────────────────────────────────────
    final_headers = _build_final_headers(table, cols, header_depth, pdf_type)    # ── 6. Lignes de données ───────────────────────────────────────────────────
    final_rows = [
        [_normalize_newlines_in_cell(str(cell or "")) for cell in table[r]]
        for r in range(header_depth, rows)
    ]

    return final_headers, final_rows, warnings



def _build_final_headers(
    table: list[list],
    cols: int,
    header_depth: int,
    pdf_type: int = 1,
) -> list[str]:
    """
    Construit la liste finale des en-têtes.

    - header_depth == 1 : lecture directe de la ligne 0.
    - header_depth >= 2 : fusion « Parent / Enfant » sur les lignes d'en-tête.

    RÈGLE DE PROPAGATION :
    - None (cellule fusionnée) → réutiliser last_parent
    - '' (cellule vide, pas fusionnée) → pas de parent
    - '(N)' (footnote marker) → ignoré comme parent
    """
    if header_depth == 1:
        return [
            _normalize_newlines_in_cell(str(table[0][c] or "")).strip()
            for c in range(cols)
        ]

    final: list[str] = []
    last_parent: str = ""

    for c in range(cols):
        parts: list[str] = []
        for r in range(header_depth):
            original = table[r][c]
            val = str(original or "").strip()
            if "\n" in val:
                if pdf_type == 2:
                    val = val.replace("\n", " ").strip()
                else:
                    val = val.split("\n")[0].strip()
            val = _normalize_newlines_in_cell(val).strip()

            if r == 0:
                if original is None:
                    effective = last_parent
                elif val and not re.match(r'^\(\d+\)$', val):
                    last_parent = val
                    effective = val
                else:
                    effective = ""
            else:
                effective = val

            if effective and (not parts or parts[-1] != effective):
                parts.append(effective)

        final.append(" / ".join(parts))

    return final



def extract_table_grid(
    pdf_path: str,
    ref: TableRef,
    family: str,
    pdf_name: str,
    output_base: Path,
    all_refs: list[TableRef] = None,
    pdf_type: int = 1,
) -> dict:
    """
    Extrait la grille d'une table identifiée par `ref`.
    Retourne un dict conforme au schéma RawTable (sérialisable en JSON).

    Pipeline interne (dans l'ordre) :
      1. _extract_from_page → grille brute pdfplumber (bordures) ou pdfplumber_text
      2. Filtrage lignes au-dessus de la légende (si bleed)
      3. Si table vide → ré-extraction depuis page suivante (body sur page N+1)
      4. Troncature si bleed (texte de table suivante dans les données)
      5. _expand_spans_and_headers : Fix 2 (headers compressés), Fix 3 (ghost cols),
         Fix 5 (rowspan/colspan géométrique), détection profondeur header
      6. Fix 4 : continuation multi-pages (colonnes, ordre, correction headers)
      7. Fix 7 : insertion colonnes page 1 si continuation en a plus (x0 géométrique)
      8. Fix 6 : propagation descendante cellules vides (rowspan)
      9. Fix 8 : garantie zéro cellule vide (Type 2 uniquement)
     10. Détection has_empty_cells (APRÈS Fix 8 — seul le vrai artefact compte)
     11. Correction glyphes (fix_headers, fix_rows)
     12. Dedup lignes consécutives + suppression lignes totalement vides
     13. Fusion colonnes adjacentes identiques (_merge_identical_adjacent_columns)
     14. Fusion colonnes fragmentées (_merge_fragmented_columns) — SEULEMENT pdfplumber_text
     15. Évaluation qualité (confidence, empty_ratio)
     16. Image de debug si nécessaire
    """
    result = {
        "table_id":              ref.table_id,
        "caption":               ref.caption,
        "pdf_name":              pdf_name,
        "family":                family,
        "url":       f"https://www.st.com/resource/en/datasheet/{pdf_name}.pdf",
        "url_table": f"https://www.st.com/resource/en/datasheet/{pdf_name}.pdf#page={ref.page}",
        "section":               ref.section,
        "page":                  ref.page,
        "merged_pages":          [ref.page],
        "headers":               [],
        "rows":                  [],
        "extraction_method":     "pdfplumber",
        "extraction_confidence": "failed",
        "empty_cell_ratio":      1.0,
        "col_count":             0,
        "warnings":              [],
    }

    with pdfplumber.open(pdf_path) as pdf:
        if ref.page < 1 or ref.page > len(pdf.pages):
            result["warnings"].append(f"page_out_of_range:{ref.page}")
            return result

        page = pdf.pages[ref.page - 1]  # pdfplumber est 0-indexé

        # ── Fix 1 : préparer la map des textes rotatifs ────────────────────────
        rotated_map = _get_rotated_text_map(page)

        # ── Extraire la grille ─────────────────────────────────────────────────
        raw_table, table_obj, method, bbox = _extract_from_page(page, ref, rotated_map, pdf_type)

        # ── Raffinement section par position Y ────────────────────────────────
        # Utilise le cache Y-position construit par _assign_sections pour
        # associer la table à la section précise (même page avec plusieurs sections)
        caption_y = _find_caption_y(page, ref.caption)
        if caption_y is not None:
            y_section = get_section_at(pdf_path, ref.page, caption_y)
            if y_section:
                result["section"] = y_section

        if raw_table is None:
            result["warnings"].append("no_table_found_on_page")
            logger.warning(f"{ref.table_id}: no table found on page {ref.page}")
            return result

        result["extraction_method"] = method

        # ── Sauvegarder le nb de colonnes original (avant filtrage caption_y) ─
        orig_col_count = max(len(r) for r in raw_table) if raw_table else 0

        # ── Non-tables : Ordering information (pas une grille) ──────────────
        # Ces entrées de la TOC sont des listes textuelles, pas des tableaux.
        # On garde l'entrée mais avec rows vides + capture image.
        # Pour Type 1, on utilise extract_ordering_info pour parser le texte.
        if "ordering information" in ref.caption.lower():
            result["headers"] = []
            result["rows"] = []
            result["empty_cell_ratio"] = 1.0
            result["col_count"] = 0
            if pdf_type == 1:
                page_text = page.extract_text() or ""
                oi = extract_ordering_info(
                    page_text=page_text,
                    doc_id=pdf_name,
                    table_id=int(ref.table_id.split("_")[1]),
                    page=ref.page,
                )
                if oi["structured_json"].get("type") == "ordering_information":
                    result["structured_json"] = oi["structured_json"]
                    result["rag_chunks"] = oi["rag_chunks"]
                    result["extraction_confidence"] = "high"
                    result["empty_cell_ratio"] = 0.0
                    result["warnings"] = ["non_table:ordering_information"]
                else:
                    result["extraction_confidence"] = "low"
                    result["warnings"] = ["non_table_captured:ordering_information"]
            else:
                result["extraction_confidence"] = "low"
                result["warnings"] = ["non_table_captured:ordering_information"]
            if SAVE_DEBUG_IMAGES:
                out_path = output_base / family / pdf_name / f"{ref.table_id}.json"
                _save_debug_image(page, bbox, out_path, "low")
            logger.info(f"{ref.table_id}: non-table ordering info, captured")
            return result

        # ── Heuristiques de tracking ─────────────────────────────────────────
        heuristics = {}

        # ── Repérer la position y de la légende ─────────────────────────────
        caption_y = None
        if raw_table:
            caption_y = _find_caption_y(page, ref.caption)

        # ── [Fix Général] Filtrer les lignes au-dessus de la légende ─────────
        # La stratégie text merge les tables adjacentes (ex: Table 8 I2C + Table 9 USART).
        # On utilise les coordonnées y du finder pour ne garder que les lignes
        # sous la légende. table_obj → None car ses coordonnées ne correspondent
        # plus au raw_table filtré (l'expansion spatiale est moins critique pour
        # les tables text-strategy sans bordures).
        if raw_table and table_obj and hasattr(table_obj, 'rows'):
            if caption_y is not None and len(table_obj.rows) == len(raw_table):
                ys = [r.bbox[1] for r in table_obj.rows]
                keep = [i for i, y in enumerate(ys) if y >= caption_y - 5]
                if keep and len(keep) < len(raw_table):
                    raw_table = [raw_table[i] for i in keep]
                    table_obj = None
                    logger.info(f"{ref.table_id}: filtered {len(keep)}/{len(ys)} rows below caption (y>{caption_y:.0f})")
                elif not keep:
                    # Toutes les lignes sont au-dessus de la légende (bleed complet)
                    raw_table = []
                    table_obj = None
                    logger.info(f"{ref.table_id}: all {len(ys)} rows above caption, treated as empty")

        # ── Détecter si la légende est en bas de page (→ table page suivante) ──
        # Si la légende est dans les 25% inférieurs de la page, la table body
        # commence probablement en haut de la page suivante. On forcera la
        # vérification même si qq lignes résiduelles (footer, etc.) sont présentes.
        caption_near_bottom = False
        if caption_y is not None:
            if caption_y > page.height * 0.75:
                caption_near_bottom = True
                heuristics["caption_near_bottom"] = True
                heuristics["caption_y_ratio"] = round(caption_y / page.height, 3)

        # ── [Fix] Si vide ou légende en bas → extraire depuis la page suivante ──
        # Quand la table body est sur la page d'après (titre page N, corps page N+1),
        # le filtrage ne laisse que 0-1 lignes. On vérifie la page immédiatement
        # suivante uniquement (offset=1) — chercher plus loin risque de capturer
        # une table complètement différente.
        # Le mot-clé de légende donne un bonus modéré (+20) pour départager,
        # sans dominer la qualité de base.
        # Seuil n_non_empty < 3 : les cas à 3-4 lignes sont des faux positifs
        # (légende mi-page avec extraction partielle). 0-2 = vraie page suivante.
        start_page = ref.page
        n_non_empty = sum(1 for row in raw_table if any(str(c).strip() for c in row)) if raw_table else 0
        should_try_next = (
            n_non_empty == 0 or
            (caption_near_bottom and method in ("pdfplumber", "pdfplumber_text") and n_non_empty < 3)
        )
        saved_pre_body = []
        if should_try_next and ref.page < len(pdf.pages):
                caption_keyword = ""
                if ref.caption:
                    parts = ref.caption.split(".")
                    if len(parts) >= 2:
                        cw = parts[-1].strip().split()
                        if cw:
                            caption_keyword = cw[0].lower()
                pg_idx = ref.page  # page suivante (déjà +1 ci-dessous)
                if pg_idx < len(pdf.pages):
                    p = pdf.pages[pg_idx]
                    # Le body du tableau commence en haut de la page suivante.
                    # Le bas de page (footer, autre contenu) est exclu pour
                    # éviter les colonnes parasites (14+ au lieu de 7).
                    crop = (p.bbox[0], p.bbox[1], p.bbox[2], p.height * 0.7)
                    p_cropped = p.within_bbox(crop)
                    r = _get_rotated_text_map(p_cropped)
                    nxt_raw, nxt_obj, nxt_method, nxt_bbox = _extract_from_page(p_cropped, ref, r, pdf_type, "")
                    if nxt_raw:
                        nq = _table_quality(nxt_raw)
                        keyword_bonus = 0
                        if caption_keyword:
                            for row in nxt_raw:
                                for c in row:
                                    if c and caption_keyword in c.lower():
                                        keyword_bonus = 20
                                        break
                                if keyword_bonus:
                                    break
                        nq_boosted = nq + keyword_bonus
                        if nq_boosted >= 10.0:
                            # ── Garde-fou : page suivante contient une AUTRE table ? ──
                            nxt_text = " ".join(w["text"] for w in p.extract_words()).lower()
                            cur_num = int(ref.table_id.split("_")[1])
                            other_tables = re.findall(r"table\s+(\d+)", nxt_text)
                            other_nums = [int(n) for n in other_tables if n.isdigit()]
                            if any(n != cur_num for n in other_nums):
                                logger.info(f"{ref.table_id}: body_on_next_page rejected "
                                            f"(page {pg_idx + 1} has a different table)")
                                nq_boosted = -1
                            else:
                                nxt_cols = max(len(r) for r in nxt_raw) if nxt_raw else 0
                                if orig_col_count > 0 and nxt_cols > orig_col_count * 2 + 5:
                                    logger.info(f"{ref.table_id}: body_on_next_page rejected "
                                                f"(cols {orig_col_count}→{nxt_cols})")
                                    nq_boosted = -1
                                else:
                                    # Sauvegarde des lignes de la page N avant remplacement
                                    saved_pre_body = raw_table
                                    raw_table, table_obj, method, bbox = nxt_raw, nxt_obj, nxt_method, nxt_bbox
                                    page = pdf.pages[pg_idx]
                                    start_page = ref.page + 1
                                    result["extraction_method"] = method
                                    heuristics["body_on_next_page"] = True
                                    heuristics["next_page_q_boosted"] = nq_boosted
                                    logger.info(f"{ref.table_id}: body on page {start_page}, re-extracted ({len(raw_table)} rows, {nxt_method}, q={nq_boosted:.0f})")

        # ── Capturer le nombre de colonnes extraites ─────────────────────────
        if raw_table:
            heuristics["cols_extracted"] = max(len(r) for r in raw_table)

        # ── [Fix] Suppression des lignes de titre débordant dans la grille ──
        # Le titre "Table N. ... (continued)" peut apparaître n'importe où
        # dans raw_table (pas seulement ligne 0) quand body_on_next_page crop
        # 70 % ou que pdfplumber_text éclate la page. On scanne TOUTES les
        # lignes et on supprime toute ligne dont UNE cellule matche
        # "Table N." avec le bon numéro de table.
        if raw_table:
            cur_id = int(re.findall(r'\d+', str(ref.table_id))[0])
            before = len(raw_table)
            cleaned = []
            for ri, row in enumerate(raw_table):
                is_bleed = False
                # Vérification cellule par cellule
                for ci, cell in enumerate(row):
                    cell_s = str(cell).strip()
                    m = re.match(r"(?:Table|Tableau)\s+(\d+)[\.:]", cell_s, re.IGNORECASE)
                    if m and int(m.group(1)) == cur_id:
                        is_bleed = True
                        break
                    # Cas fragmenté : "Table 11" / "8." / "LQFP176..." sur 3 cellules
                    if re.match(r"(?:Table|Tableau)\s+\d+\s*$", cell_s, re.IGNORECASE):
                        combined = cell_s
                        for k in range(1, 4):
                            if ci + k >= len(row):
                                break
                            combined += str(row[ci + k]).strip()
                            m2 = re.match(r"(?:Table|Tableau)\s+(\d+)[\.:]", combined, re.IGNORECASE)
                            if m2:
                                if int(m2.group(1)) == cur_id:
                                    is_bleed = True
                                break
                        if is_bleed:
                            break
                if not is_bleed:
                    cleaned.append(row)
            if len(cleaned) < before:
                raw_table = cleaned
                logger.info(f"{ref.table_id}: removed {before - len(cleaned)} caption bleed rows ({len(raw_table)} rows remaining)")

        # ── [Fix] Troncature des tables suivantes (bleed) ────────────────────
        # La stratégie texte capture tout le texte de la page, y compris les
        # tables suivantes et le pied de page. On détecte les marqueurs de
        # transition et on coupe raw_table à la première ligne concernée.
        if method == "pdfplumber_text" and raw_table:
            rows_before = len(raw_table)
            raw_table = _truncate_at_next_table(raw_table, ref.table_id)
            if len(raw_table) < rows_before:
                heuristics["truncated_rows"] = rows_before - len(raw_table)

        # ── [Fix 4b] Suppression lignes de bleed page header/footer ────────
        # pdfplumber_text éclate les titres de section (ex: "Electrical
        # characteristics") en 14+ colonnes. On supprime ces lignes avant
        # _expand_spans_and_headers pour que l'expansion + _merge_fragmented_columns
        # (étape 14) fonctionnent sur les vrais headers de la table.
        if method == "pdfplumber_text" and raw_table:
            rows_before = len(raw_table)
            raw_table = _remove_bleed_rows(raw_table, method)
            if len(raw_table) < rows_before:
                heuristics["bleed_rows_removed"] = rows_before - len(raw_table)

        # ── Vérification finale : table vide ──────────────────────────────────
        if not raw_table:
            result["warnings"].append("empty_raw_table")
            return result

        # ── Fix 2 & 5 : Headers structurels et Propagation globale ─────────────
        headers, rows_raw, span_warnings = _expand_spans_and_headers(raw_table, table_obj, page, pdf_type)
        result["warnings"].extend(span_warnings)

        # ── [Fix] Fusion des lignes de la page N avec la page N+1 ─────────────
        # Quand body_on_next_page remplace raw_table, les lignes de la page
        # originale (page N) sont perdues. On les réinsère ici en utilisant
        # le même header_depth que la page N+1 (même structure de table).
        if saved_pre_body:
            hd = len(raw_table) - len(rows_raw)
            extra = saved_pre_body[hd:]
            if extra:
                rows_raw = extra + rows_raw
                logger.info(f"body_on_next_page: merged {len(extra)} rows from page {ref.page}")

        # ── Fix 4 : Continuation multi-pages ──────────────────────────────────
        merged_pages = [start_page]
        if all_refs is not None:
            header_depth = len(raw_table) - len(rows_raw)
            first_cell_text = str(raw_table[0][0] or "").strip() if raw_table else ""
            base_x0s = _get_col_x0s(table_obj) if table_obj else []
            c_pages, c_rows, target_cols, cont_x0s_list = find_continuations(
                pdf,
                start_page,
                len(headers),
                all_refs,
                ref.table_id,
                header_depth,
                first_cell_text,
                pdf_type=pdf_type,
                base_col_x0s=base_x0s,
            )
            if c_pages and len(c_pages) > 1:
                merged_pages = c_pages

                # Fix 7 : élargir la page 1 si la continuation a scindé une colonne
                # Ex: page 1 a ["Name"] mais page N a ["Name", "Name sub"]
                # L'insertion se fait à la position dictée par la géométrie x0 :
                # les x0 présents dans la continuation mais absents de la page 1
                # (tolérance 5pt) reçoivent une colonne vide.
                # Le nouveau header est copié depuis le voisin de gauche.
                n_insert = target_cols - len(headers)
                if n_insert > 0:
                    p1_x0s = _get_col_x0s(table_obj) if table_obj else []
                    merged_cont_x0s = sorted(set(
                        round(x, 1) for lst in cont_x0s_list for x in lst
                    ))
                    surplus_x0s = []
                    for cx in merged_cont_x0s:
                        if not any(abs(cx - px) < 5 for px in p1_x0s):
                            surplus_x0s.append(cx)

                    if not p1_x0s:
                        insert_positions = list(range(len(headers), len(headers) + n_insert))
                    else:
                        insert_positions = []
                        for sx in surplus_x0s:
                            pos = sum(1 for px in p1_x0s if px < sx)
                            insert_positions.append(pos)

                    insert_positions = sorted(set(insert_positions))[:n_insert]

                    # Insertion droite→gauche pour préserver les indices
                    # La valeur insérée est copiée depuis le voisin gauche
                    # (même logique que le header). Si pos=0 (pas de voisin
                    # gauche), copier la valeur de l'ancienne colonne 0
                    # (avant insertion de Fix 7).
                    for pos in sorted(insert_positions, reverse=True):
                        header_val = headers[pos - 1] if pos > 0 else headers[0]
                        headers = headers[:pos] + [header_val] + headers[pos:]
                        for r in range(len(rows_raw)):
                            if pos > 0 and pos - 1 < len(rows_raw[r]):
                                neighbor_val = rows_raw[r][pos - 1]
                            elif pos == 0 and rows_raw[r]:
                                neighbor_val = rows_raw[r][0]
                            else:
                                neighbor_val = ""
                            rows_raw[r] = rows_raw[r][:pos] + [neighbor_val] + rows_raw[r][pos:]

                rows_raw.extend([[_cell_str(c) for c in row] for row in c_rows])
                result["warnings"].append(f"multi_page_merged:{len(merged_pages)}")

                # Fix 8 : remplir horizontalement les lignes de continuation
                # (les cellules fusionnées horizontalement ne sont pas propagées
                # dans les pages de continuation, contrairement à la page 1).
                if pdf_type == 2:
                    _fill_horizontal(rows_raw)

        # ── Fix 6 : Propagation descendante des cellules vides ─────────────
        # Les cellules fusionnées verticalement (rowspan) apparaissent comme ""
        # dans les lignes de continuation et parfois même sur la 1ère page.
        # Règle : si une cellule est "", on copie la valeur de la ligne du dessus.
        # Exception Type 2 : si la 1ère colonne change vers une valeur jamais vue
        # → nouveau groupe → ne pas propager (ex: table_6 I/O → Notes évite
        # la propagation erronée entre groupes différents).
        # Note : inactif pour pdfplumber_text (les cellules vides sont des
        # trous structurels, pas des rowspan — Fix 6 les dupliquerait).
        if rows_raw and result["extraction_method"] != "pdfplumber_text":
            n_cols = len(headers)
            for r in range(1, len(rows_raw)):
                first_changed = False
                seen_before = False
                if pdf_type == 2:
                    cur_first = (rows_raw[r][0] or "").strip() if len(rows_raw[r]) > 0 else ""
                    prev_first = (rows_raw[r-1][0] or "").strip() if len(rows_raw[r-1]) > 0 else ""
                    first_changed = cur_first != prev_first
                    if first_changed:
                        seen_before = any(
                            (rows_raw[r2][0] or "").strip() == cur_first
                            for r2 in range(r)
                        )
                for c in range(min(n_cols, len(rows_raw[r]))):
                    if rows_raw[r][c] == "" and c < len(rows_raw[r-1]) and rows_raw[r-1][c] != "":
                        if first_changed and not seen_before:
                            continue
                        rows_raw[r][c] = rows_raw[r-1][c]

        # ── Fix 8 (Type 2) : garantie zéro cellule vide ──────────────────
        # Remplit horizontalement puis verticalement toute cellule résiduelle.
        # Même les cellules vraiment vides dans le PDF reçoivent le dernier
        # voisin connu (règle "remete le père").
        if pdf_type == 2:
            _ensure_no_empty_cells(rows_raw)

        # ── Détection cellules vides après Fix 8 ─────────────────────────
        # ── Correction des glyphes ─────────────────────────────────────────────
        headers    = fix_headers(headers)
        for i in range(len(headers)):
            if _is_likely_reversed(headers[i]):
                if ' ' in headers[i]:
                    headers[i] = ' '.join(p[::-1].strip() for p in headers[i].split(' '))
                else:
                    headers[i] = headers[i][::-1].strip()
        rows_fixed = fix_rows(rows_raw)

        rows_fixed = _deduplicate_rows(rows_fixed)

        # ── [Fix] Suppression des lignes totalement vides ───────────────────
        # Les artefacts pdfplumber_text créent parfois des lignes où toutes
        # les cellules sont "" (ex: ligne séparatrice header/body mal capturée).
        rows_fixed = [row for row in rows_fixed if any(c for c in row)]

        # ── [Fix] Correction texte inversé (cellules fusionnées verticales) ─
        rows_fixed = _fix_reversed_cells(rows_fixed)

        # ── [Fix] Suppression des lignes header résiduelles dans les données ─
        # Cas rare : _expand_spans_and_headers peut laisser des lignes header
        # dans rows_fixed (ex: footnote "(1)" ou header dupliqué après merge).
        while rows_fixed and headers:
            if rows_fixed[0] == headers:
                rows_fixed.pop(0)
            elif len(rows_fixed[0]) == len(headers) and all(
                rows_fixed[0][c] == headers[c] for c in range(1, len(headers))
            ):
                rows_fixed.pop(0)
            else:
                break

        # ── [Fix] Fusion des colonnes adjacentes identiques ─────────────────
        headers, rows_fixed = _merge_identical_adjacent_columns(headers, rows_fixed)

        # ── [Fix] Fusion des colonnes fragmentées (pdfplumber_text) ─────────
        # S'applique à TOUS les pdfplumber_text (Type 1 et Type 2) car
        # l'extraction sans bordures fragmente toujours les mots, quel que
        # soit le type de PDF. Les 4 heuristiques (H1-H4) protègent les
        # vraies colonnes ("Min", "Typ", "Max", "Unit").
        if result["extraction_method"] == "pdfplumber_text":
            cols_before = len(headers)
            headers, rows_fixed, merged = _merge_fragmented_columns(headers, rows_fixed)
            if merged > 0:
                heuristics["columns_initial"] = cols_before
                heuristics["columns_merged"] = merged
                heuristics["columns_final"] = len(headers)

        # ── [Fix] Fusion header vide → droite (pdfplumber_text) ───────────
        # S'applique à TOUS les pdfplumber_text : les colonnes-pont à
        # header vide apparaissent dans tous les types de PDF quand
        # l'extraction texte est utilisée (pas de bordures pour délimiter).
        if result["extraction_method"] == "pdfplumber_text":
            cols_before_h = len(headers)
            headers, rows_fixed, merged_h = _merge_empty_header_rightward(headers, rows_fixed)
            if merged_h > 0:
                heuristics["columns_initial"] = heuristics.get("columns_initial", cols_before_h)
                heuristics["columns_merged"] = heuristics.get("columns_merged", 0) + merged_h
                heuristics["columns_final"] = len(headers)

        # ── [Fix] Suppression des footnotes trailing (pdfplumber_text) ──────
        # Les notes de bas de tableau apparaissent en fin de données avec
        # une 1ère cellule au format "N." (ex: "1.", "2.", "6.").
        # Ne supprime qu'un bloc contigu à la fin.
        if result["extraction_method"] == "pdfplumber_text" and rows_fixed:
            rows_before_fn = len(rows_fixed)
            rows_fixed, fn_removed = _remove_trailing_footnotes(rows_fixed)
            if fn_removed > 0:
                heuristics["footnote_rows_removed"] = fn_removed

        # ── [Fix] Suppression des lignes de fuite post-merge ──────────────
        # Après fusion des colonnes, les références à d'autres tables
        # deviennent visibles (ex: "Table26", "Table 23:"). On refait
        # une passe de nettoyage pour les lignes résiduelles qui :
        # 1. Référencent une autre table (même sans espace/point)
        # 2. Commencent par une minuscule (continuation parasite)
        if result["extraction_method"] == "pdfplumber_text" and rows_fixed:
            rows_before_bleed = len(rows_fixed)
            rows_fixed, bleed_removed = _remove_bleed_rows_bottom(rows_fixed, ref.table_id)
            if bleed_removed > 0:
                heuristics["post_merge_bleed_removed"] = bleed_removed

        # ── [Fix] Suppression des lignes footnote (N) résiduelles ──────────
        # Après les merges, certaines notes de bas de tableau comme "(1)"
        # peuvent rester sous forme de ligne complète (1ère cellule = "(1)").
        # _remove_trailing_footnotes les rate car elles utilisent (N) au lieu
        # de N., et _remove_bleed_rows_bottom les rate car une ligne de données
        # réelle (ex: "I DD(BOR)") bloque le scan bottom-up avant d'atteindre
        # la ligne footnote (1). Ce filtre parcourt TOUTES les lignes (pas
        # seulement celles de fin) et supprime toute ligne dont la 1ère cellule
        # est exactement (N) — format typique des notes de bas de datasheet.
        if result["extraction_method"] == "pdfplumber_text" and rows_fixed:
            before = len(rows_fixed)
            rows_fixed = [r for r in rows_fixed
                          if not re.match(r'^\(\d+\)$', str(r[0]).strip())]
            fn_removed = before - len(rows_fixed)
            if fn_removed > 0:
                heuristics["footnote_paren_removed"] = fn_removed
                logger.info(f"Removed {fn_removed} footnote (N) rows")

        # ── [Fix] Validateur post-merge anti-destruction ────────────────────
        # Si après tous les merges, un header contient "Table" + un chiffre,
        # c'est que le merge a collé la légende dans une cellule réelle.
        # evaluate_table voit un ratio vide=0.0 et marque "high" → faux positif.
        # On force low + warning pour signaler la corruption.
        corrupted = False
        if result["extraction_method"] == "pdfplumber_text":
            for h in headers:
                if re.search(r"Table\s+\d+", str(h), re.IGNORECASE):
                    corrupted = True
                    break
            if not corrupted:
                for row in rows_fixed[:5]:
                    for c in row:
                        if re.search(r"Table\s+\d+", str(c), re.IGNORECASE):
                            corrupted = True
                            break
                    if corrupted:
                        break

        # ── Évaluation qualité ─────────────────────────────────────────────────
        confidence, empty_ratio, _, warnings_eval = evaluate_table(headers, rows_fixed)
        if corrupted:
            warnings_eval.append("post_merge_corrupted")
            confidence = "low"
        result["warnings"].extend(warnings_eval)

        # ── Marquage des tables avec cellules vides ────────────────────────────
        # Détecte les vrais vides dans la sortie FINALE (rows_fixed) après
        # tous les Fix. Les cellules vides temporaires des colonnes fragmentées
        # (merge, header leak, propagation) ne comptent pas.
        has_empty_final = bool(rows_fixed) and any(
            not cell or not str(cell).strip()
            for row in rows_fixed
            for cell in row
        )
        if has_empty_final:
            result["warnings"].append("has_empty_cells")
            result["has_empty_cells"] = True

        # ── Remplissage du résultat ────────────────────────────────────────────
        if heuristics:
            result["heuristics"] = heuristics
        result.update({
            "headers":               headers,
            "rows":                  rows_fixed,
            "merged_pages":          merged_pages,
            "extraction_confidence": confidence,
            "empty_cell_ratio":      round(empty_ratio, 4),
            "col_count":             len(headers),
        })

        # ── Image de debug ─────────────────────────────────────────────────────
        if SAVE_DEBUG_IMAGES:
            out_path = output_base / family / pdf_name / f"{ref.table_id}.json"
            _save_debug_image(page, bbox, out_path, confidence,
                              has_empty_cells=result.get("has_empty_cells", False))

    logger.info(
        f"{ref.table_id} | page={ref.page} | method={method} "
        f"| confidence={confidence} | rows={len(result['rows'])} "
        f"| empty={result['empty_cell_ratio']:.2f}"
    )
    return result


def _is_likely_reversed(cell: str) -> bool:
    """True si le texte semble être lu à l'envers (vertical dans le PDF).

    Gère le texte avec newlines (ex: "A\\ntroP" = "PortA" inversé).
    Compare la distribution des majuscules entre l'original et l'inversé
    pour détecter les textes inversés même quand l'original commence
    par une majuscule (ex: "A troP" → "Port A").
    """
    clean = cell.replace("\n", "")
    if len(clean) < 5:
        return False

    rev = clean[::-1]

    # Le texte inversé doit commencer par Maj (acronyme comme "STM" ou mot comme "Port")
    if not (rev and rev[0].isupper() and len(rev) > 1):
        return False

    # La version inversée doit avoir au moins autant de lettres
    cell_alpha = sum(1 for c in clean if c.isalpha())
    rev_alpha = sum(1 for c in rev if c.isalpha())
    if rev_alpha < cell_alpha:
        return False

    # Compter les majuscules en milieu de mot (ni position 0, ni après espace)
    def _mid_word_uppers(text: str) -> int:
        count = 0
        for i, c in enumerate(text):
            if c.isupper() and i > 0 and not text[i - 1].isspace():
                count += 1
        return count

    def _initial_upper_run(text: str) -> int:
        """Longueur de la séquence majuscule au début du texte.
        Ex: "STM32..." → 3, "Port A" → 1, "Timers" → 1"""
        count = 0
        for c in text:
            if c.isupper():
                count += 1
            else:
                break
        return count

    clean_mid = _mid_word_uppers(clean)
    rev_mid = _mid_word_uppers(rev)

    # La version inversée ne doit pas avoir PLUS de majuscules en milieu de mot
    if rev_mid > clean_mid:
        return False

    # Si l'inversé a une plus longue séquence majuscule au début, c'est un
    # part number STM32 (ex: "STM32C5A3KxT" a "STM" soit 3, l'inversé
    # "TxK3A5C23MTS" a "T" soit 1)
    clean_init = _initial_upper_run(clean)
    rev_init = _initial_upper_run(rev)
    if rev_init > clean_init:
        return True

    # Si l'original commence par majuscule et les deux versions ont autant
    # de majuscules en milieu de mot, c'est symétrique → pas inversé
    if not clean[0].islower() and rev_mid >= clean_mid:
        return False

    return True


def _fix_reversed_cells(rows: list[list]) -> list[list]:
    """Corrige le texte inversé dans les cellules dues au rendu vertical.

    Quand une cellule fusionnée contient du texte écrit verticalement (rotation 90°),
    pdfplumber peut lire les caractères dans l'ordre inverse. On détecte ça en
    vérifiant si le texte inversé semble plus naturel.
    """
    if not rows:
        return rows
    fixed = []
    for row in rows:
        fixed_row = []
        for cell in row:
            if isinstance(cell, str) and len(cell) >= 5:
                clean = cell.replace("\n", "")
                if _is_likely_reversed(clean):
                    cell = clean[::-1]
            fixed_row.append(cell)
        fixed.append(fixed_row)
    return fixed


def _deduplicate_rows(rows: list[list[str]]) -> list[list[str]]:
    """
    Supprime les lignes consécutives strictement identiques.
    Fixe les doublons créés par Fix 6 quand des lignes vides artificielles
    (pdfplumber_text) reçoivent la valeur de la ligne au-dessus.
    """
    if not rows:
        return rows
    result = [rows[0]]
    for row in rows[1:]:
        if row != result[-1]:
            result.append(row)
    return result


def _remove_bleed_rows(raw_table: list, method: str) -> list:
    """
    Supprime les lignes de bleed page header/footer en haut de raw_table.
    Uniquement pour pdfplumber_text, 2 heuristiques séquentielles :
      1. Sparsité (< 30% de remplissage) — supprime les lignes creuses
         (ex: "Electrical characteristics" éclaté en 3/14 colonnes)
      2. Préfixe (>80% des cellules partagent le même préfixe 4-car.)
         (ex: "Elect" x14)
    Appliqué avant _expand_spans_and_headers pour que l'expansion travaille
    sur les vrais headers (Symbol/Parameter/...) et que l'étape 14 existante
    (_merge_fragmented_columns) fusionne correctement les fragments.
    """
    if method != "pdfplumber_text" or not raw_table:
        return raw_table
    result = list(raw_table)
    for _ in range(min(10, len(result))):
        if not result or not result[0]:
            break
        row = result[0]
        total = len(row)
        non_empty = [str(c).strip() for c in row if c and str(c).strip()]
        n_filled = len(non_empty)
        fill_ratio = n_filled / total if total > 0 else 0
        # Heuristique 1 : ligne vide ou très creuse (< 30% remplie)
        if fill_ratio < 0.3:
            logger.info(f"_remove_bleed_rows: sparse row removed ({n_filled}/{total} filled)")
            result.pop(0)
            continue
        # Heuristique 2 : >80% des cellules partagent le même préfixe
        if n_filled >= 3:
            pref_counts = {}
            for c in non_empty:
                p = c[:4].lower() if len(c) >= 4 else c.lower()
                pref_counts[p] = pref_counts.get(p, 0) + 1
            max_p, max_n = max(pref_counts.items(), key=lambda x: x[1])
            if max_n / n_filled > 0.8:
                logger.info(f"_remove_bleed_rows: prefix bleed removed ({max_p} x{max_n}/{n_filled})")
                result.pop(0)
                continue
        break
    return result


def _remove_bleed_rows_bottom(rows: list[list[str]], table_id: str) -> tuple[list[list[str]], int]:
    """
    Supprime les lignes de fuite en BAS du tableau APRÈS la fusion des
    colonnes. Détecte les lignes qui référencent une autre table ou qui
    commencent par une minuscule (continuation de texte parasite).
    Également : 1ère cellule non-alphanumérique (ex: ".4 Embe", "(1)")
    → continuation ou footnote parasite. Exclut '+' (symboles comme +3.3V).
    Retourne (rows_nettoyées, nb_supprimées).
    """
    if not rows:
        return rows, 0
    cut = len(rows)
    for i in range(len(rows) - 1, -1, -1):
        text = "".join(str(c or "") for c in rows[i])
        if not text.strip():
            cut = i
            continue
        # Référence à une autre table (ex: "Table 26", "Table26")
        m = re.search(r'\bTable\s*(\d+)', text, re.IGNORECASE)
        if m:
            nums = re.findall(r'\d+', str(table_id))
            cur_id = int(nums[0]) if nums else 0
            if int(m.group(1)) != cur_id:
                cut = i
                logger.info(f"_remove_bleed_rows_bottom: cut at row {i} (ref Table {m.group(1)}, cur={cur_id})")
                continue
        # Première cellule commence par minuscule → continuation parasite
        # (ex: "pecified by design." → 'p' minuscule = fragment de "Specified")
        first = str(rows[i][0]).strip() if rows[i] else ""
        if first and first[0].islower():
            cut = i
            logger.info(f"_remove_bleed_rows_bottom: cut at row {i} (lowercase first cell '{first[:20]}')")
            continue
        # Première cellule commence par un caractère non-alphanumérique
        # (ex: ".4 Embe", "(1)") → continuation ou footnote parasite
        # Exclut '+' car certains Symboles peuvent commencer par '+' (ex: +3.3V).
        # Cet ajout complète la détection minuscule pour les cas où le premier
        # caractère est un point, une parenthèse, etc. (continuations de texte).
        if first and not first[0].isalnum() and not first[0] == '+':
            cut = i
            logger.info(f"_remove_bleed_rows_bottom: cut at row {i} (non-alnum first cell '{first[:20]}')")
            continue
        break
    removed = len(rows) - cut
    if removed > 0:
        logger.info(f"_remove_bleed_rows_bottom: removed {removed} trailing bleed rows")
    return rows[:cut], removed


def _truncate_at_next_table(
    raw_table: list[list],
    table_id,
) -> list[list]:
    """
    [Fix] Tronque raw_table dès qu'une ligne contient une table suivante
    (ex: "Table 66." dans les données de la Table 65) ou un pied de page.
    """
    if not raw_table:
        return raw_table
    # Extraire le numéro de table (supporte "table_65", "65", 65)
    if isinstance(table_id, int):
        cur_id = table_id
    else:
        nums = re.findall(r'\d+', str(table_id))
        cur_id = int(nums[0]) if nums else 0
    cut_idx = len(raw_table)
    for i, row in enumerate(raw_table):
        text = "".join(str(c or "") for c in row)
        # Couper si "Table N.", "Table N:", ou "TableN" (sans espace)
        # avec N > table_id actuelle. Le pattern large \bTable\s*(\d+)
        # capture aussi "Table26" (fréquent dans les PDFs scannés).
        m = re.search(r'\bTable\s*(\d+)', text)
        if m and int(m.group(1)) > cur_id:
            cut_idx = i
            logger.info(f"_truncate_at_next_table: cut at row {i} (Table {m.group(1)})")
            break
        if re.search(r'\bpage\s+\d+/\d+\b', text):
            cut_idx = i
            logger.info(f"_truncate_at_next_table: cut at row {i} (footer)")
            break
        if re.search(r'\bDS\s+\d+\s*-\s*Rev\b', text):
            cut_idx = i
            logger.info(f"_truncate_at_next_table: cut at row {i} (DS footer)")
            break
    return raw_table[:cut_idx]


def _merge_identical_adjacent_columns(
    headers: list[str],
    rows: list[list[str]],
) -> tuple[list[str], list[list[str]]]:
    """
    [Fix] Fusionne les colonnes adjacentes dont tous les headers ET toutes
    les cellules de données sont identiques. Supprime les colonnes en double
    créées par les en-têtes spanning (ex: 17 colonnes → 11).
    """
    if not headers or not rows:
        return headers, rows
    cols = len(headers)
    keep = [True] * cols
    for c in range(cols - 1, 0, -1):
        if c < len(headers) and headers[c] == headers[c-1]:
            same = True
            for r in range(len(rows)):
                v1 = rows[r][c] if c < len(rows[r]) else ""
                v2 = rows[r][c-1] if c-1 < len(rows[r]) else ""
                if v1 != v2:
                    same = False
                    break
            if same:
                keep[c] = False
                logger.info(f"_merge_identical_adjacent_columns: merged col {c} into {c-1}")
    new_headers = [h for i, h in enumerate(headers) if keep[i]]
    new_rows = [[cell for i, cell in enumerate(row) if keep[i]] for row in rows]
    new_cols = len(new_headers)
    if new_cols < cols:
        logger.info(f"_merge_identical_adjacent_columns: {cols} → {new_cols} cols")
    return new_headers, new_rows


def _merge_fragmented_columns(
    headers: list[str],
    rows: list[list[str]],
) -> tuple[list[str], list[list[str]], int]:
    """
    [Fix] Fusionne les colonnes fragmentées (pdfplumber_text) dans leur voisine
    de gauche. Détecte les fragments par 4 heuristiques complémentaires :
    1. Colonne vide/tres creuse (<=40% non-vide)
    2. Header commence en minuscule = fragment de mot (ex: "ax" dans "M"/"ax")
    3. 1ere cellule donnee commence en minuscule = continuation de mot
       Guard : si le header droit est >=3 car. avec 1ère majuscule (ex: "Unit"),
       c'est une colonne réelle → ne pas fusionner.
    4. Cellules tres courtes (< 4 car.) — consecutif limite a 4
       Guard identique à H3 pour protéger les colonnes réelles.

    Détection des header leaks : pendant la fusion des données, toute cellule
    identique à l'en-tête original de sa colonne est considérée comme un
    "header leak" (fragment d'en-tête qui a coulé dans les données) et n'est
    PAS fusionnée. Ex: colonne "s" (suffixe de "Conditions") avec données "s"
    dans toutes les lignes → ignorée.

    Espace intelligent : lowercase+lowercase → pas d'espace (fragments de mot,
    ex: "ris"+"ing" = "rising"). Sinon → espace (ex: "V DD"+"rising" = "V DD rising").

    IMPORTANT : cette fonction est uniquement appelée pour les tables
    pdfplumber_text (extraction sans bordures). Pour les tables pdfplumber
    (bordures), les colonnes sont déjà correctement détectées et il ne
    faut PAS les fusionner — sinon les tables mécaniques (7 colonnes
    Symbol/mm/inches) sont écrasées en 2 colonnes (H2 traite "millimeters"
    comme un fragment). Voir le garde-fou à la ligne 1084.
    Retourne (headers, rows, merged_count).
    """
    if not headers or not rows:
        return headers, rows, 0
    cols = len(headers)
    keep = [True] * cols
    # Sauvegarde des headers originaux pour la détection des header leaks
    # pendant la fusion. Sans cette copie, les fusions successives (droite→
    # gauche) modifient headers[] et faussent la comparaison donnée==header.
    orig_headers = list(headers)
    consec_merged = 0
    for c in range(cols - 1, 0, -1):
        if not keep[c]:
            consec_merged += 1
            continue
        if consec_merged >= 4:
            # Si la colonne courante est un fragment clair (header minuscule),
            # on permet la fusion même après 4 fusions consécutives de colonnes
            # vides. Sans ce passe-droit, "itionsMinTypMax" (col 3, header 'i'
            # minuscule) serait sauté après les 4 colonnes vides 7,6,5,4.
            if c < len(headers) and headers[c] and headers[c].strip() and headers[c].strip()[0].islower():
                pass
            else:
                consec_merged = 0
                continue
        # Compter les valeurs non-vides dans cette colonne
        n_non_empty = 0
        first_non_empty = None
        max_len = 0
        for r in rows:
            if c < len(r) and r[c] and str(r[c]).strip():
                v = str(r[c]).strip()
                n_non_empty += 1
                if first_non_empty is None:
                    first_non_empty = v
                max_len = max(max_len, len(v))
        total = len(rows)
        ratio = n_non_empty / total if total > 0 else 0
        # Heuristique 1 : colonne vide/creuse
        if ratio < 0.4:
            merge = True
        # Heuristique 2 : header commence en minuscule = fragment de mot
        # (ex: header "ax" dans "M"/"ax" → fusionner avec "M" → "Max")
        elif c < len(headers) and headers[c] and headers[c].strip() and headers[c].strip()[0].islower():
            merge = True
        # Heuristique 3 : donnee commence en minuscule = continuation de mot
        # Ex: 1ère cellule "v" (continuation de "~10 V") → fusionner
        # Protection : si le header droit est ≥2 car. avec majuscule, c'est
        # une colonne réelle (ex: "Unit", "Un") → NE PAS fusionner. Sans ce
        # garde-fou, "Un" (fragment de "Unit") serait collé à "Max" → "MaxUn".
        elif first_non_empty and first_non_empty[0].islower():
            if c < len(headers) and headers[c] and headers[c].strip():
                if len(headers[c].strip()) >= 2 and headers[c].strip()[0].isupper():
                    merge = False
                else:
                    merge = True
            else:
                merge = True
        # Heuristique 4 : cellules tres courtes (< 4 car.)
        # Ex: données "mA", "V" — trop courtes pour être une colonne réelle
        # Même garde-fou que H3 : header "Unit", "Un" protégé (≥2 car., majuscule)
        elif max_len < 4:
            if c < len(headers) and headers[c] and headers[c].strip():
                if len(headers[c].strip()) >= 2 and headers[c].strip()[0].isupper():
                    merge = False
                else:
                    merge = True
            else:
                merge = True
        else:
            merge = False
        if merge:
            left_h = headers[c-1] if c-1 < len(headers) else ""
            right_h = headers[c] if c < len(headers) else ""
            # Utilise orig_headers pour la détection header leak, car headers
            # est modifié par les fusions des colonnes de droite déjà traitées.
            # Ex: col 5 "s" + col 6 "Min" → headers[5]="sMin", mais le header
            # original de la col 5 était "s" → droit_h_str="s" pour la détection.
            left_h_str = (orig_headers[c-1] or "").strip() if c-1 < len(orig_headers) else ""
            right_h_str = (orig_headers[c] or "").strip() if c < len(orig_headers) else ""
            if right_h and right_h.strip():
                if left_h and left_h.strip():
                    headers[c-1] = left_h + right_h
                else:
                    headers[c-1] = right_h
            # Fusion des données AVEC détection des header leaks
            # Header leak = donnée identique à l'en-tête de sa colonne
            # (ex: colonne "s" contient "s" dans toutes les lignes = pas une vraie donnée)
            # Quand un header leak est détecté, la cellule est ignorée lors de la fusion.
            # ESPACE INTELLIGENT : lowercase+lowercase → pas d'espace (fragment de mot),
            # sinon → espace (mots séparés, ex: "V DD"+"rising" → "V DD rising")
            for r in range(len(rows)):
                if c < len(rows[r]):
                    left_cell = rows[r][c-1] if c-1 < len(rows[r]) else ""
                    right_cell = rows[r][c] if c < len(rows[r]) else ""
                    if right_cell and right_cell.strip():
                        is_right_leak = right_cell.strip() == right_h_str
                        is_left_leak = left_cell.strip() == left_h_str
                        if not is_right_leak:
                            if left_cell and left_cell.strip() and not is_left_leak and left_cell != right_cell:
                                if left_cell[-1].islower() and right_cell[0].islower():
                                    rows[r][c-1] = left_cell + right_cell
                                else:
                                    rows[r][c-1] = left_cell + " " + right_cell
                            else:
                                rows[r][c-1] = right_cell
            keep[c] = False
            consec_merged += 1
            logger.info(f"_merge_fragmented_columns: merged col {c} into {c-1} (r={ratio:.2f}, first='{first_non_empty}')")
        else:
            consec_merged = 0
    merged = sum(1 for k in keep if not k)
    new_headers = [h for i, h in enumerate(headers) if keep[i]]
    new_rows = [[cell for i, cell in enumerate(row) if keep[i]] for row in rows]
    new_cols = len(new_headers)
    if new_cols < cols:
        logger.info(f"_merge_fragmented_columns: {cols} → {new_cols} cols")
    return new_headers, new_rows, merged


def _filter_narrow_tables(
    tables: list, finder_tables: list
) -> tuple[list, list]:
    """Filtre les tables trop étroites (< MIN_TABLE_WIDTH) = bandeaux décoratifs."""
    if not tables or not finder_tables:
        return tables or [], finder_tables or []
    filtered = [
        (t, ft) for t, ft in zip(tables, finder_tables)
        if ft.bbox[2] - ft.bbox[0] >= MIN_TABLE_WIDTH
    ]
    if not filtered:
        return tables, finder_tables
    return [t for t, ft in filtered], [ft for t, ft in filtered]


def _has_keyword(raw_table: list[list], keyword: str) -> bool:
    """Vérifie si un mot-clé apparaît dans les cellules d'une table extraite."""
    if not keyword or not raw_table:
        return False
    kw = keyword.lower()
    return any(
        kw in str(c).lower()
        for row in raw_table for c in row if c is not None
    )


def _extract_from_page(
    page: Page,
    ref: TableRef,
    rotated_map: dict,
    pdf_type: int = 1,
    caption_keyword: str = "",
) -> tuple[Optional[list], Optional[Any], str, Optional[tuple]]:
    """
    Extrait la grille brute depuis une page via pdfplumber.

    Stratégie d'extraction (3 essais) :
      1. "lines" (bordures réelles) → méthode pdfplumber
         - Filtrage Type 2 : rejet des bandeaux < MIN_TABLE_WIDTH
         - Sélection via _pick_best_table (proximité sous légende)
         - Correction Fix 1 (texte rotatif)
      2. "text" (fallback interne) → méthode pdfplumber_text
         - Même sélection + filtrage
         - Utilisé quand les bordures ne sont pas détectées
      3. Sans finder (PDF sans bordures) → bbox = page entière
         - Dernier recours pour les tables sans structure visible

    caption_keyword : si fourni, la stratégie lines n'est acceptée que si
                      ce mot-clé apparaît dans les cellules extraites
                      (évite de capturer la mauvaise table).

    Retourne (raw_table, table_obj, method_name, bbox) ou (None, None, ..., None) si échec.
    """
    settings = PDFPLUMBER_TABLE_SETTINGS_TYPE2 if pdf_type == 2 else PDFPLUMBER_TABLE_SETTINGS
    fallback = PDFPLUMBER_TABLE_SETTINGS_FALLBACK_TYPE2 if pdf_type == 2 else PDFPLUMBER_TABLE_SETTINGS_FALLBACK

    # Essai 1 : stratégie lignes
    tables = page.extract_tables(settings)
    finder = page.debug_tablefinder(settings)
    best1 = best_ft1 = bbox1 = None
    if tables and finder.tables:
        if pdf_type == 2:
            tables, finder.tables = _filter_narrow_tables(tables, finder.tables)
        best1, best_ft1, bbox1 = _pick_best_table(page, tables, finder.tables, ref.caption)
        if best1 is not None and _is_image_table(best1):
            best1 = best_ft1 = bbox1 = None
        if best1 is not None:
            best1 = _apply_rotated_fix(page, best1, rotated_map, finder.tables)
            best1 = _detect_vector_dashes(best1, best_ft1, page)

    q1 = _table_quality(best1) if best1 else -1.0
    if q1 >= 2.0:
        if not caption_keyword or _has_keyword(best1, caption_keyword):
            return best1, best_ft1, "pdfplumber", bbox1

    # Essai 2 : stratégie texte
    tables_text = page.extract_tables(fallback)
    finder_text = page.debug_tablefinder(fallback)
    best2 = best_ft2 = bbox2 = None
    if tables_text and finder_text.tables:
        if pdf_type == 2:
            tables_text, finder_text.tables = _filter_narrow_tables(tables_text, finder_text.tables)
        best2, best_ft2, bbox2 = _pick_best_table(page, tables_text, finder_text.tables, ref.caption)
        if best2 is not None and _is_image_table(best2):
            best2 = best_ft2 = bbox2 = None
        if best2 is not None:
            best2 = _apply_rotated_fix(page, best2, rotated_map, finder_text.tables)
            best2 = _detect_vector_dashes(best2, best_ft2, page)

            # Le filtrage des lignes au-dessus de la légende est fait
            # dans extract_table_grid (après _extract_from_page) pour que
            # la continuation multi-pages fonctionne correctement.

    q2 = _table_quality(best2) if best2 else -1.0

    def _kw_ok(raw):
        return not caption_keyword or _has_keyword(raw, caption_keyword)

    if best2 is not None and q2 >= q1 and _kw_ok(best2):
        return best2, best_ft2, "pdfplumber_text", bbox2
    if best1 is not None and _kw_ok(best1):
        return best1, best_ft1, "pdfplumber", bbox1
    if best2 is not None and _kw_ok(best2):
        return best2, best_ft2, "pdfplumber_text", bbox2

    # Essai 3 : tables sans finder (PDF sans bordures nettes → bbox=page entière)
    if tables:
        ft_dummy = [type("T", (), {"bbox": (0, 0, page.width, page.height)})()]
        best, best_ft, _ = _pick_best_table(page, tables, ft_dummy, ref.caption)
        if best is not None and (_is_image_table(best) or not _kw_ok(best)):
            best = None
        if best is not None:
            return best, best_ft, "pdfplumber", None

    return None, None, "pdfplumber", None


def _apply_rotated_fix(
    page: Page,
    raw_table: list,
    rotated_map: dict,
    finder_tables: list,
) -> list:
    """
    [Fix 1] Pour chaque cellule, si elle est dans une zone de texte rotatif,
    remplacer son texte par la version correctement ordonnée.

    On utilise les bboxes de cellule du finder pour la localisation.
    """
    if not rotated_map:
        return raw_table

    # Récupérer les cellules du finder pour avoir les bboxes
    try:
        finder_cells = {}
        for ft in finder_tables:
            for cell in ft.cells:
                # cell = (x0, top, x1, bottom) dans pdfplumber
                finder_cells[(round(cell[0]), round(cell[1]))] = cell
    except Exception:
        return raw_table

    # Appliquer la correction
    fixed_table = []
    for row_idx, row in enumerate(raw_table):
        fixed_row = []
        for col_idx, cell in enumerate(row):
            if cell and isinstance(cell, str):
                cell_clean = re.sub(r'[^a-zA-Z0-9]', '', cell[::-1])
                
                for (rx0, ry0, rx1, ry1), corrected in rotated_map.items():
                    corrected_clean = re.sub(r'[^a-zA-Z0-9]', '', corrected)
                    if corrected_clean == cell_clean or corrected_clean.startswith(cell_clean):
                        cell = corrected
                        break
            fixed_row.append(cell)
        fixed_table.append(fixed_row)
    
    return fixed_table


def _remove_trailing_footnotes(rows: list[list]) -> tuple[list[list], int]:
    """
    Supprime les lignes de footnotes en fin de tableau.
    Heuristique : lignes trailing consécutives dont la 1ère cellule = N.
    (ex: "1.", "2.", "6.") — motif typique des notes de bas de datasheet.
    Ne supprime qu'un bloc contigu à la fin, minimum 2 lignes pour éviter
    de toucher aux lignes de données légitimes (ex: "1." isolé = donnée).
    """
    if not rows:
        return rows, 0
    remove = 0
    for i in range(len(rows) - 1, -1, -1):
        first = str(rows[i][0]).strip() if rows[i] else ""
        if re.match(r'^\d+\.', first):  # fix: sans $ pour matcher "1.The pull-up" et "6.3.16"
            remove += 1
        else:
            break
    if remove < 2:
        return rows, 0
    cleaned = rows[:-remove]
    logger.info(f"_remove_trailing_footnotes: removed {remove} trailing footnote rows")
    return cleaned, remove


def _merge_empty_header_rightward(
    headers: list[str],
    rows: list[list[str]],
) -> tuple[list[str], list[list[str]], int]:
    """
    Fusionne les colonnes à header vide vers la colonne réelle de droite.
    Les colonnes à header vide entre deux colonnes réelles contiennent
    des fragments de texte (ex: "NRST i" dans l'espace Symbol↔Parameter).
    On les fusionne vers la droite pour reconstituer le texte complet.
    Processed right-to-left pour préserver les indices.
    """
    if not headers or not rows:
        return headers, rows, 0
    cols = len(headers)
    keep = [True] * cols
    merged_count = 0

    for c in range(cols - 1, -1, -1):
        if not keep[c]:
            continue
        h_c = headers[c].strip() if c < len(headers) else ""
        if h_c:
            continue
        # Trouver le voisin réel à droite
        right_real = None
        for t in range(c + 1, cols):
            if keep[t] and t < len(headers) and headers[t] and headers[t].strip():
                right_real = t
                break
        if right_real is None:
            continue
        # Fusionner : prepend les données de c dans right_real
        for ri in range(len(rows)):
            if c < len(rows[ri]) and rows[ri][c] and str(rows[ri][c]).strip():
                val = str(rows[ri][c]).strip()
                if right_real < len(rows[ri]):
                    target = rows[ri][right_real] or ""
                    if val != target:  # fix: evite duplication (ex: "Symbol"+"Symbol")
                        if target:
                            # Ajouter un espace si la jointure n'est pas un
                            # fragment de mot. Règle : si val finit par une
                            # lettre minuscule ET target commence par une
                            # lettre minuscule, c'est un fragment de mot
                            # (ex: "res"+"et"→"reset") → pas d'espace.
                            if val[-1].isalpha() and target[0].isalpha() and val[-1].islower() and target[0].islower():
                                rows[ri][right_real] = val + target
                            else:
                                rows[ri][right_real] = val + " " + target
                        else:
                            rows[ri][right_real] = val
        keep[c] = False
        merged_count += 1
        logger.info(
            f"_merge_empty_header_rightward: col {c} (h='') → col {right_real} "
            f"(h='{headers[right_real]}') [{merged_count}]"
        )

    new_headers = [h for i, h in enumerate(headers) if keep[i]]
    new_rows = [[cell for i, cell in enumerate(row) if keep[i]] for row in rows]
    if merged_count > 0:
        logger.info(
            f"_merge_empty_header_rightward: {cols} → {len(new_headers)} cols "
            f"({merged_count} merged)"
        )
    return new_headers, new_rows, merged_count
