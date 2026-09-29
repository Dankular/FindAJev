"""Gradio dashboard: the live Stateless state-machine graph is the centrepiece.

Everything shown is folded from the harness's EVENT lines (see server.Live) and rendered server-side on a timer, so there is
no custom JavaScript to keep in sync. The graph itself is drawn from the machine's own GetInfo() export, not hard-coded.
"""
import io, json
from html import escape
from pathlib import Path

import gradio as gr
import numpy as np
from fastapi import HTTPException
from PIL import Image

COLORS = {"Queued": "#8b93a1", "Inferring": "#4f8cff", "Auditing": "#38bdf8", "Enforcing": "#a78bfa", "Correct": "#34d399", "WrongButSafe": "#2dd4bf",
          "Overblocked": "#fbbf24", "Unsafe": "#f87171", "Misclassified": "#fb923c", "Errored": "#f472b6"}
STATES = list(COLORS)   # order must match server.TEST_STATES (the compact per-test state codes)
GROUPS = [("activated", ["Inferring", "Auditing", "Enforcing"]), ("passed", ["Correct", "WrongButSafe"]),
          ("failed", ["Overblocked", "Unsafe", "Misclassified", "Errored"]), ("queued", ["Queued"])]
CRUMBS = ["Pending", "ModelReady", "SessionLoaded", "WarmedUp", "Measured", "Scored"]

# Hand-placed layout for the known test machine. Unknown states fall into a spare row so a machine change never hides a node.
POS = {"Queued": (60, 190), "Inferring": (200, 190), "Auditing": (340, 190), "Enforcing": (480, 190), "Correct": (670, 50), "WrongButSafe": (670, 120),
       "Overblocked": (670, 215), "Unsafe": (670, 285), "Misclassified": (670, 350), "Errored": (830, 285)}
W, H = 118, 46
TXT = "var(--body-text-color)"
MUT = "var(--body-text-color-subdued)"


