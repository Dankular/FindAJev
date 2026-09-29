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
DATASET_SIZE = 2600  # fallback estimate only (fast-decisions single-label decisions); real counts come from the encoded files


def load_json(rel):
    return json.loads((ROOT / rel).read_text())


def suite_registry():
    """The declarative suites (suites.json): what can be run, dynamically."""
    return load_json("suites.json")["suites"]


def policy_params():
    """Default Cedar policy parameters (policies/params.json)."""
    return {k: v for k, v in load_json("policies/params.json").items() if not k.startswith("_")}


def count_items(family, suites=None, limit=0):
    """Number of model calls a run of `family` will make: lines in the encoded files of the selected suites (limit applies per file)."""
    total = 0
    for s in suite_registry():
        if suites and s["id"] not in suites:
            continue
        f = ROOT / "data/encoded" / s["file"].format(family=family)
        if f.exists():
            with f.open() as fh:
                n = sum(1 for _ in fh)
            total += min(n, limit) if limit else n
    return total or None


def variant_of(suites, params):
    """Same string the harness computes: non-default selections that make a result comparable only to like runs ("" = default)."""
    parts = []
    if suites:
        parts.append("suites=" + ",".join(sorted(suites)))
    parts += [f"{k}={v}" for k, v in sorted((params or {}).items())]
    return ";".join(parts)


def result_suffix(variant):
    import hashlib
    return "" if not variant else "." + hashlib.sha1(variant.encode()).hexdigest()[:8]


def normalize(suites, params):
    """Drop selections equal to the defaults so a 'select everything' run stays comparable with a plain run."""
    all_ids = {x["id"] for x in suite_registry()}
    suites = None if not suites or set(suites) >= all_ids else sorted(set(suites))
    defaults = policy_params()
    params = {k: int(v) for k, v in (params or {}).items() if k not in defaults or int(v) != defaults[k]}
    return suites, params


CHECKS = {"updated": None, "cedar": None, "results": {}}   # latest Cedar check outcomes, shown on the dashboard

# Leaf states of the per-test machine, in the order used for the compact per-test state codes sent to the dashboard.
TEST_STATES = ["Queued", "Inferring", "Auditing", "Enforcing", "Correct", "WrongButSafe", "Overblocked", "Unsafe", "Misclassified", "Errored"]
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
        self.flagged = bytearray()          # 1 where the label-free Cedar oracles flagged the test
        self.oracle = {}                    # rule id -> {"flagged", "tp", "gold"} folded from verdict events

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
                self.flagged, self.oracle = bytearray(n), {}
            elif kind == "test" and self.plan:
                i, frm, to = ev["i"], ev["from"], ev["to"]
                self.states[i] = CODE[to]
                self.counts[frm] -= 1
                self.counts[to] += 1
                self.edges[f"{frm}>{to}"] += 1
                self.log.append((i, CODE[to]))
            elif kind == "verdict":
                self.verdicts[ev["i"]] = ev
                if ev.get("flags"):
                    self.flagged[ev["i"]] = 1
                for rule in ev.get("flags", []):
                    o = self.oracle.setdefault(rule, {"flagged": 0, "tp": 0, "gold": 0})
                    o["flagged"] += 1
                    o["tp"] += 1 if ev.get("wrong") else 0
                for rule in ev.get("goldFlags", []):
                    self.oracle.setdefault(rule, {"flagged": 0, "tp": 0, "gold": 0})["gold"] += 1
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
                    "policyHits": {k: dict(v) for k, v in self.policy_hits.items()}, "recent": list(self.recent),
                    "flagged": bytes(self.flagged), "oracle": {k: dict(v) for k, v in self.oracle.items()}}

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
    suites: Optional[list[str]] = Field(None, description="suite ids from GET /suites (default: all)")
    params: dict[str, int] = Field(default_factory=dict, description="Cedar policy parameter overrides from GET /params")


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


