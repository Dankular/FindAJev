using System.Diagnostics;
using System.Text.Json;
using System.Text.RegularExpressions;
using CedarDotNet;
using CedarDotNet.Values;

namespace FindAJev.Bench;

public sealed record CheckInfo(string Id, string Title, string Description, bool Hard);

public sealed class CheckOutcome
{
    public string Id { get; set; } = "";
    public string Title { get; set; } = "";
    public bool Hard { get; set; }
    public bool Passed { get; set; }
    public int Pass { get; set; }
    public int Total { get; set; }
    public string Summary { get; set; } = "";
    public List<string> Findings { get; set; } = new();
    public object? Details { get; set; }
    public double Seconds { get; set; }
}

/// <summary>
/// The Cedar checks, discoverable and individually selectable (`cedar-suite --list`, `--checks a,b`). "Hard" checks gate a run: a failure
/// means the policies or the engine are wrong. The others report findings (things worth a human look) or measurements.
/// </summary>
public static class CedarChecks
{
    public static readonly CheckInfo[] All =
    {
        new("policy-validate", "Schema validation", "Every policy validated against the Cedar schema in strict mode (typos, type errors, missing has-guards).", true),
        new("golden-cases", "Golden cases", "policies/cases.json: hand-written expected decisions for our own policies, incl. guardrail boundaries.", true),
        new("conformance", "Cedar conformance", "policies/conformance/*.json: Cedar language semantics, extensions, templates, validation, parsing, formatting, partial evaluation.", true),
        new("properties", "Policy properties", "Invariants checked exhaustively over every label combination of every domain pack (confidence floor, monotonicity, order invariance) plus safety-net findings.", true),
        new("mutation", "Mutation testing", "Break each policy on purpose (delete, flip effect, swap literal, relax comparison); a mutant the golden cases do not catch marks a gap in the tests.", false),
        new("gold-coverage", "Gold-label coverage", "Run every dataset row through Cedar with gold labels: which actions are allowed/denied, and which policies never decide anything.", false),
        new("oracle-gold-audit", "Oracle rules vs gold", "Run every oracle rule over the GOLD labels: a sound contradiction rule should almost never fire on human-labelled truth; when one does, the rule or the ontology disagrees with the dataset.", false),
        new("noise-sweep", "Label-noise sensitivity", "Corrupt gold labels at 5-40% and measure how often Cedar's outcome turns Unsafe or Overblocked: how fragile each policy pack is to classifier mistakes, independent of any model.", false),
        new("admission-properties", "Training-data admission", "Learning loop: every combination of consent, licence, PII, flags, consensus, confidence and margin through the admission and review policies, against an independent recomputation; nothing private, unlicensed or self-labelled-and-suspect may be admitted.", true),
        new("promotion-gate", "Promotion gate", "Learning loop: every boundary combination of the promotion policy against an independent recomputation (a learner never promotes, no regression on safety, held-out or latency), plus a demo on real results.", true),
        new("curation-sim", "Curation simulator", "Learning loop: replay logged predictions (synthetic + recorded runs) through the admission policy; measures label noise, yield and errors caught against gold, and audits independently that no forbidden example leaked.", true),
        new("bench", "Cedar latency", "Authorization latency per domain pack (CedarDotNet re-sends the policy set on every call).", false),
    };

