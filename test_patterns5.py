import sys, re
sys.path.insert(0, 'table_extractor_raw')
from core.grid_extractor import _is_likely_reversed

pat = r'[A-Z]\d+[a-z][A-Z]/'
tests = [
    "C092xB/xC",
    "ISL f",
    "6.3 ot 0.2",
]
for t in tests:
    before = _is_likely_reversed(t)
    m = bool(re.search(pat, t))
    print(f"  before={before} match={m:5} | \"{t}\"")