def graph_svg(graph, counts, edges):
    if not graph:
        return f'<div style="padding:24px;color:{MUT}">Waiting for the harness to report its state machine — start a run from the <b>Run</b> tab.</div>'
    states = graph["states"]
    pos, spare = dict(POS), 0
    for s in states:
        if s["id"] not in pos and not any(x["parent"] == s["id"] for x in states):
            pos[s["id"]] = (70 + 120 * spare, 400); spare += 1
    boxes = {}
    for cid in {s["parent"] for s in states if s["parent"]}:
        kids = [k for k in states if k["parent"] == cid and k["id"] in pos]
        if kids:
            xs, ys = [pos[k["id"]][0] for k in kids], [pos[k["id"]][1] for k in kids]
            boxes[cid] = (min(xs) - W / 2 - 14, min(ys) - H / 2 - 24, max(xs) + W / 2 + 14, max(ys) + H / 2 + 14)

    def centre(i):
        if i in pos: return pos[i]
        b = boxes[i]; return ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)

    def anchor(i, toward):
        cx, cy = centre(i)
        w, h = (W, H) if i in pos else (boxes[i][2] - boxes[i][0], boxes[i][3] - boxes[i][1])
        dx, dy = toward[0] - cx, toward[1] - cy
        k = min((w / 2) / max(abs(dx), 1e-9), (h / 2) / max(abs(dy), 1e-9))
        return cx + dx * k, cy + dy * k

    out = ['<svg viewBox="0 0 900 430" style="width:100%;height:auto" xmlns="http://www.w3.org/2000/svg">',
           f'<defs><marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
           f'<path d="M0 0L10 5L0 10z" fill="{MUT}"/></marker></defs>']
    for cid, b in boxes.items():
        label = "activated" if cid == "Active" else cid.lower()
        out.append(f'<rect x="{b[0]}" y="{b[1]}" width="{b[2]-b[0]}" height="{b[3]-b[1]}" rx="12" fill="none" stroke="{MUT}" stroke-opacity=".5" stroke-dasharray="4 4"/>'
                   f'<text x="{b[0]+10}" y="{b[1]+15}" fill="{MUT}" font-size="11" letter-spacing=".06em">{label.upper()}</text>')
    mx = max([1, *edges.values()])
    import math
    for e in graph["edges"]:
        if (e["from"] not in pos and e["from"] not in boxes) or (e["to"] not in pos and e["to"] not in boxes):
            continue
        a, b = anchor(e["from"], centre(e["to"])), anchor(e["to"], centre(e["from"]))
        c = edges.get(f'{e["from"]}>{e["to"]}', 0)
        if not c and e["from"] in boxes:   # edge declared on a superstate: count its children's transitions
            c = sum(edges.get(f'{k["id"]}>{e["to"]}', 0) for k in states if k["parent"] == e["from"])
        mxp, myp = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2 - (0 if a[0] == b[0] else 8)
        width = 1 + 3 * math.log2(1 + c) / math.log2(1 + mx)
        tip = escape(f'{e["trigger"]}{" — " + e["guard"] if e.get("guard") else ""}  ({c} observed)')
        out.append(f'<path d="M{a[0]:.0f} {a[1]:.0f} Q{mxp:.0f} {myp:.0f} {b[0]:.0f} {b[1]:.0f}" fill="none" stroke="{MUT}" '
                   f'stroke-width="{width:.1f}" opacity="{0.9 if c else 0.25}" marker-end="url(#ah)"><title>{tip}</title></path>')
    for s in states:
        if s["id"] not in pos or s["id"] in boxes:
            continue
        x, y = pos[s["id"]]; col = COLORS.get(s["id"], MUT)
        out.append(f'<g transform="translate({x-W/2},{y-H/2})"><rect width="{W}" height="{H}" rx="9" fill="{col}" fill-opacity=".18" stroke="{col}" stroke-width="2"/>'
                   f'<text x="10" y="19" font-size="13" fill="{TXT}">{s["id"]}</text>'
                   f'<text x="10" y="38" font-size="16" font-weight="700" fill="{TXT}">{counts.get(s["id"], 0)}</text></g>')
    out.append("</svg>")
    return "".join(out)


