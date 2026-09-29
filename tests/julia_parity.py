"""Encoder parity: tools/encode.py Julia encoder + ORT CPU vs the 100 PyTorch reference logits in Julia-1-ONNX/parity-cases.json."""
import json, sys
from pathlib import Path
import numpy as np, onnxruntime as ort
from tokenizers import Tokenizer
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import encode

P = ROOT / "models/Julia-1-ONNX"
pad = Tokenizer.from_file(str(P / "tokenizer.json")).token_to_id("<pad>")
s = ort.InferenceSession(str(P / "model.onnx"), providers=["CPUExecutionProvider"])
enc = encode.julia_encoder()
cases = json.load(open(P / "parity-cases.json"))
match, maxerr, bad = 0, 0.0, []
for i, c in enumerate(cases):
    r = c["request"]
    st = r["state"] if isinstance(r["state"], str) else json.dumps(r["state"], ensure_ascii=False)
    e = enc(dict(text=st, question=r["question"], labels=r["options"]), r.get("type", "choice"))
    n = len(e["ids"]); L = (n + 7) // 8 * 8
    ids = np.full((1, L), pad, np.int64); ids[0, :n] = e["ids"]
    att = np.zeros((1, L), np.int64); att[0, :n] = 1
    pos = np.array([e["pos"]], np.int64)
    out = s.run(None, {"input_ids": ids, "attention_mask": att, "marker_pos": pos,
                       "marker_mask": np.ones(pos.shape, bool), "qtype": np.array([e["qtype"]], np.int64)})[0][0, :len(r["options"])]
    ref = np.array(c["pytorch_logits"]); ok = out.argmax() == ref.argmax(); match += int(ok)
    err = float(np.abs(out - ref).max()); maxerr = max(maxerr, err)
    if not ok or err > 0.01: bad.append((i, round(err, 4), r["question"][:60]))
print(f"argmax match {match}/{len(cases)}  max|dlogit| {maxerr:.5f}")
for b in bad[:10]: print("  mismatch", b)
sys.exit(0 if match == len(cases) and maxerr < 0.01 else 1)
