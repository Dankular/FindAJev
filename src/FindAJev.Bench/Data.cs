using System.Text.Json;
using System.Text.Json.Nodes;

namespace FindAJev.Bench;

public sealed record SuiteDef(string Id, string Title, string Dataset, string File, string[] Provides);

public static class Data
{
    /// <summary>The declarative suite registry (suites.json). Adding a suite there makes it loadable, selectable and listed everywhere.</summary>
    public static List<SuiteDef> Registry(string root)
    {
        var path = Path.Combine(root, "suites.json");
        var arr = JsonNode.Parse(File.ReadAllText(path))!["suites"]!.AsArray();
        return arr.Select(n => new SuiteDef(n!["id"]!.GetValue<string>(), n["title"]!.GetValue<string>(), n["dataset"]!.GetValue<string>(),
            n["file"]!.GetValue<string>(), n["provides"]!.AsArray().Select(x => x!.GetValue<string>()).ToArray())).ToList();
    }

    /// <summary>
    /// Load encoded items for every registered suite whose file exists, in registry order (dataset by dataset).
    /// `suites` filters by suite id (null = all); `limitPerFile` &gt; 0 keeps the first N items of each file so a smoke run touches every suite.
    /// </summary>
    public static List<Item> Load(string root, string family, ISet<string>? suites, int limitPerFile)
    {
        var reg = Registry(root);
        if (suites is not null)
        {
            var unknown = suites.Where(s => reg.All(r => r.Id != s)).ToList();
            if (unknown.Count > 0) throw new ArgumentException($"unknown suite(s) {string.Join(", ", unknown)}; available: {string.Join(", ", reg.Select(r => r.Id))}");
        }
        var items = new List<Item>();
        foreach (var def in reg)
        {
            if (suites is not null && !suites.Contains(def.Id)) continue;
            var path = Path.Combine(root, "data/encoded", def.File.Replace("{family}", family));
            if (!File.Exists(path)) continue;
            var part = File.ReadLines(path).Select(l => JsonSerializer.Deserialize<Item>(l)!);
            items.AddRange(limitPerFile > 0 ? part.Take(limitPerFile) : part);
        }
        return items;
    }
}
