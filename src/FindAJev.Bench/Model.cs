using System.Text.Json;
using System.Text.Json.Serialization;

namespace FindAJev.Bench;

public sealed record ModelSpec(
    [property: JsonPropertyName("id")] string Id,
    [property: JsonPropertyName("family")] string Family,
    [property: JsonPropertyName("repo")] string Repo,
    [property: JsonPropertyName("onnx")] string Onnx,
    [property: JsonPropertyName("precision")] string Precision,
    [property: JsonPropertyName("license")] string License = "unknown",
    [property: JsonPropertyName("sizeMb")] int SizeMb = 0);

/// <summary>One pre-tokenized decision (see tools/encode.py).</summary>
public sealed record Item(
    [property: JsonPropertyName("id")] string Id,
    [property: JsonPropertyName("domain")] string Domain,
    [property: JsonPropertyName("n")] int N,
    [property: JsonPropertyName("gold")] int Gold,
    [property: JsonPropertyName("ids")] long[] Ids,
    [property: JsonPropertyName("pos")] long[] Pos,
    [property: JsonPropertyName("qtype")] long QType = 0,
    [property: JsonPropertyName("task")] string Task = "",
    [property: JsonPropertyName("labels")] string[]? Labels = null,
    [property: JsonPropertyName("suite")] string Suite = "",
    [property: JsonPropertyName("optAttrs")] Dictionary<string, string[]>? OptAttrs = null,
    [property: JsonPropertyName("ctx")] Dictionary<string, string>? Ctx = null);

public sealed class RunResult
{
    public string Id { get; set; } = "";
    public string Family { get; set; } = "";
    public string Precision { get; set; } = "";
    public string State { get; set; } = "";
    public string? Error { get; set; }
    public int Threads { get; set; }
    public int Items { get; set; }
    public double LoadSeconds { get; set; }
    public double MeanMs { get; set; }
    public double P50Ms { get; set; }
    public double P95Ms { get; set; }
    public double ItemsPerSec { get; set; }
    public double Accuracy { get; set; }
    public Dictionary<string, double> AccuracyByDomain { get; set; } = new();
    public double PeakRssMb { get; set; }
    /// <summary>Non-default selections that make this run comparable only to runs with the same variant ("" = all suites, default parameters).</summary>
    public string Variant { get; set; } = "";
    public string[] SuitesRun { get; set; } = Array.Empty<string>();
    public Dictionary<string, long> PolicyParams { get; set; } = new();
    /// <summary>Per-suite outcome counts from the per-test state machines (empty when run without policies).</summary>
    public Dictionary<string, SuiteResult> Suites { get; set; } = new();
    public string Cpu { get; set; } = "";
    public string OrtVersion { get; set; } = "";
}

public sealed class SuiteResult
{
    public int Heads { get; set; }              // model calls (single-label decisions) in this suite
    public double Accuracy { get; set; }        // head-level argmax accuracy
    public double MeanMs { get; set; }
    public double P50Ms { get; set; }
    public double P95Ms { get; set; }
    public int Tests { get; set; }
    public int Correct { get; set; }
    public int WrongButSafe { get; set; }
    public int Overblocked { get; set; }
    public int Unsafe { get; set; }
    public int Misclassified { get; set; }
    public int Errored { get; set; }
}

public static class Json
{
    public static readonly JsonSerializerOptions Opts = new() { WriteIndented = true, PropertyNamingPolicy = JsonNamingPolicy.CamelCase };
}
