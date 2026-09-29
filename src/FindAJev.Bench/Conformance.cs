using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace FindAJev.Bench;

public sealed record CaseResult(string Category, string Name, bool Pass, string? Detail);

/// <summary>
/// Data-driven Cedar language conformance checks: policies/conformance/*.json, one file per category (categories are discovered from
/// the directory, so adding a file adds a category). Each case names a `kind` (authorize, validate, parse-*, format, roundtrip, parts,
/// partial) and what the native library must answer. These test the Cedar engine itself (semantics, extensions, validation,
/// parsing) independent of any model, so a Cedar upgrade that changes behaviour shows up here first.
/// </summary>
public static class Conformance
{
    public static IEnumerable<string> Categories(string root) =>
        Directory.GetFiles(Path.Combine(root, "policies/conformance"), "*.json").Select(f => Path.GetFileNameWithoutExtension(f)).OrderBy(x => x);

    static readonly Regex UidRx = new("^(.*)::\"(.*)\"$");
    static JsonObject Uid(string s) { var m = UidRx.Match(s); return new JsonObject { ["type"] = m.Groups[1].Value, ["id"] = m.Groups[2].Value }; }

    static JsonObject Policies(JsonNode c)
    {
        var set = new JsonObject();
        var sp = new JsonObject();
        var list = c["policies"]?.AsArray();
        if (list is not null) for (var i = 0; i < list.Count; i++) sp[$"p{i}"] = list[i]!.GetValue<string>();
        set["staticPolicies"] = sp;
        if (c["templates"] is JsonObject t) set["templates"] = t.DeepClone();
        if (c["links"] is JsonArray l) set["templateLinks"] = l.DeepClone();
        return set;
    }

    static JsonArray Entities(JsonNode c)
    {
        var arr = new JsonArray();
        foreach (var e in c["entities"]?.AsArray() ?? new JsonArray())
        {
            arr.Add(new JsonObject
            {
                ["uid"] = Uid(e!["uid"]!.GetValue<string>()),
                ["attrs"] = e["attrs"]?.DeepClone() ?? new JsonObject(),
                ["parents"] = new JsonArray((e["parents"]?.AsArray() ?? new JsonArray()).Select(p => (JsonNode)Uid(p!.GetValue<string>())).ToArray()),
                ["tags"] = e["tags"]?.DeepClone() ?? new JsonObject(),
            });
        }
        return arr;
    }

    static JsonNode Schema(JsonNode c) => c["schema"] is null ? JsonValue.Create<string?>(null)! : c["schema"]!.DeepClone();

    public static List<CaseResult> Run(string root, ISet<string>? only = null)
    {
        var results = new List<CaseResult>();
        foreach (var cat in Categories(root))
        {
            if (only is not null && !only.Contains(cat)) continue;
            var cases = JsonNode.Parse(File.ReadAllText(Path.Combine(root, "policies/conformance", cat + ".json")))!.AsArray();
            foreach (var c in cases)
            {
                var name = c!["name"]!.GetValue<string>();
                try { results.Add(new CaseResult(cat, name, true, null).With(Check(c))); }
                catch (Exception e) { results.Add(new CaseResult(cat, name, false, "exception: " + e.Message)); }
            }
        }
        return results;
    }

    static CaseResult With(this CaseResult r, string? failure) => failure is null ? r : r with { Pass = false, Detail = failure };

