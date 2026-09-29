"""HTTP API around the FindAJev harness. One benchmark job at a time (parallel jobs would corrupt each other's timings).

If the API_KEY environment variable (a Space secret) is set, the endpoints that start work (POST /run, /run-all) require
`Authorization: Bearer <API_KEY>`. Reads (leaderboard, results, jobs, SSE, dashboard) are public.

Dashboard: a Gradio app (ui.py) is mounted at "/" and is the live view. The harness writes `EVENT {json}` lines (state-machine graphs, per-test transitions, Cedar decisions). This server folds
them into a `Live` object and streams it to the dashboard over SSE (`/live`), with per-test detail at `/tests/{i}`.

Results are kept in results/ and, when HF_TOKEN + RESULTS_REPO (a dataset repo id) are set, mirrored to that dataset so they
survive Space restarts.
"""
import asyncio, base64, collections, json, os, subprocess, threading, time, uuid
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

ROOT = Path(os.environ.get("FINDAJEV_ROOT", Path(__file__).parent)).resolve()
DLL = ROOT / "bin" / "FindAJev.Bench.dll"
REGISTRY = {m["id"]: m for m in json.loads((ROOT / "models.json").read_text())}
DATASET_SIZE = 2600  # single-label decisions in fastino/fast-decisions (see tools/encode.py)

# Leaf states of the per-test machine, in the order used for the compact per-test state codes sent to the dashboard.
TEST_STATES = ["Queued", "Inferring", "Enforcing", "Correct", "WrongButSafe", "Overblocked", "Unsafe", "Misclassified", "Errored"]
CODE = {s: i for i, s in enumerate(TEST_STATES)}


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

app = FastAPI(title="FindAJev", description="CPU benchmark of typed-decision models, with Cedar policy enforcement")
_lock = threading.Lock()
_jobs: dict[str, dict] = {}


# ---------------------------------------------------------------------------------------------- live state
class Live:
    """Everything the dashboard shows, folded from harness events. Thread-safe: ingest() runs on the job thread, readers on SSE tasks."""

    def __init__(self):
        self.lock = threading.Lock()
        self.epoch = 0                      # bumps on every new model (new plan), telling clients to re-init
        self.reset(None)

    def reset(self, model):
        self.model = model
        self.graphs = getattr(self, "graphs", {})
        self.run_state = "Pending"
        self.run_policy = []                # Cedar decisions for FetchModel / RunModel
        self.plan = None
        self.states = bytearray()
        self.counts = collections.Counter()
        self.edges = collections.Counter()  # "From>To" -> transitions observed
        self.log = []                       # (test index, state code) in arrival order; clients keep a cursor into it
        self.verdicts = {}
        self.policy_hits = {}               # policy id -> {"allow": n, "deny": n} over pred-side decisions
        self.recent = collections.deque(maxlen=40)   # latest Unsafe / Overblocked / Errored verdict summaries
        self.recent_seq = 0

    def ingest(self, ev):
        kind = ev.get("e")
        with self.lock:
            if kind == "graph":
                self.graphs[ev["name"]] = ev
            elif kind == "run":
                self.run_state = ev["to"]
                self.model = ev["model"]
            elif kind == "policy":
                self.run_policy.append({k: ev[k] for k in ("action", "allow", "by")})
            elif kind == "plan":
                n = ev["tests"]
                self.epoch += 1
                self.plan = {"tests": n, "suites": ev["suites"], "domains": ev["domains"],
                         "packDomains": ev.get("packDomains", {}), "keys": ev["keys"]}
                self.states = bytearray(n)      # all zero == Queued
                self.counts = collections.Counter({"Queued": n})
                self.edges, self.log, self.verdicts = collections.Counter(), [], {}
                self.policy_hits, self.recent = {}, collections.deque(maxlen=40)
            elif kind == "test" and self.plan:
                i, frm, to = ev["i"], ev["from"], ev["to"]
                self.states[i] = CODE[to]
                self.counts[frm] -= 1
                self.counts[to] += 1
                self.edges[f"{frm}>{to}"] += 1
                self.log.append((i, CODE[to]))
            elif kind == "verdict":
                self.verdicts[ev["i"]] = ev
                for a in ev.get("acts", []):
                    for pid in a["by"]:
                        h = self.policy_hits.setdefault(pid, {"allow": 0, "deny": 0})
                        h["allow" if a["p"] else "deny"] += 1
                if ev["to"] in ("Unsafe", "Overblocked", "Errored"):
                    self.recent_seq += 1
                    self.recent.append({"seq": self.recent_seq, "i": ev["i"], "k": ev["k"], "to": ev["to"],
                                        "heads": ev.get("heads"), "acts": ev.get("acts"), "err": ev.get("err")})

    def view(self):
        """A consistent copy of everything the Gradio dashboard renders."""
        with self.lock:
            return {"model": self.model, "runState": self.run_state, "runPolicy": list(self.run_policy), "graphs": dict(self.graphs),
                    "plan": self.plan, "states": bytes(self.states), "counts": dict(self.counts), "edges": dict(self.edges),
                    "policyHits": {k: dict(v) for k, v in self.policy_hits.items()}, "recent": list(self.recent)}

    def init_payload(self):
        with self.lock:
            return {"epoch": self.epoch, "model": self.model, "runState": self.run_state, "runPolicy": self.run_policy,
                    "graphs": self.graphs, "plan": self.plan, "counts": dict(self.counts), "edges": dict(self.edges),
                    "states": base64.b64encode(bytes(self.states)).decode(), "cursor": len(self.log),
                    "policyHits": self.policy_hits, "recent": list(self.recent), "codes": TEST_STATES}

    def delta(self, cursor, recent_seq):
        with self.lock:
            new = self.log[cursor:]
            return {"epoch": self.epoch, "model": self.model, "runState": self.run_state, "runPolicy": self.run_policy,
                    "counts": dict(self.counts), "edges": dict(self.edges), "changes": new, "cursor": len(self.log),
                    "policyHits": self.policy_hits, "recent": [r for r in self.recent if r["seq"] > recent_seq]}


