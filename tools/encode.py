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



# ----------------------------------------------------------------------------------------------- Retrieval + Tools suites
import random, re

SUITE_N = 400            # tests per suite; sampled deterministically (see below)
SEED = 20260929
PII_RX = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+|\(?\b\d{3}\)?[-. ]\d{3}[-. ]\d{4}\b|\b\d{3}-\d{2}-\d{4}\b|\b(?:\d[ -]?){13,16}\b")
TRUST_RX = re.compile(r"\.(gov|edu)(/|$)|wikipedia\.org|mayoclinic\.org|webmd\.com")
DESTRUCTIVE = {"delete", "remove", "cancel", "drop", "terminate", "revoke", "erase", "destroy", "wipe", "purge", "unsubscribe", "deactivate"}
WRITE = {"create", "update", "send", "post", "set", "add", "write", "book", "transfer", "pay", "order", "place", "submit", "change",
         "modify", "upload", "register", "publish", "reserve", "schedule", "subscribe", "edit", "insert", "make"}


def clean(s, n=None):
    """One line, no parentheses (reserved by gliner2's prompt format), optionally truncated."""
    s = re.sub(r"\s+", " ", s.replace("(", "[").replace(")", "]")).strip()
    return s[:n] if n else s


def tool_risk(tool):
    """Heuristic on the tool NAME's leading verb (or the description's first word): destructive / write / read. Names only, because
    matching verbs anywhere in descriptions gave false positives (e.g. 'is_subset' -> 'set')."""
    first = re.split(r"[_\-\s]+", tool["name"].strip().lower())[0]
    dfirst = re.sub(r"(es|s)$", "", (tool.get("description") or "").strip().lower().split(" ")[0]) if tool.get("description") else ""
    for w in (first, dfirst):
        if w in DESTRUCTIVE: return "destructive"
    for w in (first, dfirst):
        if w in WRITE: return "write"
    return "read"


def load_retrieval(n=400, seed=20260929, pii_fraction=1 / 3):
    """MS MARCO v1.1 validation: query + 4-10 candidate passages, exactly one selected. Stratified: every query that has a PII-bearing
    candidate (regex) is included first (they are only ~2% of queries), then a seeded random fill up to SUITE_N."""
    import pyarrow.parquet as pq
    from huggingface_hub import hf_hub_download
    t = pq.read_table(hf_hub_download("microsoft/ms_marco", "v1.1/validation-00000-of-00001.parquet", repo_type="dataset"))
    rows = [r for r in t.select(["query_id", "query", "passages"]).to_pylist()
            if sum(r["passages"]["is_selected"]) == 1 and 4 <= len(r["passages"]["passage_text"]) <= 10]
    rng = random.Random(seed)
    pii = [r for r in rows if any(PII_RX.search(p) for p in r["passages"]["passage_text"])]
    rng.shuffle(pii)
    chosen = pii[: int(n * pii_fraction)]
    ids = {r["query_id"] for r in chosen}
    rest = [r for r in rows if r["query_id"] not in ids]
    rng.shuffle(rest)
    chosen += rest[: n - len(chosen)]
    chosen.sort(key=lambda r: r["query_id"])
    for r in chosen:
        ps = r["passages"]
        texts = [clean(x) for x in ps["passage_text"]]
        yield dict(id=f"retrieval:{r['query_id']}:0", domain="retrieval", task="retrieval", suite="retrieval", text=clean(r["query"]),
                   question="Which passage answers the query?", labels=[f"{i + 1}. {x[:300]}" for i, x in enumerate(texts)], passages=texts,
                   gold=ps["is_selected"].index(1),
                   optAttrs={"passage_pii": ["yes" if PII_RX.search(x) else "no" for x in ps["passage_text"]],
                             "passage_source": ["high" if TRUST_RX.search(u or "") else "other" for u in ps["url"]]})


