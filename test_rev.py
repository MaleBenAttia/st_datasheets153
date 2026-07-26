import sys, re
sys.path.insert(0, 'table_extractor_raw')
from core.grid_extractor import _is_likely_reversed

tests = [
    ("ISL f", True, "f LSI"),
    ("f LSI", False, None),
    ("tSU(LSI)", False, None),
    ("tSTAB(LSI)", False, None),
    ("IDD(Run)", False, None),
    ("Vprog", False, None),
    ("A troP", True, "Port A"),
    ("TxK3A5C23MTS", True, "STM32C5A3KxT"),
    ("Port A", False, None),
    ("STM32C5A3KxT", False, None),
    ("V (1) IL", False, None),
    ("fLSI", False, None),
    ("I DD(LSI)", False, None),
    ("2.0 to 3.6 V", False, None),
]

ok = 0
fail = 0
for cell, expected, correct in tests:
    result = _is_likely_reversed(cell)
    s = "OK" if result == expected else "FAIL"
    if s == "OK":
        ok += 1
    else:
        fail += 1
    rev = cell[::-1]
    extra = f'  -> should be "{correct}"' if correct else ""
    print(f'{s}: "{cell}" reversed={result} (expected={expected}){extra}')

print(f"\n{ok}/{ok+fail} passed, {fail} failed")
