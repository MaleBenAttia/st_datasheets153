"""
toc_detector.py — Étape 1 : détection des tables via "List of Tables" ou scan page par page.

Extrait : table_id, caption, page (1ère occurrence).
Stratégie : H2.0 (annotations PDF multi-pages) → H2.1-H2.4 (texte regex) → H2.5 (inline scan).
"""
from __future__ import annotations
import re
import logging
from dataclasses import dataclass
from typing import Optional

import pdfplumber
import pypdf

logger = logging.getLogger(__name__)

# ── Cache des sections par position Y ───────────────────────────────────────────
# _SECTION_CACHE[pdf_path][page_num] = [(y_top, "5.3.6 Supply current characteristics"), ...]
_SECTION_CACHE: dict[str, dict[int, list[tuple[float, str]]]] = {}


def get_section_at(pdf_path: str, page: int, y_top: float) -> str:
    """Retourne la section la plus proche au-dessus de y_top.

    Cherche d'abord sur la page courante (par Y-position).
    Si rien trouvé, remonte page par page vers l'arrière.
    """
    cache = _SECTION_CACHE.get(pdf_path, {})
    headings = cache.get(page, [])
    best = ""
    for hy, label in headings:
        if hy <= y_top:
            best = label
        else:
            break
    if best:
        return best
    # Cross-page backward walk
    for pgn in range(page - 1, 0, -1):
        prev = cache.get(pgn, [])
        if prev:
            return prev[-1][1]
    return ""

# ══════════════════════════════════════════════════════════════════════════════
# Cache des mappings ST→actual (évite de rescanner le PDF pour chaque entrée)
# ══════════════════════════════════════════════════════════════════════════════
_ST_MAPPING_CACHE: dict[int, dict[int, int]] = {}  # {total_st: {st_page: actual_page}}

def _st_to_actual_page(pdf: pdfplumber.PDF, st_page: int) -> int:
    """
    Convertit un numéro de page ST (imprimé dans le document) en index PDF réel.
    Assemble TOUS les patterns 'X/TOTAL' en un mapping unique.
    """
    global _ST_MAPPING_CACHE
    cache_id = id(pdf)

    if cache_id not in _ST_MAPPING_CACHE:
        _ST_MAPPING_CACHE.clear()
        counters: dict[str, int] = {}

        for pgnum, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            for m in re.finditer(r"(\d+)/(\d{3})\b", text):
                counters[m.group(2)] = counters.get(m.group(2), 0) + 1

        total_strs = [t for t, c in counters.items() if c >= 3]
        combined: dict[int, int] = {}

        for total_str in total_strs:
            for pgnum, page in enumerate(pdf.pages):
                text = page.extract_text() or ""
                footer = text[-300:] if len(text) > 300 else text
                m = re.search(r"(\d+)/" + total_str + r"\b", footer)
                if m:
                    st = int(m.group(1))
                    combined[st] = pgnum + 1  # le dernier total gagne en cas de conflit

        _ST_MAPPING_CACHE[cache_id] = combined

    mapping = _ST_MAPPING_CACHE[cache_id]
    if st_page in mapping:
        return mapping[st_page]

    # Fallback : estimer par offset
    if mapping:
        sorted_st = sorted(mapping.keys())
        last_st = sorted_st[-1]
        last_actual = mapping[last_st]
        offset = last_st - last_actual
        # Tenter d'extrapoler pour tout st_page > dernier_st connu
        if st_page > last_st:
            est = st_page - offset
            if 1 <= est <= len(pdf.pages):
                logger.debug(f"Estimated ST {st_page} -> actual {est} (offset={offset})")
                return est
        # ou si st_page est entre des pages du mapping mais pas dans le mapping
        # (gap dans le mapping) : interpolation linéaire
        for i in range(len(sorted_st) - 1):
            if sorted_st[i] < st_page < sorted_st[i + 1]:
                # Interpolation entre deux points connus
                st_a, st_b = sorted_st[i], sorted_st[i + 1]
                act_a, act_b = mapping[st_a], mapping[st_b]
                ratio = (st_page - st_a) / (st_b - st_a)
                est = round(act_a + ratio * (act_b - act_a))
                if 1 <= est <= len(pdf.pages):
                    logger.debug(f"Interpolated ST {st_page} -> actual {est}")
                    return est
                break

    logger.debug(f"ST {st_page} not in mapping, using as-is")
    return st_page


# ── Patterns de détection ──────────────────────────────────────────────────────

