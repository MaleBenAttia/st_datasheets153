import sys, re
sys.path.insert(0, 'table_extractor_raw')
from core.grid_extractor import _is_likely_reversed

false_positives = [
    # Order codes in pinout tables (hundreds of hits)
    "Bx/8x170C23MTS",
    "Cx/Bx290C23MTS Cx/Bx190C23MTS",
    # Formulas
    "f = f HCLK HSI48/HSIDIV ( > 32 kHz), f = f HCLK LSI ( = 32 kHz)",
    # CSS RAM with multi-word parens
    "CSS RAM (parity error)",
    # Slash-separated acronyms
    "RTC / RNG / AES / VREFBUF",
    # Sentence starting lowercase
    "the GPIO pins are shared",
    "the GPIO pins are shared with",
    "nternal RC oscillat",
    "e LSE can also be",
    "g to I/O control registers.",
    "er with a DMA request si",
    # A/D shorthand
    "A/D con",
    "nd the PWM output",
    "x-M0+ exceptio",
    # Number with part number
    "36 (STM32C091xx) / 30 (STM32C092xx)",
    "C092xB/xC",
    # All-caps multi-word
    "USER TRIM COVERAGE",
    # PF2-NRST
    "PF2-NRST",
    # Hz internal RC
    "Hz internal RC (LSI",
    "48MHz",
    "16MHz",
    "V . CORE",
    "CD (binar",
    "ne I2C per",
    "2.0 V to 3.",
    "6 V operatin",
    "d APB domai",
    "tex-M0+ processor co",
]
true_positives = [
    "ISL f",
    "6.3 ot 0.2",
    "N/PG 02POSST",  # TSSOP20 GP/N - actually reversed
]

for t in false_positives:
    r = _is_likely_reversed(t)
    print(f'  {r!s:5} | "{t}"')

print("\n--- True positives ---")
for t in true_positives:
    r = _is_likely_reversed(t)
    print(f'  {r!s:5} | "{t}"')
