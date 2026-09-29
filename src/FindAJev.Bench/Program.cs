using System.Text;
using System.Text.Json;
using FindAJev.Bench;

// findajev list | run <id> [--threads N] [--warmup N] [--repeats N] [--limit N] | rank | graph
var root = Environment.GetEnvironmentVariable("FINDAJEV_ROOT") ?? Directory.GetCurrentDirectory();
var registry = JsonSerializer.Deserialize<List<ModelSpec>>(File.ReadAllText(Path.Combine(root, "models.json")))!;
var argv = args.ToList();
string Opt(string name, string def) { var i = argv.IndexOf(name); return i >= 0 && i + 1 < argv.Count ? argv[i + 1] : def; }

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
        var items = File.ReadLines(Path.Combine(root, "data/encoded", spec.Family + ".jsonl"))
            .Select(l => JsonSerializer.Deserialize<Item>(l)!).ToList();
        if (limit > 0) items = items.Take(limit).ToList();

        var machine = new RunMachine(spec, root, items, threads, warmup, repeats);
        var r = machine.Run();
        var outDir = Path.Combine(root, "results");
        Directory.CreateDirectory(outDir);
        File.WriteAllText(Path.Combine(outDir, $"{spec.Id}.t{threads}.json"), JsonSerializer.Serialize(r, Json.Opts));
        Console.WriteLine(r.State == "Scored"
            ? $"{r.Id} [{r.State}] acc={r.Accuracy:P1} p50={r.P50Ms:F1}ms p95={r.P95Ms:F1}ms {r.ItemsPerSec:F1}/s rss={r.PeakRssMb:F0}MB load={r.LoadSeconds:F1}s"
            : $"{r.Id} [{r.State}] {r.Error}");
        return r.State == "Scored" ? 0 : 1;
    }

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
