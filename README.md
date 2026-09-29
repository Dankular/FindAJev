# FindAJev

CPU benchmark and ranking of small "typed decision" (JEV-style) models: given a text and a set of options, pick one.
Each model is run on the same decisions, on CPU only, and ranked on accuracy and latency.

The harness is C# (.NET 8) using [Stateless](https://github.com/dotnet-state-machine/stateless) 5.20.1 for the per-model run lifecycle and
ONNX Runtime (`CPUExecutionProvider`) for inference.

## Models

| id | source | notes |
|---|---|---|
| `gliner25-decide-{fp32,fp16,int8}` | [nishparadox/gliner2.5-decide-onnx](https://huggingface.co/nishparadox/gliner2.5-decide-onnx) | classification path of fastino/GLiNER2.5-Decide (DeBERTa-v3-large) |
| `julia-1-fp32` | [SupersonicLabs/Julia-1-ONNX](https://huggingface.co/SupersonicLabs/Julia-1-ONNX) | repo is aimed at WebGPU; its own `parity.py` runs `model.onnx` on `CPUExecutionProvider` |
| `laya-fp32` | [receptron/laya-onnx](https://huggingface.co/receptron/laya-onnx) | ModernBERT-large + decision head |

Add a model: append an entry to `models.json`, add an encoder in `tools/encode.py` if it needs a new prompt layout, and add its input
names to `BuildInputs` in `src/FindAJev.Bench/RunMachine.cs`.

## Workload

Shared dataset: [fastino/fast-decisions](https://huggingface.co/datasets/fastino/fast-decisions) (17 domains, 1,700 rows).
The unit of work is one **single-label task** of one row (2,600 decisions). Multi-label tasks are skipped because Julia and Laya
choose exactly one option, so there is no shared gold answer.

Each family gets the layout its own code uses (task name as the question; the labels as options):

* GLiNER: the reference `gliner_onnx.py` encoder from the model repo, unchanged.
* Julia / Laya: `[CLS] "choice question: <task>?" [SEP] ([MASK] option)* [SEP] state [SEP]`, following `sequence()` in Julia-1's
  `julia/data.py` and `build_sequence()` in Laya's `rl_common.py`. The question wording (`--question`, default `{task}?`) is **our choice**,
  not something either model card specifies; all decisions are typed `choice` (never `noul`/`score`).

Tokenization is done once in Python (`tools/encode.py`) and excluded from the timings, because I did not find a .NET loader for
these Hugging Face `tokenizer.json` files. The numbers are pure model-inference CPU time.

## Measurement

Per decision, batch size 1, sequential, wall clock around `InferenceSession.Run` only, after a 20-decision warm-up; `--threads` sets
intra-op threads (inter-op 1, sequential execution, all graph optimisations). Peak RSS is `PeakWorkingSet64` of the run process (one process
per model). Accuracy is argmax over the logits vs the gold label. Results go to `results/<id>.t<threads>.json`; `RANKING.md` is generated.

Ranking order: accuracy (desc), then p50 latency (asc). The Pareto column marks runs that no other run beats on both accuracy and p50 latency.
This is a presentation choice, not a claim about which trade-off matters.

## Run

```bash
pip install -r requirements.txt
python tools/fetch.py                      # list models
python tools/encode.py gliner && python tools/encode.py julia && python tools/encode.py laya
scripts/run_all.sh 4                       # fetch + run everything sequentially, then rank
dotnet bin/FindAJev.Bench.dll run julia-1-fp32 --threads 4 --limit 100   # one model
dotnet bin/FindAJev.Bench.dll graph        # Graphviz DOT of the run lifecycle (docs/lifecycle.dot)
```

## Run lifecycle (Stateless)

`Pending → ModelReady → SessionLoaded → WarmedUp → Measured → Scored`, all substates of `Running`, which permits `Fail → Failed`
(so any missing file or runtime error ends in `Failed` with the message recorded, and the other models still run). See `docs/lifecycle.dot`.

## Verification status

* Julia encoder + ORT CPU vs the 100 PyTorch reference logits shipped in Julia-1-ONNX: 100/100 argmax, max |Δlogit| 7e-5
  (`python tests/julia_parity.py`).
* GLiNER uses the model author's own encoder. No independent parity check was run here.
* **Laya has no reference logits in its repo and torch is not installed here, so the Laya encoder is unverified against PyTorch.**
  Its I/O names and sequence layout were checked against the model card and `rl_common.py` only.
* The ONNX graphs' input/output names were read from the loaded sessions, not assumed.
* Published latency numbers in the model cards come from other machines and are not comparable to ours.
