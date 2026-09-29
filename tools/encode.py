"""Pre-tokenize the shared decision dataset for each model family.

The C# harness benchmarks ONNX Runtime CPU inference only, so tokenization happens here, once,
with the reference `tokenizers` library and each model's own prompt layout.

Shared dataset: fastino/fast-decisions. Unit of work = one single-label task of one row
(multi-label tasks are skipped: Julia scores exactly one option, so there is no common gold).

Output: data/encoded/<family>.jsonl, one item per line:
  gliner: {id, domain, n, gold, ids, pos}
  julia : {id, domain, n, gold, ids, pos, qtype}
"""
import argparse, glob, importlib.util, json, sys
from pathlib import Path

from huggingface_hub import snapshot_download
from tokenizers import Tokenizer

ROOT = Path(__file__).resolve().parent.parent
GLINER_MAX = 512  # context stated on the onnx-community card
JULIA_MAX, JULIA_HEAD, JULIA_OPT = 1024, 256, 48  # values used by Julia-1-ONNX/parity.py


def load_items(limit_per_domain=None):
    d = snapshot_download("fastino/fast-decisions", repo_type="dataset")
    for f in sorted(glob.glob(d + "/*.jsonl")):
        domain = Path(f).stem
        with open(f) as fh:
            for n, line in enumerate(fh):
                if limit_per_domain and n >= limit_per_domain:
                    break
                row = json.loads(line)
                for k, t in enumerate(row["output"]["classifications"]):
                    if t["multi_label"] or len(t["true_label"]) != 1:
                        continue
                    labels = t["labels"]
                    yield dict(id=f"{domain}:{n}:{k}", domain=domain, text=row["input"], task=t["task"],
                               labels=labels, gold=labels.index(t["true_label"][0]))


def gliner_encoder():
    p = ROOT / "models/gliner2.5-decide-onnx"
    spec = importlib.util.spec_from_file_location("gliner_onnx", p / "gliner_onnx.py")
    mod = importlib.util.module_from_spec(spec); sys.modules["gliner_onnx"] = mod; spec.loader.exec_module(mod)
    g = mod.GlinerOnnx.__new__(mod.GlinerOnnx)  # skip the ORT session; only the encoder is needed
    g.tok, g._cache = Tokenizer.from_file(str(p / "tokenizer.json")), {}

    def enc(item):
        ids, pos = g.encode(item["text"], [mod.Task(item["task"], dict.fromkeys(item["labels"]))])
        return dict(ids=ids[:GLINER_MAX], pos=pos)
    return enc


def decision_encoder(tok_path, max_len, head_len, opt_len=48):
    """Sequence layout shared by Julia-1 and Laya: [CLS] "<type> question: <q>" [SEP] ([MASK] option)* [SEP] state [SEP].
    Julia: non-strict path of sequence() in Julia-1/julia/data.py. Laya: build_sequence() in convaiinnovations/laya rl_common.py."""
    tok_path = Path(tok_path)
    tok = Tokenizer.from_file(str(tok_path / "tokenizer.json"))
    cfg = json.loads((tok_path / "tokenizer_config.json").read_text())
    tid = lambda name: tok.token_to_id(cfg[name])
    mask, cls, sep = tid("mask_token"), tid("cls_token"), tid("sep_token")
    for n, v in (("mask", mask), ("cls", cls), ("sep", sep)):
        assert v is not None, f"tokenizer has no {n} token"
    encode = lambda s: tok.encode(s, add_special_tokens=False).ids
    clean = lambda s: s.replace(cfg["mask_token"], " ")

    def enc(item, qtype="choice"):
        head = encode(f"{qtype} question: {clean(item['question'])}")
        opt_ids = [encode(" " + clean(x)) for x in item["labels"]]
        options = [[mask] + x[:opt_len] for x in opt_ids]
        budget = head_len - sum(map(len, options))
        if budget < 16:
            per = max(4, (head_len - 16) // len(options))
            options = [x[:per] for x in options]
            budget = head_len - sum(map(len, options))
        ids = [cls] + head[:max(8, budget)] + [sep]
        pos = []
        for o in options:
            pos.append(len(ids)); ids.extend(o)
        ids.append(sep)
        room = max_len - len(ids) - 1
        assert room >= 1, "question/options exceed sequence budget"
        ids += encode(clean(item["text"]))[:room] + [sep]
        assert all(p < max_len for p in pos)
        return dict(ids=ids[:max_len], pos=pos, qtype={"choice": 0, "score": 1, "noul": 2}[qtype])
    return enc


def julia_encoder():
    return decision_encoder(ROOT / "models/Julia-1-ONNX", JULIA_MAX, JULIA_HEAD, JULIA_OPT)


def laya_encoder():
    cfg = json.loads((ROOT / "models/laya-onnx/laya_config.json").read_text())
    return decision_encoder(ROOT / "models/laya-onnx/tokenizer", cfg["max_len"], cfg["head_max_len"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("family", choices=["gliner", "julia", "laya"])
    ap.add_argument("--limit-per-domain", type=int)
    ap.add_argument("--question", default="{task}?", help="Julia/Laya question template ({task} = task name, underscores->spaces)")
    a = ap.parse_args()
    enc = {"gliner": gliner_encoder, "julia": julia_encoder, "laya": laya_encoder}[a.family]()
    out = ROOT / "data/encoded" / f"{a.family}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out.open("w") as fh:
        for it in load_items(a.limit_per_domain):
            it["question"] = a.question.format(task=it["task"].replace("_", " "))
            e = enc(it)
            fh.write(json.dumps(dict(id=it["id"], domain=it["domain"], n=len(it["labels"]), gold=it["gold"], **e)) + "\n")
            n += 1
    print(f"wrote {n} items -> {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
