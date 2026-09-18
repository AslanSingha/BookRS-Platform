import re, sys
p = "compose.yaml"
s = open(p, encoding="utf-8").read()
m = re.search(r"\n  api:\n", s)
end = re.search(r"\n  [a-z_]+:\n", s[m.end():])
a, b = m.start(), m.end() + (end.start() if end else len(s) - m.end())
block = s[a:b]
if "BOOKRS_ONNX_DIR" not in block:
    block = re.sub(r'(\n      BOOKRS_ALLOWED_ORIGINS:[^\n]*)',
                   r'\1\n      BOOKRS_ONNX_DIR: "/models/minilm-onnx/${BOOKRS_ONNX_VARIANT:-int8}"',
                   block, count=1)
if "/models/minilm-onnx:ro" not in block:
    block = block.replace("      - ./tests:/app/tests:ro",
                          "      - ./tests:/app/tests:ro\n      - ./models/minilm-onnx:/models/minilm-onnx:ro", 1)
s = s[:a] + block + s[b:]
open(p, "w", encoding="utf-8").write(s)
print("compose.yaml patched" if "BOOKRS_ONNX_DIR" in block and "/models/minilm-onnx:ro" in block else "PATCH FAILED: paste compose.yaml api block")
