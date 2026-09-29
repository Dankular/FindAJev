"""Gradio dashboard: the live Stateless state-machine graph is the centrepiece.

Everything shown is folded from the harness's EVENT lines (see server.Live) and rendered server-side on a timer, so there is
no custom JavaScript to keep in sync. The graph itself is drawn from the machine's own GetInfo() export, not hard-coded.
"""
import io, json
from html import escape

import gradio as gr
import numpy as np
from fastapi import HTTPException
from PIL import Image

COLORS = {"Queued": "#8b93a1", "Inferring": "#4f8cff", "Enforcing": "#a78bfa", "Correct": "#34d399", "WrongButSafe": "#2dd4bf",
          "Overblocked": "#fbbf24", "Unsafe": "#f87171", "Misclassified": "#fb923c", "Errored": "#f472b6"}
STATES = list(COLORS)
GROUPS = [("activated", ["Inferring", "Enforcing"]), ("passed", ["Correct", "WrongButSafe"]),
          ("failed", ["Overblocked", "Unsafe", "Misclassified", "Errored"]), ("queued", ["Queued"])]
CRUMBS = ["Pending", "ModelReady", "SessionLoaded", "WarmedUp", "Measured", "Scored"]

# Hand-placed layout for the known test machine. Unknown states fall into a spare row so a machine change never hides a node.
POS = {"Queued": (70, 190), "Inferring": (240, 190), "Enforcing": (410, 190), "Correct": (610, 50), "WrongButSafe": (610, 120),
       "Overblocked": (610, 215), "Unsafe": (610, 285), "Misclassified": (610, 350), "Errored": (790, 285)}
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


def grid_image(states, n):
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
    return Image.fromarray(img)


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
    return [[d, "automation" if d in packs else "classification", *doms[d]] for d in sorted(doms)]


def build_ui(srv):
    """Build the Blocks app against the server module `srv` (LIVE, jobs, start_job, ranking...)."""
    def latest_job():
        return list(srv._jobs.values())[-1] if srv._jobs else None

    def tick():
        v = srv.LIVE.view()
        job = latest_job()
        snap = srv.snapshot(job) if job else None
        counts = v["counts"]
        img = grid_image(v["states"], v["plan"]["tests"]) if v["plan"] else None
        g = v["graphs"].get("test")
        return (status_html(v, snap), graph_svg(g, counts, v["edges"]), legend_html(counts), img,
                policy_rows(v["policyHits"]), feed_rows(v["recent"]), domain_rows(v))

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

    def start(model, all_models, skip_done, threads, limit, key):
        need = srv.os.environ.get("API_KEY")
        if need and key != need:
            return "API key required (Space secret API_KEY)."
        try:
            if all_models:
                r = srv.run_all(threads=int(threads), limit=int(limit), warmup=20, skip_done=bool(skip_done))
            else:
                r = srv.run(srv.RunRequest(model=model, threads=int(threads), limit=int(limit)))
            return f"started: {json.dumps(r)}"
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
                graph = gr.HTML()
                legend = gr.HTML()
                with gr.Row():
                    grid = gr.Image(label="tests (click a cell to inspect)", interactive=False, buttons=[], scale=3)
                    pol = gr.Dataframe(headers=["Cedar policy (model labels)", "allow", "deny"], interactive=False, scale=2, max_height=420)
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
            with gr.Tab("Run"):
                gr.Markdown(f"Runs are sequential (one at a time) on **{srv.CPUS} CPU(s)**. Models: {', '.join(srv.REGISTRY)}.")
                with gr.Row():
                    model = gr.Dropdown(list(srv.REGISTRY), value=list(srv.REGISTRY)[0], label="model")
                    threads = gr.Number(value=srv.CPUS, precision=0, label="threads")
                    limit = gr.Number(value=0, precision=0, label="limit (0 = all)")
                with gr.Row():
                    allm = gr.Checkbox(label="run every model")
                    skip = gr.Checkbox(label="skip models that already have a result", value=True)
                    key = gr.Textbox(label="API key", type="password")
                go = gr.Button("Start", variant="primary")
                out = gr.Textbox(label="result", interactive=False)
                go.click(start, [model, allm, skip, threads, limit, key], out)

        timer = gr.Timer(0.5)
        timer.tick(tick, outputs=[status, graph, legend, grid, pol, feed, dom])
        grid.select(on_select, outputs=[idx, detail])
        btn.click(inspect, idx, detail)
        demo.load(ranking, outputs=rank)
    return demo
