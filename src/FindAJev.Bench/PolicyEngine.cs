using System.Runtime.InteropServices;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using CedarDotNet;
using CedarDotNet.Models;
using CedarDotNet.Values;

namespace FindAJev.Bench;

public sealed record Pack(string[] Actions, Dictionary<string, string> Heads, string? Suite = null, string[]? OptionAttrs = null, Dictionary<string, string>? Refs = null)
{
    public string SuiteName => Suite ?? "automation";
}

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
    readonly Dictionary<string, string> _oracle = new(); // id -> text, label-free oracle rules (Action::"Audit")
    readonly Dictionary<string, PolicySet> _oracleByDomain = new();
    readonly List<Entity> _ontology = new();
    readonly Dictionary<string, PolicySet> _byDomain = new();
    public PolicyEngine Reordered() { var r = WithTestPolicies(_test.Reverse().ToDictionary(kv => kv.Key, kv => kv.Value)); return r; }
    public Dictionary<string, Pack> Packs { get; }
    public IReadOnlyDictionary<string, string> TestPolicies => _test;
    public int PolicyCountFor(string domain) => _byDomain[domain].StaticPolicies.Count;
    public IReadOnlyDictionary<string, string> RunPolicies => _run;
    public IReadOnlyDictionary<string, string> OraclePolicies => _oracle;

    /// <summary>Effective policy parameters (params.json + overrides).</summary>
    public IReadOnlyDictionary<string, long> Params { get; }
    static readonly Regex ParamRx = new("\\{\\{(\\w+)\\}\\}", RegexOptions.Compiled);

    public PolicyEngine(string dir, IReadOnlyDictionary<string, long>? overrides = null)
    {
        var pp = new Dictionary<string, long>();
        var pfile = Path.Combine(dir, "params.json");
        if (File.Exists(pfile))
            foreach (var kv in JsonNode.Parse(File.ReadAllText(pfile))!.AsObject().Where(kv => !kv.Key.StartsWith("_")))
                pp[kv.Key] = kv.Value!.GetValue<long>();
        foreach (var (k, v) in overrides ?? new Dictionary<string, long>())
        {
            if (!pp.ContainsKey(k)) throw new ArgumentException($"unknown policy parameter '{k}' (known: {string.Join(", ", pp.Keys)})");
            pp[k] = v;
        }
        Params = pp;

        _schemaText = File.ReadAllText(Path.Combine(dir, "schema.cedarschema"));
        _schema = Schema.FromText(_schemaText);
        Packs = JsonSerializer.Deserialize<JsonObject>(File.ReadAllText(Path.Combine(dir, "packs.json")))!["packs"]!
            .Deserialize<Dictionary<string, Pack>>(new JsonSerializerOptions { PropertyNameCaseInsensitive = true })!;

        var ofile = Path.Combine(dir, "ontology.json");
        if (File.Exists(ofile))
            foreach (var e in JsonNode.Parse(File.ReadAllText(ofile))!["entities"]!.AsArray())
                _ontology.Add(new Entity
                {
                    Uid = EntityUid.Create(e!["type"]!.GetValue<string>(), e["id"]!.GetValue<string>()),
                    Attrs = e["attrs"]!.AsObject().ToDictionary(kv => kv.Key, kv => kv.Value!.GetValueKind() == JsonValueKind.True ? (Value)true
                        : kv.Value.GetValueKind() == JsonValueKind.False ? (Value)false : kv.Value.GetValueKind() == JsonValueKind.Number ? (Value)kv.Value.GetValue<long>() : (Value)kv.Value.GetValue<string>()),
                });

        foreach (var file in Directory.GetFiles(dir, "*.cedar").OrderBy(f => f))
        {
            var fname = Path.GetFileName(file);
            var target = fname == "run.cedar" ? _run : fname.StartsWith("oracle") ? _oracle : _test;
            var source = ParamRx.Replace(File.ReadAllText(file), m =>
                pp.TryGetValue(m.Groups[1].Value, out var val) ? val.ToString() : throw new InvalidDataException($"{file}: unknown parameter {{{{{m.Groups[1].Value}}}}}"));
            foreach (var text in CedarUtilities.LoadPolicySet(source))
            {
                var m = IdRx.Match(text);
                if (!m.Success) throw new InvalidDataException($"{file}: every policy needs an @id annotation: {text[..Math.Min(80, text.Length)]}");
                if (!target.TryAdd(m.Groups[1].Value, text)) throw new InvalidDataException($"duplicate policy id {m.Groups[1].Value}");
            }
        }
        foreach (var d in Packs.Keys) _byDomain[d] = new PolicySet { StaticPolicies = ForDomain(d) };
    }

    /// <summary>A copy of this engine with different per-test policy texts (id -> text); used by mutation testing.</summary>
    public PolicyEngine WithTestPolicies(Dictionary<string, string> replacement)
    {
        var copy = (PolicyEngine)MemberwiseClone();
        copy._test.Clear();
        foreach (var kv in replacement) copy._test[kv.Key] = kv.Value;
        copy._byDomain.Clear();
        foreach (var d in Packs.Keys) copy._byDomain[d] = new PolicySet { StaticPolicies = copy.ForDomain(d) };
        return copy;
    }

    /// <summary>Actions declared `in [AutoAct]` in the schema: the autonomous ones that guardrails target.</summary>
    public HashSet<string> AutoActions() =>
        Regex.Matches(_schemaText, @"action\s+(\w+)\s+in\s+\[AutoAct\]").Select(m => m.Groups[1].Value).ToHashSet();

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
        foreach (var (name, set) in new[] { ("test", _test), ("run", _run), ("oracle", _oracle) })
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

    /// <summary>
    /// Run the label-free oracle rules over a prediction's context. Allow = flagged as suspicious; Reasons = the rule ids that fired.
    /// Works for any domain: domains without a pack only get the generic rules.
    /// </summary>
    public PolicyDecision AuditTest(string domain, string testId, Dictionary<string, Value> ctx)
    {
        if (!_oracleByDomain.TryGetValue(domain, out var set))
        {
            var known = Packs.Keys;
            set = _oracleByDomain[domain] = new PolicySet
            {
                StaticPolicies = _oracle.Where(kv => kv.Value.Contains($"\"{domain}\"") || !known.Any(d => kv.Value.Contains($"\"{d}\"")))
                                        .ToDictionary(kv => kv.Key, kv => kv.Value),
            };
        }
        var item = new Entity { Uid = EntityUid.Create("Item", testId), Attrs = new Dictionary<string, Value> { ["domain"] = domain } };
        var principal = new Entity { Uid = Session };
        var ans = CedarFunctions.IsAuthorized(new AuthorizationCall
        {
            Principal = Session, Action = EntityUid.Create("Action", "Audit"), Resource = item.Uid, Context = ctx, Schema = _schema,
            ValidateRequest = true, Policies = set, Entities = new List<Entity> { item, principal }.Concat(_ontology).ToList(),
        });
        return ans switch
        {
            AuthorizationAnswerSuccess ok => new PolicyDecision("Audit", ok.Response.Decision == Decision.Allow, ok.Response.Diagnostics.Reason.ToArray(),
                ok.Response.Diagnostics.Errors.Count > 0 ? string.Join("; ", ok.Response.Diagnostics.Errors.Select(e => $"{e.PolicyId}: {e.Error.Message}")) : null),
            AuthorizationAnswerFailure bad => new PolicyDecision("Audit", false, Array.Empty<string>(), string.Join("; ", bad.Errors.Select(e => e.Message))),
            _ => new PolicyDecision("Audit", false, Array.Empty<string>(), "unknown answer"),
        };
    }

    /// <summary>The domain an oracle rule is written for (its text names the domain), or null for a generic rule.</summary>
    public string? OracleDomain(string ruleId) =>
        _oracle.TryGetValue(ruleId, out var text) ? Packs.Keys.FirstOrDefault(d => text.Contains($"\"{d}\"")) : null;

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

    public static Dictionary<string, Value> Ctx(string domain, long minConfidence, long options, IEnumerable<KeyValuePair<string, string>> attrs, long minMargin = 100)
    {
        var d = new Dictionary<string, Value> { ["domain"] = domain, ["minConfidence"] = minConfidence, ["options"] = options, ["minMargin"] = minMargin };
        foreach (var (k, v) in attrs) d[k] = v;
        return d;
    }
}
