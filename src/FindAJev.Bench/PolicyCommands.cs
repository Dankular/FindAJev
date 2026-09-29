using System.Text.Json;
using System.Text.Json.Nodes;
using CedarDotNet.Values;

namespace FindAJev.Bench;

/// <summary>One dataset row = one test: all of its single-label heads (consecutive items sharing "domain:row").</summary>
public sealed record Row(string Key, string Domain, List<Item> Heads, int Start);

public static class Rows
{
    public static List<Row> Group(IEnumerable<Item> items)
    {
        var rows = new List<Row>();
        var idx = 0;
        foreach (var it in items)
        {
            var parts = it.Id.Split(':');
            var key = parts[0] + ":" + parts[1];
            if (rows.Count == 0 || rows[^1].Key != key) rows.Add(new Row(key, it.Domain, new(), idx));
            rows[^1].Heads.Add(it);
            idx++;
        }
        return rows;
    }

    /// <summary>Cedar context for a row given one label index per head (gold or predicted). Heads outside the pack are ignored.</summary>
    public static Dictionary<string, Value> Context(PolicyEngine pe, Row row, Func<Item, int> label, long minConfidence)
    {
        var pack = pe.Packs[row.Domain];
        var attrs = new List<KeyValuePair<string, string>>();
        // test-level facts from the dataset (e.g. a harm category): not model outputs, so identical on the model's side and the gold side
        foreach (var (k, v) in row.Heads[0].Ctx ?? new Dictionary<string, string>()) attrs.Add(new(k, v));
        foreach (var h in row.Heads)
        {
            if (pack.Heads.TryGetValue(h.Task, out var attr) && h.Labels is not null)
                attrs.Add(new(attr, h.Labels[label(h)]));
            // per-option metadata (retrieval: passage_pii/passage_source, tools: tool_risk): the chosen option's value
            foreach (var a in pack.OptionAttrs ?? Array.Empty<string>())
                if (h.OptAttrs is not null && h.OptAttrs.TryGetValue(a, out var vals))
                    attrs.Add(new(a, vals[label(h)]));
        }
        return PolicyEngine.Ctx(row.Domain, minConfidence, row.Heads.Max(h => h.N), attrs);
    }
}

public static class PolicyCommands
{
    /// <summary>Validate policies against the schema, then run policies/cases.json. Exit code 0 only if everything passes.</summary>
    public static int Test(string root)
    {
        var pe = new PolicyEngine(Path.Combine(root, "policies"));
        Console.WriteLine($"loaded {pe.TestPolicies.Count} test policies, {pe.RunPolicies.Count} run policies, {pe.Packs.Count} domain packs");
        var problems = pe.Validate();
        foreach (var p in problems) Console.WriteLine("  validate: " + p);
        var failed = problems.Count(p => !p.Contains("warning:"));
        Console.WriteLine(failed == 0 ? "schema validation: OK (strict)" : $"schema validation: {failed} error(s)");

        var (pass, total, fails) = RunGolden(pe, root);
        foreach (var f in fails) Console.WriteLine("  FAIL " + f);
        Console.WriteLine($"golden cases: {pass}/{total} passed");
        return failed == 0 && pass == total ? 0 : 1;
    }

    /// <summary>Run policies/cases.json (golden decisions for our own policies) against an engine. Returns (passed, total, failure descriptions).</summary>
    public static (int pass, int total, List<string> failures) RunGolden(PolicyEngine pe, string root)
    {
        var cases = JsonNode.Parse(File.ReadAllText(Path.Combine(root, "policies/cases.json")))!.AsArray();
        int pass = 0; var fails = new List<string>();
        foreach (var c in cases)
        {
            var name = c!["name"]!.GetValue<string>();
            var action = c["action"]!.GetValue<string>();
            PolicyDecision d;
            if (c["kind"]!.GetValue<string>() == "run")
            {
                var spec = new ModelSpec("case", "x", "org/case", "x", "fp32", c["license"]!.GetValue<string>(), c["sizeMb"]!.GetValue<int>());
                d = pe.AuthorizeRun(action, spec, c["threads"]!.GetValue<int>(), c["cpus"]!.GetValue<int>());
            }
            else
            {
                var ctx = new Dictionary<string, Value> { ["domain"] = c["domain"]!.GetValue<string>() };
                foreach (var (k, v) in c["ctx"]!.AsObject())
                    ctx[k] = v!.GetValueKind() == JsonValueKind.Number ? (Value)v.GetValue<long>() : (Value)v.GetValue<string>();
                d = pe.AuthorizeTest(c["domain"]!.GetValue<string>(), "case", action, ctx);
            }
            var want = c["expect"]!.GetValue<string>() == "allow";
            var by = c["by"]?.GetValue<string>();
            var exact = c["exact"]?.GetValue<bool>() == true;     // "exact": the reason set must be exactly {by}, not merely contain it
            if (d.Error is null && d.Allow == want && (by is null || (exact ? d.Reasons.Length == 1 && d.Reasons[0] == by : d.Reasons.Contains(by)))) pass++;
            else fails.Add($"{name}: got {(d.Allow ? "allow" : "deny")} by [{string.Join(", ", d.Reasons)}] err={d.Error}; want {(want ? "allow" : "deny")}{(by is null ? "" : " by " + by)}");
        }
        return (pass, cases.Count, fails);
    }

    /// <summary>
    /// Gold-label coverage: authorize every pack action for every dataset row using the GOLD labels (no model involved).
    /// Shows whether policies are exercised (not all-allow / all-deny) and lists policies that never determined an outcome.
    /// </summary>
    public static int Coverage(string root, string family)
    {
        var pe = new PolicyEngine(Path.Combine(root, "policies"));
        var items = Data.Load(root, family, null, 0);
        var rows = Rows.Group(items);
        var stats = new SortedDictionary<string, (int allow, int deny, int err)>();
        var hits = pe.TestPolicies.Keys.ToDictionary(k => k, _ => 0);
        int inPack = 0;
        foreach (var row in rows)
        {
            if (!pe.Packs.TryGetValue(row.Domain, out var pack)) continue;
            inPack++;
            var ctx = Rows.Context(pe, row, h => h.Gold, 100);
            foreach (var action in pack.Actions)
            {
                var d = pe.AuthorizeTest(row.Domain, row.Key, action, ctx);
                var k = $"{row.Domain,-18} {action}";
                var (a, dn, e) = stats.GetValueOrDefault(k);
                stats[k] = d.Error is not null ? (a, dn, e + 1) : d.Allow ? (a + 1, dn, e) : (a, dn + 1, e);
                foreach (var r in d.Reasons) if (hits.ContainsKey(r)) hits[r]++;
            }
        }
        Console.WriteLine($"{rows.Count} rows, {inPack} in a policy pack ({rows.Count - inPack} accuracy-only)\n");
        Console.WriteLine($"{"domain / action",-36} {"allow",6} {"deny",6} {"err",4}");
        foreach (var (k, v) in stats) Console.WriteLine($"{k,-36} {v.allow,6} {v.deny,6} {v.err,4}");
        // guard.* policies act on model confidence, which gold labels do not have (100 by construction): never expected to fire here.
        var dead = hits.Where(kv => kv.Value == 0 && !kv.Key.StartsWith("guard.")).Select(kv => kv.Key).ToList();
        Console.WriteLine($"\npolicies (excluding guard.*) that never determined a gold outcome: {(dead.Count == 0 ? "none" : string.Join(", ", dead))}");
        return stats.Values.Any(v => v.err > 0) ? 1 : 0;
    }
}