def dataset_html(v, registry):
    """One card per registered dataset with live progress; the dataset being processed right now is highlighted.
    A test's suite comes from its domain's policy pack (packDomains) or is 'classification'; the registry entry whose `provides`
    lists that suite is its dataset. Tests run in file order (suite by suite), so the active dataset is the one holding the furthest
    started test."""
    plan, states = v["plan"], v["states"]
    if not plan:
        return f'<div style="color:{MUT}">No run yet.</div>'
    packs = plan.get("packDomains", {})
    by_suite = {sn: e for e in registry for sn in e["provides"]}
    ds_of = []
    for k in plan["keys"]:
        dom = k.split("|")[1]
        suite = packs.get(dom) if isinstance(packs.get(dom), str) else ("automation" if dom in packs else "classification")
        ds_of.append(by_suite.get(suite, {}).get("id", "?"))
    info = {e["id"]: e for e in registry}
    tot, done = {}, {}
    for i, d in enumerate(ds_of):
        tot[d] = tot.get(d, 0) + 1
        if states[i] not in (0, 1, 2, 3):     # not Queued / Inferring / Auditing / Enforcing
            done[d] = done.get(d, 0) + 1
    started = [i for i in range(len(states)) if states[i] != 0]
    cur = ds_of[max(started)] if started else ds_of[0]
    measuring = v["runState"] == "Measured"        # the run machine's state while tests are being processed
    cards = []
    for e in registry:
        name = e["id"]
        if name not in tot:
            continue
        n, dn = tot[name], done.get(name, 0)
        is_active = measuring and name == cur
        state = "processing now" if is_active else ("done" if dn == n else "waiting")
        border = "#4f8cff" if is_active else "var(--border-color-primary)"
        glow = "box-shadow:0 0 0 3px #4f8cff55;" if is_active else ""
        badge = {"processing now": "background:#4f8cff;color:#fff", "done": "background:#34d399;color:#fff", "waiting": f"border:1px solid {MUT};color:{MUT}"}[state]
        cards.append(
            f'<div style="flex:1;min-width:250px;border:2px solid {border};{glow}border-radius:10px;padding:10px 12px;opacity:{1 if is_active or dn == n else .6}">'
            f'<div style="display:flex;justify-content:space-between;align-items:center"><b>{escape(e["title"])}</b>'
            f'<span style="padding:1px 8px;border-radius:99px;font-size:12px;{badge}">{state}</span></div>'
            f'<div style="color:{MUT};font-size:12px;margin:3px 0">{escape(e.get("what", ""))}</div>'
            f'<div style="font-size:12px">{" · ".join(f"<a href=https://huggingface.co/datasets/{r.strip()} target=_blank>{escape(r.strip())}</a>" for r in e.get("repo", "").split(",") if r.strip())} · '
            f'suites: <b>{", ".join(e["provides"])}</b> · {escape(e.get("license", ""))}</div>'
            f'<div style="height:6px;background:{MUT}33;border-radius:99px;overflow:hidden;margin:6px 0 2px"><div style="height:100%;width:{100 * dn / n:.0f}%;background:{"#4f8cff" if is_active else "#34d399"}"></div></div>'
            f'<div style="font-size:12px;color:{MUT}">{dn}/{n} tests</div></div>')
    head = (f'<div style="margin:0 0 8px;font-size:15px">Now processing: <b>{escape(info[cur]["title"])}</b> '
            f'<span style="color:{MUT}">({", ".join(info[cur]["provides"])})</span></div>') if measuring and cur in info else ""
    return head + f'<div style="display:flex;flex-wrap:wrap;gap:10px">{"".join(cards)}</div>'


