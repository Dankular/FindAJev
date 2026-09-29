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
        foreach (var h in row.Heads)
            if (pack.Heads.TryGetValue(h.Task, out var attr) && h.Labels is not null)
                attrs.Add(new(attr, h.Labels[label(h)]));
        return PolicyEngine.Ctx(row.Domain, minConfidence, attrs);
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

        var registry = JsonSerializer.Deserialize<List<ModelSpec>>(File.ReadAllText(Path.Combine(root, "models.json")))!;
        var cases = JsonNode.Parse(File.ReadAllText(Path.Combine(root, "policies/cases.json")))!.AsArray();
        int pass = 0;
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
            var ok = d.Error is null && d.Allow == want && (by is null || d.Reasons.Contains(by));
            if (ok) pass++;
            else Console.WriteLine($"  FAIL {name}: got {(d.Allow ? "allow" : "deny")} by [{string.Join(", ", d.Reasons)}] err={d.Error}; want {(want ? "allow" : "deny")}{(by is null ? "" : " by " + by)}");
        }
        Console.WriteLine($"golden cases: {pass}/{cases.Count} passed");
        return failed == 0 && pass == cases.Count ? 0 : 1;
    }

    /// <summary>
    /// Gold-label coverage: authorize every pack action for every dataset row using the GOLD labels (no model involved).
    /// Shows whether policies are exercised (not all-allow / all-deny) and lists policies that never determined an outcome.
    /// </summary>
    public static int Coverage(string root, string family)
    {
        var pe = new PolicyEngine(Path.Combine(root, "policies"));
        var items = File.ReadLines(Path.Combine(root, "data/encoded", family + ".jsonl")).Select(l => JsonSerializer.Deserialize<Item>(l)!).ToList();
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
