using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using CedarDotNet.Values;

namespace FindAJev.Bench;

// =============================================================================================================================
// The learning loop, model-free parts: an audit ledger, the promotion gate's inputs, and a curation simulator that replays the
// predictions our runs already recorded through the Cedar admission policy. Nothing here trains a model.
// =============================================================================================================================

/// <summary>Append-only, hash-chained decision log (ledger/decisions.jsonl). Each line's `body` is hashed together with the previous hash.</summary>
public static class Ledger
{
    static string PathFor(string root) => Path.Combine(root, "ledger", "decisions.jsonl");
    static string Hash(string prev, string body) => Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(prev + "\n" + body))).ToLowerInvariant();

    public static void Append(string root, string kind, object payload)
    {
        var path = PathFor(root);
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        var prev = File.Exists(path) && File.ReadLines(path).LastOrDefault() is { Length: > 0 } last
            ? JsonDocument.Parse(last).RootElement.GetProperty("hash").GetString()! : new string('0', 64);
        var body = JsonSerializer.Serialize(new { ts = DateTime.UtcNow.ToString("o"), kind, payload });
        File.AppendAllText(path, JsonSerializer.Serialize(new { body, prev, hash = Hash(prev, body) }) + "\n");
    }

    /// <summary>Recompute the chain. Returns (ok, entries, first problem).</summary>
    public static (bool ok, int n, string? problem) Verify(string root)
    {
        var path = PathFor(root);
        if (!File.Exists(path)) return (true, 0, null);
        var prev = new string('0', 64); var n = 0;
        foreach (var line in File.ReadLines(path).Where(l => l.Length > 0))
        {
            var e = JsonDocument.Parse(line).RootElement;
            var body = e.GetProperty("body").GetString()!;
            if (e.GetProperty("prev").GetString() != prev) return (false, n, $"entry {n}: previous-hash link is broken");
            if (e.GetProperty("hash").GetString() != Hash(prev, body)) return (false, n, $"entry {n}: content does not match its hash");
            prev = e.GetProperty("hash").GetString()!; n++;
        }
        return (true, n, null);
    }
}

public static class Promotion
{
    /// <summary>Cedar context for "may this candidate replace the champion?", computed from two harness results. Deltas in basis points.</summary>
    public static Dictionary<string, Value> Context(RunResult cand, RunResult champ, bool humanApproved, IReadOnlySet<string> heldOut)
    {
        static double Rate(SuiteResult s, Func<SuiteResult, int> k) => s.Tests == 0 ? 0 : (double)k(s) / s.Tests;
        var shared = cand.Suites.Keys.Intersect(champ.Suites.Keys).ToList();
        long Bp(double d) => (long)Math.Round(d * 10000);
        var comparable = cand.Variant == champ.Variant && cand.Items == champ.Items && cand.SuitesRun.OrderBy(x => x).SequenceEqual(champ.SuitesRun.OrderBy(x => x))
                         && cand.PolicyParams.OrderBy(kv => kv.Key).SequenceEqual(champ.PolicyParams.OrderBy(kv => kv.Key));
        var suiteDeltas = shared.Select(k => Bp(cand.Suites[k].Accuracy - champ.Suites[k].Accuracy)).ToList();
        var heldDeltas = shared.Where(heldOut.Contains).Select(k => Bp(cand.Suites[k].Accuracy - champ.Suites[k].Accuracy)).ToList();
        return new Dictionary<string, Value>
        {
            ["human_approved"] = humanApproved,
            ["comparable"] = comparable,
            ["d_core_acc_bp"] = Bp(cand.Accuracy - champ.Accuracy),
            ["d_worst_suite_acc_bp"] = suiteDeltas.Count == 0 ? 0L : Math.Min(0, suiteDeltas.Min()),
            ["d_unsafe_bp"] = shared.Count == 0 ? 0L : shared.Max(k => Bp(Rate(cand.Suites[k], s => s.Unsafe) - Rate(champ.Suites[k], s => s.Unsafe))),
            ["d_overblocked_bp"] = shared.Count == 0 ? 0L : shared.Max(k => Bp(Rate(cand.Suites[k], s => s.Overblocked) - Rate(champ.Suites[k], s => s.Overblocked))),
            ["held_out_delta_bp"] = heldDeltas.Count == 0 ? 0L : heldDeltas.Min(),
            ["latency_ratio_pct"] = champ.P50Ms <= 0 ? 100L : (long)Math.Round(100 * cand.P50Ms / champ.P50Ms),
            ["errored"] = (long)cand.Suites.Values.Sum(s => s.Errored),
            ["tests"] = (long)cand.Suites.Values.Sum(s => s.Tests),
        };
    }

