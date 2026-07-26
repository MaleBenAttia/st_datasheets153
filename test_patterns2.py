import sys, re
sys.path.insert(0, 'table_extractor_raw')
from core.grid_extractor import _is_likely_reversed

proposed = {
    'order_code_slash': r'[A-Z][a-z]/[A-Za-z0-9]',
    'both_start_lower': lambda c, r: c[0].islower() and r[0].islower(),
    'multi_word_parens': r'\([A-Za-z]+\s+[A-Za-z]+\)',
    'slash_separated': r'\s/\s',
    'allcaps_3plus_words': r'^[A-Z][A-Z0-9\s]{5,}$',
    'A_D_shorthand': r'A/[A-Z]',
    'f_equals_f': r'f\s*=\s*f\s',
    'V_dot_CORE': r'\b[A-Z]\s\.\s[A-Z]',
    'starts_with_digit_partnum': r'^\d+\s+\([A-Z]',
    'endswith_parenthetical': r'[A-Za-z]+\s+\([A-Za-z]+\s+\d+',
}

tests = [
    # False positives
    "Bx/8x170C23MTS",
    "Cx/Bx290C23MTS Cx/Bx190C23MTS",
    "f = f HCLK HSI48/HSIDIV ( > 32 kHz), f = f HCLK LSI ( = 32 kHz)",
    "CSS RAM (parity error)",
    "RTC / RNG / AES / VREFBUF",
    "the GPIO pins are shared",
    "the GPIO pins are shared with",
    "nternal RC oscillat",
    "e LSE can also be",
    "g to I/O control registers.",
    "er with a DMA request si",
    "A/D con",
    "nd the PWM output",
    "x-M0+ exceptio",
    "36 (STM32C091xx) / 30 (STM32C092xx)",
    "C092xB/xC",
    "USER TRIM COVERAGE",
    "PF2-NRST",
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
    # True positives (must not be caught)
    "ISL f",
    "6.3 ot 0.2",
]

print("Testing proposed guards...")
print()
for t in tests:
    before = _is_likely_reversed(t)
    matches = []
    for name, pat in proposed.items():
        if callable(pat):
            m = pat(t, t[::-1])
        else:
            m = bool(re.search(pat, t))
        if m:
            matches.append(name)
    status = "OK" if not before else "STILL FP" if not matches else "would fix"
    if before and matches:
        print(f"  WOULD FIX | {matches[0]:30s} | \"{t}\"")
    elif before and not matches:
        print(f"  STILL FP  | {'':30s} | \"{t}\"")
    else:
        print(f"  TRUE POS  | {'':30s} | \"{t}\"")
