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
from core.toc_detector import TableRef
from core.glyph_fixer import CID_PATTERN, FOOTER_PATTERN, fix_headers, fix_rows
from core.quality_flags import evaluate_table
from core.continuation import find_continuations, _get_col_x0s

logger = logging.getLogger(__name__)


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

def _same_line(a: dict, b: dict, tol: int = 3) -> bool:
    """Deux mots sont sur la même ligne si leur top diffère de moins de tol px."""
    return abs(a["top"] - b["top"]) < tol


def _find_caption_y(page: Page, caption: str) -> Optional[float]:
    """
    Cherche la position y (bord bas) de la légende sur la page.

    Méthode 1 : mot-à-mot strict — ne match UNIQUEMENT la vraie légende
    (pas une mention « Refer to Table … ») grâce à deux vérifications
    100 % déterministes :

      1. Aucun mot significatif avant « table » sur la même ligne →
         une légende commence une ligne, une référence est encastrée.
      2. Le numéro de table doit être suivi d'un point « 1. » →
         une référence utilise « 1: » ou « 1 » nu.

    Méthode 2 (fallback) : sous-chaîne normalisée compacte typographique,
    gère les mots scindés par le PDF ("T able" → "Table", "(1)(2)" en suffixe).
    """
    caption_words = caption.lower().split()[:5]
    if not caption_words:
        return None

    words = page.extract_words()
    if not words:
        return None

    # ── Méthode 1 : mot-à-mot strict + vérifications ────────────────
    for idx, word in enumerate(words):
        word_clean = word["text"].lower().rstrip(".,:;!?")
        caption_word_clean = caption_words[0].rstrip(".,:;!?")
        if word_clean != caption_word_clean:
            continue

        # VÉRIF 1 : pas de mot significatif avant « table » sur la même ligne
        if idx > 0 and _same_line(words[idx - 1], word):
            prev = words[idx - 1]["text"].lower().rstrip(".,:;!?").strip()
            if prev and not prev.isdigit():
                continue

        # VÉRIF 2 : le numéro doit être suivi d'un point « 1. » (pas « 1: »)
        if idx + 1 < len(words):
            num_text = words[idx + 1]["text"]
            if not re.match(r'^\d+[.)]$', num_text):
                continue

        match_count = 1
        for k in range(1, len(caption_words)):
            if idx + k < len(words):
                w_clean = words[idx + k]["text"].lower().rstrip(".,:;!?")
                c_clean = caption_words[k].rstrip(".,:;!?")
                if w_clean == c_clean:
                    match_count += 1
                else:
                    break
        if match_count >= min(3, len(caption_words)):
            return word["bottom"]

    # ── Méthode 2 : normalisé compact (tolérant aux scissions) ────────
    # Construire la forme compacte du caption (uniquement lettres+chiffres)
    cap_compact = "".join(re.findall(r"[a-z0-9]+", "".join(caption_words)))

    # Construire le texte compact de la page avec positions y
    text_compact = ""
    word_y_map = []  # (start_char, end_char, y_bottom)
    char_pos = 0
    for w in words:
        wt = "".join(re.findall(r"[a-z0-9]+", w["text"].lower()))
        if wt:
            text_compact += wt
            word_y_map.append((char_pos, char_pos + len(wt), w["bottom"]))
            char_pos += len(wt)

    match_pos = text_compact.find(cap_compact)
    if match_pos >= 0:
        for ws, we, wy in word_y_map:
            if ws <= match_pos < we:
                return wy

    return None


