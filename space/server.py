"""HTTP API around the FindAJev harness. One benchmark job at a time (parallel jobs would corrupt each other's timings).

If the API_KEY environment variable (a Space secret) is set, the endpoints that start work (POST /run, /run-all) require
`Authorization: Bearer <API_KEY>`. Reads (leaderboard, results, jobs, SSE) are public.

Results are kept in results/ and, when HF_TOKEN + RESULTS_REPO (a dataset repo id) are set, mirrored to that dataset so they
survive Space restarts.
"""
import asyncio, json, os, subprocess, threading, time, uuid
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

ROOT = Path(os.environ.get("FINDAJEV_ROOT", Path(__file__).parent)).resolve()
DLL = ROOT / "bin" / "FindAJev.Bench.dll"
REGISTRY = {m["id"]: m for m in json.loads((ROOT / "models.json").read_text())}
DATASET_SIZE = 2600  # single-label decisions in fastino/fast-decisions (see tools/encode.py)

def cpu_info():
    """CPUs this container may actually use. os.cpu_count() reports the host's cores, which can exceed the cgroup quota."""
    info = {"os_cpu_count": os.cpu_count(), "affinity": len(os.sched_getaffinity(0)), "cgroup_quota_cpus": None}
    try:  # cgroup v2: "<quota|max> <period>"
        q, per = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        if q != "max":
            info["cgroup_quota_cpus"] = int(q) / int(per)
    except (OSError, ValueError):
        try:  # cgroup v1
            q = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read_text())
            per = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read_text())
            if q > 0:
                info["cgroup_quota_cpus"] = q / per
        except (OSError, ValueError):
            pass
    usable = info["affinity"]
    if info["cgroup_quota_cpus"]:
        usable = min(usable, max(1, int(info["cgroup_quota_cpus"])))
    info["usable_cpus"] = usable
    return info


CPUS = cpu_info()["usable_cpus"]

app = FastAPI(title="FindAJev", description="CPU benchmark of typed-decision models")
_lock = threading.Lock()
_jobs: dict[str, dict] = {}


# ---------------------------------------------------------------------------------------------- persistence
def persist(path: Path):
    repo, tok = os.environ.get("RESULTS_REPO"), os.environ.get("HF_TOKEN")
    if repo and tok and path.exists():
        try:
            from huggingface_hub import HfApi
            HfApi(token=tok).upload_file(path_or_fileobj=str(path), path_in_repo=f"results/{path.name}",
                                         repo_id=repo, repo_type="dataset")
        except Exception as e:  # persistence must never fail a benchmark job
            print("persist failed:", e)


def restore():
    repo, tok = os.environ.get("RESULTS_REPO"), os.environ.get("HF_TOKEN")
    if repo and tok:
        try:
            from huggingface_hub import snapshot_download
            snapshot_download(repo, repo_type="dataset", token=tok, local_dir=str(ROOT), allow_patterns=["results/*.json"])
        except Exception as e:
            print("restore skipped:", e)


restore()


# ---------------------------------------------------------------------------------------------- auth + models
def auth(authorization: Optional[str] = Header(None)):
    key = os.environ.get("API_KEY")
    if key and authorization != f"Bearer {key}":
        raise HTTPException(401, "missing or wrong bearer token")


class RunRequest(BaseModel):
    model: str
    threads: int = Field(default_factory=lambda: CPUS, ge=1, le=256)
    limit: int = Field(0, ge=0, description=f"0 = all {DATASET_SIZE} decisions")
    warmup: int = Field(20, ge=0)


# ---------------------------------------------------------------------------------------------- running jobs
def sh(cmd, log, on_line=None):
    """Run a command, streaming its merged output line by line. on_line(line) -> True consumes the line (not logged)."""
    log.append("$ " + " ".join(cmd))
    p = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    for line in p.stdout:
        line = line.rstrip("\n")
        if on_line and on_line(line):
            continue
        log.append(line[-500:])
        del log[:-300]  # keep the log bounded
    return p.wait()


