import re
from pathlib import Path

text = Path("app/ocr_extractor.py").read_text(encoding="utf-8")
block = text.split("_KNOWN_VENDORS = [", 1)[1].split("]\n\n_PHONE_RE", 1)[0]
parts = re.split(r"# —— (United States|India|Australia) ——", block)
for i in range(1, len(parts), 2):
    country = parts[i]
    n = len(re.findall(r'\{"name":', parts[i + 1]))
    print(f"{country}: {n}")
print("total", sum(len(re.findall(r'\{"name":', parts[i + 1])) for i in range(1, len(parts), 2)))