def _locate_caption_page(
    pdf: "pdfplumber.PDF",
    caption: str,
    declared_page: int,
    window: int = 2,
    pdf_type: int = 1,
) -> Optional[int]:
    """
    Scanne `declared_page ± window` pour le caption TOC.
    La page déclarée est testée en premier, puis on s'éloigne.

    Retourne le numéro de page (1-indexé) où la légende ET un tableau
    réel sont trouvés, ou None.
    """
    tbl_settings = PDFPLUMBER_TABLE_SETTINGS_TYPE2 if pdf_type == 2 else PDFPLUMBER_TABLE_SETTINGS

    # Ordre : déclarée d'abord, puis on s'éloigne
    order = [declared_page]
    for d in range(1, window + 1):
        p_lo = declared_page - d
        p_hi = declared_page + d
        if 1 <= p_lo <= len(pdf.pages):
            order.append(p_lo)
        if 1 <= p_hi <= len(pdf.pages):
            order.append(p_hi)
    order = list(dict.fromkeys(order))  # dédoublonne en gardant l'ordre

    best = None
    for pno in order:
        cap_y = _find_caption_y(pdf.pages[pno - 1], caption)
        if cap_y is None:
            continue
        # VÉRIF : la page contient-elle un tableau réel sous la légende ?
        tables = pdf.pages[pno - 1].find_tables(tbl_settings)
        has_grid = any(t.bbox[1] > cap_y - 20 for t in tables)
        if has_grid:
            return pno  # candidat parfait : légende + grille
        if best is None:
            best = pno  # garder le premier qui a au moins la légende
    return best


def _table_quality(table: list) -> float:
    """
    Score de 'qualité' d'un tableau réel (vs fragment d'image CID).
    Privilégie : beaucoup de cellules réelles (non-CID), pas d'image fragmentée.
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
    return n_real if n_real > 0 else -1.0


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
        # Garder seulement les tables sous la légende
        below = [(t, ft) for t, ft in candidates
                 if ft.bbox[1] > caption_y - 20]  # -20 px de tolérance
        if below:
            # La plus proche (bord haut minimal)
            best_t, best_ft = min(below, key=lambda x: x[1].bbox[1])
            return best_t, best_ft, best_ft.bbox
        # Si aucune n'est sous la légende, prendre la première quand même
        best_t, best_ft = candidates[0]
        return best_t, best_ft, best_ft.bbox

    # Fallback : la plus haute sur la page, mais éviter les fragments d'image
    # (dessins mécaniques fragmentés en mini-tables par pdfplumber, ex: F4 table_84)
    best_t, best_ft = min(candidates, key=lambda x: x[1].bbox[1])
    if _table_quality(best_t) < 0.5:
        best_alt = max(candidates, key=lambda x: _table_quality(x[0]))
        if _table_quality(best_alt[0]) > _table_quality(best_t):
            best_t, best_ft = best_alt
    return best_t, best_ft, best_ft.bbox


# ══════════════════════════════════════════════════════════════════════════════
# Extraction principale
# ══════════════════════════════════════════════════════════════════════════════

def _cell_str(cell) -> str:
    """Convertit une cellule pdfplumber (str ou None) en string propre."""
    if cell is None:
        return ""
    # Fix 4 : normaliser les \n internes dans les cellules de data
    return _normalize_newlines_in_cell(str(cell))


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
) -> None:
    """Sauvegarde un crop de la zone de la table pour debug visuel."""
    try:
        if SAVE_IMAGES_ONLY_ON_ISSUE and confidence == "high":
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
    """
    if not hasattr(page, 'rects') or not hasattr(table_obj, 'rows') or not table_obj.rows:
        return 0

    table_bbox = table_obj.bbox
    if not table_bbox:
        return 0

    # Collecter les rectangles remplis en bleu foncé dans la zone table
    header_y_bottoms = set()
    for r in page.rects:
        if (r['x0'] >= table_bbox[0] - 5 and r['x1'] <= table_bbox[2] + 5
                and r['top'] >= table_bbox[1] - 5 and r['bottom'] <= table_bbox[3] + 5):
            fill = r.get('non_stroking_color')
            if fill and len(fill) == 3:
                r_norm, g_norm, b_norm = fill
                # Bleu foncé : R et G faibles, B dominant
                if r_norm < 0.15 and g_norm < 0.25 and b_norm > 0.25:
                    # Arrondir à l'entier pour grouper les lignes proches
                    header_y_bottoms.add(round(r['bottom'], 0))

    if not header_y_bottoms:
        return 0

    # Trier les Y
    sorted_y = sorted(header_y_bottoms)

    # Filtrer : ne garder que les Y qui correspondent à une ligne réelle de table_obj.rows
    row_y_bottoms = set(round(row.bbox[3], 0) for row in table_obj.rows)
    matching = [y for y in sorted_y if y in row_y_bottoms]

    if not matching:
        return 0

    # Compter les lignes consécutives depuis le haut
    sorted_row_y = sorted(row_y_bottoms)
    first_header_y = matching[0]
    try:
        first_idx = sorted_row_y.index(first_header_y)
    except ValueError:
        return 1

    count = 0
    for i in range(first_idx, min(first_idx + 10, len(sorted_row_y))):
        if sorted_row_y[i] in matching:
            count += 1
        else:
            break

    return count


