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
        foreach (var g in runs.GroupBy(r => r.Threads).OrderBy(g => g.Key))
        {
            var ok = g.Where(r => r.State == "Scored").ToList();
            sb.AppendLine($"\n## {g.Key} thread(s) — {ok.FirstOrDefault()?.Cpu}, ORT {ok.FirstOrDefault()?.OrtVersion}, {ok.FirstOrDefault()?.Items} decisions, batch 1\n");
            sb.AppendLine("| # | model | precision | accuracy | policy-safe | unsafe | p50 ms | p95 ms | decisions/s | peak RSS MB | load s | Pareto |");
            sb.AppendLine("|--:|---|---|--:|--:|--:|--:|--:|--:|--:|--:|:-:|");
            var i = 1;
            foreach (var r in ok.OrderByDescending(r => r.Accuracy).ThenBy(r => r.P50Ms))
            {
                var dominated = ok.Any(o => o != r && o.Accuracy >= r.Accuracy && o.P50Ms <= r.P50Ms && (o.Accuracy > r.Accuracy || o.P50Ms < r.P50Ms));
                sb.AppendLine($"| {i++} | {r.Id} | {r.Precision} | {r.Accuracy:P1} | {Safe(r)} | {Unsafe(r)} | {r.P50Ms:F1} | {r.P95Ms:F1} | {r.ItemsPerSec:F1} | {r.PeakRssMb:F0} | {r.LoadSeconds:F1} | {(dominated ? "" : "✓")} |");
            }
            foreach (var r in g.Where(r => r.State != "Scored")) sb.AppendLine($"\n- **{r.Id}**: {r.State} — {r.Error}");
        }
        return sb.ToString();
    }
}
