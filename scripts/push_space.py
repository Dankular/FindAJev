"""Upload the project to the Hugging Face Space as a Docker Space. Token from env HF_TOKEN (never stored).
Usage: HF_TOKEN=... python scripts/push_space.py [space-id]"""
import os, shutil, sys, tempfile
from pathlib import Path
from huggingface_hub import HfApi

ROOT = Path(__file__).resolve().parent.parent
space = sys.argv[1] if len(sys.argv) > 1 else "Daankular/FindAJev"
with tempfile.TemporaryDirectory() as d:
    d = Path(d)
    for name in ("src", "tools"):
        shutil.copytree(ROOT / name, d / name, ignore=shutil.ignore_patterns("bin", "obj", "__pycache__"))
    for name in ("models.json", "requirements.txt"):
        shutil.copy(ROOT / name, d / name)
    shutil.copy(ROOT / "README.md", d / "PROJECT.md")
    for f in (ROOT / "space").iterdir():
        if f.is_file():
            shutil.copy(f, d / f.name)
    api = HfApi(token=os.environ["HF_TOKEN"])
    api.upload_folder(repo_id=space, repo_type="space", folder_path=str(d), commit_message="Deploy FindAJev harness API")
print("pushed to", space)
