import sys, re
sys.path.insert(0, 'table_extractor_raw')
from core.grid_extractor import _is_likely_reversed

proposed = {
    'order_code_slash': r'[A-Z][a-z]/[A-Za-z0-9]',
    'gen_slash_code': r'[A-Z][a-z]+/[A-Za-z0-9]',
    'both_start_lower': lambda c, r: c[0].islower() and r[0].islower(),
    'multi_word_parens': r'\([A-Za-z]+\s+[A-Za-z]+\)',
    'slash_separated': r'\s/\s',
    'allcaps_3plus_words': r'^[A-Z][A-Z0-9\s]{5,}$',
    'A_D_shorthand': r'A/[A-Z]',
    'f_equals_f': r'f\s*=\s*f\s',
    'V_dot_CORE': r'\b[A-Z]\s\.\s[A-Z]',
    'ends_with_period': lambda c, r: c.endswith('.'),
    'pin_name_dash': r'^[A-Z0-9]+-[A-Z0-9]+$',
    'digit_with_unit': r'^\d+[A-Za-z]+$',
    'voltage_range_to': r'\d+\.\d+\s*V\s+to\s',
    'digit_fragment': r'^\d+\s+[a-z]',
    'starts_with_digit_partnum': r'^\d+\s+\([A-Z]',
}

tests = [
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
    "C092xB/xC",
    # True positive (must not be caught)
    "ISL f",
    "6.3 ot 0.2",
]

print("Testing proposed guards...")
for t in tests:
    before = _is_likely_reversed(t)
    fixers = []
    for name, pat in proposed.items():
        if callable(pat):
            m = pat(t, t[::-1])
        else:
            m = bool(re.search(pat, t))
        if m:
            fixers.append(name)
    if before and fixers:
        print(f"  WOULD FIX | {fixers[0]:25s} | \"{t}\"")
    elif before and not fixers:
        print(f"  STILL FP  | {'':25s} | \"{t}\"")
    elif not before:
        print(f"  ALREADY OK| {'':25s} | \"{t}\"")
