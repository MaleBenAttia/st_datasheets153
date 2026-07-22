"""
page1_features.py — Extracts Features/Summary data from pages 1..N of STM32 datasheets.
Output: outJason/<family>/<pdf_name>/features.json

100% independent from the table extraction pipeline.
"""
from __future__ import annotations
import json
import logging
import re
from pathlib import Path
from typing import Literal, Optional
import pdfplumber
from pydantic import BaseModel, Field

logger = logging.getLogger("page1_features")

# ── Regex patterns ──────────────────────────────────────────────────────────

PACKAGE_RE = re.compile(
    r'(?P<name>(?:LQFP|UFQFPN|TSSOP|UFBGA|WLCSP|VFBGA|SO\d*N?|TFBGA|UQFN|LFBGA|QFN)\d*)'
    r'(?:\s*\(([^)]+)\))?',
    re.IGNORECASE
)
# Exclude false positives like SO7816 (from ISO7816 interface) — SO package ends with N or digit < 4 chars
_PACKAGE_SO_FP = re.compile(r'^SO\d{4,}$', re.IGNORECASE)
_DIMENSION_RE = re.compile(r'\((\d+\.?\d*\s*[×x]\s*\d+\.?\d*\s*mm)\)')

TYPE1_FOOTER_RE = re.compile(
    r'(\w+\s+\d{4})\s+(DS\d+)\s+Rev\s+(\d+)\s+\d+/\d+\s*$', re.MULTILINE
)
TYPE2_FOOTER_RE = re.compile(
    r'(DS\d+)\s*-\s*Rev\s+(\d+)\s*-\s*(\w+\s+\d{4})', re.MULTILINE
)

END_MARKERS = [
    re.compile(r'^Contents$', re.IGNORECASE),
    re.compile(r'^Table of Contents$', re.IGNORECASE),
    re.compile(r'^1\s+Introduction', re.IGNORECASE),
    re.compile(r'^1\s+About this document', re.IGNORECASE),
    re.compile(r'^1\.\s+\w'),
]

CORE_RE = re.compile(r'Cortex[®\s]*-([A-Z0-9+]+)')
FPU_RE = re.compile(r'(?:with\s+)?FPU|floating\s+point\s+unit', re.IGNORECASE)
FREQ_RE = re.compile(r'(?:frequency\s+up\s+to|up\s+to)\s+(\d+)\s*MHz', re.IGNORECASE)
FLASH_RE = re.compile(r'(\d+(?:\.\d+)?)\s*-?\s*(?P<unit>[KMk])bytes?\s.*?flash', re.IGNORECASE)
RAM_RE = re.compile(r'(\d+(?:\.\d+)?)\s*-?\s*(?P<unit>[KMk])bytes?\s.*?SRAM', re.IGNORECASE)
VOLTAGE_RE = re.compile(r'(\d+\.?\d*)\s*(?:V\s*)?(?:to|-)\s*(\d+\.?\d*)\s*V')
TEMP_RE = re.compile(
    r'(-?\d+)\s*°C?\s*to\s*(-?\d+)(?:\s*°C?)?(?:\s*/\s*(-?\d+)(?:\s*°C?)?)?(?:\s*/\s*(-?\d+)(?:\s*°C?)?)?(?:\s*/\s*(-?\d+)(?:\s*°C?)?)?',
    re.IGNORECASE
)
COREMARK_RE = re.compile(r'(\d+\.?\d*)\s*CoreMark', re.IGNORECASE)
DMA_RE = re.compile(r'(?:(\d+)\s*-?\s*(?:channel|channels)\s+(?:DMA|LPDMA)|(?:DMA|LPDMA).*?(\d+)\s*(?:channel|channels))', re.IGNORECASE)
PART_RE = re.compile(r'STM32[A-Z0-9]{6,}')
TIMER_LINE_RE = re.compile(r'(?:(\d+)\s*[x×]\s+\d+-bit\s+timers?|(?:Up\s+to\s+)?(\d+)\s*timers?\b)', re.IGNORECASE)
ADC_LINE_RE = re.compile(r'(?:\d+\s*-?\s*bits?.*ADC|\d+-bit.*ADC|ADC.*\d+\s*-?\s*bits?)', re.IGNORECASE)
COMM_INTF_RE = re.compile(
    r'(?:\d+\s*[x×]?\s*)?(?:Up\s+to\s+\d+\s*[x×]?\s*)?'
    r'(I2C|USART|UART|(?<!X)SPI(?!M)|FDCAN|CAN\b|USB|\bSAI\b|SDMMC|SDIO|Ethernet|ETH|I3C|LPUART)',
    re.IGNORECASE
)
SECURITY_KW_RE = re.compile(
    r'(SESIP|PSA\s+Level|secure\s+boot|tamper|HASH|RNG|TrustZone|OTP|antitamper|DPA)',
    re.IGNORECASE
)

