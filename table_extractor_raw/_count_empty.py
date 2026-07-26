import json, os

outdir = os.path.join("outJason")
empty = []
total = 0

for root, dirs, files in os.walk(outdir):
    for f in files:
        if f.endswith(".json"):
            total += 1
            path = os.path.join(root, f)
            try:
                d = json.load(open(path, encoding="utf-8"))
                rows = d.get("rows", [])
                if len(rows) == 0:
                    rel = os.path.relpath(path, outdir)
                    empty.append(rel)
            except Exception as e:
                pass

print(f"Total JSON files: {total}")
print(f"Empty tables: {len(empty)}")
for e in empty[:30]:
    print(f"  {e}")
if len(empty) > 30:
    print(f"  ... and {len(empty)-30} more")
