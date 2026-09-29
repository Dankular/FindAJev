using System.Text;
using System.Text.Json;

namespace FindAJev.Bench;

public static class Ranking
{
    /// <summary>
    /// Ranks scored runs at each thread count. Order: accuracy (desc), then p50 latency (asc).
    /// A run is Pareto-optimal if no other run is at least as accurate AND at least as fast, and strictly better on one.
    /// This is a presentation choice, not a claim about which trade-off matters; the raw numbers are all in the table.
    /// </summary>
    // Automation suite: share of tests whose Cedar outcomes match gold (Correct + WrongButSafe); Unsafe = guardrail failures.
    static string Safe(RunResult r) => r.Suites.TryGetValue("automation", out var s) && s.Tests > 0 ? $"{(double)(s.Correct + s.WrongButSafe) / s.Tests:P1}" : "–";
    static string Unsafe(RunResult r) => r.Suites.TryGetValue("automation", out var s) ? $"{s.Unsafe}/{s.Tests}" : "–";

    public static string Render(string dir)
    {
        var runs = Directory.GetFiles(dir, "*.json")
            .Select(f => JsonSerializer.Deserialize<RunResult>(File.ReadAllText(f), Json.Opts)!)
            .ToList();
        var sb = new StringBuilder("# Ranking\n");
        foreach (var g in runs.GroupBy(r => (r.Threads, r.Variant)).OrderBy(g => g.Key.Variant.Length).ThenBy(g => g.Key.Threads))
        {
            var ok = g.Where(r => r.State == "Scored").ToList();
            sb.AppendLine($"\n## {g.Key.Threads} thread(s){(g.Key.Variant.Length > 0 ? " — variant `" + g.Key.Variant + "`" : "")} — {ok.FirstOrDefault()?.Cpu}, ORT {ok.FirstOrDefault()?.OrtVersion}, {ok.FirstOrDefault()?.Items} decisions, batch 1\n");
            sb.AppendLine("| # | model | precision | accuracy | policy-safe | unsafe | p50 ms | p95 ms | decisions/s | peak RSS MB | load s | Pareto |");
            sb.AppendLine("|--:|---|---|--:|--:|--:|--:|--:|--:|--:|--:|:-:|");
            var i = 1;
            foreach (var r in ok.OrderByDescending(r => r.Accuracy).ThenBy(r => r.P50Ms))
            {
                var dominated = ok.Any(o => o != r && o.Accuracy >= r.Accuracy && o.P50Ms <= r.P50Ms && (o.Accuracy > r.Accuracy || o.P50Ms < r.P50Ms));
                sb.AppendLine($"| {i++} | {r.Id} | {r.Precision} | {r.Accuracy:P1} | {Safe(r)} | {Unsafe(r)} | {r.P50Ms:F1} | {r.P95Ms:F1} | {r.ItemsPerSec:F1} | {r.PeakRssMb:F0} | {r.LoadSeconds:F1} | {(dominated ? "" : "✓")} |");
            }
            var withSuites = ok.Where(r => r.Suites.Values.Any(x => x.Heads > 0)).ToList();
            if (withSuites.Count > 0)
            {
                sb.AppendLine("\n**Per suite** — `accuracy` is head-level argmax; `safe` = Correct + WrongButSafe; `unsafe` = the model's labels let Cedar allow what gold denies; `overblocked` = denied what gold allows (includes low-confidence abstentions).\n");
                sb.AppendLine("| model | suite | tests | accuracy | safe | unsafe | overblocked | p50 ms | p95 ms |");
                sb.AppendLine("|---|---|--:|--:|--:|--:|--:|--:|--:|");
                foreach (var r in withSuites.OrderBy(r => r.Id))
                    foreach (var (name, s) in r.Suites.OrderBy(kv => kv.Key))
                    {
                        var pol = s.Tests > 0 && name is "automation" or "retrieval" or "tools";
                        sb.AppendLine($"| {r.Id} | {name} | {s.Tests} | {s.Accuracy:P1} | {(pol ? $"{(double)(s.Correct + s.WrongButSafe) / s.Tests:P1}" : "–")} | {(pol ? s.Unsafe : "–")} | {(pol ? s.Overblocked : "–")} | {s.P50Ms:F1} | {s.P95Ms:F1} |");
                    }
            }
            var withOracle = ok.Where(r => r.OracleRules.Count > 0).ToList();
            if (withOracle.Count > 0)
            {
                sb.AppendLine("\n**Oracle rules** (label-free Cedar audits) — `precision` = flagged tests that really had a wrong label; `gold` = tests where the rule also fires on the gold labels (a sound rule fires on ~0%); a rule that fires on gold disagrees with the dataset, so its flags are weak evidence.\n");
                sb.AppendLine("| model | rule | eligible | base error | flagged | precision | lift | fires on gold | verdict |");
                sb.AppendLine("|---|---|--:|--:|--:|--:|--:|--:|---|");
                foreach (var r in withOracle.OrderBy(r => r.Id))
                    foreach (var (rule, st) in r.OracleRules.OrderBy(kv => kv.Key).Where(kv => kv.Value.Flagged > 0 || kv.Value.FiredOnGold > 0))
                    {
                        var goldPct = 100.0 * st.FiredOnGold / st.Eligible;
                        var baseRate = (double)st.EligibleWrong / st.Eligible;
                        var prec = st.Flagged == 0 ? 0 : (double)st.FlaggedWrong / st.Flagged;
                        var lift = baseRate > 0 && st.Flagged > 0 ? prec / baseRate : 0;
                        var verdict = goldPct > 5 ? "unsound on this data" : st.Flagged < 5 ? "too few flags" : lift < 1.1 ? "no better than chance" : "usable";
                        sb.AppendLine($"| {r.Id} | {rule} | {st.Eligible} | {100 * baseRate:F0}% | {st.Flagged} | {(st.Flagged == 0 ? "–" : $"{100 * prec:F0}%")} | {(st.Flagged == 0 ? "–" : $"{lift:F2}×")} | {goldPct:F1}% | {verdict} |");
                    }
            }
            foreach (var r in g.Where(r => r.State != "Scored")) sb.AppendLine($"\n- **{r.Id}**: {r.State} — {r.Error}");
        }
        return sb.ToString();
    }
}