def run_checks(ids, log=None):
    """Run Cedar checks through the harness (`cedar-suite --json`); updates CHECKS. Returns the outcome dicts."""
    p = subprocess.run(["dotnet", str(DLL), "cedar-suite", "--checks", ",".join(ids), "--json"], cwd=ROOT, capture_output=True, text=True)
    try:
        out = json.loads(p.stdout)             # the harness prints one (indented) JSON document
    except ValueError:
        raise RuntimeError("cedar-suite produced no result: " + (p.stderr or p.stdout)[-300:])
    CHECKS["updated"], CHECKS["cedar"] = time.time(), f'{out["cedar"]} (language {out["language"]})'
    for c in out["checks"]:
        CHECKS["results"][c["id"]] = c
    return out["checks"]


GATE = ["policy-validate", "golden-cases", "conformance"]   # fast hard checks that must pass before any model time is spent


def run_one(req: RunRequest, job):
    """Cedar pre-flight, fetch, encode and benchmark one model. Returns the result dict, or raises. Does not touch the lock."""
    m, log = REGISTRY[req.model], job["log"]
    LIVE.reset(req.model)
    suites, params = normalize(req.suites, req.params)

    if not job.get("gated"):
        job["stage"] = "cedar gate"
        bad = [c for c in run_checks(GATE, log) if not c["passed"]]
        if bad:
            raise RuntimeError("Cedar gate failed: " + "; ".join(f'{c["id"]}: {c["summary"]}' for c in bad))
        job["gated"] = True

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
    job["stage"] = f"{req.model}: encode"       # idempotent: encodes only the suite files that are missing
    if sh(["python3", "tools/encode.py", m["family"]], log):
        raise RuntimeError("encode failed")
    job["stage"] = f"{req.model}: run"
    result_file = []

    def on_line(line):
        if line.startswith("PROGRESS "):
            job["progress"] = dict(json.loads(line[9:]), updated=time.time())
            return True
        if line.startswith("RESULT_FILE "):
            result_file.append(line[12:].strip())
            return True
        if line.startswith("EVENT "):
            try:
                LIVE.ingest(json.loads(line[6:]))
            except (ValueError, KeyError) as e:
                log.append(f"bad event: {e}")
            return True

    cmd = ["dotnet", str(DLL), "run", req.model, "--threads", str(req.threads), "--cpus", str(CPUS), "--limit", str(req.limit),
           "--warmup", str(req.warmup)]
    if suites:
        cmd += ["--suites", ",".join(suites)]
    for k, v in sorted(params.items()):
        cmd += ["--param", f"{k}={v}"]
    sh(cmd, log, on_line)
    f = ROOT / result_file[-1] if result_file else ROOT / "results" / f"{req.model}.t{req.threads}.json"
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
        lim = job["request"].get("limit") or 0
        cur = job.get("current")
        queued = job["queue"][job["queue"].index(cur) + 1:] if cur in job["queue"] else []
        for m in queued:
            est = prior_seconds(m, threads, count_items(REGISTRY[m]["family"], job["request"].get("suites"), lim) or DATASET_SIZE) if threads else None
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
    return start_runs([req.model], req.threads, req.limit, req.warmup, False, req.suites, req.params)


@app.post("/run-all", status_code=202, dependencies=[Depends(auth)])
def run_all(threads: int = CPUS, limit: int = 0, warmup: int = 20, skip_done: bool = False, suites: str = "", params: str = ""):
    """Benchmark every registry model sequentially; follow the returned job via /live or its SSE stream.
    suites: comma-separated suite ids (GET /suites); params: comma-separated name=value Cedar overrides (GET /params);
    skip_done=true skips models that already have a Scored result for the same threads, suites, parameters and decision count."""
    sel = [x for x in suites.split(",") if x] or None
    ov = dict(kv.split("=", 1) for kv in params.split(",") if kv)
    return start_runs(list(REGISTRY), threads, limit, warmup, skip_done, sel, {k: int(v) for k, v in ov.items()})


