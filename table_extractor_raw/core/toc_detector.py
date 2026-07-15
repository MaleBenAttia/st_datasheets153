"""
toc_detector.py — Étape 1 : détection des tables via "List of Tables" ou scan page par page.

Extrait uniquement : table_id, caption, page (première occurrence).
Ne touche pas au contenu des tables.
"""
from __future__ import annotations
import re
import logging
from dataclasses import dataclass, field

import pdfplumber

logger = logging.getLogger(__name__)

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


@dataclass
class TableRef:
    """Référence à une table détectée (sans son contenu)."""
    table_id: str          # "table_12"
    caption:  str          # légende complète
    page:     int          # page de début (1-indexé)
    section:  str = ""     # section parente si détectable


def detect_tables(pdf_path: str) -> list[TableRef]:
    """
    Détecte toutes les tables d'un PDF via :
    1. "List of Tables" si présente
    2. Fallback : scan page par page pour les légendes inline

    Retourne une liste de TableRef triée par page.
    """
    with pdfplumber.open(pdf_path) as pdf:
        # Tentative 1 : TOC
        refs = _from_toc(pdf)
        if refs:
            logger.info(f"TOC found: {len(refs)} tables detected via List of Tables")
            return refs

        # Fallback : scan inline
        logger.info("No TOC found, scanning pages for inline captions")
        refs = _from_inline_scan(pdf)
        logger.info(f"Inline scan: {len(refs)} tables detected")
        return refs


def _from_toc(pdf: pdfplumber.PDF) -> list[TableRef]:
    """
    Cherche une section 'List of Tables' et en extrait toutes les entrées,
    y compris sur plusieurs pages consécutives.

    Stratégie :
    - Détecter l'entrée dans le TOC (header "List of tables")
    - Lire TOUTES les pages consécutives du TOC (le TOC peut s'étaler sur 2+ pages)
    - Sortir uniquement quand on rencontre une vraie nouvelle section de contenu
      (page dont le premier mot ≠ "List" et qui contient du texte de corps)
    - Ne jamais breaker au milieu d'une page de TOC
    """
    refs: list[TableRef] = []
    in_toc = False
    toc_page_count = 0           # pages consécutives de TOC lues
    MAX_TOC_PAGES = 10           # sécurité anti-boucle
    
    pending_num = None
    pending_caption = ""

    # Patterns de lignes qui indiquent qu'on est sorti du TOC
    BODY_SECTION_PATTERN = re.compile(
        r"^(?:\d+\.?\s+)?[A-Z][A-Za-z\s]+$"  # ex: "Introduction", "1. Description"
    )

    for page_num, page in enumerate(pdf.pages, start=1):
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
                    
                    caption_clean = re.sub(r"[\s.]+$", "", pending_caption).strip()
                    existing_ids = {r.table_id for r in refs}
                    if f"table_{pending_num}" not in existing_ids:
                        refs.append(TableRef(
                            table_id=f"table_{pending_num}",
                            caption=f"Table {pending_num}. {caption_clean}",
                            page=toc_page,
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
                existing_ids = {r.table_id for r in refs}
                if f"table_{num}" not in existing_ids:
                    caption_clean = re.sub(r"[\s.]+$", "", caption_raw).strip()
                    refs.append(TableRef(
                        table_id=f"table_{num}",
                        caption=f"Table {num}. {caption_clean}",
                        page=toc_page,
                    ))
            else:
                # Peut-être le début d'une entrée multi-lignes ?
                start_match = re.match(r"^(?:Table|Tableau)\s+(\d+)[.:]?\s+(.*)$", line_stripped, re.IGNORECASE)
                if start_match:
                    pending_num = start_match.group(1)
                    pending_caption = start_match.group(2).strip()

    return refs


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
                    caption_text = m.group(2).strip()
                    # Nettoyer les points de suite résiduels
                    caption_text = re.sub(r"\s*\.{2,}\s*\d+\s*$", "", caption_text).strip()
                    refs.append(TableRef(
                        table_id=tid,
                        caption=f"Table {num}. {caption_text}",
                        page=page_num,
                    ))
                    seen_ids.add(tid)

    return sorted(refs, key=lambda r: (r.page, r.table_id))
