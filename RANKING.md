# Ranking

## 2 thread(s) — Intel(R) Xeon(R) Platinum 8375C CPU @ 2.90GHz, ORT 1.30.0, 2600 decisions, batch 1

| # | model | precision | accuracy | p50 ms | p95 ms | decisions/s | peak RSS MB | load s | Pareto |
|--:|---|---|--:|--:|--:|--:|--:|--:|:-:|
| 1 | gliner25-decide-fp32 | fp32 | 66.5 % | 735.2 | 1290.2 | 1.3 | 2873 | 9.0 | ✓ |
| 2 | gliner25-decide-fp16 | fp16 | 66.5 % | 1235.0 | 1937.5 | 0.8 | 2316 | 5.5 |  |
| 3 | gliner25-decide-int8 | int8 | 63.7 % | 425.5 | 714.3 | 2.3 | 1549 | 6.1 | ✓ |
| 4 | laya-fp32 | fp32 | 53.2 % | 966.9 | 1528.2 | 1.0 | 1834 | 2.0 |  |
| 5 | julia-1-fp32 | fp32 | 46.1 % | 143.8 | 254.3 | 6.6 | 601 | 0.3 | ✓ |

