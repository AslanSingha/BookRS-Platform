"""Query encoder for the api service: ONNX Runtime + tokenizers, no torch.
Pooling must match bookrs/embedding/encoder.py exactly: masked mean pooling
over the last hidden state, then L2 normalisation.
"""
import json
import os

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer


class OnnxEncoder:
    def __init__(self, model_dir: str | None = None, max_len: int | None = None, batch_size: int = 32):
        self.model_dir = model_dir or os.environ.get("BOOKRS_ONNX_DIR", "/models/minilm-onnx")
        self.max_len = int(max_len or os.environ.get("BOOKRS_MAX_SEQ", "128"))
        self.batch_size = batch_size

        self.tok = Tokenizer.from_file(os.path.join(self.model_dir, "tokenizer.json"))
        pad_token = self._pad_token()
        pad_id = self.tok.token_to_id(pad_token)
        self.tok.enable_truncation(self.max_len)
        self.tok.enable_padding(pad_id=pad_id, pad_token=pad_token)

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = int(os.environ.get("BOOKRS_ONNX_THREADS", "2"))
        self.sess = ort.InferenceSession(
            os.path.join(self.model_dir, "model.onnx"), opts, providers=["CPUExecutionProvider"]
        )
        self.input_names = {i.name for i in self.sess.get_inputs()}
        self.output_name = self.sess.get_outputs()[0].name

    def _pad_token(self) -> str:
        path = os.path.join(self.model_dir, "special_tokens_map.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                pad = json.load(fh).get("pad_token")
            if isinstance(pad, dict):
                pad = pad.get("content")
            if pad:
                return pad
        for cand in ("<pad>", "[PAD]"):
            if self.tok.token_to_id(cand) is not None:
                return cand
        raise RuntimeError("cannot determine pad token")

    def encode(self, texts: list[str]) -> np.ndarray:
        if isinstance(texts, str):
            texts = [texts]
        out = []
        for i in range(0, len(texts), self.batch_size):
            out.append(self._encode_batch(texts[i : i + self.batch_size]))
        return np.vstack(out) if out else np.zeros((0, 384), dtype=np.float32)

    def _encode_batch(self, texts: list[str]) -> np.ndarray:
        enc = self.tok.encode_batch(texts)
        ids = np.array([e.ids for e in enc], dtype=np.int64)
        mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
        feed = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self.input_names:
            feed["token_type_ids"] = np.zeros_like(ids)
        hidden = self.sess.run([self.output_name], feed)[0]  # (batch, seq, dim)
        m = mask[:, :, None].astype(np.float32)
        pooled = (hidden * m).sum(axis=1) / np.clip(m.sum(axis=1), 1e-9, None)
        norms = np.linalg.norm(pooled, axis=1, keepdims=True)
        return (pooled / np.clip(norms, 1e-12, None)).astype(np.float32)
