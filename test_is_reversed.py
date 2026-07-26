import sys
sys.path.insert(0, 'table_extractor_raw')
from core.grid_extractor import _is_likely_reversed
tests = [
    'TRIMOFFSETP TRIMLPOFFSETP',
    'TRIMOFFSETN TRIMLPOFFSETN',
    'VDDA -100 mV',
    'PKA (ECDSA signature verification)',
    'ISL f',
    '6.3 ot 0.2',
]
for t in tests:
    r = _is_likely_reversed(t)
    print(f'  {r!s:5} | "{t}"')
