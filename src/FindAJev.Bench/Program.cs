using System.Text;
using System.Text.Json;
using FindAJev.Bench;

// findajev list | run <id> [--threads N] [--warmup N] [--repeats N] [--limit N] | rank | graph
var root = Environment.GetEnvironmentVariable("FINDAJEV_ROOT") ?? Directory.GetCurrentDirectory();
var registry = JsonSerializer.Deserialize<List<ModelSpec>>(File.ReadAllText(Path.Combine(root, "models.json")))!;
var argv = args.ToList();
string Opt(string name, string def) { var i = argv.IndexOf(name); return i >= 0 && i + 1 < argv.Count ? argv[i + 1] : def; }

try
{
return Dispatch();
}
catch (ArgumentException e) { Console.Error.WriteLine("error: " + e.Message); return 2; }
catch (FormatException e) { Console.Error.WriteLine("error: bad number in arguments (" + e.Message + ")"); return 2; }

int Dispatch()
{
switch (argv.FirstOrDefault())
{
    case "list":
        foreach (var m in registry)
            Console.WriteLine($"{m.Id,-24} {m.Family,-7} {m.Precision,-5} {(File.Exists(Path.Combine(root, m.Onnx)) ? "fetched" : "missing")}");
        return 0;

    case "run":
    {
        var id = argv.ElementAtOrDefault(1) ?? throw new ArgumentException("run <model-id>");
        var spec = registry.FirstOrDefault(m => m.Id == id) ?? throw new ArgumentException($"unknown model {id}");
        var threads = int.Parse(Opt("--threads", Environment.ProcessorCount.ToString()));
        var warmup = int.Parse(Opt("--warmup", "20"));
        var repeats = int.Parse(Opt("--repeats", "1"));
        var limit = int.Parse(Opt("--limit", "0"));
        var suites = Opt("--suites", "") is { Length: > 0 } sv ? sv.Split(',').ToHashSet() : null;   // suite ids from suites.json
        var items = Data.Load(root, spec.Family, suites, limit);
        var overrides = argv.Select((a, i) => (a, i)).Where(x => x.a == "--param" && x.i + 1 < argv.Count)
            .Select(x => argv[x.i + 1].Split('=', 2)).ToDictionary(kv => kv[0], kv => long.Parse(kv[1]));

        var cpus = int.Parse(Opt("--cpus", Environment.ProcessorCount.ToString()));
        var policyDir = Path.Combine(root, "policies");
        PolicyEngine? pe = argv.Contains("--no-policy") || !Directory.Exists(policyDir) ? null : new PolicyEngine(policyDir, overrides);
        if (pe is not null && items.Any(i => i.Labels is null)) pe = null; // encoded data predates label strings: no policies possible
        var machine = new RunMachine(spec, root, items, threads, warmup, repeats, pe, cpus);
        var r = machine.Run();
        // A run with a non-default suite selection or parameter override gets its own result file and is ranked separately.
        var variant = string.Join(";", new[] { suites is null ? "" : "suites=" + string.Join(",", suites.OrderBy(x => x)) }
            .Concat(overrides.OrderBy(kv => kv.Key).Select(kv => $"{kv.Key}={kv.Value}")).Where(x => x.Length > 0));
        r.Variant = variant;
        r.SuitesRun = (suites ?? Data.Registry(root).Select(x => x.Id).ToHashSet()).OrderBy(x => x).ToArray();
        if (pe is not null) r.PolicyParams = pe.Params.ToDictionary(kv => kv.Key, kv => kv.Value);
        var suffix = variant.Length == 0 ? "" : "." + Convert.ToHexString(System.Security.Cryptography.SHA1.HashData(System.Text.Encoding.UTF8.GetBytes(variant)))[..8].ToLowerInvariant();
        var outDir = Path.Combine(root, "results");
        Directory.CreateDirectory(outDir);
        var resultPath = Path.Combine(outDir, $"{spec.Id}.t{threads}{suffix}.json");
        File.WriteAllText(resultPath, JsonSerializer.Serialize(r, Json.Opts));
        Console.WriteLine("RESULT_FILE " + Path.GetRelativePath(root, resultPath));
        Console.WriteLine(r.State == "Scored"
            ? $"{r.Id} [{r.State}] acc={r.Accuracy:P1} p50={r.P50Ms:F1}ms p95={r.P95Ms:F1}ms {r.ItemsPerSec:F1}/s rss={r.PeakRssMb:F0}MB load={r.LoadSeconds:F1}s"
            : $"{r.Id} [{r.State}] {r.Error}");
        return r.State == "Scored" ? 0 : 1;
    }

    case "check": // pre-flight for the server, before any download: may this model be fetched and run here?
    {
        var id = argv.ElementAtOrDefault(1) ?? throw new ArgumentException("check <model-id>");
        var spec = registry.FirstOrDefault(m => m.Id == id) ?? throw new ArgumentException($"unknown model {id}");
        var pe = new PolicyEngine(Path.Combine(root, "policies"));
        var threads = int.Parse(Opt("--threads", Environment.ProcessorCount.ToString()));
        var cpus = int.Parse(Opt("--cpus", Environment.ProcessorCount.ToString()));
        var ds = new[] { "FetchModel", "RunModel" }.Select(a => pe.AuthorizeRun(a, spec, threads, cpus)).ToList();
        Console.WriteLine(JsonSerializer.Serialize(new { allow = ds.All(d => d.Allow), decisions = ds.Select(d => new { action = d.Action, allow = d.Allow, by = d.Reasons, error = d.Error }) }));
        return ds.All(d => d.Allow) ? 0 : 3;
    }

    case "probe": // developer aid: run one raw native call from a JSON file: probe <authorize|partial|validate> <file>
    {
        var body = File.ReadAllText(argv[2]);
        Console.WriteLine(argv[1] switch { "authorize" => CedarNative.Authorize(body), "partial" => CedarNative.AuthorizePartial(body), "validate" => CedarNative.ValidatePolicies(body), _ => "unknown call" });
        return 0;
    }

    case "cedar-suite": // the Cedar checks: cedar-suite [--list] [--checks a,b] [--json]
    {
        if (argv.Contains("--list"))
        {
            Console.WriteLine(JsonSerializer.Serialize(CedarChecks.All, Json.Opts));
            return 0;
        }
        var ids = Opt("--checks", "") is { Length: > 0 } cv ? cv.Split(',') : CedarChecks.All.Select(c => c.Id).ToArray();
        var outcomes = ids.Select(id => CedarChecks.Run(id, root)).ToList();
        if (argv.Contains("--json"))
            Console.WriteLine(JsonSerializer.Serialize(new { cedar = CedarNative.SdkVersionString(), language = CedarNative.LangVersionString(), checks = outcomes }, Json.Opts));
        else
            foreach (var c in outcomes)
            {
                Console.WriteLine($"{(c.Passed ? "PASS" : "FAIL")} {c.Id,-16} {c.Summary} ({c.Seconds}s)");
                foreach (var f in c.Findings.Take(8)) Console.WriteLine("       " + (f.Length > 220 ? f[..220] + "…" : f));
            }
        return outcomes.All(c => c.Passed || !c.Hard) ? 0 : 1;
    }

    case "cedar-test": // Cedar language conformance cases (policies/conformance/*.json)
    {
        var cats = Opt("--categories", "") is { Length: > 0 } cv ? cv.Split(',').ToHashSet() : null;
        var res = Conformance.Run(root, cats);
        foreach (var g in res.GroupBy(r => r.Category))
            Console.WriteLine($"{g.Key,-14} {g.Count(r => r.Pass),3}/{g.Count()}");
        foreach (var r in res.Where(r => !r.Pass)) Console.WriteLine($"  FAIL [{r.Category}] {r.Name}: {r.Detail}");
        Console.WriteLine($"cedar {CedarNative.SdkVersionString()} (language {CedarNative.LangVersionString()}): {res.Count(r => r.Pass)}/{res.Count} conformance cases passed");
        return res.All(r => r.Pass) ? 0 : 1;
    }

    case "policy-test":
        return PolicyCommands.Test(root);

    case "policy-coverage":
        return PolicyCommands.Coverage(root, argv.ElementAtOrDefault(1) ?? "julia");

    case "rank":
        var md = Ranking.Render(Path.Combine(root, "results"));
        File.WriteAllText(Path.Combine(root, "RANKING.md"), md);
        Console.WriteLine(md);
        return 0;

    case "graph": // Graphviz DOT of the run lifecycle
        var dummy = new RunMachine(registry[0], root, new(), 1, 0, 1);
        Console.WriteLine(dummy.Dot());
        return 0;

    default:
        Console.Error.WriteLine("usage: findajev list | run <id> [--threads N --warmup N --repeats N --limit N] | rank | graph");
        return 2;
}
}
