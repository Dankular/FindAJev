using System.Text.Json;
using System.Text.Json.Serialization;

namespace FindAJev.Bench;

public sealed record ModelSpec(
    [property: JsonPropertyName("id")] string Id,
    [property: JsonPropertyName("family")] string Family,
    [property: JsonPropertyName("repo")] string Repo,
    [property: JsonPropertyName("onnx")] string Onnx,
    [property: JsonPropertyName("precision")] string Precision);

/// <summary>One pre-tokenized decision (see tools/encode.py).</summary>
public sealed record Item(
    [property: JsonPropertyName("id")] string Id,
    [property: JsonPropertyName("domain")] string Domain,
    [property: JsonPropertyName("n")] int N,
    [property: JsonPropertyName("gold")] int Gold,
    [property: JsonPropertyName("ids")] long[] Ids,
    [property: JsonPropertyName("pos")] long[] Pos,
    [property: JsonPropertyName("qtype")] long QType = 0);

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
    public string Cpu { get; set; } = "";
    public string OrtVersion { get; set; } = "";
}

public static class Json
{
    public static readonly JsonSerializerOptions Opts = new() { WriteIndented = true, PropertyNamingPolicy = JsonNamingPolicy.CamelCase };
}