    public static CheckOutcome Run(string id, string root)
    {
        var sw = Stopwatch.StartNew();
        var o = new CheckOutcome { Id = id, Title = All.First(c => c.Id == id).Title, Hard = All.First(c => c.Id == id).Hard };
        var pe = new PolicyEngine(Path.Combine(root, "policies"));
        switch (id)
        {
            case "policy-validate":
            {
                var probs = pe.Validate();
                var errs = probs.Where(p => !p.Contains("warning:")).ToList();
                o.Total = pe.TestPolicies.Count + pe.RunPolicies.Count + pe.OraclePolicies.Count; o.Pass = errs.Count == 0 ? o.Total : o.Total - errs.Count;
                o.Passed = errs.Count == 0; o.Summary = $"{o.Total} policies, {errs.Count} error(s), {probs.Count - errs.Count} warning(s)";
                o.Findings = probs.Select(p => p.Replace("\n", " ")).Take(20).ToList();
                break;
            }
            case "golden-cases":
            {
                var (pass, total, fails) = PolicyCommands.RunGolden(pe, root);
                o.Pass = pass; o.Total = total; o.Passed = pass == total; o.Summary = $"{pass}/{total} cases"; o.Findings = fails;
                break;
            }
            case "conformance":
            {
                var res = Conformance.Run(root);
                o.Pass = res.Count(r => r.Pass); o.Total = res.Count; o.Passed = o.Pass == o.Total;
                o.Summary = $"{o.Pass}/{o.Total} cases on Cedar {CedarNative.SdkVersionString()} (language {CedarNative.LangVersionString()})";
                o.Findings = res.Where(r => !r.Pass).Select(r => $"[{r.Category}] {r.Name}: {r.Detail}").ToList();
                o.Details = res.GroupBy(r => r.Category).Select(g => new { category = g.Key, pass = g.Count(r => r.Pass), total = g.Count() }).ToList();
                break;
            }
            case "properties": Properties(pe, root, o); break;
            case "mutation": Mutation(pe, root, o); break;
            case "gold-coverage": GoldCoverage(pe, root, o); break;
            case "oracle-gold-audit": OracleGoldAudit(pe, root, o); break;
            case "noise-sweep": NoiseSweep(pe, root, o); break;
            case "admission-properties": LearningChecks.AdmissionProperties(root, o); break;
            case "promotion-gate": LearningChecks.PromotionGate(root, o); break;
            case "curation-sim": LearningChecks.CurationSim(root, o); break;
            case "bench": Bench(pe, root, o); break;
            default: throw new ArgumentException($"unknown check '{id}'; available: {string.Join(", ", All.Select(c => c.Id))}");
        }
        o.Seconds = Math.Round(sw.Elapsed.TotalSeconds, 1);
        return o;
    }

    // ------------------------------------------------------------------------------------------------------ helpers
    static List<Item> AnyFamilyItems(string root)
    {
        foreach (var fam in new[] { "julia", "gliner", "laya" })
        {
            var items = Data.Load(root, fam, null, 0);
            if (items.Count > 0) return items;
        }
        return new();
    }

    /// <summary>Per pack: every context attribute the pack uses and the values it can take, learned from the encoded data.</summary>
    static Dictionary<string, List<(string attr, string[] values)>> AttributeDomains(PolicyEngine pe, List<Item> items)
    {
        var res = new Dictionary<string, List<(string, string[])>>();
        foreach (var (domain, pack) in pe.Packs)
        {
            var list = new List<(string, string[])>();
            foreach (var (task, attr) in pack.Heads)
            {
                var vals = items.Where(i => i.Domain == domain && i.Task == task && i.Labels is not null).SelectMany(i => i.Labels!).Distinct().OrderBy(x => x).ToArray();
                if (vals.Length > 0) list.Add((attr, vals));
            }
            foreach (var attr in pack.OptionAttrs ?? Array.Empty<string>())
            {
                var vals = items.Where(i => i.Domain == domain && i.OptAttrs is not null && i.OptAttrs.ContainsKey(attr)).SelectMany(i => i.OptAttrs![attr]).Distinct().OrderBy(x => x).ToArray();
                if (vals.Length > 0) list.Add((attr, vals));
            }
            foreach (var g in items.Where(i => i.Domain == domain && i.Ctx is not null).SelectMany(i => i.Ctx!).GroupBy(kv => kv.Key))
                list.Add((g.Key, g.Select(kv => kv.Value).Distinct().OrderBy(x => x).ToArray()));
            res[domain] = list;
        }
        return res;
    }