LIVE = Live()


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
    """Cedar pre-flight, fetch, encode and benchmark one model. Returns the result dict, or raises. Does not touch the lock."""
    m, log = REGISTRY[req.model], job["log"]
    LIVE.reset(req.model)

    job["stage"] = f"{req.model}: policy check"
    chk = subprocess.run(["dotnet", str(DLL), "check", req.model, "--threads", str(req.threads), "--cpus", str(CPUS)],
                         cwd=ROOT, capture_output=True, text=True)
    try:
        decisions = json.loads(chk.stdout.strip().splitlines()[-1])["decisions"]
        for d in decisions:
            LIVE.ingest({"e": "policy", "action": d["action"], "allow": d["allow"], "by": d["by"]})
    except (ValueError, IndexError, KeyError):
        decisions = []
    if chk.returncode != 0:
        why = [f'{d["action"]} denied ({", ".join(d["by"]) or "no permit policy matched"})' for d in decisions if not d["allow"]]
        raise RuntimeError("Cedar denied before download: " + ("; ".join(why) or chk.stderr[-300:] or "check failed"))

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
        if line.startswith("EVENT "):
            try:
                LIVE.ingest(json.loads(line[6:]))
            except (ValueError, KeyError) as e:
                log.append(f"bad event: {e}")
            return True

    sh(["dotnet", str(DLL), "run", req.model, "--threads", str(req.threads), "--cpus", str(CPUS), "--limit", str(req.limit),
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
    return {"job": jid, "poll": f"/jobs/{jid}", "events": f"/jobs/{jid}/events", "live": "/live", "dashboard": "/"}


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


async def live_stream():
    """Dashboard stream: `init` (full state) then `delta` (only what changed) at ~4 Hz; re-inits when a new model starts."""
    init = LIVE.init_payload()
    epoch, cursor, recent_seq = init["epoch"], init["cursor"], LIVE.recent_seq
    yield f"event: init\ndata: {json.dumps(init)}\n\n"
    idle = 0
    while True:
        await asyncio.sleep(0.25)
        job = list(_jobs.values())[-1] if _jobs else None
        if LIVE.epoch != epoch:
            init = LIVE.init_payload()
            epoch, cursor, recent_seq = init["epoch"], init["cursor"], LIVE.recent_seq
            yield f"event: init\ndata: {json.dumps(init)}\n\n"
            continue
        d = LIVE.delta(cursor, recent_seq)
        cursor = d["cursor"]
        if d["recent"]:
            recent_seq = d["recent"][-1]["seq"]
        d["job"] = snapshot(job) if job else None
        payload = json.dumps(d)
        if d["changes"] or d["recent"] or idle >= 4:   # heartbeat every ~1 s carries progress / ETA even when no test moved
            yield f"event: delta\ndata: {payload}\n\n"
            idle = 0
        else:
            idle += 1


SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"}

# ---------------------------------------------------------------------------------------------- endpoints
@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    from fastapi import Response
    return Response(status_code=204)


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
    """Benchmark every registry model sequentially; follow the returned job via /live or its SSE stream.
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


@app.get("/live")
def live():
    """Dashboard SSE: per-test state machine transitions, Cedar decisions, progress and ETA."""
    return StreamingResponse(live_stream(), media_type="text/event-stream", headers=SSE_HEADERS)


@app.get("/tests/{i}")
def test_detail(i: int):
    """Verdict detail for one test of the current model: labels (model vs gold) and every Cedar decision with policy ids."""
    with LIVE.lock:
        v = LIVE.verdicts.get(i)
    if v is None:
        raise HTTPException(404, "no verdict for this test (yet)")
    return v


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


# ---------------------------------------------------------------------------------------------- Gradio dashboard at "/"
# Mounted last so every API route above keeps priority.
import sys
import gradio as gr
import ui

app = gr.mount_gradio_app(app, ui.build_ui(sys.modules[__name__]), path="/")