# Pattern "List of Tables" / "Table des matières des figures"
TOC_SECTION_PATTERNS = [
    re.compile(r"list\s+of\s+tables", re.IGNORECASE),
    re.compile(r"liste\s+des\s+tableaux", re.IGNORECASE),
]

# Pattern d'une entrée de table dans le TOC :
# "Table 12. I2C characteristics ....... 78"
# Gère les variations : points collés, espaces entre points, tirets
TOC_ENTRY_PATTERN = re.compile(
    r"(?:Table|Tableau)\s+(\d+)[.:]?\s+(.+?)\s+[.\-\s]{3,}\s*(\d+)\s*$",
    re.IGNORECASE,
)

# Pattern de légende inline sur page (si pas de TOC)
# "Table 12. I2C characteristics" ou "Table 12:" etc.
INLINE_CAPTION_PATTERN = re.compile(
    r"(?:Table|Tableau)\s+(\d+)[.:\s]+(.+)",
    re.IGNORECASE,
)

# Pattern de titre de section dans un datasheet STM32
# Ex: "5.3.6 Supply current characteristics"
# Rejette les entrées de TOC (contiennent "...." ou un numéro de page à la fin)
SECTION_HEADING_PATTERN = re.compile(
    r"^(\d+(?:\.\d+){0,3})\s+([A-Z][A-Za-z\s\-,/°µΩ()]+)$",
)


def _clean_caption(text: str) -> str:
    """Supprime les points de remplissage du TOC et le numéro de page en fin."""
    text = re.sub(r'(?:\s*\.\s*){2,}', ' ', text)
    text = re.sub(r'\s+\d{1,4}\s*$', '', text)
    return text.strip()


@dataclass
class TableRef:
    """Référence à une table détectée (sans son contenu)."""
    table_id: str          # "table_12"
    caption:  str          # légende complète
    page:     int          # page de début (1-indexé)
    dest_y: Optional[float] = None  # Y en pdfplumber coords (haut de la table sur page cible), depuis H2.0
    section: str = ""      # Titre de la section contenant la table (ex: "5.3.6 Supply current characteristics")


def detect_tables(pdf_path: str, pdf_type: int = 1) -> list[TableRef]:
    """
    Détecte toutes les tables d'un PDF via :
    1. H2.0 : annotations PDF (liens /Link → /GoTo) sur toutes les pages LOT
    2. H2.1-H2.4 : regex texte sur les pages LOT
    3. H2.5 : scan inline fallback page par page

    Retourne une liste de TableRef triée par page, avec le champ section renseigné.
    """
    with pdfplumber.open(pdf_path) as pdf:
        # Tentative 1 : H2.0 annotations PDF
        refs = _from_toc_links(pdf_path, pdf, pdf_type)
        if refs:
            logger.info("H2.0 (annotations): %d tables detected", len(refs))
            _assign_sections(pdf_path, pdf, refs)
            return refs

        # Tentative 2 : H2.1-H2.4 regex texte
        if pdf_type == 2:
            refs = _from_toc_reverse(pdf)
        else:
            refs = _from_toc(pdf)
        if refs:
            logger.info("H2.1-H2.4 (text regex): %d tables detected", len(refs))
            _assign_sections(pdf_path, pdf, refs)
            return refs

        # Tentative 3 : H2.5 inline scan
        logger.info("No TOC found, scanning pages for inline captions")
        refs = _from_inline_scan(pdf)
        logger.info("H2.5 (inline scan): %d tables detected", len(refs))
        _assign_sections(pdf_path, pdf, refs)
        return refs


