using System.Runtime.InteropServices;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using CedarDotNet;
using CedarDotNet.Models;
using CedarDotNet.Values;

namespace FindAJev.Bench;

public sealed record Pack(string[] Actions, Dictionary<string, string> Heads);

public sealed record PolicyDecision(string Action, bool Allow, string[] Reasons, string? Error = null);

/// <summary>
/// Cedar policy engine for the harness: loads policies/*.cedar + schema, validates them against the schema with the native
/// validator, and answers per-test and run-lifecycle authorization requests. Policy ids come from the @id annotation.
/// </summary>
public sealed class PolicyEngine
{
    static readonly Regex IdRx = new("@id\\(\"([^\"]+)\"\\)", RegexOptions.Compiled);

    readonly Schema _schema;
    readonly string _schemaText;
    readonly Dictionary<string, string> _test = new();   // id -> text, per-test policies
    readonly Dictionary<string, string> _run = new();    // id -> text, run-lifecycle policies
    readonly Dictionary<string, PolicySet> _byDomain = new();
    public Dictionary<string, Pack> Packs { get; }
    public IReadOnlyDictionary<string, string> TestPolicies => _test;
    public IReadOnlyDictionary<string, string> RunPolicies => _run;

    public PolicyEngine(string dir)
    {
        _schemaText = File.ReadAllText(Path.Combine(dir, "schema.cedarschema"));
        _schema = Schema.FromText(_schemaText);
        Packs = JsonSerializer.Deserialize<JsonObject>(File.ReadAllText(Path.Combine(dir, "packs.json")))!["packs"]!
            .Deserialize<Dictionary<string, Pack>>(new JsonSerializerOptions { PropertyNameCaseInsensitive = true })!;

        foreach (var file in Directory.GetFiles(dir, "*.cedar").OrderBy(f => f))
        {
            var target = Path.GetFileName(file) == "run.cedar" ? _run : _test;
            foreach (var text in CedarUtilities.LoadPolicySet(File.ReadAllText(file)))
            {
                var m = IdRx.Match(text);
                if (!m.Success) throw new InvalidDataException($"{file}: every policy needs an @id annotation: {text[..Math.Min(80, text.Length)]}");
                if (!target.TryAdd(m.Groups[1].Value, text)) throw new InvalidDataException($"duplicate policy id {m.Groups[1].Value}");
            }
        }
        foreach (var d in Packs.Keys) _byDomain[d] = new PolicySet { StaticPolicies = ForDomain(d) };
    }

    /// <summary>Policies relevant to a domain: those naming it, plus those naming no domain at all (cross-domain guardrails).</summary>
    Dictionary<string, string> ForDomain(string domain) =>
        _test.Where(kv => kv.Value.Contains($"\"{domain}\"") || !Packs.Keys.Any(d => kv.Value.Contains($"\"{d}\"")))
             .ToDictionary(kv => kv.Key, kv => kv.Value);

    // ----------------------------------------------------------------------------------------------- validation
    [DllImport("cedar_dotnet_ffi", EntryPoint = "validate")] static extern IntPtr NativeValidate([MarshalAs(UnmanagedType.LPUTF8Str)] string call);
    [DllImport("cedar_dotnet_ffi", EntryPoint = "free_string")] static extern void NativeFree(IntPtr p);

    /// <summary>Validate every policy against the schema (strict mode). Returns human-readable problems; empty = valid.</summary>
    public List<string> Validate()
    {
        var problems = new List<string>();
        foreach (var (name, set) in new[] { ("test", _test), ("run", _run) })
        {
            var call = new JsonObject
            {
                ["validationSettings"] = new JsonObject { ["mode"] = "strict" },
                ["schema"] = _schemaText,
                ["policies"] = new JsonObject { ["staticPolicies"] = JsonSerializer.SerializeToNode(set) },
            };
            var ptr = NativeValidate(call.ToJsonString());
            try
            {
                var res = JsonNode.Parse(Marshal.PtrToStringUTF8(ptr)!)!.AsObject();
                if (res["type"]?.GetValue<string>() != "success")
                    problems.Add($"[{name}] {res["errors"]}");
                else
                {
                    foreach (var e in res["validationErrors"]!.AsArray()) problems.Add($"[{name}] {e}");
                    foreach (var w in res["validationWarnings"]!.AsArray()) problems.Add($"[{name}] warning: {w}");
                }
            }
            finally { NativeFree(ptr); }
        }
        return problems;
    }

    // ----------------------------------------------------------------------------------------------- authorization
    static readonly EntityUid Session = EntityUid.Create("Session", "test");
    static readonly EntityUid Runner = EntityUid.Create("Runner", "harness");

    PolicyDecision Call(string action, EntityUid principal, Entity resource, Entity? principalEntity, Dictionary<string, Value> ctx, PolicySet policies)
    {
        var entities = new List<Entity> { resource, principalEntity ?? new Entity { Uid = principal } };
        var ans = CedarFunctions.IsAuthorized(new AuthorizationCall
        {
            Principal = principal,
            Action = EntityUid.Create("Action", action),
            Resource = resource.Uid,
            Context = ctx,
            Schema = _schema,
            ValidateRequest = true,
            Policies = policies,
            Entities = entities,
        });
        return ans switch
        {
            AuthorizationAnswerSuccess ok => new PolicyDecision(action, ok.Response.Decision == Decision.Allow,
                ok.Response.Diagnostics.Reason.ToArray(),
                ok.Response.Diagnostics.Errors.Count > 0 ? string.Join("; ", ok.Response.Diagnostics.Errors.Select(e => $"{e.PolicyId}: {e.Error.Message}")) : null),
            AuthorizationAnswerFailure bad => new PolicyDecision(action, false, Array.Empty<string>(),
                string.Join("; ", bad.Errors.Select(e => e.Message))),
            _ => new PolicyDecision(action, false, Array.Empty<string>(), "unknown answer"),
        };
    }

    /// <summary>Authorize one action for a test. ctx keys: "domain", "minConfidence" (long) plus the pack's head attributes.</summary>
    public PolicyDecision AuthorizeTest(string domain, string testId, string action, Dictionary<string, Value> ctx)
    {
        var item = new Entity { Uid = EntityUid.Create("Item", testId), Attrs = new Dictionary<string, Value> { ["domain"] = domain } };
        return Call(action, Session, item, null, ctx, _byDomain[domain]);
    }

    /// <summary>Authorize FetchModel / RunModel for the run lifecycle.</summary>
    public PolicyDecision AuthorizeRun(string action, ModelSpec m, int threads, int cpus)
    {
        var model = new Entity
        {
            Uid = EntityUid.Create("Model", m.Id),
            Attrs = new Dictionary<string, Value> { ["license"] = m.License, ["sizeMb"] = (long)m.SizeMb, ["publisher"] = m.Repo.Split('/')[0] },
        };
        var ctx = new Dictionary<string, Value> { ["threads"] = (long)threads, ["cpus"] = (long)cpus };
        return Call(action, Runner, model, null, ctx, new PolicySet { StaticPolicies = _run });
    }

    public static Dictionary<string, Value> Ctx(string domain, long minConfidence, IEnumerable<KeyValuePair<string, string>> attrs)
    {
        var d = new Dictionary<string, Value> { ["domain"] = domain, ["minConfidence"] = minConfidence };
        foreach (var (k, v) in attrs) d[k] = v;
        return d;
    }
}