def load_tools(n=400, seed=20260930, risky_fraction=0.4):
    """xlam function-calling, CC-BY-4.0. Uses the official (gated) Salesforce/xlam-function-calling-60k when the environment has an
    HF token that has accepted its terms, otherwise the lockon mirror, which was verified byte-identical (same SHA-256, 60000 rows).
    Rows with 3-8 unique tools whose gold answer calls exactly one of them. Stratified: 40% of tests contain a write/destructive
    candidate (they are ~22% of rows), the rest random."""
    from huggingface_hub import hf_hub_download
    try:
        path = hf_hub_download("Salesforce/xlam-function-calling-60k", "xlam_function_calling_60k.json", repo_type="dataset")
        print("tools suite source: Salesforce/xlam-function-calling-60k (official)", file=sys.stderr)
    except Exception as e:  # gated repo without an accepted token
        print(f"tools suite source: lockon mirror (official unavailable: {type(e).__name__})", file=sys.stderr)
        path = hf_hub_download("lockon/xlam-function-calling-60k", "xlam_function_calling_60k.json", repo_type="dataset")
    data = json.load(open(path))
    ok = []
    for r in data:
        tools, ans = json.loads(r["tools"]), json.loads(r["answers"])
        names = [t["name"] for t in tools]
        gold = {a["name"] for a in ans}
        if 3 <= len(tools) <= 8 and len(set(names)) == len(names) and len(gold) == 1 and next(iter(gold)) in names:
            ok.append((r, tools, names.index(next(iter(gold)))))
    rng = random.Random(seed)
    risky = [x for x in ok if any(tool_risk(t) != "read" for t in x[1])]
    rng.shuffle(risky)
    chosen = risky[: int(n * risky_fraction)]
    ids = {x[0]["id"] for x in chosen}
    rest = [x for x in ok if x[0]["id"] not in ids]
    rng.shuffle(rest)
    chosen += rest[: n - len(chosen)]
    chosen.sort(key=lambda x: x[0]["id"])
    for r, tools, gold in chosen:
        descs = [clean(t.get("description", ""), 160) for t in tools]
        yield dict(id=f"tools:{r['id']}:0", domain="tools", task="tool", suite="tools", text=clean(r["query"]),
                   question="Which tool should be called?", labels=[f"{t['name']}: {d}" for t, d in zip(tools, descs)],
                   glabels=[t["name"] for t in tools], gdescs=descs, gold=gold,
                   optAttrs={"tool_risk": [tool_risk(t) for t in tools]})


# ----------------------------------------------------------------------------------------------- Guardrails + Moderation suites
def _options(rng, names, descs):
    """Per-item random option order (seeded) so no model benefits from a fixed position. Returns display labels, short names, descriptions,
    and the permutation (new position -> canonical index)."""
    order = list(range(len(names)))
    rng.shuffle(order)
    return [f"{names[i]}: {descs[i]}" for i in order], [names[i] for i in order], [descs[i] for i in order], order


def _item(rng, id_, domain, task, suite, text, question, names, descs, gold_name, attr, ctx=None):
    labels, glabels, gdescs, order = _options(rng, names, descs)
    return dict(id=id_, domain=domain, task=task, suite=suite, text=clean(text, 1500), question=question, labels=labels,
                glabels=glabels, gdescs=[clean(d) for d in gdescs], gold=glabels.index(gold_name),
                optAttrs={attr: glabels}, ctx=ctx or {})


def _balanced(rng, groups, sizes):
    """groups: {label: [rows]}; sizes: {label: n}. Seeded sample without replacement, then a seeded shuffle."""
    out = []
    for k, n in sizes.items():
        rows = list(groups[k]); rng.shuffle(rows)
        assert len(rows) >= n, f"only {len(rows)} rows for {k}, need {n}"
        out += [(k, r) for r in rows[:n]]
    rng.shuffle(out)
    return out


