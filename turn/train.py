"""Build the training data and fit the detector. Data: DailyDialog (roskoN/dailydialog zips; licence of the original: cc-by-nc-sa-4.0 -> research use only).
Complete = a whole turn. Incomplete = a strict word-prefix of a turn. Noise controls (labels here are constructed, not human-annotated):
 * a prefix that is itself a complete turn somewhere in the corpus (e.g. 'yes', 'how are you') is dropped, not labelled incomplete;
 * prefixes of a multi-sentence turn that end at a sentence boundary are dropped (a speaker may well have stopped there)."""
import json, random, re, sys
from pathlib import Path
import numpy as np
from scipy.sparse import csr_matrix
from sklearn.linear_model import LogisticRegression
sys.path.insert(0, str(Path(__file__).parent))
from detector import NF, features, normalize

D = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/dd")


def turns(split):
    for line in (D / split / split / f"dialogues_{split}.txt").read_text().splitlines():
        for t in line.split("__eou__"):
            t = t.strip()
            if t: yield t


def build(split, complete_set, rng):
    X = []
    for t in turns(split):
        n = normalize(t)
        words = n.split()
        if len(words) < 2: continue
        X.append((n, 1))
        # word offsets at which the raw turn has sentence-final punctuation
        raw_words = re.sub(r"\s+", " ", t.lower()).split(" ")
        boundaries, k = set(), 0
        for rw in raw_words:
            if normalize(rw): k += 1
            if re.search(r"[.?!]$", rw): boundaries.add(k)
        cuts = [c for c in range(1, len(words)) if c not in boundaries]
        for c in rng.sample(cuts, min(len(cuts), 2)):
            p = " ".join(words[:c])
            if p in complete_set: continue
            X.append((p, 0))
    return X


def matrix(rows):
    ind, ptr = [], [0]
    for text, _ in rows:
        ind += features(text); ptr.append(len(ind))
    return csr_matrix((np.ones(len(ind)), ind, ptr), shape=(len(rows), NF)), np.array([y for _, y in rows])


if __name__ == "__main__":
    rng = random.Random(7)
    complete = {normalize(t) for s in ("train", "validation", "test") for t in turns(s)}
    tr, va, te = (build(s, complete, rng) for s in ("train", "validation", "test"))
    print("rows", len(tr), len(va), len(te), "positive share", round(np.mean([y for _, y in tr]), 3))
    Xtr, ytr = matrix(tr); Xva, yva = matrix(va)
    best = None
    for C in (0.1, 0.3, 1, 3):
        m = LogisticRegression(C=C, max_iter=2000, class_weight="balanced").fit(Xtr, ytr)
        acc = (m.predict(Xva) == yva).mean(); print("C", C, "val acc", round(acc, 4))
        if best is None or acc > best[0]: best = (acc, C, m)
    _, C, m = best
    out = Path(__file__).parent / "turn_detector.json"
    out.write_text(json.dumps({"w": [round(float(x), 4) for x in m.coef_[0]], "b": round(float(m.intercept_[0]), 4), "C": C, "data": "DailyDialog (research use)"}))
    print("saved", out, out.stat().st_size // 1024, "KB; C =", C)
    Path("/tmp/dd/test_rows.json").write_text(json.dumps(te))