MAX_SCAN_PAGES = 10


def _detect_pdf_type(pdf_path: str) -> int:
    """Détecte le type de PDF : 1 = Acrobat, 2 = Antenna House."""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            producer = (pdf.metadata or {}).get("Producer", "")
        return 2 if "antenna" in producer.lower() else 1
    except Exception:
        return 1


# ── Pydantic models ─────────────────────────────────────────────────────────

class ExtractionMeta(BaseModel):
    source_pages: list[int]
    extraction_method: str
    text_extraction_reliable: bool = True
    packages_source: Literal["text", "image_label", "uncertain"] = "text"
    confidence: Literal["high", "medium", "low"] = "high"
    missing_fields: list[str] = []
    low_confidence_fields: list[str] = []


class DeviceFeatures(BaseModel):
    pdf_name: str
    family: str
    pdf_type: int
    doc_ref: Optional[str] = None
    revision: Optional[str] = None
    date: Optional[str] = None
    title: Optional[str] = None
    core: Optional[str] = None
    fpu: bool = False
    max_frequency_mhz: Optional[int] = None
    flash_kb: Optional[int] = None
    ram_kb: Optional[int] = None
    voltage_min_v: Optional[float] = None
    voltage_max_v: Optional[float] = None
    operating_temp_c: list[str] = []
    coremark: Optional[float] = None
    packages: list[str] = []
    part_numbers: list[str] = []
    extraction_meta: ExtractionMeta


# ── Helpers ─────────────────────────────────────────────────────────────────

def _get_pdf_page_count(pdf_path: str) -> int:
    try:
        with pdfplumber.open(pdf_path) as pdf:
            return len(pdf.pages)
    except Exception:
        return 999


def _get_page_text(pdf_path: str, page: int) -> str:
    try:
        with pdfplumber.open(pdf_path) as pdf:
            if page < 1 or page > len(pdf.pages):
                return ""
            text = pdf.pages[page - 1].extract_text()
            return text or ""
    except Exception as e:
        logger.warning(f"pdfplumber page {page} failed: {e}")
        return ""


# ── Page range detection (100% dynamic, no type-based rules) ──────────────

def detect_features_page_range(pdf_path: str) -> list[int]:
    total = _get_pdf_page_count(pdf_path)
    for p in range(1, min(total, MAX_SCAN_PAGES) + 1):
        text = _get_page_text(pdf_path, p)
        if not text:
            continue
        # Scan all lines for end markers (header may precede Contents/Introduction)
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if any(m.match(stripped) for m in END_MARKERS):
                return list(range(1, p)) if p > 1 else [1]
    return [1]


def _extract_text_for_pages(pdf_path: str, pages: list[int]) -> str:
    if not pages:
        return ""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            parts = []
            for p in pages:
                if 1 <= p <= len(pdf.pages):
                    text = pdf.pages[p - 1].extract_text()
                    if text:
                        parts.append(text)
            return "\n\n".join(parts)
    except Exception as e:
        logger.warning(f"pdfplumber pages {pages} failed: {e}")
        return ""


# ── Parsers ─────────────────────────────────────────────────────────────────

def _parse_header_footer(text: str, pdf_type: int) -> dict:
    result = {"doc_ref": None, "revision": None, "date": None, "title": None}

    if pdf_type == 1:
        m = TYPE1_FOOTER_RE.search(text)
        if m:
            result["date"] = m.group(1).strip()
            result["doc_ref"] = m.group(2)
            result["revision"] = f"Rev {m.group(3)}"
    else:
        m = TYPE2_FOOTER_RE.search(text)
        if m:
            result["doc_ref"] = m.group(1)
            result["revision"] = f"Rev {m.group(2)}"
            result["date"] = m.group(3).strip()

    # Title: first substantial line not matching noise patterns
    for line in text.splitlines():
        line = line.strip()
        if len(line) < 10:
            continue
        if any(x in line for x in ("Datasheet - production", "Datasheet\n", "DS", "Rev", "page", "Features", "/")):
            continue
        if line.startswith(("•", "-", "–", "Table", "Product", "1", "2", "3")):
            continue
        result["title"] = line
        break

    return result


def _parse_packages(text: str) -> list[str]:
    seen = set()
    packages = []
    pkg_list = []
    for m in PACKAGE_RE.finditer(text):
        name = m.group("name")
        if not name or len(name) < 4:
            continue
        if _PACKAGE_SO_FP.match(name):
            continue
        key = name.upper()
        if key in seen:
            continue
        seen.add(key)
        pkg_list.append((m.start(), name))

    dims_list = [(m.start(), m.group(1)) for m in _DIMENSION_RE.finditer(text)]

    for i, (pos, name) in enumerate(pkg_list):
        dims = None
        if i < len(dims_list):
            dims = dims_list[i][1]
        entry = f"{name} ({dims})" if dims else name
        packages.append(entry)
    return packages