def run_one(req: RunRequest, job):
    """Fetch, encode and benchmark one model. Returns the result dict, or raises. Does not touch the lock."""
    m, log = REGISTRY[req.model], job["log"]
    job["stage"] = f"{req.model}: fetch"
    if sh(["python3", "tools/fetch.py", req.model], log):
        raise RuntimeError("fetch failed")
    if not (ROOT / "data/encoded" / f"{m['family']}.jsonl").exists():
        job["stage"] = f"{req.model}: encode"
        if sh(["python3", "tools/encode.py", m["family"]], log):
            raise RuntimeError("encode failed")
    job["stage"] = f"{req.model}: run"

    def on_line(line):
        if line.startswith("PROGRESS "):
            job["progress"] = dict(json.loads(line[9:]), updated=time.time())
            return True

    sh(["dotnet", str(DLL), "run", req.model, "--threads", str(req.threads), "--limit", str(req.limit),
        "--warmup", str(req.warmup)], log, on_line)
    f = ROOT / "results" / f"{req.model}.t{req.threads}.json"
    if not f.exists():
        raise RuntimeError("no result file produced")
    persist(f)
    result = json.loads(f.read_text())
    if result.get("state") != "Scored":
        raise RuntimeError(result.get("error") or "run failed")
    return result


def start_job(request: dict, reqs: list):
    """Take the single job lock (409 if busy) and run `reqs` sequentially on a background thread."""
    if not _lock.acquire(blocking=False):
        raise HTTPException(409, "a benchmark job is already running; poll /jobs")
    jid = uuid.uuid4().hex[:12]
    _jobs[jid] = job = dict(id=jid, request=request, status="running", stage="queued", started=time.time(), log=[],
                            result=[], errors={}, progress=None, queue=[r.model for r in reqs], current=None)

    def go():
        try:
            for r in reqs:
                job["current"], job["progress"] = r.model, None
                try:
                    job["result"].append(run_one(r, job))
                except Exception as e:  # one failing model must not stop the others
                    job["errors"][r.model] = str(e)
            job["status"] = "done" if not job["errors"] else ("failed" if not job["result"] else "partial")
        finally:
            job["stage"], job["current"], job["finished"] = None, None, time.time()
            _lock.release()

    threading.Thread(target=go, daemon=True).start()
    return {"job": jid, "poll": f"/jobs/{jid}", "events": f"/jobs/{jid}/events"}


# ---------------------------------------------------------------------------------------------- progress + ETA
def prior_seconds(model: str, threads: int, items: int):
    """Estimate for a queued model from an earlier result (same model + threads); None if there is none."""
    f = ROOT / "results" / f"{model}.t{threads}.json"
    if not f.exists():
        return None
    r = json.loads(f.read_text())
    return r.get("loadSeconds", 0) + r.get("meanMs", 0) / 1000 * items


def snapshot(job):
    """Job state + ETA estimates. Everything under "eta" is an extrapolation, not a measurement."""
    pr = job.get("progress")
    out = {k: job.get(k) for k in ("id", "status", "stage", "current", "started", "finished", "errors")}
    out["elapsed_s"] = round((job.get("finished") or time.time()) - job["started"], 1)
    out["done_models"] = [r["id"] for r in job["result"]]
    out["progress"] = pr
    eta = {"basis": "current model: remaining decisions x mean latency so far; queued models: an earlier result for the same "
                    "model+threads (otherwise listed in queued_unknown)",
           "current_s": None, "queued_s": 0.0, "queued_unknown": []}
    if job["status"] == "running":
        if pr and pr["phase"] == "measure" and pr["done"] > 0:
            eta["current_s"] = round((pr["total"] - pr["done"]) * pr["meanMs"] / 1000, 1)
            out["percent"] = round(100 * pr["done"] / pr["total"], 1)
        threads = job["request"].get("threads")
        n = job["request"].get("limit") or DATASET_SIZE
        cur = job.get("current")
        queued = job["queue"][job["queue"].index(cur) + 1:] if cur in job["queue"] else []
        for m in queued:
            est = prior_seconds(m, threads, n) if threads else None
            if est is None:
                eta["queued_unknown"].append(m)
            else:
                eta["queued_s"] += est
        eta["queued_s"] = round(eta["queued_s"], 1)
        eta["note"] = "excludes model download and dataset encoding time, which are not measured"
    out["eta"] = eta
    return out


async def sse(job):
    last, idle = None, 0
    while True:
        snap = snapshot(job)
        data = json.dumps(snap)
        if data != last:
            yield f"event: progress\ndata: {data}\n\n"
            last, idle = data, 0
        elif idle >= 15:  # keep proxies from closing an idle stream
            yield ": keep-alive\n\n"
            idle = 0
        if job["status"] != "running":
            yield f"event: done\ndata: {data}\n\n"
            return
        idle += 1
        await asyncio.sleep(1)


SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"}

PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>FindAJev ranking</title>
<style>body{font:15px system-ui;margin:2rem auto;max-width:70rem;padding:0 1rem}pre{overflow:auto;background:#8881;padding:1rem;border-radius:6px}</style>
<h1>FindAJev</h1><p>CPU ranking of typed-decision models on this Space. <a href=/docs>API</a> · <a href=/results>raw results</a> · <a href=/jobs>jobs</a></p>
<pre id=s>idle</pre><pre id=r>loading…</pre>
<script>
const load=()=>fetch('/ranking').then(r=>r.json()).then(j=>r.textContent=j.markdown||'no results yet');load();
const es=new EventSource('/events');
es.addEventListener('progress',e=>{const d=JSON.parse(e.data),p=d.progress||{};
 s.textContent=`${d.status} ${d.current||''} ${p.phase||''} ${p.done||0}/${p.total||0} ${d.percent??''}%  ETA current ${d.eta.current_s??'?'}s, queued ${d.eta.queued_s}s (+unknown: ${d.eta.queued_unknown.join(', ')||'none'})`});
es.addEventListener('done',e=>{es.close();load()});
</script>"""


# ---------------------------------------------------------------------------------------------- endpoints
@app.get("/", response_class=HTMLResponse)
def root():
    return PAGE


@app.get("/info")
def info():
    return {"name": "FindAJev", "cpus": CPUS, "cpu_detail": cpu_info(), "models": list(REGISTRY), "docs": "/docs"}


@app.get("/models")
def models():
    return [dict(m, fetched=(ROOT / m["onnx"]).exists()) for m in REGISTRY.values()]


@app.post("/run", status_code=202, dependencies=[Depends(auth)])
def run(req: RunRequest):
    if req.model not in REGISTRY:
        raise HTTPException(404, f"unknown model {req.model}")
    return start_job(req.model_dump(), [req])


@app.post("/run-all", status_code=202, dependencies=[Depends(auth)])
def run_all(threads: int = CPUS, limit: int = 0, warmup: int = 20, skip_done: bool = False):
    """Benchmark every registry model sequentially; follow the returned job via its SSE stream.
    skip_done=true skips models that already have a Scored result for the same threads and decision count."""
    def done(m):
        f = ROOT / "results" / f"{m}.t{threads}.json"
        if not f.exists():
            return False
        r = json.loads(f.read_text())
        return r.get("state") == "Scored" and r.get("items") == (limit or DATASET_SIZE)
    todo = [m for m in REGISTRY if not (skip_done and done(m))]
    if not todo:
        raise HTTPException(200, "nothing to do: every model already has a result")
    return start_job(dict(all=True, threads=threads, limit=limit, skip_done=skip_done),
                     [RunRequest(model=m, threads=threads, limit=limit, warmup=warmup) for m in todo])


@app.get("/jobs")
def jobs():
    return [{k: j[k] for k in ("id", "status", "stage", "request")} for j in _jobs.values()]


@app.get("/jobs/{jid}")
def job(jid: str):
    if jid not in _jobs:
        raise HTTPException(404, "unknown job")
    return _jobs[jid]


@app.get("/jobs/{jid}/events")
def job_events(jid: str):
    """Server-Sent Events: `progress` snapshots whenever they change, then one `done`."""
    if jid not in _jobs:
        raise HTTPException(404, "unknown job")
    return StreamingResponse(sse(_jobs[jid]), media_type="text/event-stream", headers=SSE_HEADERS)


@app.get("/events")
def latest_events():
    """SSE for the most recent job. When no job has started, emits one `done` with status idle."""
    if not _jobs:
        idle = dict(id=None, status="idle", stage=None, current=None, started=time.time(), finished=time.time(), errors={},
                    result=[], progress=None, queue=[], request={})
        return StreamingResponse(sse(idle), media_type="text/event-stream", headers=SSE_HEADERS)
    return StreamingResponse(sse(list(_jobs.values())[-1]), media_type="text/event-stream", headers=SSE_HEADERS)


@app.get("/results")
def results():
    return [json.loads(f.read_text()) for f in sorted((ROOT / "results").glob("*.json"))]


@app.get("/ranking")
def ranking():
    try:
        p = subprocess.run(["dotnet", str(DLL), "rank"], cwd=ROOT, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        raise HTTPException(503, f"ranking unavailable: {e}")
    return {"markdown": p.stdout}