def _assign_sections(pdf_path: str, pdf: pdfplumber.PDF, refs: list[TableRef]) -> None:
    """
    Construit le cache Y-position des sections ET assigne chaque table
    à sa section (fallback page-level).

    1. Ignore les pages du sommaire (List of Tables)
    2. Scanne les pages avec extract_text_lines() pour capturer
       les titres de section + leur position Y
    3. Stocke le tout dans _SECTION_CACHE[pdf_path] pour raffinement
       Y-position dans grid_extractor.py
    4. Assigne chaque table à la section active sur sa page (fallback)
    """
    if not refs:
        return

    TABLE_CONTENT_RE = re.compile(
        r'\b(Yes|No|N/A|NA|Enabled|Disabled)\b', re.IGNORECASE
    )

    # 1. Trouver les pages de TOC à ignorer
    lot_pages = set()
    for pg_idx in range(len(pdf.pages)):
        text = (pdf.pages[pg_idx].extract_text() or "")[:1500].lower()
        if any(p.search(text) for p in TOC_SECTION_PATTERNS):
            lot_pages.add(pg_idx + 1)
            for k in range(pg_idx + 1, min(pg_idx + 15, len(pdf.pages))):
                t = (pdf.pages[k].extract_text() or "")[:500].lower()
                if re.search(r"table\s+\d+\.", t) or any(p.search(t) for p in TOC_SECTION_PATTERNS):
                    lot_pages.add(k + 1)
                else:
                    break
            break

    # 2. Construire le cache Y-position
    y_cache: dict[int, list[tuple[float, str]]] = {}

    for pg_idx, page in enumerate(pdf.pages):
        pgnum = pg_idx + 1
        if pgnum in lot_pages:
            continue

        lines = page.extract_text_lines() or []
        for line_info in lines:
            text = line_info["text"].strip()
            top = line_info["top"]
            if "..." in text or ". ." in text:
                continue
            m = SECTION_HEADING_PATTERN.match(text)
            if m:
                sec_num = m.group(1)
                sec_title = m.group(2).strip().rstrip(".")
                sec_title = re.sub(r'\s{2,}', ' ', sec_title)
                if sec_title and not TABLE_CONTENT_RE.search(sec_title):
                    label = f"{sec_num} {sec_title}"
                    y_cache.setdefault(pgnum, []).append((top, label))

    _SECTION_CACHE[pdf_path] = y_cache

    # 3. Trier les headings par Y pour chaque page
    for page_headings in y_cache.values():
        page_headings.sort(key=lambda x: x[0])

    # 4. Fallback page-level : dernier heading avec page ≤ page de la table
    all_headings: list[tuple[str, int]] = []  # (label, page)

    for pg_idx, page in enumerate(pdf.pages):
        pgnum = pg_idx + 1
        if pgnum in lot_pages:
            continue

        text = page.extract_text() or ""
        for line in text.splitlines():
            line = line.strip()
            if "..." in line or ". ." in line:
                continue
            m = SECTION_HEADING_PATTERN.match(line)
            if m:
                sec_num = m.group(1)
                sec_title = m.group(2).strip().rstrip(".")
                sec_title = re.sub(r'\s{2,}', ' ', sec_title)
                if sec_title and not TABLE_CONTENT_RE.search(sec_title):
                    all_headings.append((f"{sec_num} {sec_title}", pgnum))

    # 5. Assigner chaque table
    for ref in refs:
        best = ""
        for label, s_page in all_headings:
            if s_page <= ref.page:
                best = label
            else:
                break
        if not best and all_headings:
            best = "General purpose / Overview"
        ref.section = best


def _find_lot_pages(pdf: pdfplumber.PDF, pdf_type: int = 1) -> list[int]:
    """
    Trouve TOUTES les pages de la section 'List of Tables'.
    
    Cherche dans la direction suggérée par pdf_type, puis inverse si rien trouvé.
    
    Retourne une liste d'indices 0-indexés.
    """
    total = len(pdf.pages)
    
    def _search_in_direction(start: int, direction: int) -> list[int]:
        """Cherche le header LOT à partir de 'start' puis suit les pages consécutives."""
        lot_start = None
        end = total if direction > 0 else -1
        for i in range(start, end, direction):
            text = (pdf.pages[i].extract_text() or "")[:1500].lower()
            for pat in TOC_SECTION_PATTERNS:
                if pat.search(text):
                    lot_start = i
                    break
            if lot_start is not None:
                break
        
        if lot_start is None:
            return []
        
        lot_pages = [lot_start]
        for i in range(lot_start + direction, max(-1, min(total, lot_start + direction * 11)), direction):
            text = (pdf.pages[i].extract_text() or "")
            text_lower = text[:1500].lower()
            
            has_header = any(p.search(text_lower) for p in TOC_SECTION_PATTERNS)
            if has_header:
                lot_pages.append(i)
                continue
            
            has_entries = bool(re.search(r'(?:Table|Tableau)\s+\d+\.', text))
            if has_entries:
                lot_pages.append(i)
            else:
                break
        
        return sorted(lot_pages)
    
    # Direction initiale basée sur pdf_type
    if pdf_type == 2:
        result = _search_in_direction(total - 1, -1)
    else:
        result = _search_in_direction(0, 1)
    
    if result:
        return result
    
    # Fallback : chercher dans l'autre sens
    if pdf_type == 2:
        return _search_in_direction(0, 1)
    else:
        return _search_in_direction(total - 1, -1)