    public static IEnumerable<RunResult> LoadResults(string root) =>
        Directory.GetFiles(Path.Combine(root, "results"), "*.json").Select(f => JsonSerializer.Deserialize<RunResult>(File.ReadAllText(f), Json.Opts)!).Where(r => r.State == "Scored");
}

/// <summary>One logged interaction as the curation simulator sees it: what the models predicted, what an oracle flagged, and gold (evaluation only).</summary>
public sealed record SimExample(string Key, string Domain, string Suite, int[] Proposed, int[] Gold, bool ProposedWrong, int Confidence, int Margin,
                                bool Flagged, string Consensus, int Models, string ProposedLabel);

public static class Curation
{
    sealed record HeadRec(string t, int p, string? pl, int g, int c, int m, int n);
    sealed record PredRec(string k, string d, string suite, bool wrong, string[] flags, List<HeadRec> heads);
    static readonly JsonSerializerOptions Ci = new() { PropertyNameCaseInsensitive = true };

    /// <summary>Predictions written by benchmark runs (results/&lt;model&gt;.t&lt;threads&gt;.preds.jsonl, default variant only), keyed by model id.</summary>
    public static Dictionary<string, List<PredRecPublic>> LoadPreds(string root)
    {
        var res = new Dictionary<string, List<PredRecPublic>>();
        foreach (var f in Directory.GetFiles(Path.Combine(root, "results"), "*.preds.jsonl").OrderBy(x => x))
        {
            var m = Regex.Match(Path.GetFileName(f), @"^(.+)\.t(\d+)\.preds\.jsonl$");   // a hash suffix (variant runs) does not match: not comparable
            if (!m.Success || res.ContainsKey(m.Groups[1].Value)) continue;
            res[m.Groups[1].Value] = File.ReadLines(f).Where(l => l.Length > 0).Select(l => JsonSerializer.Deserialize<PredRecPublic>(l, Ci)!).ToList();
        }
        return res;
    }

    public sealed record HeadPublic(string t, int p, string? pl, int g, int c, int m, int n);
    public sealed record PredRecPublic(string k, string d, string suite, bool wrong, string[] flags, List<HeadPublic> heads);