def start_runs(models, threads, limit, warmup, skip_done, suites, params):
    check_selection(suites, params)
    nsuites, nparams = normalize(suites, params)
    suffix = result_suffix(variant_of(nsuites, nparams))

    def done(m):
        f = ROOT / "results" / f"{m}.t{threads}{suffix}.json"
        if not f.exists():
            return False
        r = json.loads(f.read_text())
        want = count_items(REGISTRY[m]["family"], nsuites, limit)
        return r.get("state") == "Scored" and want is not None and r.get("items") == want
    todo = [m for m in models if not (skip_done and done(m))]
    if not todo:
        raise HTTPException(200, "nothing to do: every selected model already has a result for this selection")
    return start_job(dict(all=len(models) > 1, threads=threads, limit=limit, skip_done=skip_done, suites=nsuites, params=nparams),
                     [RunRequest(model=m, threads=threads, limit=limit, warmup=warmup, suites=suites, params=params) for m in todo])


def check_selection(suites, params):
    """Reject unknown suites / parameters up front with the list of valid ones (the harness would reject them too, but only after a download)."""
    known = {x["id"] for x in suite_registry()}
    bad = [x for x in (suites or []) if x not in known]
    if bad:
        raise HTTPException(422, f"unknown suite(s) {bad}; available: {sorted(known)}")
    defaults = policy_params()
    badp = [k for k in (params or {}) if k not in defaults]
    if badp:
        raise HTTPException(422, f"unknown policy parameter(s) {badp}; available: {sorted(defaults)}")


@app.get("/suites")
def suites_endpoint():
    """The declarative suite registry, with which encoded files exist per model family."""
    out = []
    for s in suite_registry():
        enc = {fam: (ROOT / "data/encoded" / s["file"].format(family=fam)).exists() for fam in ("gliner", "julia", "laya")}
        out.append(dict(s, encoded=enc))
    return out


@app.get("/params")
def params_endpoint():
    """Cedar policy parameters and their defaults; override per run with `params`."""
    return policy_params()


@app.get("/checks")
def checks_endpoint():
    """Available Cedar checks (from the harness, so new checks appear automatically) and the latest outcome of each."""
    p = subprocess.run(["dotnet", str(DLL), "cedar-suite", "--list"], cwd=ROOT, capture_output=True, text=True)
    try:
        listing = json.loads(p.stdout)
    except ValueError:
        raise HTTPException(503, "harness unavailable: " + p.stderr[-200:])
    return {"cedar": CHECKS["cedar"], "updated": CHECKS["updated"], "checks": [dict(c, last=CHECKS["results"].get(c["id"])) for c in listing]}


class CheckRequest(BaseModel):
    checks: Optional[list[str]] = Field(None, description="check ids from GET /checks (default: all)")


@app.post("/checks", status_code=202, dependencies=[Depends(auth)])
def run_checks_endpoint(req: CheckRequest):
    """Run Cedar checks as a job (they share the single job lock: they use CPU, so never run beside a benchmark)."""
    if not _lock.acquire(blocking=False):
        raise HTTPException(409, "a job is already running; poll /jobs")
    known = [c["id"] for c in checks_endpoint()["checks"]]
    ids = req.checks or known
    bad = [i for i in ids if i not in known]
    if bad:
        _lock.release()
        raise HTTPException(422, f"unknown check(s) {bad}; available: {known}")
    jid = uuid.uuid4().hex[:12]
    _jobs[jid] = job = dict(id=jid, request=dict(checks=ids), status="running", stage="cedar checks", started=time.time(), log=[],
                            result=[], errors={}, progress=None, queue=ids, current=None)

    def go():
        try:
            job["result"] = run_checks(ids)
            failed = [c["id"] for c in job["result"] if not c["passed"] and c["hard"]]
            job["status"] = "failed" if failed else "done"
            if failed:
                job["errors"] = {"hard checks failed": ", ".join(failed)}
        except Exception as e:
            job["status"], job["errors"] = "failed", {"cedar-suite": str(e)}
        finally:
            job["stage"], job["finished"] = None, time.time()
            _lock.release()

    threading.Thread(target=go, daemon=True).start()
    return {"job": jid, "poll": f"/jobs/{jid}"}


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