def _expand_spans_and_headers(
    raw_table: list[list],
    table_obj: Optional[Any] = None,
    page: Optional[Any] = None,
    pdf_type: int = 1,
) -> tuple[list[str], list[list[str]], list[str]]:
    """
    Propagation géométrique + détection de profondeur d'en-tête + compression.

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

    # ── Type 2 : détection couleur des lignes d'en-tête ────────────────────
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

    RÈGLE DE PROPAGATION : si la cellule parente (ligne 0) est vide/None pour
    une colonne donnée (cellule fusionnée horizontalement dans le PDF), on
    réutilise le dernier libellé parent connu.

    Ex. Table 2 :
      row0 = ["Peripheral", "", "STM32C031_", None, None, None, ...]
      row1 = ["", "",         "_F4",        "_F6", "_G4", "_G6", ...]
    → headers = ["Peripheral", "", "STM32C031_ / _F4", "STM32C031_ / _F6", ...]
    """
    if header_depth == 1:
        return [
            _normalize_newlines_in_cell(str(table[0][c] or "")).strip()
            for c in range(cols)
        ]

    final: list[str] = []
    last_parent: str = ""  # dernier label parent vu (propagation horizontale)

    for c in range(cols):
        parts: list[str] = []
        for r in range(header_depth):
            val = str(table[r][c] or "").strip()
            if "\n" in val:
                if pdf_type == 2:
                    val = val.replace("\n", " ").strip()
                else:
                    val = val.split("\n")[0].strip()
            val = _normalize_newlines_in_cell(val).strip()

            if r == 0:
                # Niveau parent : mettre à jour last_parent si non vide,
                # sinon réutiliser le dernier parent connu (cellule fusionnée).
                if val:
                    last_parent = val
                effective = last_parent
            else:
                effective = val

            # Ajouter uniquement si non vide et non doublon du niveau précédent
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
    """
    result = {
        "table_id":              ref.table_id,
        "caption":               ref.caption,
        "pdf_name":              pdf_name,
        "family":                family,
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

    effective_page = ref.page  # peut être mis à jour par le look-ahead

    with pdfplumber.open(pdf_path) as pdf:
        if ref.page < 1 or ref.page > len(pdf.pages):
            result["warnings"].append(f"page_out_of_range:{ref.page}")
            return result

        # ── Localisation guidée par le TOC : trouver la page réelle du caption ──
        # Le TOC peut être off-by-one (ex H7 : TOC dit page 194, caption p195).
        scan_page = _locate_caption_page(pdf, ref.caption, ref.page, pdf_type=pdf_type) or ref.page
        if scan_page != ref.page:
            effective_page = scan_page
            result["page"] = scan_page
            result["warnings"].append(f"caption_page:{scan_page}")
            logger.info(f"{ref.table_id}: caption found on page {scan_page} (TOC: {ref.page})")
        page = pdf.pages[effective_page - 1]

        # ── Fix 1 : préparer la map des textes rotatifs ────────────────────────
        rotated_map = _get_rotated_text_map(page)

        # ── Extraire la grille sur cette page ───────────────────────────────
        raw_table, table_obj, method, bbox = _extract_from_page(page, ref, rotated_map, pdf_type)

        # ── Détection de faux positif (footer/bandeau au lieu de la vraie table) ──
        if raw_table is not None and len(raw_table) <= 2:
            cell_text = " ".join(str(c) for row in raw_table for c in row if c)
            if FOOTER_PATTERN.search(cell_text):
                logger.info(f"{ref.table_id}: false positive (footer) on page {effective_page}, trying neighbors...")
                raw_table = None

        # ── Fix look-ahead : si rien sur la page trouvée, essayer page+1 puis page-1 ──
        if raw_table is None:
            for delta in (1, -1):
                cand_idx = (effective_page - 1) + delta
                if 0 <= cand_idx < len(pdf.pages):
                    cand_page = pdf.pages[cand_idx]
                    cand_rotated = _get_rotated_text_map(cand_page)
                    cand_table, cand_obj, cand_method, cand_bbox = _extract_from_page(
                        cand_page, ref, cand_rotated, pdf_type
                    )
                    if cand_table is not None:
                        page = cand_page
                        rotated_map = cand_rotated
                        raw_table, table_obj, method, bbox = cand_table, cand_obj, cand_method, cand_bbox
                        effective_page = cand_idx + 1
                        result["page"] = effective_page
                        result["warnings"].append(f"page_shifted:{effective_page}")
                        logger.info(f"{ref.table_id}: shifted from page {ref.page} to {effective_page}")
                        break

        if raw_table is None:
            result["warnings"].append("no_table_found_on_page")
            logger.warning(f"{ref.table_id}: no table found on page {ref.page} (or neighbors)")
            return result

        result["extraction_method"] = method

        if not raw_table:
            result["warnings"].append("empty_raw_table")
            return result

        # ── Fix 2 & 5 : Headers structurels et Propagation globale ─────────────
        headers, rows_raw, span_warnings = _expand_spans_and_headers(raw_table, table_obj, page, pdf_type)
        result["warnings"].extend(span_warnings)

        # ── Fix 4 : Continuation multi-pages ──────────────────────────────────
        merged_pages = [effective_page]
        if all_refs is not None:
            header_depth = len(raw_table) - len(rows_raw)
            first_cell_text = str(raw_table[0][0] or "").strip() if raw_table else ""
            c_pages, c_rows, target_cols, cont_x0s_list = find_continuations(
                pdf,
                effective_page,
                len(headers),
                all_refs,
                ref.table_id,
                header_depth,
                first_cell_text,
                pdf_type=pdf_type,
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
                    for pos in sorted(insert_positions, reverse=True):
                        header_val = headers[pos - 1] if pos > 0 else headers[0]
                        headers = headers[:pos] + [header_val] + headers[pos:]
                        for r in range(len(rows_raw)):
                            rows_raw[r] = rows_raw[r][:pos] + [""] + rows_raw[r][pos:]

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
        if rows_raw:
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

        # ── Détection glyphes CID non mappés (avant nettoyage graphe) ──
        _raw_hdr = " ".join(headers)
        _raw_rows = " ".join(str(c) for row in rows_raw for c in row if c)
        cid_detected = bool(CID_PATTERN.search(_raw_hdr) or CID_PATTERN.search(_raw_rows))

        # ── Correction des glyphes ─────────────────────────────────────────────
        headers    = fix_headers(headers)
        rows_fixed = fix_rows(rows_raw)

        # ── Évaluation qualité ─────────────────────────────────────────────────
        confidence, empty_ratio, _, warnings_eval = evaluate_table(headers, rows_fixed, cid_detected)
        result["warnings"].extend(warnings_eval)

        # ── Self-check : détection d'ambiguïté résiduelle ──────────────────
        # Si la table extraite est quasi-vide ou ne contient que des CID,
        # on marque un doute (sans écraser "failed" déjà posé par le guard).
        if empty_ratio >= 0.9 and result.get("status") is None:
            _raw = " ".join(headers) + " ".join(str(c) for row in rows_fixed for c in row)
            _cleaned = CID_PATTERN.sub("", _raw).strip()
            if not _cleaned:
                result["status"] = "review_needed"

        # ── Remplissage du résultat ────────────────────────────────────────────
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
            _save_debug_image(page, bbox, out_path, confidence)

    logger.info(
        f"{ref.table_id} | page={effective_page} | method={method} "
        f"| confidence={confidence} | rows={len(result['rows'])} "
        f"| empty={result['empty_cell_ratio']:.2f}"
    )
    return result


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


def _extract_from_page(
    page: Page,
    ref: TableRef,
    rotated_map: dict,
    pdf_type: int = 1,
) -> tuple[Optional[list], Optional[Any], str, Optional[tuple]]:
    """
    Tente d'extraire la grille depuis la page avec pdfplumber.
    Stratégie : "lines" (bordures réelles) → "text" (fallback interne).
    pdf_type=2 : applique les réglages Type 2 + filtre les bandeaux.

    IMPORTANT : ne retourne JAMAIS un résultat garbage de la stratégie lignes
    si la stratégie texte (tables sans bordures, très fréquentes chez STM32)
    trouve un meilleur résultat. Les deux stratégies sont comparées par qualité.
    Retourne (raw_table, table_obj, method_name, bbox) ou (None, None, ..., None) si échec.
    """
    settings = PDFPLUMBER_TABLE_SETTINGS_TYPE2 if pdf_type == 2 else PDFPLUMBER_TABLE_SETTINGS
    fallback = PDFPLUMBER_TABLE_SETTINGS_FALLBACK_TYPE2 if pdf_type == 2 else PDFPLUMBER_TABLE_SETTINGS_FALLBACK

    # ── Essai 1 : stratégie lignes ─────────────────────────────────────────────
    tables = page.extract_tables(settings)
    finder = page.debug_tablefinder(settings)
    best1 = best_ft1 = bbox1 = None
    if tables and finder.tables:
        if pdf_type == 2:
            tables, finder.tables = _filter_narrow_tables(tables, finder.tables)
        best1, best_ft1, bbox1 = _pick_best_table(page, tables, finder.tables, ref.caption)
        if best1 is not None:
            best1 = _apply_rotated_fix(page, best1, rotated_map, finder.tables)

    q1 = _table_quality(best1) if best1 else -1.0
    if q1 >= 2.0:
        # La stratégie lignes a trouvé une table réaliste → l'utiliser
        return best1, best_ft1, "pdfplumber", bbox1

    # ── Essai 2 : stratégie texte (gère les tables sans bordures visibles) ────
    tables_text = page.extract_tables(fallback)
    finder_text = page.debug_tablefinder(fallback)
    best2 = best_ft2 = bbox2 = None
    if tables_text and finder_text.tables:
        if pdf_type == 2:
            tables_text, finder_text.tables = _filter_narrow_tables(tables_text, finder_text.tables)
        best2, best_ft2, bbox2 = _pick_best_table(page, tables_text, finder_text.tables, ref.caption)
        if best2 is not None:
            best2 = _apply_rotated_fix(page, best2, rotated_map, finder_text.tables)

    q2 = _table_quality(best2) if best2 else -1.0

    # Retourner le meilleur des deux (tie-break : préférer lignes)
    if best2 is not None and q2 >= q1:
        return best2, best_ft2, "pdfplumber_text", bbox2
    if best1 is not None:
        return best1, best_ft1, "pdfplumber", bbox1
    if best2 is not None:
        return best2, best_ft2, "pdfplumber_text", bbox2

    # ── Essai 3 : tables sans finder (PDF sans bordures nettes) ──────────────
    if tables:
        ft_dummy = [type("T", (), {"bbox": (0, 0, page.width, page.height)})()]
        best, best_ft, _ = _pick_best_table(page, tables, ft_dummy, ref.caption)
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
                # We need to map this cell string to the rotated_map.
                # Since matching by exact reversed text didn't work robustly (spacing etc),
                # Let's just check if reversed text matches WITHOUT any spaces.
                reversed_text = cell[::-1].replace(" ", "").replace("\n", "")
                
                for (rx0, ry0, rx1, ry1), corrected in rotated_map.items():
                    if corrected.replace(" ", "") == reversed_text:
                        cell = corrected
                        break
            fixed_row.append(cell)
        fixed_table.append(fixed_row)

    return fixed_table
