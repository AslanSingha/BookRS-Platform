import os, sys, numpy as np
from sentence_transformers import SentenceTransformer
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bookrs", "api"))
from encoder_onnx import OnnxEncoder

MODEL = os.environ["BOOKRS_EMBED_MODEL"]
MAXLEN = int(os.environ["BOOKRS_MAX_SEQ"])
OUT = os.environ["OUT"]

texts = [
    "Introduction to machine learning",
    "Réseaux de neurones et apprentissage profond",
    "សៀវភៅគណិតវិទ្យាសម្រាប់វិស្វករ",
    "Database systems: the complete book",
    "Histoire du Cambodge moderne",
    "Signal processing for communications engineering",
    "Le Petit Prince",
    "Organic chemistry, 9th edition",
    "Python programming for data analysis",
    "Thermodynamics: an engineering approach",
    "Poésie khmère contemporaine",
    "Computer networks: a top-down approach",
]

st = SentenceTransformer(MODEL)
st.max_seq_length = MAXLEN
ref = st.encode(texts, normalize_embeddings=True, convert_to_numpy=True).astype(np.float32)

for tag in ("fp32", "int8"):
    got = OnnxEncoder(os.path.join(OUT, tag), max_len=MAXLEN).encode(texts)
    cos = (ref * got).sum(axis=1)
    print(f"{tag}: dim={got.shape[1]}  min cos={cos.min():.5f}  mean cos={cos.mean():.5f}")

enc = OnnxEncoder(os.path.join(OUT, "int8"), max_len=MAXLEN)
q = enc.encode(["deep learning with neural networks"])[0]
order = np.argsort(-(enc.encode(texts) @ q))
print("top-3 for 'deep learning with neural networks':", [texts[i] for i in order[:3]])