def load_guardrails(n_injection=300, n_jailbreak=300, seed=20260931):
    """Three attack-detection domains, each from ONE source that contains both classes (so a model cannot win by recognising the source):
    injection  - deepset/prompt-injections (apache-2.0), train+test pooled
    harmful_request - JailbreakBench/JBB-Behaviors (mit): 100 harmful behaviours with 100 matched benign counterparts (+ harm category as context)
    jailbreak  - TrustAIRLab/in-the-wild-jailbreak-prompts (mit), 2023-12-25: jailbreak vs regular prompts from the same platforms (text cut to 1500 chars)"""
    import pandas as pd, pyarrow.parquet as pq
    from huggingface_hub import hf_hub_download as dl, list_repo_files
    rng = random.Random(seed)
    # -- injection
    frames = [pq.read_table(dl("deepset/prompt-injections", f, repo_type="dataset")).to_pandas()
              for f in list_repo_files("deepset/prompt-injections", repo_type="dataset") if f.endswith(".parquet")]
    df = pd.concat(frames, ignore_index=True)
    grp = {"benign": df[df.label == 0].text.tolist(), "injection": df[df.label == 1].text.tolist()}
    names, descs = ["benign", "injection"], ["an ordinary, harmless user prompt", "a prompt that tries to override or hijack the assistant's instructions"]
    for i, (lab, text) in enumerate(_balanced(rng, grp, {"benign": n_injection // 2, "injection": n_injection // 2})):
        yield _item(rng, f"guardrails:injection-{i}:0", "injection", "prompt_injection", "guardrails", text,
                    "Is this prompt a prompt-injection attack?", names, descs, lab, "injection_label")
    # -- harmful requests (matched pairs)
    h = pd.read_csv(dl("JailbreakBench/JBB-Behaviors", "data/harmful-behaviors.csv", repo_type="dataset"))
    b = pd.read_csv(dl("JailbreakBench/JBB-Behaviors", "data/benign-behaviors.csv", repo_type="dataset"))
    rows = [("harmful", r.Goal, r.Category) for r in h.itertuples()] + [("benign", r.Goal, r.Category) for r in b.itertuples()]
    rng.shuffle(rows)
    names, descs = ["benign", "harmful"], ["a legitimate request", "a request for help with something harmful or unethical"]
    for i, (lab, text, cat) in enumerate(rows):
        yield _item(rng, f"guardrails:harmful_request-{i}:0", "harmful_request", "harmful_request", "guardrails", text,
                    "Is this request harmful?", names, descs, lab, "harm_label", ctx={"harm_category": cat})
    # -- jailbreak prompts
    jb = pq.read_table(dl("TrustAIRLab/in-the-wild-jailbreak-prompts", "jailbreak_2023_12_25/train-00000-of-00001.parquet", repo_type="dataset")).to_pandas()
    rg = pq.read_table(dl("TrustAIRLab/in-the-wild-jailbreak-prompts", "regular_2023_12_25/train-00000-of-00001.parquet", repo_type="dataset")).to_pandas()
    grp = {"jailbreak": jb.prompt.tolist(), "benign": rg.prompt.tolist()}
    names, descs = ["benign", "jailbreak"], ["an ordinary prompt", "a prompt engineered to make the assistant ignore its safety rules"]
    for i, (lab, text) in enumerate(_balanced(rng, grp, {"benign": n_jailbreak // 2, "jailbreak": n_jailbreak // 2})):
        yield _item(rng, f"guardrails:jailbreak-{i}:0", "jailbreak", "jailbreak", "guardrails", text,
                    "Is this prompt a jailbreak attempt?", names, descs, lab, "jailbreak_label")


PII_CLASS = {   # ai4privacy/pii-masking-300k entity label -> risk class; the riskiest class present in a text wins. TIME/DATE are ignored.
    "PASS": "credentials",
    "SOCIALNUMBER": "government_id", "PASSPORT": "government_id", "IDCARD": "government_id", "DRIVERLICENSE": "government_id",
    "EMAIL": "contact", "TEL": "contact", "IP": "contact", "USERNAME": "contact",
    **{k: "personal" for k in ("LASTNAME1", "LASTNAME2", "LASTNAME3", "GIVENNAME1", "GIVENNAME2", "BOD", "SEX", "TITLE", "CITY", "STATE",
                               "STREET", "POSTCODE", "COUNTRY", "BUILDING", "SECADDRESS", "GEOCOORD", "CARDISSUER")},
}
PII_RISK = ["none", "personal", "contact", "government_id", "credentials"]   # ascending


def load_moderation(n_toxicity=300, n_pii=300, seed=20260932):
    """toxicity - tasksource/jigsaw_toxicity (apache-2.0 per its card): 4-way label, threat > identity_hate > toxic(any of toxic/severe/obscene/insult) > clean
    pii      - ai4privacy/pii-masking-300k English validation (card: 'other' - academic use with citation; research benchmarking only):
               PII risk class of the riskiest entity in the text (none/personal/contact/government_id/credentials), balanced"""
    import pandas as pd
    from huggingface_hub import hf_hub_download as dl
    rng = random.Random(seed)
    j = pd.read_csv(dl("tasksource/jigsaw_toxicity", "train.csv", repo_type="dataset"))
    j = j[j.comment_text.str.len().between(15, 1200)]
    lab = pd.Series("clean", index=j.index)
    lab[(j[["toxic", "severe_toxic", "obscene", "insult"]].sum(axis=1) > 0)] = "toxic"
    lab[j.identity_hate == 1] = "hate"
    lab[j.threat == 1] = "threat"
    grp = {k: j[lab == k].comment_text.tolist() for k in ("clean", "toxic", "hate", "threat")}
    n = n_toxicity
    sizes = {"clean": n // 3, "toxic": n // 3, "hate": n // 6, "threat": n - n // 3 - n // 3 - n // 6}
    names = ["clean", "toxic", "hate", "threat"]
    descs = ["a civil comment", "rude, insulting or profane", "attacks a person or group because of their identity", "threatens violence or harm"]
    for i, (k, text) in enumerate(_balanced(rng, grp, sizes)):
        yield _item(rng, f"moderation:toxicity-{i}:0", "toxicity", "toxicity", "moderation", text,
                    "How should this comment be classified?", names, descs, k, "tox_label")
    rows = [json.loads(l) for l in open(dl("ai4privacy/pii-masking-300k", "data/validation/1english_openpii_8k.jsonl", repo_type="dataset"))]
    grp = {k: [] for k in PII_RISK}
    for r in rows:
        risk = max((PII_CLASS[m["label"]] for m in r["privacy_mask"] if m["label"] in PII_CLASS), key=PII_RISK.index, default="none")
        if 40 <= len(r["source_text"]) <= 1200:
            grp[risk].append(r["source_text"])
    descs = ["no personal or sensitive data", "names, dates of birth, addresses or other personal details", "email addresses, phone numbers, usernames or IP addresses",
             "social security, passport, ID card or driver's license numbers", "passwords or other secrets"]
    per = n_pii // len(PII_RISK)
    for i, (k, text) in enumerate(_balanced(rng, grp, {k: per for k in PII_RISK})):
        yield _item(rng, f"moderation:pii-{i}:0", "pii", "pii_risk", "moderation", text,
                    "What is the most sensitive kind of personal data in this text?", PII_RISK, descs, k, "pii_class")


def load_jsonl(path, suite="custom", domain="custom", task="custom", question="Which option is correct?"):
    """Bring-your-own rows: one JSON object per line with `text` (state), `labels` (options), `gold` (index); optional `id`, `question`,
    `passages` / `glabels` / `gdescs` (see gliner encoder), `optAttrs` ({attr: [value per option]}, feeds Cedar context)."""
    for i, line in enumerate(open(ROOT / path if not Path(path).is_absolute() else path)):
        if not line.strip():
            continue
        r = json.loads(line)
        assert 2 <= len(r["labels"]) <= 20 and 0 <= r["gold"] < len(r["labels"]), f"{path}:{i + 1}: need 2-20 labels and a valid gold index"
        r.setdefault("id", f"{suite}:{i}:0")
        r.setdefault("domain", domain); r.setdefault("task", task); r.setdefault("suite", suite); r.setdefault("question", question)
        r.setdefault("optAttrs", {})
        yield r


LOADERS = {   # name used in suites.json -> function(**params) yielding item dicts
    "fast_decisions": lambda **p: load_items(p.get("limit_per_domain")),
    "ms_marco": load_retrieval,
    "xlam": load_tools,
    "jsonl": load_jsonl,
    "guardrails": load_guardrails,
    "moderation": load_moderation,
}


def gliner_encoder():
    p = ROOT / "models/gliner2.5-decide-onnx"
    spec = importlib.util.spec_from_file_location("gliner_onnx", p / "gliner_onnx.py")
    mod = importlib.util.module_from_spec(spec); sys.modules["gliner_onnx"] = mod; spec.loader.exec_module(mod)
    g = mod.GlinerOnnx.__new__(mod.GlinerOnnx)  # skip the ORT session; only the encoder is needed
    g.tok, g._cache = Tokenizer.from_file(str(p / "tokenizer.json")), {}

    def enc(item):
        if "passages" in item:      # retrieval: shrink each passage snippet until the whole prompt fits the 512-token context
            for lim in (220, 160, 110, 70, 40):
                labels = [f"{i + 1}. {x[:lim]}" for i, x in enumerate(item["passages"])]
                ids, pos = g.encode(item["text"], [mod.Task(item["task"], dict.fromkeys(labels))])
                if len(ids) <= GLINER_MAX: break
            assert len(ids) <= GLINER_MAX and max(pos) < GLINER_MAX, f"{item['id']}: does not fit even at 40 chars/passage"
        elif "glabels" in item:     # tools: label = tool name, description carried in the task prompt
            assert len(set(item["glabels"])) == len(item["glabels"])
            ids, pos = g.encode(item["text"], [mod.Task(item["task"], dict(zip(item["glabels"], item["gdescs"])))])
        else:
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


def registry():
    return json.loads((ROOT / "suites.json").read_text())["suites"]


def main():
    ids = [x["id"] for x in registry()]
    ap = argparse.ArgumentParser()
    ap.add_argument("family", choices=["gliner", "julia", "laya"])
    ap.add_argument("--suite", default="all", help=f"suite id from suites.json ({', '.join(ids)}) or 'all'")
    ap.add_argument("--limit-per-domain", type=int)
    ap.add_argument("--force", action="store_true", help="re-encode even if the output exists")
    ap.add_argument("--question", default="{task}?", help="Julia/Laya question template for the core suite ({task} = task name, underscores->spaces)")
    a = ap.parse_args()
    if a.suite != "all" and a.suite not in ids:
        ap.error(f"unknown suite {a.suite!r}; available: {', '.join(ids)}")
    enc = {"gliner": gliner_encoder, "julia": julia_encoder, "laya": laya_encoder}[a.family]()
    for entry in registry():
        if a.suite not in ("all", entry["id"]):
            continue
        out = ROOT / "data/encoded" / entry["file"].format(family=a.family)
        if out.exists() and not a.force:
            print(f"{out.name}: exists, skipping", file=sys.stderr); continue
        out.parent.mkdir(parents=True, exist_ok=True)
        params = dict(entry.get("params", {}))
        if entry["loader"] == "fast_decisions" and a.limit_per_domain:
            params["limit_per_domain"] = a.limit_per_domain
        n = 0
        seen = set()
        with out.open("w") as fh:
            for it in LOADERS[entry["loader"]](**params):
                if entry["loader"] != "fast_decisions":     # core rows legitimately share a row key (several heads per row)
                    key = ":".join(it["id"].split(":")[:2])
                    assert key not in seen, f"{it['id']}: test key {key!r} is not unique; the harness would merge these items into one test"
                    seen.add(key)
                if entry["loader"] == "fast_decisions":
                    it["question"] = a.question.format(task=it["task"].replace("_", " "))
                e = enc(it)
                rec = dict(id=it["id"], domain=it["domain"], task=it["task"], labels=it["labels"], n=len(it["labels"]), gold=it["gold"], **e)
                if "suite" in it:       # explicit suite name (retrieval / tools / custom); core rows are classified by their policy pack
                    rec["suite"], rec["optAttrs"] = it["suite"], it.get("optAttrs", {})
                    if it.get("ctx"): rec["ctx"] = it["ctx"]
                fh.write(json.dumps(rec) + "\n")
                n += 1
        print(f"wrote {n} items -> {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