def checks_html(listing, results):
    """Cedar checks: status, summary, findings and the check-specific tables."""
    if not listing:
        return f'<div style="color:{MUT}">Check list unavailable.</div>'
    out = []
    for c in listing:
        r = results.get(c["id"])
        if r is None:
            badge, col = "not run", MUT
        elif r["passed"]:
            badge, col = ("PASS" if c["hard"] else "done"), "#34d399"
        else:
            badge, col = "FAIL", "#f87171"
        body = ""
        if r:
            d = r.get("details")
            if c["id"] == "conformance" and d:
                body += "<table>" + "".join(f'<tr><td>{escape(x["category"])}</td><td>{x["pass"]}/{x["total"]}</td></tr>' for x in d) + "</table>"
            if c["id"] == "properties" and d:
                body += "<table><tr><th>property</th><th>checks</th><th>violations</th></tr>" + "".join(
                    f'<tr><td>{escape(x["property"])}</td><td>{x["checkedCount"]}</td><td style="color:{"#f87171" if x["violations"] else "inherit"}">{x["violations"]}</td></tr>' for x in d["properties"]) + "</table>"
            if c["id"] == "bench" and d:
                body += "<table><tr><th>domain</th><th>policies</th><th>calls</th><th>p50 ms</th><th>p95 ms</th></tr>" + "".join(
                    f'<tr><td>{escape(x["domain"])}</td><td>{x["policies"]}</td><td>{x["calls"]}</td><td>{x["p50Ms"]}</td><td>{x["p95Ms"]}</td></tr>' for x in d) + "</table>"
            if c["id"] == "noise-sweep" and d:
                body += ("<table><tr><th>domain</th><th>suite</th>" + "".join(f"<th>{int(x['rate'] * 100)}% noise: unsafe / overblocked</th>" for x in d[0]["sweep"]) + "</tr>" +
                         "".join(f'<tr><td>{escape(t["domain"])}</td><td>{t["suite"]}</td>' + "".join(f'<td>{x["unsafePct"]}% / {x["overblockedPct"]}%</td>' for x in t["sweep"]) + "</tr>" for t in d) + "</table>")
            if c["id"] == "gold-coverage" and d:
                body += "<details><summary>domain / action outcomes on gold labels</summary><table><tr><th>pair</th><th>allow</th><th>deny</th></tr>" + "".join(
                    f'<tr><td>{escape(x["pair"])}</td><td>{x["allow"]}</td><td>{x["deny"]}</td></tr>' for x in d) + "</table></details>"
            if c["id"] == "curation-sim" and d:
                for st in d:
                    body += (f"<div style='margin-top:6px'><b>{escape(st['set'])}</b></div><table><tr><th>scenario</th><th>admitted</th><th>human</th><th>self</th><th>label noise %</th>"
                             "<th>yield %</th><th>errors caught %</th><th>leaks</th></tr>" + "".join(
                        f'<tr><td>{escape(x["scenario"])}</td><td>{x["admitted"]}</td><td>{x["admittedHuman"]}</td><td>{x["admittedSelf"]}</td><td>{x["labelNoisePct"]}</td>'
                        f'<td>{x["yieldPct"]}</td><td>{x["errorsCaughtPct"]}</td><td>{x["leakNoConsent"] + x["leakNotOpen"] + x["leakPii"] + x["leakAttackSelf"]}</td></tr>' for x in st["rows"]) + "</table>")
            if c["id"] == "promotion-gate" and d:
                body += "<table><tr><th>candidate</th><th>champion</th><th>comparable</th><th>decision</th><th>by</th></tr>" + "".join(
                    f'<tr><td>{escape(x["candidate"])}</td><td>{escape(x["champion"])}</td><td>{x["comparable"]}</td><td>{"allow" if x["allow"] else "deny"}</td><td>{escape(", ".join(x["by"]))}</td></tr>' for x in d) + "</table>"
            if r["findings"]:
                body += "<div style='margin-top:6px'><b>findings</b><ul>" + "".join(f"<li><code>{escape(f)}</code></li>" for f in r["findings"][:15]) + "</ul></div>"
        out.append(
            f'<div style="border:1px solid {MUT}55;border-radius:10px;padding:10px 12px;margin-bottom:10px">'
            f'<div style="display:flex;justify-content:space-between"><b>{escape(c["title"])}</b>'
            f'<span style="padding:1px 9px;border-radius:99px;font-size:12px;background:{col};color:#fff">{badge}{" · gate" if c["hard"] else ""}</span></div>'
            f'<div style="color:{MUT};font-size:12px">{escape(c["description"])}</div>'
            + (f'<div style="margin-top:4px">{escape(r["summary"])} <span style="color:{MUT}">({r["seconds"]}s)</span></div>' if r else "") + body + "</div>")
    return "".join(out)