    static IEnumerable<Dictionary<string, string>> States(List<(string attr, string[] values)> attrs)
    {
        IEnumerable<Dictionary<string, string>> acc = new[] { new Dictionary<string, string>() };
        foreach (var (attr, values) in attrs)
            acc = acc.SelectMany(d => values.Select(v => new Dictionary<string, string>(d) { [attr] = v })).ToList();
        return acc;
    }

    static Dictionary<string, Value> Ctx(string domain, long conf, long options, Dictionary<string, string> state) =>
        PolicyEngine.Ctx(domain, conf, options, state);

    // ------------------------------------------------------------------------------------------------------ properties
    static void Properties(PolicyEngine pe, string root, CheckOutcome o)
    {
        var items = AnyFamilyItems(root);
        if (items.Count == 0) { o.Passed = true; o.Summary = "skipped: no encoded data (run tools/encode.py) to learn label sets from"; return; }
        var attrDomains = AttributeDomains(pe, items);
        long few = pe.Params["few_max_options"], minFew = pe.Params["min_conf_few"], minMany = pe.Params["min_conf_many"];
        var auto = pe.AutoActions();
        var reversed = pe.Reordered();
        var optionsGrid = new long[] { 2, 10 };
        var confGrid = new long[] { 100, minFew, minFew - 1, minMany, minMany - 1 }.Where(c => c >= 0).Distinct().OrderBy(c => c).ToArray();
        Func<long, long> bar = opt => opt <= few ? minFew : minMany;

        var hard = new Dictionary<string, (int checkedCount, List<string> bad)>
        {
            ["confidence-floor"] = (0, new()), ["guard-non-interference"] = (0, new()), ["monotone-in-confidence"] = (0, new()), ["order-invariance"] = (0, new()),
        };
        var findings = new Dictionary<string, (int total, List<string> examples)> { ["no-human-path-after-safety-forbid"] = (0, new()), ["bot-and-human-both-allowed"] = (0, new()) };
        void Hard(string k, bool ok, string ex) { var (c, b) = hard[k]; hard[k] = (c + 1, b); if (!ok && b.Count < 5) b.Add(ex); if (!ok && b.Count >= 5) { } if (!ok) { } }
        var violations = new Dictionary<string, int>();
        void Bad(string k, bool ok, string ex)
        {
            var (c, b) = hard[k]; hard[k] = (c + 1, b);
            if (!ok) { violations[k] = violations.GetValueOrDefault(k) + 1; if (b.Count < 5) b.Add(ex); }
        }

        int stateCount = 0;
        foreach (var (domain, pack) in pe.Packs)
        {
            var states = States(attrDomains[domain]).ToList();
            if (states.Count == 0) continue;
            for (var si = 0; si < states.Count; si++)
            {
                var st = states[si]; stateCount++;
                var desc = $"{domain} {string.Join(",", st.Select(kv => kv.Key + "=" + kv.Value))}";
                foreach (var opt in optionsGrid)
                {
                    var byConf = new Dictionary<long, Dictionary<string, PolicyDecision>>();
                    foreach (var conf in confGrid)
                    {
                        var ctx = Ctx(domain, conf, opt, st);
                        byConf[conf] = pack.Actions.ToDictionary(a => a, a => pe.AuthorizeTest(domain, "prop", a, ctx));
                    }
                    var b = bar(opt);
                    foreach (var conf in confGrid)
                        foreach (var a in pack.Actions)
                        {
                            var d = byConf[conf][a];
                            if (d.Error is not null) throw new InvalidOperationException($"{desc}: Cedar error on {a}: {d.Error}");
                            if (auto.Contains(a) && conf < b) Bad("confidence-floor", !d.Allow, $"{desc} options={opt} conf={conf}: {a} allowed below the bar {b}");
                            if (conf >= b) Bad("guard-non-interference", d.Allow == byConf[100][a].Allow, $"{desc} options={opt} conf={conf}: {a} differs from conf=100");
                        }
                    foreach (var a in pack.Actions)
                        for (var i = 1; i < confGrid.Length; i++)
                            if (byConf[confGrid[i - 1]][a].Allow) Bad("monotone-in-confidence", byConf[confGrid[i]][a].Allow, $"{desc} options={opt}: {a} allowed at {confGrid[i - 1]} but not at {confGrid[i]}");
                }
                // order invariance on every 4th state (Cedar policy sets are unordered; this also guards the harness plumbing)
                if (si % 4 == 0)
                    foreach (var a in pack.Actions)
                    {
                        var ctx = Ctx(domain, 100, 2, st);
                        var x = pe.AuthorizeTest(domain, "prop", a, ctx); var y = reversed.AuthorizeTest(domain, "prop", a, ctx);
                        Bad("order-invariance", x.Allow == y.Allow && x.Reasons.OrderBy(r => r).SequenceEqual(y.Reasons.OrderBy(r => r)), $"{desc}: {a} differs when policies are reordered");
                    }
                // findings at full confidence
                var full = Ctx(domain, 100, 2, st);
                var dec = pack.Actions.ToDictionary(a => a, a => pe.AuthorizeTest(domain, "prop", a, full));
                var forbidDenied = pack.Actions.Where(a => auto.Contains(a) && !dec[a].Allow && dec[a].Reasons.Length > 0).ToList();
                if (forbidDenied.Count > 0)
                {
                    var (t, ex) = findings["no-human-path-after-safety-forbid"]; t++;
                    // every forbidden autonomous action must leave the request some way forward: another allowed action, automatic or human
                    var stranded = forbidDenied.Where(fa => !pack.Actions.Any(b => b != fa && dec[b].Allow)).ToList();
                    if (stranded.Count > 0) ex.Add($"{desc}: {string.Join("/", stranded)} forbidden and no other action allowed");
                    findings["no-human-path-after-safety-forbid"] = (t, ex);
                }
                if (pack.Actions.Any(a => auto.Contains(a) && dec[a].Allow) && dec.TryGetValue("EscalateHuman", out var esc) && esc.Allow)
                {
                    var (t, ex) = findings["bot-and-human-both-allowed"]; t++;
                    if (ex.Count < 5) ex.Add(desc);
                    findings["bot-and-human-both-allowed"] = (t, ex);
                }
            }
        }
        o.Total = hard.Sum(h => h.Value.checkedCount);
        o.Pass = o.Total - violations.Sum(v => v.Value);
        o.Passed = violations.Count == 0;
        o.Summary = $"{stateCount} label combinations, {o.Total} invariant checks, {violations.Sum(v => v.Value)} violation(s)";
        o.Findings = hard.Where(h => h.Value.bad.Count > 0).SelectMany(h => h.Value.bad.Select(b => $"VIOLATION {h.Key}: {b}")).ToList();
        foreach (var (k, (t, ex)) in findings)
        {
            var real = k == "no-human-path-after-safety-forbid" ? ex.Count : t;
            if (real > 0) o.Findings.Add($"finding {k}: {real} state(s), e.g. {string.Join(" | ", ex.Take(4))}");
        }
        o.Details = new
        {
            properties = hard.Select(h => new { property = h.Key, checkedCount = h.Value.checkedCount, violations = violations.GetValueOrDefault(h.Key) }),
            findings = findings.Select(f => new { finding = f.Key, states = f.Key == "no-human-path-after-safety-forbid" ? f.Value.examples.Count : f.Value.total, examples = f.Value.examples }),
        };
    }