    /// <summary>Join the models' predictions per test. The proposed label is the majority vote (ties: the more confident model); with one model it is that model's label.</summary>
    public static (List<SimExample> examples, int skipped) BuildExamples(Dictionary<string, List<PredRecPublic>> preds)
    {
        var byKey = new Dictionary<string, List<PredRecPublic>>();
        foreach (var recs in preds.Values) foreach (var r in recs) (byKey.TryGetValue(r.k, out var l) ? l : byKey[r.k] = new()).Add(r);
        var res = new List<SimExample>(); int skipped = 0;
        foreach (var (key, recs) in byKey)
        {
            var first = recs[0];
            // the models must have seen identical options in identical order, otherwise voting on indices is meaningless
            if (recs.Any(r => r.heads.Count != first.heads.Count || r.heads.Zip(first.heads).Any(z => z.First.g != z.Second.g || z.First.n != z.Second.n))) { skipped++; continue; }
            var proposed = new int[first.heads.Count]; var conf = new List<int>(); var marg = new List<int>(); var unanimousAll = true; var majorityAll = true;
            for (var h = 0; h < proposed.Length; h++)
            {
                var votes = recs.GroupBy(r => r.heads[h].p).Select(g => (label: g.Key, n: g.Count(), conf: g.Average(r => r.heads[h].c), marg: g.Average(r => r.heads[h].m)))
                                .OrderByDescending(v => v.n).ThenByDescending(v => v.conf).ToList();
                proposed[h] = votes[0].label; conf.Add((int)votes[0].conf); marg.Add((int)votes[0].marg);
                if (votes.Count > 1) unanimousAll = false;
                if (votes[0].n * 2 <= recs.Count) majorityAll = false;
            }
            var consensus = recs.Count == 1 ? "single" : unanimousAll ? "unanimous" : majorityAll ? "majority" : "split";
            var gold = first.heads.Select(x => x.g).ToArray();
            var pl = recs.SelectMany(r => r.heads.Where((x, i) => x.p == proposed[i] && x.pl is not null).Select(x => x.pl!)).FirstOrDefault() ?? "";
            res.Add(new SimExample(key, first.d, first.suite, proposed, gold, !proposed.SequenceEqual(gold), conf.Min(), marg.Min(),
                                   recs.Any(r => r.flags.Length > 0), consensus, recs.Count, pl));
        }
        return (res.OrderBy(e => e.Key, StringComparer.Ordinal).ToList(), skipped);
    }

    static int Stable(string s, int mod) => (int)(BitConverter.ToUInt32(SHA1.HashData(Encoding.UTF8.GetBytes(s)), 0) % (uint)mod);

    public sealed record Scenario(string Name, Dictionary<string, long> Overrides, double ReviewBudgetPct, double AnnotatorAccuracy, bool UseCedar = true);

    public sealed class ScenarioResult
    {
        public string Name { get; set; } = "";
        public int Examples { get; set; }
        public int Reviewed { get; set; }
        public int Admitted { get; set; }
        public int AdmittedHuman { get; set; }
        public int AdmittedSelf { get; set; }
        public int WrongAdmitted { get; set; }
        public int WrongAdmittedSelf { get; set; }
        public double BaseError { get; set; }             // wrong share among ALL proposed labels
        public double WrongErrorsCaught { get; set; }     // share of wrong proposals that a human saw
        public int LeakNoConsent { get; set; }            // independent audit of the gate: must be 0
        public int LeakNotOpen { get; set; }
        public int LeakPii { get; set; }
        public int LeakAttackSelf { get; set; }
    }

    /// <summary>Governance attributes of an example that do not come from the model (consent is simulated deterministically: 10% of interactions).</summary>
    public static (string consent, string license, string pii) Governance(string root, SimExample e)
    {
        var lic = "open";
        var gfile = Path.Combine(root, "policies", "governance.json");
        if (File.Exists(gfile) && System.Text.Json.Nodes.JsonNode.Parse(File.ReadAllText(gfile))!["domains"]![e.Domain]?["license_class"] is { } v) lic = v.GetValue<string>();
        var pii = e.Domain == "pii" ? (e.ProposedLabel.StartsWith("none") ? "low" : "high") : "low";
        return (Stable(e.Key, 10) == 0 ? "no" : "yes", lic, pii);
    }

    public static Dictionary<string, Value> TrainContext(string root, SimExample e, string labelSource)
    {
        var (consent, lic, pii) = Governance(root, e);
        return new Dictionary<string, Value>
        {
            ["domain"] = e.Domain, ["label_source"] = labelSource, ["consent"] = consent, ["license_class"] = lic, ["pii_risk"] = pii,
            ["flagged"] = e.Flagged, ["consensus"] = e.Consensus, ["confidence"] = (long)e.Confidence, ["margin"] = (long)e.Margin,
        };
    }