def _from_toc_links(pdf_path: str, pdf: pdfplumber.PDF, pdf_type: int = 1) -> Optional[list[TableRef]]:
    """
    H2.0 : Extrait les tables depuis les annotations PDF (liens /Link → /GoTo).
    
    Stratégie :
    1. Trouve toutes les pages de la List of Tables
    2. Extrait les annotations /Link avec destination /GoTo
    3. Résout les destinations via pypdf (objid → page_index)
    4. Matche le texte sous chaque annotation via pdfplumber coords
    
    Retourne None si 0 table trouvée (pour fallback vers H2.1-H2.5).
    """
    lot_pages = _find_lot_pages(pdf, pdf_type)
    if not lot_pages:
        logger.debug("H2.0: no LOT pages found")
        return None
    
    try:
        reader = pypdf.PdfReader(pdf_path)
    except Exception as e:
        logger.warning("H2.0: cannot open PDF with pypdf: %s", e)
        return None
    
    idnum_to_page = {}
    for i, p in enumerate(reader.pages):
        ref = p.indirect_reference
        if ref:
            idnum_to_page[ref.idnum] = i
    
    pattern = re.compile(r'(?:Table|Tableau)\s+(\d+)[.:]\s+(.+)')
    new_by_id: dict[str, tuple[str, int, Optional[float]]] = {}
    
    for lot_idx in lot_pages:
        page = pdf.pages[lot_idx]
        try:
            annots = page.annots or []
        except Exception:
            annots = []
        try:
            lines = page.extract_text_lines()
        except Exception:
            continue
        
        for a in annots:
            d = a.get('data', {})
            action = d.get('A', {})
            dest = action.get('D', None)
            if not dest or not isinstance(dest, list):
                continue
            page_ref = dest[0]
            if not hasattr(page_ref, 'objid'):
                continue
            page_idx = idnum_to_page.get(page_ref.objid)
            if page_idx is None:
                continue
            
            # Extraire le Y de destination (top de la table sur la page cible)
            dest_y = None
            if len(dest) >= 4 and isinstance(dest[3], (int, float)):
                target_h = pdf.pages[page_idx].height
                dest_y = target_h - dest[3]  # PDF → pdfplumber
            
            y0, y1 = a['top'], a['bottom']
            matching = [l for l in lines if l['top'] >= y0 - 5 and l['bottom'] <= y1 + 5]
            text = ' '.join(l['text'] for l in matching)
            m = pattern.search(text)
            if m:
                tid = 'table_%s' % m.group(1)
                caption = _clean_caption(m.group(2))
                new_by_id[tid] = (caption, page_idx + 1, dest_y)
    
    if not new_by_id:
        logger.debug("H2.0: 0 tables from annotations, will fallback")
        return None
    
    refs = []
    for tid, (caption, page_num, dest_y) in sorted(new_by_id.items(), key=lambda x: x[1][1]):
        refs.append(TableRef(
            table_id=tid,
            caption=f"Table {tid.split('_')[1]}. {caption}",
            page=page_num,
            dest_y=dest_y,
        ))
    
    return refs


