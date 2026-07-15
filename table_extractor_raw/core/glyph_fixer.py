"""
glyph_fixer.py — Correction des glyphes mal encodés dans les PDFs STM32.

Principe : table de correspondance pure, zéro logique conditionnelle par
type de table. S'applique uniformément sur tous les textes (headers + rows).

La table est construite empiriquement sur le corpus réel. Compléter au fur
et à mesure des cas observés.
"""
from __future__ import annotations
import re
import unicodedata

# ── Table de correspondance glyphe → Unicode ──────────────────────────────────
# Clés : caractères ou séquences qui apparaissent mal encodés dans les PDFs STM32
# Valeurs : leur équivalent Unicode correct
GLYPH_MAP: dict[str, str] = {
    # Unités courantes mal encodées
    "\uf06d":  "µ",   # mu (micro) - police Symbol/custom STM32
    "\uf0b5":  "µ",   # variante mu
    "\u00b5":  "µ",   # MICRO SIGN → GREEK SMALL LETTER MU (normalisation)
    "\uf057":  "Ω",   # Omega (ohm) - police custom
    "\uf0b0":  "°",   # degré
    "\u00b0":  "°",   # degré (déjà correct mais normalisé)
    "\uf0b2":  "²",   # exposant 2
    "\uf0b3":  "³",   # exposant 3
    "\uf032":  "²",   # variante exposant 2
    "\uf033":  "³",   # variante exposant 3
    # Tirets / espaces
    "\u2013":  "–",   # EN DASH (garder, juste normaliser)
    "\u2212":  "-",   # MINUS SIGN → tiret standard
    "\uf02d":  "-",   # tiret custom
    # Guillemets
    "\u201c":  '"',
    "\u201d":  '"',
    "\u2018":  "'",
    "\u2019":  "'",
    # Séquences multi-caractères fréquentes
    "Ω":       "Ω",   # déjà correct
    "µ":       "µ",   # déjà correct
    # Puces / flèches
    "\uf0d8":  "↑",
    "\uf0da":  "↓",
    "\uf0e0":  "→",
    # Exposants numériques inline (ex: "10-6" encodé en glyphes)
    # Traités séparément par _fix_superscripts()
}

# Séquences regex à corriger APRÈS la table de glyphes
_REGEX_FIXES: list[tuple[str, str]] = [
    # "V DD" → "VDD" (espaces parasites dans les noms de signaux)
    # Attention : trop agressif → désactivé par défaut, commenter pour activer
    # (r"\bV\s+([A-Z]{2,})\b", r"V\1"),
    # Espaces multiples → espace simple
    (r"  +", " "),
    # Retours à la ligne internes → espace
    (r"[\r\n]+", " "),
]


def fix_text(text: str) -> str:
    """
    Applique la correction de glyphes sur un texte brut.
    1. Remplacement glyphe par glyphe
    2. NFC normalization
    3. Corrections regex
    4. Strip
    """
    if not text:
        return text

    # Étape 1 : remplacement glyphe → Unicode
    for src, dst in GLYPH_MAP.items():
        text = text.replace(src, dst)

    # Étape 2 : normalisation Unicode NFC
    text = unicodedata.normalize("NFC", text)

    # Étape 3 : corrections regex
    for pattern, replacement in _REGEX_FIXES:
        text = re.sub(pattern, replacement, text)

    return text.strip()


def fix_headers(headers: list[str]) -> list[str]:
    """Applique fix_text sur chaque header."""
    return [fix_text(h) for h in headers]


def fix_rows(rows: list[list[str | None]]) -> list[list[str]]:
    """
    Applique fix_text sur chaque cellule.
    None → "" (cellule vide explicite, pas d'ambiguïté).
    """
    return [
        [fix_text(cell) if cell is not None else "" for cell in row]
        for row in rows
    ]