def status_html(v, job):
    rs = v["runState"] or "Pending"; i = CRUMBS.index(rs) if rs in CRUMBS else -1
    chips = []
    for k, c in enumerate(CRUMBS):
        col = "#f87171" if rs == "Failed" else ("#34d399" if k < i else "#4f8cff" if k == i else MUT)
        fill = "background:" + col + ";color:#fff;" if (k == i and rs != "Failed") else ""
        chips.append(f'<span style="padding:2px 9px;border-radius:99px;border:1px solid {col};color:{col if not fill else "#fff"};{fill}">{c}</span>')
    if rs == "Failed":
        chips.append('<span style="padding:2px 9px;border-radius:99px;background:#f87171;color:#fff">Failed</span>')
    pol = "".join(f'<span style="border:1px solid {MUT};border-radius:8px;padding:2px 8px;margin-right:6px">'
                  f'<b style="color:{"#34d399" if p["allow"] else "#f87171"}">{"✓" if p["allow"] else "✗"}</b> Cedar {p["action"]} '
                  f'<code>{escape(", ".join(p["by"]) or "no permit matched")}</code></span>' for p in v["runPolicy"])
    line = "no job yet"
    if job:
        p, e = job.get("progress") or {}, job.get("eta") or {}
        unk = f" (+ unknown: {', '.join(e['queued_unknown'])})" if e.get("queued_unknown") else ""
        line = (f'{job["status"]} · {job.get("stage") or "idle"} · {p.get("phase", "")} {p.get("done", "")}/{p.get("total", "")} '
                f'({job.get("percent", 0)}%) · mean {p.get("meanMs", "?")} ms · elapsed {round(job["elapsed_s"])}s · '
                f'ETA this model {e.get("current_s", "?")}s · queued {e.get("queued_s", 0)}s{unk} · done: {", ".join(job["done_models"]) or "none"}')
        if job["errors"]:
            line += " · errors: " + " | ".join(f"{k}: {escape(str(x))}" for k, x in job["errors"].items())
    pct = (job or {}).get("percent") or 0
    return (f'<div style="margin:2px 0 6px"><b>{escape(v["model"] or "")}</b></div><div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px">{"".join(chips)}</div>'
            f'<div style="height:8px;background:{MUT}33;border-radius:99px;overflow:hidden"><div style="height:100%;width:{pct}%;background:#4f8cff;transition:width .4s"></div></div>'
            f'<div style="color:{MUT};margin:6px 0">{line}</div><div>{pol}</div>')


def legend_html(counts):
    parts = []
    for g, ss in GROUPS:
        inner = " ".join(f'<span style="display:inline-block;width:10px;height:10px;border-radius:3px;background:{COLORS[s]}"></span> {s} <b>{counts.get(s, 0)}</b>' for s in ss)
        parts.append(f'<span style="border:1px solid {MUT}66;border-radius:8px;padding:3px 9px;margin:0 8px 6px 0;display:inline-block"><b>{g} {sum(counts.get(s, 0) for s in ss)}</b> &nbsp;{inner}</span>')
    return "".join(parts)


CELL, GAP, COLS = 10, 2, 60