    public static ScenarioResult Simulate(string root, List<SimExample> ex, Scenario sc, out List<string> decisions)
    {
        decisions = new();
        var res = new ScenarioResult { Name = sc.Name, Examples = ex.Count, BaseError = ex.Count == 0 ? 0 : (double)ex.Count(e => e.ProposedWrong) / ex.Count };
        if (!sc.UseCedar)   // baseline: train on everything the model said, like a naive self-training loop
        {
            res.Admitted = res.AdmittedSelf = ex.Count; res.WrongAdmitted = res.WrongAdmittedSelf = ex.Count(e => e.ProposedWrong);
            foreach (var e in ex) { var (c, l, p) = Governance(root, e); res.LeakNoConsent += c == "no" ? 1 : 0; res.LeakNotOpen += l != "open" ? 1 : 0; res.LeakPii += p == "high" ? 1 : 0; }
            return res;
        }
        var pe = new PolicyEngine(Path.Combine(root, "policies"), sc.Overrides);
        // 1) which examples may a human look at, ranked by how suspicious they are (flag first, then the closest call)
        var candidates = new List<(SimExample e, PolicyDecision d)>();
        foreach (var e in ex)
        {
            var d = pe.AuthorizeTrain("Review", e.Domain, TrainContext(root, e, "model"));
            if (d.Error is not null) throw new InvalidOperationException(d.Error);
            // a human's time is only worth spending on examples that could be admitted once labelled (not no-consent, not a restricted source, not high PII)
            var admissible = pe.AuthorizeTrain("Admit", e.Domain, TrainContext(root, e, "human"));
            if (admissible.Error is not null) throw new InvalidOperationException(admissible.Error);
            if (d.Allow && admissible.Allow) candidates.Add((e, d));
        }
        var budget = (int)Math.Ceiling(ex.Count * sc.ReviewBudgetPct / 100.0);
        var reviewed = candidates.OrderByDescending(c => c.d.Reasons.Length).ThenBy(c => c.e.Margin).ThenBy(c => c.e.Key, StringComparer.Ordinal).Take(budget).Select(c => c.e.Key).ToHashSet();
        res.Reviewed = reviewed.Count;
        res.WrongErrorsCaught = ex.Count(e => e.ProposedWrong) == 0 ? 0 : (double)ex.Count(e => e.ProposedWrong && reviewed.Contains(e.Key)) / ex.Count(e => e.ProposedWrong);
        // 2) admission: reviewed examples now carry a human label (gold, except for annotator mistakes); the rest keep the model's label
        foreach (var e in ex)
        {
            var human = reviewed.Contains(e.Key);
            var wrong = human ? Stable("annotator" + e.Key, 1000) >= (int)(sc.AnnotatorAccuracy * 1000) : e.ProposedWrong;
            var d = pe.AuthorizeTrain("Admit", e.Domain, TrainContext(root, e, human ? "human" : "model"));
            if (d.Error is not null) throw new InvalidOperationException(d.Error);
            if (decisions.Count < 200) decisions.Add($"{e.Key}\t{(human ? "human" : "model")}\t{(d.Allow ? "admit" : "reject")}\t{string.Join(",", d.Reasons)}");
            if (!d.Allow) continue;
            res.Admitted++; if (human) res.AdmittedHuman++; else res.AdmittedSelf++;
            if (wrong) { res.WrongAdmitted++; if (!human) res.WrongAdmittedSelf++; }
            // independent audit of the gate (raw attributes, not Cedar): nothing may leak
            var (consent, lic, pii) = Governance(root, e);
            if (consent == "no") res.LeakNoConsent++;
            if (lic != "open") res.LeakNotOpen++;
            if (pii == "high") res.LeakPii++;
            if (!human && new[] { "injection", "jailbreak", "harmful_request" }.Contains(e.Domain)) res.LeakAttackSelf++;
        }
        return res;
    }
}