    // ------------------------------------------------------------------------------------------------------ mutation
    static void Mutation(PolicyEngine pe, string root, CheckOutcome o)
    {
        var (bp, bt, _) = PolicyCommands.RunGolden(pe, root);
        if (bp != bt) { o.Passed = false; o.Summary = "golden cases must pass before mutation testing is meaningful"; return; }
        var all = pe.TestPolicies.ToDictionary(kv => kv.Key, kv => kv.Value);
        int total = 0, killed = 0, invalid = 0; var survivors = new List<string>();
        foreach (var (id, text) in all)
        {
            var mutants = new List<(string name, string? text)> { ("delete", null) };
            var flipped = Regex.Replace(text, @"(?m)^(permit|forbid)\b", m => m.Value == "permit" ? "forbid" : "permit", RegexOptions.None, TimeSpan.FromSeconds(1));
            if (flipped != text) mutants.Add(("flip-effect", flipped));
            var yes = text.IndexOf("\"yes\"", StringComparison.Ordinal); var no = text.IndexOf("\"no\"", StringComparison.Ordinal);
            if (yes >= 0 && (no < 0 || yes < no)) mutants.Add(("swap-literal yes->no", text.Remove(yes, 5).Insert(yes, "\"no\"")));
            else if (no >= 0) mutants.Add(("swap-literal no->yes", text.Remove(no, 4).Insert(no, "\"yes\"")));
            var lt = Regex.Match(text, @" < (?!=)");
            if (lt.Success) mutants.Add(("relax < to <=", text.Remove(lt.Index, 3).Insert(lt.Index, " <= ")));
            var gt = Regex.Match(text, @" > (?!=)");
            if (gt.Success) mutants.Add(("relax > to >=", text.Remove(gt.Index, 3).Insert(gt.Index, " >= ")));
            foreach (var (name, mtext) in mutants)
            {
                var set = new Dictionary<string, string>(all);
                if (mtext is null) set.Remove(id);
                else
                {
                    try { CedarUtilities.LoadPolicySet(mtext); } catch { invalid++; continue; }   // an unparsable mutant is not a real test of anything
                    set[id] = mtext;
                }
                total++;
                var (mp, mt, _) = PolicyCommands.RunGolden(pe.WithTestPolicies(set), root);
                if (mp < mt) killed++; else survivors.Add($"{id}: {name}");
            }
        }
        o.Total = total; o.Pass = killed; o.Passed = true;   // informative: the score is the deliverable, not a gate
        o.Summary = $"{killed}/{total} mutants killed ({(total == 0 ? 0 : 100.0 * killed / total):F0}%), {invalid} unparsable mutants skipped";
        o.Findings = survivors.Select(s => "survived: " + s).ToList();
        o.Details = new { killed, total, score = total == 0 ? 0 : (double)killed / total, survivors };
    }

