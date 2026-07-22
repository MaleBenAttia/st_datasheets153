import sys, json
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, '.')
from table_extractor_raw.core.page1_features import extract_features_page_range

for label, path in [
    ('C0 stm32c011d6', 'DataSHEET/C0/stm32c011d6.pdf'),
    ('N6 stm32n645a0', 'DataSHEET/N6/stm32n645a0.pdf'),
    ('C5 stm32c532cb', 'DataSHEET/C5/stm32c532cb.pdf'),
]:
    r = extract_features_page_range(path)
    meta = r['extraction_meta']
    print(f'=== {label} ===')
    print(f'  title={r.get("title")}')
    print(f'  url_pdf={r.get("url_pdf")}')
    print(f'  page={r.get("page")}')
    print(f'  doc_ref={r.get("doc_ref")} rev={r.get("revision")} date={r.get("date")}')
    print(f'  core={r["core"]} freq={r["max_frequency_mhz"]}MHz flash={r["flash_kb"]}KB ram={r["ram_kb"]}KB')
    print(f'  voltage={r["voltage_min_v"]}-{r["voltage_max_v"]}V fpu={r["fpu"]}')
    print(f'  temp={r["operating_temp_c"]}')
    print(f'  packages={r["packages"]}')
    print(f'  missing_fields={meta["missing_fields"]}')
    print()