def grid_image(states, n, flagged=b""):
    """One cell per test, coloured by state. Returns (PIL image, cols) — cell (i) sits at column i % COLS, row i // COLS."""
    if not n:
        return None
    rows = -(-n // COLS)
    step = CELL + GAP
    img = np.zeros((rows * step, COLS * step, 3), dtype=np.uint8)
    img[:] = (24, 26, 33)
    pal = [tuple(int(COLORS[s][k:k + 2], 16) for k in (1, 3, 5)) for s in STATES]
    for i in range(n):
        y, x = (i // COLS) * step, (i % COLS) * step
        img[y:y + CELL, x:x + CELL] = pal[states[i]]
        if i < len(flagged) and flagged[i]:                       # oracle-flagged: a white centre dot
            img[y + 3:y + CELL - 3, x + 3:x + CELL - 3] = (255, 255, 255)
    return Image.fromarray(img)


def oracle_rows(rules):
    """Live view of the label-free oracle rules: how often each flags, how often a flag was a real error, and whether it also fires on gold."""
    rows = []
    for rule, o in sorted(rules.items(), key=lambda kv: -kv[1]["flagged"]):
        prec = f'{100 * o["tp"] / o["flagged"]:.0f}%' if o["flagged"] else "–"
        rows.append([rule, o["flagged"], prec, o["gold"]])
    return rows


def policy_rows(hits):
    return [[k, h["allow"], h["deny"]] for k, h in sorted(hits.items(), key=lambda kv: -(kv[1]["allow"] + kv[1]["deny"]))]


def feed_rows(recent):
    rows = []
    for r in reversed(recent):
        flips = "; ".join(f'{a["a"]}: model {"allow" if a["p"] else "deny"} / gold {"allow" if a["g"] else "deny"}' for a in (r.get("acts") or []) if a["p"] != a["g"])
        heads = ", ".join(f'{h["t"]}: {h["p"]} ({h["c"]}%) vs {h["g"]}' for h in (r.get("heads") or []))
        rows.append([r["i"], r["to"], r["k"], heads, flips or (r.get("err") or "")])
    return rows


def domain_rows(v):
    plan, states = v["plan"], v["states"]
    if not plan:
        return []
    doms, packs = {}, plan.get("packDomains", {})
    for i, k in enumerate(plan["keys"]):
        d = k.split("|")[1]
        row = doms.setdefault(d, [0] * len(STATES))
        row[states[i]] += 1
    return [[d, packs.get(d) if isinstance(packs.get(d), str) else ("automation" if d in packs else "classification"), *doms[d]] for d in sorted(doms)]


def build_ui(srv):
    """Build the Blocks app against the server module `srv` (LIVE, jobs, start_job, ranking...)."""
    registry = srv.suite_registry()
    defaults = srv.policy_params()
    try:
        listing = srv.checks_endpoint()["checks"]
    except HTTPException:
        listing = []

    def latest_job():
        return list(srv._jobs.values())[-1] if srv._jobs else None

    def tick():
        v = srv.LIVE.view()
        job = latest_job()
        snap = srv.snapshot(job) if job else None
        counts = v["counts"]
        img = grid_image(v["states"], v["plan"]["tests"], v["flagged"]) if v["plan"] else None
        g = v["graphs"].get("test")
        return (status_html(v, snap), dataset_html(v, registry), graph_svg(g, counts, v["edges"]), legend_html(counts), img,
                policy_rows(v["policyHits"]), feed_rows(v["recent"]), domain_rows(v), checks_html(listing, srv.CHECKS["results"]), oracle_rows(v["oracle"]))

    def inspect(i):
        try:
            i = int(i)
        except (TypeError, ValueError):
            return "enter a test index"
        with srv.LIVE.lock:
            ver = srv.LIVE.verdicts.get(i)
        return json.dumps(ver, indent=1) if ver else f"no verdict for test {i} (yet)"

    def on_select(evt: gr.SelectData):
        x, y = evt.index
        i = int(y // (CELL + GAP)) * COLS + int(x // (CELL + GAP))
        return i, inspect(i)

    def start(model, all_models, skip_done, threads, limit, key, suites, *pvals):
        need = srv.os.environ.get("API_KEY")
        if need and key != need:
            return "API key required (Space secret API_KEY)."
        overrides = {k: int(v) for k, v in zip(defaults, pvals) if v is not None and int(v) != defaults[k]}
        try:
            models = list(srv.REGISTRY) if all_models else [model]
            r = srv.start_runs(models, int(threads), int(limit), 20, bool(skip_done), list(suites or []), overrides)
            return f"started: {json.dumps(r)}"
        except HTTPException as e:
            return f"not started: {e.detail}"

    def start_checks(selected, key):
        need = srv.os.environ.get("API_KEY")
        if need and key != need:
            return "API key required (Space secret API_KEY)."
        try:
            return f"started: {json.dumps(srv.run_checks_endpoint(srv.CheckRequest(checks=list(selected or []) or None)))}"
        except HTTPException as e:
            return f"not started: {e.detail}"

    def ranking():
        try:
            return srv.ranking()["markdown"]
        except HTTPException as e:
            return f"ranking unavailable: {e.detail}"

    with gr.Blocks(title="FindAJev live", fill_width=True) as demo:
        gr.Markdown("# FindAJev — live test state machines with Cedar policy enforcement\n"
                    "Each test is a [Stateless](https://github.com/dotnet-state-machine/stateless) machine; [Cedar](https://www.cedarpolicy.com/) "
                    "decides every action with the model's labels and again with gold labels — a difference is a guardrail failure.")
        with gr.Tabs():
            with gr.Tab("Live"):
                status = gr.HTML()
                datasets = gr.HTML()
                graph = gr.HTML()
                legend = gr.HTML()
                with gr.Row():
                    grid = gr.Image(label="tests (click a cell to inspect)", interactive=False, buttons=[], scale=3)
                    pol = gr.Dataframe(headers=["Cedar policy (model labels)", "allow", "deny"], interactive=False, scale=2, max_height=420)
                gr.Markdown("**Oracle rules** — label-free Cedar audits that flag suspicious predictions (white dot in the grid). *flagged* = how often the rule fired; "
                            "*precision* = share of flags that were real errors (compare with the base error rate); *on gold* = times the rule also fires on the gold labels "
                            "(a sound rule ≈ 0).")
                oracle = gr.Dataframe(headers=["rule", "flagged", "precision", "on gold"], interactive=False, max_height=260)
            with gr.Tab("Failures & inspect"):
                feed = gr.Dataframe(headers=["test", "state", "key", "model vs gold", "decision flips"], interactive=False, max_height=420)
                with gr.Row():
                    idx = gr.Number(label="test index", precision=0, scale=1)
                    btn = gr.Button("Inspect", scale=1)
                detail = gr.Code(label="verdict (labels, confidence, every Cedar decision with policy ids)", language="json")
            with gr.Tab("By domain"):
                dom = gr.Dataframe(headers=["domain", "suite", *STATES], interactive=False, max_height=600)
            with gr.Tab("Ranking"):
                rank = gr.Markdown()
                gr.Button("Refresh").click(ranking, outputs=rank)
            with gr.Tab("Cedar checks"):
                gr.Markdown("Tests of the Cedar policies and of Cedar itself — independent of any model. **Gate** checks run automatically before every "
                            "benchmark job and must pass. They use CPU, so they share the single job slot with benchmark runs.")
                with gr.Row():
                    check_pick = gr.CheckboxGroup([(c["title"], c["id"]) for c in listing], value=[c["id"] for c in listing], label="checks (discovered from the harness)")
                    check_key = gr.Textbox(label="API key", type="password")
                run_checks_btn = gr.Button("Run selected checks", variant="primary")
                check_out = gr.Textbox(label="result", interactive=False)
                checks_view = gr.HTML()
                run_checks_btn.click(start_checks, [check_pick, check_key], check_out)
            with gr.Tab("Run"):
                gr.Markdown(f"Runs are sequential (one at a time) on **{srv.CPUS} CPU(s)**. Models: {', '.join(srv.REGISTRY)}.")
                with gr.Row():
                    model = gr.Dropdown(list(srv.REGISTRY), value=list(srv.REGISTRY)[0], label="model")
                    threads = gr.Number(value=srv.CPUS, precision=0, label="threads")
                    limit = gr.Number(value=0, precision=0, label="limit (0 = all)")
                suite_pick = gr.CheckboxGroup([(f'{e["title"]} — {", ".join(e["provides"])}', e["id"]) for e in registry],
                                              value=[e["id"] for e in registry], label="suites (from suites.json)")
                with gr.Accordion("Cedar policy parameters (defaults from policies/params.json; a changed value gets its own ranking)", open=False):
                    pnums = [gr.Number(value=v, precision=0, label=k) for k, v in defaults.items()]
                with gr.Row():
                    allm = gr.Checkbox(label="run every model")
                    skip = gr.Checkbox(label="skip models that already have a result", value=True)
                    key = gr.Textbox(label="API key", type="password")
                go = gr.Button("Start", variant="primary")
                out = gr.Textbox(label="result", interactive=False)
                go.click(start, [model, allm, skip, threads, limit, key, suite_pick, *pnums], out)

        timer = gr.Timer(0.5)
        timer.tick(tick, outputs=[status, datasets, graph, legend, grid, pol, feed, dom, checks_view, oracle])
        grid.select(on_select, outputs=[idx, detail])
        btn.click(inspect, idx, detail)
        demo.load(ranking, outputs=rank)
    return demo
