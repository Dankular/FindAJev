"""Evaluate the detector: (1) constructed held-out test, (2) hand-written probes, (3) a trivial rule baseline, (4) CPU latency."""
import json, statistics, time
from pathlib import Path
from detector import TurnDetector, normalize
H = Path(__file__).parent
det = TurnDetector(H / "turn_detector.json")
from detector import FUNC
def rule(t):  # baseline: complete unless it ends in a function word
    w = normalize(t).split()
    return 0.0 if not w or w[-1] in FUNC else 1.0
def report(name, rows, score, thr=0.5):
    tp = fp = tn = fn = 0
    for text, y in rows:
        p = score(text) >= thr
        tp += p and y == 1; fp += p and y == 0; tn += (not p) and y == 0; fn += (not p) and y == 1
    n = tp + fp + tn + fn
    print(f"{name:34s} acc {(tp+tn)/n:.3f}  P(complete) prec {tp/max(1,tp+fp):.3f} rec {tp/max(1,tp+fn):.3f}  | interrupts (called complete but wasn't): {fp}/{fp+tn}  waits too long (called incomplete but was complete): {fn}/{fn+tp}  n={n}")
    return dict(acc=(tp+tn)/n, fp=fp, fn=fn, n=n)
te = [tuple(r) for r in json.loads(Path("/tmp/dd/test_rows.json").read_text())]
pr = json.loads((H / "probes.json").read_text()); probes = [(t, 1) for t in pr["complete"]] + [(t, 0) for t in pr["incomplete"]]
out = {}
for nm, rows in (("constructed test (DailyDialog)", te), ("hand-written probes", probes)):
    out[nm] = {"model": report(nm + " / model", rows, det.p_complete), "rule": report(nm + " / rule baseline", rows, rule)}
for thr in (0.3, 0.5, 0.7, 0.9): report(f"probes / model @ {thr}", probes, det.p_complete, thr)
ts = []
for t, _ in probes * 20:
    s = time.perf_counter(); det.p_complete(t); ts.append((time.perf_counter() - s) * 1e6)
ts.sort(); print(f"latency per call (pure Python, this box): p50 {ts[len(ts)//2]:.0f} us  p95 {ts[int(len(ts)*.95)]:.0f} us  (n={len(ts)})")
print("\nmodel errors on probes:")
for t, y in probes:
    p = det.p_complete(t)
    if (p >= .5) != (y == 1): print(f"  gold={'complete' if y else 'incomplete'} p={p:.2f}  {t!r}")
(H / "eval.json").write_text(json.dumps(out, indent=1))