def _from_toc(pdf: pdfplumber.PDF, start_from: int = 1) -> list[TableRef]:
    """
    Cherche une section 'List of Tables' et en extrait toutes les entrées,
    y compris sur plusieurs pages consécutives.

    start_from : page de début (1-indexé). Pour Type 1 : 1, pour Type 2 : dernière page - 30.

    Stratégie :
    - Détecter l'entrée dans le TOC (header "List of tables")
    - Lire TOUTES les pages consécutives du TOC (le TOC peut s'étaler sur 2+ pages)
    - Sortir uniquement quand on rencontre une vraie nouvelle section de contenu
      (page dont le premier mot ≠ "List" et qui contient du texte de corps)
    - Ne jamais breaker au milieu d'une page de TOC
    """
    refs: list[TableRef] = []
    in_toc = False
    toc_page_count = 0
    MAX_TOC_PAGES = 10

    pending_num = None
    pending_caption = ""

    for page_num, page in enumerate(pdf.pages, start=1):
        # Skip pages before start_from
        if page_num < start_from:
            continue
        text = page.extract_text() or ""
        lines = text.splitlines()

        # Détecter si cette page contient le header TOC
        page_has_toc_header = any(
            p.search(line) for line in lines for p in TOC_SECTION_PATTERNS
        )

        if page_has_toc_header:
            in_toc = True
            toc_page_count += 1

        if not in_toc:
            # Si on était dans le TOC et qu'on arrive sur une page sans header TOC
            # → vérifier si c'est une continuation (des entrées Table N. existent)
            # ou si c'est vraiment la fin du TOC
            if refs and toc_page_count > 0:
                page_entries = [
                    TOC_ENTRY_PATTERN.match(line.strip())
                    for line in lines if line.strip()
                ]
                page_entries = [m for m in page_entries if m]
                if page_entries:
                    # Continuation implicite du TOC (même format, pas de header répété)
                    in_toc = True
                    toc_page_count += 1
                else:
                    # Vraie fin du TOC
                    break
            else:
                continue

        # Limite de sécurité
        if toc_page_count > MAX_TOC_PAGES:
            logger.warning(f"TOC exceeded {MAX_TOC_PAGES} pages, stopping")
            break

        # Lire toutes les entrées de cette page de TOC
        for line in lines:
            line_stripped = line.strip()
            if not line_stripped:
                continue

            # Ignorer les headers de page (ex: "STM32C011x4/x6 List of tables")
            if any(p.search(line_stripped) for p in TOC_SECTION_PATTERNS):
                continue

            # Si on attend la fin d'une entrée multi-lignes
            if pending_num:
                end_match = re.search(r"\s+[.\-\s]{3,}\s*(\d+)\s*$", line_stripped)
                if end_match:
                    # Ligne finale de l'entrée
                    toc_page = int(end_match.group(1))
                    caption_part = line_stripped[:end_match.start()].strip()
                    if caption_part:
                        pending_caption += " " + caption_part
                    
                    caption_clean = _clean_caption(pending_caption)
                    actual_page = _st_to_actual_page(pdf, toc_page)
                    existing_ids = {r.table_id for r in refs}
                    if f"table_{pending_num}" not in existing_ids:
                        refs.append(TableRef(
                            table_id=f"table_{pending_num}",
                            caption=f"Table {pending_num}. {caption_clean}",
                            page=actual_page,
                        ))
                    
                    pending_num = None
                    pending_caption = ""
                    continue
                else:
                    # Soit c'est une ligne intermédiaire de légende, soit le début d'une nouvelle table
                    # (auquel cas la précédente est abandonnée car mal formée)
                    start_match = re.match(r"^(?:Table|Tableau)\s+(\d+)[.:]?\s+(.*)$", line_stripped, re.IGNORECASE)
                    if start_match:
                        pending_num = start_match.group(1)
                        pending_caption = start_match.group(2).strip()
                    else:
                        pending_caption += " " + line_stripped
                    continue

            # Pas d'entrée en attente, on teste si c'est une ligne complète
            m = TOC_ENTRY_PATTERN.match(line_stripped)
            if m:
                num = m.group(1)
                caption_raw = m.group(2).strip()
                toc_page = int(m.group(3))
                actual_page = _st_to_actual_page(pdf, toc_page)
                existing_ids = {r.table_id for r in refs}
                if f"table_{num}" not in existing_ids:
                    caption_clean = _clean_caption(caption_raw)
                    refs.append(TableRef(
                        table_id=f"table_{num}",
                        caption=f"Table {num}. {caption_clean}",
                        page=actual_page,
                    ))
            else:
                # Peut-être le début d'une entrée multi-lignes ?
                start_match = re.match(r"^(?:Table|Tableau)\s+(\d+)[.:]?\s+(.*)$", line_stripped, re.IGNORECASE)
                if start_match:
                    pending_num = start_match.group(1)
                    pending_caption = start_match.group(2).strip()

    return refs


def _from_toc_reverse(pdf: pdfplumber.PDF) -> list[TableRef]:
    """
    Cherche la TOC en commençant par la fin du document (Type 2).
    Les PDFs Antenna House placent la 'List of Tables' dans les dernières pages.
    """
    total = len(pdf.pages)
    start_page = max(1, total - 30)

    refs = _from_toc(pdf, start_page)
    if refs:
        return refs

    # Fallback : chercher dans TOUT le document (au cas où la TOC serait ailleurs)
    return _from_toc(pdf)


def _from_inline_scan(pdf: pdfplumber.PDF) -> list[TableRef]:
    """Scan chaque page pour détecter des légendes de table inline."""
    refs: list[TableRef] = []
    seen_ids: set[str] = set()

    for page_num, page in enumerate(pdf.pages, start=1):
        text = page.extract_text() or ""
        for line in text.splitlines():
            m = INLINE_CAPTION_PATTERN.match(line.strip())
            if m:
                num = m.group(1)
                tid = f"table_{num}"
                if tid not in seen_ids:
                    caption_text = _clean_caption(m.group(2))
                    refs.append(TableRef(
                        table_id=tid,
                        caption=f"Table {num}. {caption_text}",
                        page=page_num,
                    ))
                    seen_ids.add(tid)

    return sorted(refs, key=lambda r: (r.page, r.table_id))
