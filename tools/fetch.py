"""Download the files a models.json entry needs: python tools/fetch.py <id> [<id>...]  (no ids = list)"""
import json, sys
from pathlib import Path
from huggingface_hub import hf_hub_download

ROOT = Path(__file__).resolve().parent.parent
reg = {m["id"]: m for m in json.load(open(ROOT / "models.json"))}
if len(sys.argv) < 2:
    print("\n".join(reg)); sys.exit(0)
for i in sys.argv[1:]:
    m = reg[i]; dest = ROOT / "models" / m["repo"].split("/")[1]
    for f in m["files"]:
        print("fetch", m["repo"], f, file=sys.stderr)
        hf_hub_download(m["repo"], f, local_dir=dest)