    // ------------------------------------------------------------------------------------------------------ gold coverage
    static void GoldCoverage(PolicyEngine pe, string root, CheckOutcome o)
    {
        var items = AnyFamilyItems(root);
        if (items.Count == 0) { o.Passed = true; o.Summary = "skipped: no encoded data"; return; }
        var rows = Rows.Group(items);
        var stats = new SortedDictionary<string, (int allow, int deny)>();
        var hits = pe.TestPolicies.Keys.ToDictionary(k => k, _ => 0);
        int inPack = 0;
        foreach (var row in rows)
        {
            if (!pe.Packs.TryGetValue(row.Domain, out var pack)) continue;
            inPack++;
            var ctx = Rows.Context(pe, row, h => h.Gold, 100);
            foreach (var a in pack.Actions)
            {
                var d = pe.AuthorizeTest(row.Domain, row.Key, a, ctx);
                var k = $"{row.Domain} / {a}"; var (al, de) = stats.GetValueOrDefault(k);
                stats[k] = d.Allow ? (al + 1, de) : (al, de + 1);
                foreach (var r in d.Reasons) if (hits.ContainsKey(r)) hits[r]++;
            }
        }
        var dead = hits.Where(kv => kv.Value == 0 && !kv.Key.StartsWith("guard.")).Select(kv => kv.Key).ToList();
        var constant = stats.Where(kv => kv.Value.allow == 0 || kv.Value.deny == 0).Select(kv => kv.Key).ToList();
        o.Total = stats.Count; o.Pass = stats.Count - constant.Count; o.Passed = true;
        o.Summary = $"{rows.Count} rows ({inPack} in a pack), {stats.Count} domain/action pairs, {dead.Count} policy(ies) never decide a gold outcome";
        o.Findings = dead.Select(d => "never decides a gold outcome: " + d).Concat(constant.Select(c => "always the same decision on gold labels: " + c)).ToList();
        o.Details = stats.Select(kv => new { pair = kv.Key, allow = kv.Value.allow, deny = kv.Value.deny });
    }

