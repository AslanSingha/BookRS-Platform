"""Does int8 change what a patron sees? Top-10 agreement, fp32 vs int8,
against the real catalogue vectors. Run inside the api container."""
import os
import numpy as np
import psycopg
from bookrs.api.encoder_onnx import OnnxEncoder

QUERIES = [
    "books about how neural networks learn",
    "introduction to programming for beginners",
    "histoire de la révolution française",
    "roman policier à Paris",
    "climate change and the environment",
    "poetry about love and loss",
    "electrical circuits and electronics",
    "philosophie de la science",
    "children's stories about animals",
    "economics of developing countries",
    "cuisine et recettes traditionnelles",
    "the second world war in Europe",
    "mathematics for engineers",
    "musique classique et compositeurs",
    "database design and SQL",
    "religion and spirituality in Asia",
    "architecture moderne et urbanisme",
    "medical textbook anatomy",
    "learning English as a second language",
    "voyage et découverte de l'Asie du Sud-Est",
]

with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
    rows = conn.execute(
        "SELECT e.work_id, e.vector FROM embeddings e JOIN works w ON w.id = e.work_id "
        "WHERE w.deleted_at IS NULL"
    ).fetchall()
ids = np.array([r[0] for r in rows])
M = np.array([r[1] for r in rows], dtype=np.float32)
print(f"catalogue vectors: {M.shape}")

base = os.environ.get("BOOKRS_ONNX_BASE", "/models/minilm-onnx")
A = OnnxEncoder(os.path.join(base, "fp32")).encode(QUERIES)
B = OnnxEncoder(os.path.join(base, "int8")).encode(QUERIES)

overlap10, top1 = [], []
for a, b in zip(A, B):
    ra = ids[np.argsort(-(M @ a))[:10]]
    rb = ids[np.argsort(-(M @ b))[:10]]
    overlap10.append(len(set(ra) & set(rb)) / 10)
    top1.append(float(ra[0] == rb[0]))
print(f"overlap@10  mean={np.mean(overlap10):.3f}  min={np.min(overlap10):.3f}")
print(f"top-1 agreement = {np.mean(top1):.3f}")
print("GATE:", "int8 OK" if np.mean(overlap10) >= 0.9 and np.mean(top1) >= 0.9 else "use fp32")
