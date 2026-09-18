import os, shutil, sys
from onnxruntime.quantization import quantize_dynamic, QuantType
src, dst = sys.argv[1], sys.argv[2]
os.makedirs(dst, exist_ok=True)
quantize_dynamic(os.path.join(src, "model.onnx"), os.path.join(dst, "model.onnx"), weight_type=QuantType.QInt8)
for f in ("tokenizer.json", "tokenizer_config.json", "special_tokens_map.json", "config.json", "sentencepiece.bpe.model"):
    p = os.path.join(src, f)
    if os.path.exists(p):
        shutil.copy(p, dst)
print("fp32:", os.path.getsize(os.path.join(src,"model.onnx"))//2**20, "MB   int8:", os.path.getsize(os.path.join(dst,"model.onnx"))//2**20, "MB")