def _parse_part_numbers(text: str) -> list[str]:
    seen = set()
    parts = []
    for m in PART_RE.finditer(text):
        pn = m.group(0)
        if "x" in pn.lower() or len(pn) < 9:
            continue
        if pn not in seen:
            seen.add(pn)
            parts.append(pn)
    return sorted(parts)


def _parse_features_bullets(text: str) -> dict:
    result = {
        "core": None, "fpu": False,
        "max_frequency_mhz": None, "flash_kb": None, "ram_kb": None,
        "voltage_min_v": None, "voltage_max_v": None,
        "operating_temp_c": [], "coremark": None,
    }

    m = CORE_RE.search(text)
    if m:
        result["core"] = f"Cortex-{m.group(1)}"

    if FPU_RE.search(text):
        result["fpu"] = True

    m = FREQ_RE.search(text)
    if m:
        result["max_frequency_mhz"] = int(m.group(1))

    flash_matches = list(FLASH_RE.finditer(text))
    if flash_matches:
        def _flash_val(m):
            v = float(m.group(1))
            u = m.group("unit") or "K"
            return int(round(v * 1024)) if u.upper() == "M" else int(v)
        best = max(flash_matches, key=_flash_val)
        result["flash_kb"] = _flash_val(best)

    m = RAM_RE.search(text)
    if m:
        val = float(m.group(1))
        unit = m.group("unit") or "K"
        result["ram_kb"] = int(round(val * 1024)) if unit.upper() == "M" else int(val)

    m = VOLTAGE_RE.search(text)
    if m:
        result["voltage_min_v"] = float(m.group(1))
        result["voltage_max_v"] = float(m.group(2))

    for m in TEMP_RE.finditer(text):
        groups = [m.group(i) for i in range(1, 6) if m.group(i)]
        if len(groups) >= 2:
            low = groups[0]
            temps = [f"{low}°C to {t}°C" for t in groups[1:]]
        else:
            temps = []
        result["operating_temp_c"] = temps
        break

    return result


# ── Main entry point ────────────────────────────────────────────────────────

def extract_features_page_range(pdf_path: str) -> dict:
    p = Path(pdf_path)
    pdf_name = p.stem
    family = p.parent.name
    pdf_type_val = _detect_pdf_type(str(p))

    pages = detect_features_page_range(str(p))
    full_text = _extract_text_for_pages(str(p), pages)

    header = _parse_header_footer(full_text, pdf_type_val)
    pkgs = _parse_packages(full_text)
    parts = _parse_part_numbers(full_text)
    features = _parse_features_bullets(full_text)

    extraction_method = f"regex_type{pdf_type_val}"

    missing_fields = []
    for field in ["core", "max_frequency_mhz", "flash_kb", "ram_kb",
                   "voltage_min_v", "voltage_max_v", "coremark"]:
        if features.get(field) is None:
            missing_fields.append(field)

    low_confidence_fields = []
    if not features.get("operating_temp_c"):
        missing_fields.append("operating_temp_c")

    meta = ExtractionMeta(
        source_pages=pages,
        extraction_method=extraction_method,
        confidence="high" if len(missing_fields) <= 1 else "medium",
        missing_fields=missing_fields,
        low_confidence_fields=low_confidence_fields,
        text_extraction_reliable=True,
        packages_source="text",
    )

    result = DeviceFeatures(
        pdf_name=pdf_name,
        family=family,
        pdf_type=pdf_type_val,
        doc_ref=header.get("doc_ref"),
        revision=header.get("revision"),
        date=header.get("date"),
        title=header.get("title"),
        core=features["core"],
        fpu=features["fpu"],
        max_frequency_mhz=features["max_frequency_mhz"],
        flash_kb=features["flash_kb"],
        ram_kb=features["ram_kb"],
        voltage_min_v=features["voltage_min_v"],
        voltage_max_v=features["voltage_max_v"],
        operating_temp_c=features["operating_temp_c"],
        coremark=features["coremark"],
        packages=pkgs,
        part_numbers=parts,
        extraction_meta=meta,
    )

    return result.model_dump()


def extract_and_save(pdf_path: str, output_dir: str | Path) -> dict:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / "features.json"
    if out_path.exists():
        return json.loads(out_path.read_text(encoding="utf-8"))

    result = extract_features_page_range(pdf_path)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"Features saved: {out_path}")
    return result
