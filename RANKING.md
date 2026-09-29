# Ranking

## 2 thread(s) — Intel(R) Xeon(R) Processor @ 2.80GHz, ORT 1.30.0, 15 decisions, batch 1

| # | model | precision | accuracy | p50 ms | p95 ms | decisions/s | peak RSS MB | load s | Pareto |
|--:|---|---|--:|--:|--:|--:|--:|--:|:-:|
| 1 | julia-1-fp32 | fp32 | 60.0 % | 160.9 | 219.9 | 6.5 | 474 | 0.3 | ✓ |

## 4 thread(s) — Intel(R) Xeon(R) Processor @ 2.80GHz, ORT 1.30.0, 2600 decisions, batch 1

| # | model | precision | accuracy | p50 ms | p95 ms | decisions/s | peak RSS MB | load s | Pareto |
|--:|---|---|--:|--:|--:|--:|--:|--:|:-:|
| 1 | gliner25-decide-int8 | int8 | 63.7 % | 305.6 | 466.2 | 3.2 | 1746 | 9.0 | ✓ |
