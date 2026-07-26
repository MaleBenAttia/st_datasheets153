import sys, re
sys.path.insert(0, 'table_extractor_raw')
from core.grid_extractor import _is_likely_reversed

extra = {
    'cap_lower_space': r'^[A-Z][a-z]{1,}\s',
    'paren_lower': r'\([a-z]',
    'digit_V_lower': r'^\d+\s+V\s+[a-z]',
    'digit_letter_slash': r'\d+[a-z]/[A-Z]',
}

tests = [
    "Hz internal RC (LSI",
    "CD (binar",
    "6 V operatin",
    "C092xB/xC",
    # True positives (must stay detected)
    "ISL f",
    "6.3 ot 0.2",
    "031 C°",
]

for t in tests:
    before = _is_likely_reversed(t)
    fixers = []
    for name, pat in extra.items():
        m = bool(re.search(pat, t))
        if m:
            fixers.append(name)
    if before and fixers:
        print(f"  WOULD FIX | {fixers[0]:20s} | \"{t}\"")
    elif before and not fixers:
        print(f"  STILL FP  | {'':20s} | \"{t}\"")
    elif not before:
        print(f"  ALREADY OK| {'':20s} | \"{t}\"")