    // ------------------------------------------------------------------------------------------------------ oracle vs gold
    static void OracleGoldAudit(PolicyEngine pe, string root, CheckOutcome o)
    {
        var items = AnyFamilyItems(root);
        if (items.Count == 0) { o.Passed = true; o.Summary = "skipped: no encoded data"; return; }
        var rows = Rows.Group(items);
        var fired = pe.OraclePolicies.Keys.ToDictionary(k => k, _ => 0);
        var examples = pe.OraclePolicies.Keys.ToDictionary(k => k, _ => new List<string>());
        var domainRows = rows.GroupBy(r => r.Domain).ToDictionary(g => g.Key, g => g.Count());
        var ruleDomain = new Dictionary<string, string?>();
        foreach (var (id, text) in pe.OraclePolicies)
            ruleDomain[id] = domainRows.Keys.FirstOrDefault(d => text.Contains($"\"{d}\""));   // null = generic rule
        foreach (var row in rows)
        {
            // margin = 100: this audits the coherence rules, not the confidence-based ones (gold labels have no uncertainty)
            var d = pe.AuditTest(row.Domain, row.Key, Rows.Context(pe, row, h => h.Gold, 100, 100));
            if (d.Error is not null) throw new InvalidOperationException(d.Error);
            if (d.Allow)
                foreach (var r in d.Reasons)
                {
                    fired[r]++;
                    if (examples[r].Count < 3) examples[r].Add(string.Join(", ", row.Heads.Select(h => $"{h.Task}={h.Labels?[h.Gold]}")));
                }
        }
        var table = pe.OraclePolicies.Keys.OrderBy(k => k).Select(k =>
        {
            var dom = ruleDomain[k]; var n = dom is null ? rows.Count : domainRows[dom];
            return new { rule = k, domain = dom ?? "(all)", rows = n, firedOnGold = fired[k], rate = Math.Round(100.0 * fired[k] / n, 2), examples = examples[k] };
        }).ToList();
        o.Total = table.Count; o.Pass = table.Count(t => t.firedOnGold == 0); o.Passed = true;
        o.Summary = $"{table.Count} rules, {o.Pass} never fire on gold; {table.Count - o.Pass} disagree with the dataset on at least one row";
        o.Findings = table.Where(t => t.firedOnGold > 0).Select(t => $"{t.rule} fires on {t.firedOnGold}/{t.rows} gold rows ({t.rate}%), e.g. {string.Join(" | ", t.examples)}").ToList();
        o.Details = table;
    }

