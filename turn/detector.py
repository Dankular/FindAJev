"""Tiny end-of-turn (utterance-complete) detector for TEXT (an ASR transcript so far). Hashed n-gram features + a linear scorer.
Inference is pure Python + a JSON weight file; there is no audio model here (silence duration is a separate signal handled by policy)."""
import json, math, re, zlib
from pathlib import Path

NF = 1 << 16
_norm_rx = re.compile(r"[^a-z0-9' ]+")


def normalize(text: str) -> str:
    """ASR-style: lowercase, no punctuation (real ASR output rarely carries reliable punctuation)."""
    return re.sub(r"\s+", " ", _norm_rx.sub(" ", text.lower().replace("’", "'"))).strip()


def features(text: str) -> list[int]:
    w = normalize(text).split()
    if not w:
        return []
    toks = [f"L1={w[-1]}", f"L2={'_'.join(w[-2:])}", f"L3={'_'.join(w[-3:])}", f"F1={w[0]}", f"N={min(len(w), 12)}", f"S2={'_'.join(w[:2])}"]
    toks += [f"U={x}" for x in w[-6:]]
    toks += [f"B={a}_{b}" for a, b in zip(w[-6:], w[-5:])]
    return sorted({zlib.crc32(t.encode()) % NF for t in toks})


class TurnDetector:
    def __init__(self, path):
        d = json.loads(Path(path).read_text())
        self.w, self.b = d["w"], d["b"]

    def p_complete(self, text: str) -> float:
        z = self.b + sum(self.w[i] for i in features(text))
        return 1 / (1 + math.exp(-z))

FUNC = {"a","an","the","to","of","for","and","or","but","if","that","in","on","at","with","my","your","his","her","our","their","is","are","was","be","i","we","you","he","she","they","it","this","than","so","because","um","uh","like","would","could","should","will","can","do","does","have","has","had","not","no","from","by","as","about","into","what","where","when","how","who","which","there","just"}


def dangling(text: str) -> bool:
    """Independent rule: the transcript ends on a connective/article/auxiliary, so the sentence cannot be finished yet."""
    w = normalize(text).split()
    return bool(w) and w[-1] in FUNC
