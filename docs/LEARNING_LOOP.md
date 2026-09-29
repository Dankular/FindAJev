# Governed learning loop (Cedar side)

The idea: the user works with a model during the day; overnight, curated interactions may become training data; a new model version replaces the
old one only if it clears a gate. Cedar is the rule engine for the two decisions, and the harness is the evidence source.

## What exists

| piece | file | status |
|---|---|---|
| Which interactions may become training data (`Admit`) and which a human should look at (`Review`) | `policies/training.cedar` | implemented, tested |
| Whether a candidate model may replace the champion (`Promote`) | `policies/promote.cedar` | implemented, tested |
| Governance facts per source (licence class) | `policies/governance.json` | implemented; consent is **simulated** (deterministic 10 % no-consent) |
| Hash-chained decision ledger | `ledger/decisions.jsonl` (`Ledger` in `Learning.cs`) | implemented |
| Curation simulator (replays recorded predictions through `Admit`/`Review`) | `Curation` in `Learning.cs` | implemented |
| **Training executor / fine-tuning / LLM call** | — | **not implemented** (planned: an LLM call will supply real examples later) |

## Admission rules (`training.cedar`)

* Permit: human label; verified outcome; the model's own label **only** if `admit_self_labels = 1` and all models agree (unanimous), confidence >= `admit_min_conf`, margin >= `review_margin`. Off by default.
* Forbid (always wins): no consent; source licence not `open` (`research_only`, `unverified`); high PII risk; a model's own label that any oracle rule flagged; a model's own label on attack text (injection / jailbreak / harmful request).
* Review (what goes to a human): flagged by the Cedar oracle rules, split vote across models, close call (margin < `review_margin`); never shown to a reviewer if consent is missing or PII risk is high.

## Promotion rules (`promote.cedar`)

Permit needs a human approval, a comparable run (same suites, variant, parameters and item count) and no core-accuracy regression. Forbid: the principal is a `Learner`;
unsafe rate up by more than `promote_max_unsafe_rise_bp`; a suite's accuracy down by more than `promote_max_suite_drop_bp`; any held-out suite (`heldOut` in `suites.json`) worse;
any errored test; p50 latency above `promote_max_latency_pct` of the champion's. Deltas are computed from two harness results by `Promotion.Context`.

## Commands

```
findajev cedar-suite --checks admission-properties,promotion-gate,curation-sim
findajev promote-check <candidate-id> <champion-id> [--threads N] [--approved] [--principal Human|Learner]   # logs to the ledger
findajev curation-sim [--budget 10] [--annotator 0.98]      # needs results/*.preds.jsonl from `run`; logs to the ledger
findajev ledger-verify
```

## What the checks establish (and what they do not)

* `admission-properties`: every combination of consent x licence x PII x flag x consensus x confidence x margin (at the policy's boundaries) x label source x domain type, with `admit_self_labels` at 0 and 1, matches an independent recomputation written in C#, and the "never admit ..." invariants hold. This proves the *policy* does what the rule text says; it says nothing about whether the rules are the right ones.
* `promotion-gate`: the same for the promotion policy over its boundary values.
* `curation-sim`: on synthetic interactions and, when present, recorded runs: label noise of the admitted set, yield, share of wrong proposals a human saw, and an independent audit that nothing forbidden was admitted (must be 0). Human labels are simulated as gold with a configurable annotator accuracy; consent is simulated. On synthetic data the numbers only show the mechanism.
* Not verified: any effect on a trained model (none is trained here); the consensus signal's usefulness on real data beyond what `curation-sim` reports for the recorded runs; that the ledger is tamper-*proof* (it is tamper-*evident*: someone who can rewrite the whole file can rebuild the chain; anchor the last hash elsewhere for more).