    // ------------------------------------------------------------------------------------------------------ noise sweep
    static void NoiseSweep(PolicyEngine pe, string root, CheckOutcome o)
    {
        var items = AnyFamilyItems(root);
        if (items.Count == 0) { o.Passed = true; o.Summary = "skipped: no encoded data"; return; }
        var rng = new Random(7);
        var rates = new[] { 0.05, 0.10, 0.20, 0.40 };
        const int trials = 5, rowsPerDomain = 60;
        var memo = new Dictionary<string, bool>();   // decisions are deterministic, and label space is small: memoise by (domain, labels, action)
        bool Allow(Row row, int[] labels, string action)
        {
            var key = row.Domain + "|" + string.Join(",", labels) + "|" + action;
            if (memo.TryGetValue(key, out var v)) return v;
            var ctx = Rows.Context(pe, row, h => labels[row.Heads.IndexOf(h)], 100);
            var d = pe.AuthorizeTest(row.Domain, row.Key, action, ctx);
            if (d.Error is not null) throw new InvalidOperationException(d.Error);
            return memo[key] = d.Allow;
        }
        var table = new List<object>();
        foreach (var g in Rows.Group(items).Where(r => pe.Packs.ContainsKey(r.Domain)).GroupBy(r => r.Domain))
        {
            var pack = pe.Packs[g.Key];
            var rows = g.OrderBy(_ => rng.Next()).Take(rowsPerDomain).ToList();
            var perRate = new List<object>();
            foreach (var rate in rates)
            {
                int n = 0, unsafeN = 0, overN = 0;
                foreach (var row in rows)
                {
                    var gold = row.Heads.Select(h => h.Gold).ToArray();
                    for (var t = 0; t < trials; t++)
                    {
                        var pred = gold.ToArray();
                        for (var h = 0; h < pred.Length; h++)
                            if (rng.NextDouble() < rate && row.Heads[h].N > 1)
                                pred[h] = (gold[h] + 1 + rng.Next(row.Heads[h].N - 1)) % row.Heads[h].N;   // a different label, uniformly
                        var unsafeRow = pack.Actions.Any(a => Allow(row, pred, a) && !Allow(row, gold, a));
                        var overRow = !unsafeRow && pack.Actions.Any(a => !Allow(row, pred, a) && Allow(row, gold, a));
                        n++; if (unsafeRow) unsafeN++; else if (overRow) overN++;
                    }
                }
                perRate.Add(new { rate, unsafePct = Math.Round(100.0 * unsafeN / n, 1), overblockedPct = Math.Round(100.0 * overN / n, 1) });
            }
            table.Add(new { domain = g.Key, suite = pack.SuiteName, rows = rows.Count, sweep = perRate });
        }
        o.Passed = true; o.Total = table.Count; o.Pass = table.Count;
        o.Summary = $"{table.Count} packs x {rates.Length} noise rates x {trials} trials on up to {rowsPerDomain} rows each (confidence held at 100 to isolate label effects)";
        o.Details = table;
        o.Findings = table.Cast<dynamic>().Where(t => ((IEnumerable<dynamic>)t.sweep).Any(x => x.rate == 0.20 && x.unsafePct >= 30))
            .Select(t => $"fragile: {t.domain} — {((IEnumerable<dynamic>)t.sweep).First(x => x.rate == 0.20).unsafePct}% Unsafe at 20% label noise").Cast<string>().ToList();
    }

    // ------------------------------------------------------------------------------------------------------ bench
    static void Bench(PolicyEngine pe, string root, CheckOutcome o)
    {
        var items = AnyFamilyItems(root);
        if (items.Count == 0) { o.Passed = true; o.Summary = "skipped: no encoded data"; return; }
        var rows = Rows.Group(items).GroupBy(r => r.Domain).Where(g => pe.Packs.ContainsKey(g.Key)).ToList();
        var table = new List<object>(); var all = new List<double>();
        foreach (var g in rows)
        {
            var ms = new List<double>();
            foreach (var row in g.Take(60))
            {
                var ctx = Rows.Context(pe, row, h => h.Gold, 100);
                foreach (var a in pe.Packs[g.Key].Actions)
                {
                    var sw = Stopwatch.GetTimestamp();
                    pe.AuthorizeTest(row.Domain, row.Key, a, ctx);
                    ms.Add(Stopwatch.GetElapsedTime(sw).TotalMilliseconds);
                }
            }
            ms.Sort(); all.AddRange(ms);
            table.Add(new { domain = g.Key, policies = pe.PolicyCountFor(g.Key), calls = ms.Count, p50Ms = Math.Round(ms[ms.Count / 2], 3), p95Ms = Math.Round(ms[(int)(ms.Count * 0.95)], 3) });
        }
        all.Sort();
        o.Total = all.Count; o.Pass = all.Count; o.Passed = true;
        o.Summary = $"{all.Count} authorizations, p50 {all[all.Count / 2]:F2} ms, p95 {all[(int)(all.Count * 0.95)]:F2} ms (each call re-sends the domain's policy set)";
        o.Details = table;
    }
}