    /// <summary>Returns null when the case passes, otherwise a description of what differed.</summary>
    static string? Check(JsonNode c)
    {
        var kind = c["kind"]!.GetValue<string>();
        var expect = c["expect"]!;
        switch (kind)
        {
            case "authorize":
            {
                var req = c["request"]!;
                var call = new JsonObject
                {
                    ["principal"] = Uid(req["principal"]!.GetValue<string>()), ["action"] = Uid(req["action"]!.GetValue<string>()),
                    ["resource"] = Uid(req["resource"]!.GetValue<string>()), ["context"] = req["context"]?.DeepClone() ?? new JsonObject(),
                    ["schema"] = Schema(c), ["validateRequest"] = c["validateRequest"]?.GetValue<bool>() ?? false,
                    ["policies"] = Policies(c), ["entities"] = Entities(c),
                };
                var ans = CedarNative.Obj(CedarNative.Authorize(call.ToJsonString()));
                if (expect["failure"]?.GetValue<bool>() == true)
                    return ans["type"]!.GetValue<string>() == "failure" ? Contains(ans["errors"], expect) : $"expected a failure, got {ans["type"]}";
                if (ans["type"]!.GetValue<string>() != "success") return "unexpected failure: " + Trim(ans["errors"]);
                var resp = ans["response"]!;
                var decision = resp["decision"]!.GetValue<string>();
                if (expect["decision"] is { } d && d.GetValue<string>() != decision) return $"decision {decision}, expected {d}";
                var diag = resp["diagnostics"]!;
                if (expect["reasons"] is JsonArray want)
                {
                    var got = diag["reason"]!.AsArray().Select(x => x!.GetValue<string>()).OrderBy(x => x).ToArray();
                    var exp = want.Select(x => x!.GetValue<string>()).OrderBy(x => x).ToArray();
                    if (!got.SequenceEqual(exp)) return $"reasons [{string.Join(",", got)}], expected [{string.Join(",", exp)}]";
                }
                if (expect["errors"] is { } ne && diag["errors"]!.AsArray().Count != ne.GetValue<int>())
                    return $"{diag["errors"]!.AsArray().Count} policy error(s), expected {ne}";
                return null;
            }
            case "validate":
            {
                var call = new JsonObject { ["validationSettings"] = new JsonObject { ["mode"] = c["mode"]?.GetValue<string>() ?? "strict" },
                    ["schema"] = Schema(c), ["policies"] = Policies(c) };
                var ans = CedarNative.Obj(CedarNative.ValidatePolicies(call.ToJsonString()));
                if (ans["type"]!.GetValue<string>() != "success")
                    return expect["valid"]!.GetValue<bool>() ? "validator failure: " + Trim(ans["errors"]) : Contains(ans["errors"], expect);
                var errs = ans["validationErrors"]!.AsArray();
                if (expect["warning"] is { } w)   // valid, but the validator must warn (e.g. an impossible policy)
                {
                    if (errs.Count > 0) return "expected only warnings, got errors: " + Trim(errs);
                    return ans["validationWarnings"]!.ToJsonString().Contains(w.GetValue<string>(), StringComparison.OrdinalIgnoreCase)
                        ? null : $"no warning containing '{w}': {Trim(ans["validationWarnings"])}";
                }
                if (expect["valid"]!.GetValue<bool>()) return errs.Count == 0 ? null : "expected valid, got: " + Trim(errs);
                return errs.Count == 0 ? "expected validation errors, got none" : Contains(errs, expect);
            }
            case "parse-schema": return Ok(CedarNative.ParseSchema(Schema(c).ToJsonString()), expect);
            case "parse-policies": return Ok(CedarNative.ParsePolicySet(Policies(c).ToJsonString()), expect);
            case "parse-entities":
                return Ok(CedarNative.ParseEntities(new JsonObject { ["entities"] = Entities(c), ["schema"] = Schema(c) }.ToJsonString()), expect);
            case "parse-context":
                return Ok(CedarNative.ParseContext(new JsonObject { ["context"] = c["context"]!.DeepClone(), ["schema"] = Schema(c),
                    ["action"] = c["action"] is null ? JsonValue.Create<string?>(null) : Uid(c["action"]!.GetValue<string>()) }.ToJsonString()), expect);
            case "format":
            {
                string F(string t) => CedarNative.Obj(CedarNative.FormatPolicy(new JsonObject { ["policyText"] = t, ["lineWidth"] = 80, ["indentWidth"] = 2 }.ToJsonString()))["formatted_policy"]?.GetValue<string>() ?? "";
                var once = F(c["policy"]!.GetValue<string>());
                if (once.Length == 0) return "formatter returned nothing";
                if (F(once) != once) return "formatting is not idempotent";
                if (expect["contains"] is { } s && !once.Contains(s.GetValue<string>())) return $"formatted text lacks '{s}': {once}";
                return null;
            }
            case "roundtrip":
            {
                var j1 = CedarNative.PolicyTextToJson(c["policy"]!.GetValue<string>());
                var text = CedarNative.PolicyJsonToText(j1);
                var j2 = CedarNative.PolicyTextToJson(text);
                return JsonNode.DeepEquals(JsonNode.Parse(j1), JsonNode.Parse(j2)) ? null : $"JSON changed after text round trip:\n{j1}\n{j2}";
            }
            case "parts":
            {
                var ans = CedarNative.Obj(CedarNative.PolicySetParts(c["text"]!.GetValue<string>()));
                if (ans["type"]?.GetValue<string>() != "success") return "failure: " + Trim(ans["errors"]);
                var np = ans["policies"]!.AsArray().Count; var nt = ans["policy_templates"]!.AsArray().Count;
                return np == expect["policies"]!.GetValue<int>() && nt == expect["templates"]!.GetValue<int>() ? null : $"{np} policies/{nt} templates, expected {expect["policies"]}/{expect["templates"]}";
            }
            case "partial":
            {
                var req = c["request"]!; var call = new JsonObject { ["schema"] = Schema(c), ["validateRequest"] = false, ["policies"] = Policies(c), ["entities"] = Entities(c) };
                foreach (var slot in new[] { "principal", "action", "resource" })
                    if (req[slot] is not null) call[slot] = Uid(req[slot]!.GetValue<string>());
                call["context"] = req["context"]?.DeepClone() ?? new JsonObject();
                var ans = CedarNative.Obj(CedarNative.AuthorizePartial(call.ToJsonString()));
                if (ans["type"]!.GetValue<string>() != "residuals") return "expected residuals, got " + Trim(ans);
                var decision = ans["response"]!["decision"]?.GetValue<string>() ?? "unknown";
                if (expect["decision"] is { } d && d.GetValue<string>() != decision) return $"decision {decision}, expected {d}";
                if (expect["nontrivialResiduals"] is { } nr && (ans["response"]!["nontrivialResiduals"]?.AsArray().Count ?? 0) != nr.GetValue<int>())
                    return $"{ans["response"]!["nontrivialResiduals"]?.AsArray().Count ?? 0} nontrivial residual(s), expected {nr}";
                return null;
            }
            default: return $"unknown case kind '{kind}'";
        }
    }

    static string Trim(JsonNode? n) { var s = n?.ToJsonString() ?? "null"; return s.Length > 300 ? s[..300] + "…" : s; }

    static string? Contains(JsonNode? errors, JsonNode expect)
    {
        if (expect["contains"] is not { } want) return null;
        var text = errors?.ToJsonString() ?? "";
        return text.Contains(want.GetValue<string>(), StringComparison.OrdinalIgnoreCase) ? null : $"error text lacks '{want}': {Trim(errors)}";
    }

    static string? Ok(string json, JsonNode expect)
    {
        var ans = CedarNative.Obj(json);
        var ok = ans["type"]!.GetValue<string>() == "success";
        if (expect["ok"]!.GetValue<bool>()) return ok ? null : "expected success, got: " + Trim(ans["errors"]);
        return ok ? "expected failure, got success" : Contains(ans["errors"], expect);
    }
}
