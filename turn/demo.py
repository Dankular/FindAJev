"""Stream an utterance word by word, then a pause: the detector scores each prefix, Cedar decides Respond / Backchannel / Wait.
Usage: python turn/demo.py "i'd like to book a table for two tonight" [silence_ms_after_the_last_word]"""
import json, os, subprocess, sys
from pathlib import Path
from detector import TurnDetector, dangling
ROOT = Path(__file__).resolve().parent.parent
det = TurnDetector(Path(__file__).parent / "turn_detector.json")
env = dict(os.environ, FINDAJEV_ROOT=str(ROOT), PATH=os.environ["PATH"] + ":/opt/dotnet", DOTNET_ROOT="/opt/dotnet")
def decide(text, silence, speaking):
    a = ["dotnet", str(ROOT / "bin/FindAJev.Bench.dll"), "turn-decide", "--p", str(round(100 * det.p_complete(text))), "--silence", str(silence)]
    a += (["--speaking"] if speaking else []) + (["--dangling"] if dangling(text) else [])
    out = json.loads(subprocess.run(a, capture_output=True, text=True, env=env, cwd=ROOT).stdout)["decisions"]
    return [d["action"] for d in out if d["allow"] and d["action"] != "Wait"] or ["Wait"]
words = sys.argv[1].split(); final_silence = int(sys.argv[2]) if len(sys.argv) > 2 else 600
for i in range(1, len(words) + 1):
    t = " ".join(words[:i]); last = i == len(words)
    print(f"{t!r:60s} P(complete)={det.p_complete(t):.2f} dangling={dangling(t)!s:5s}", "mid-speech:", decide(t, 0, True), "| after", f"{final_silence}ms pause:" if last else "300ms pause:", decide(t, final_silence if last else 300, False))
