using System.Runtime.InteropServices;
using System.Text.Json.Nodes;

namespace FindAJev.Bench;

/// <summary>
/// Raw access to every function the CedarDotNet FFI library exports, with JSON in / JSON out. The conformance checks use this
/// instead of the managed wrapper so they can exercise inputs the typed models cannot express (extension values, malformed
/// requests, partial requests) exactly as the native library sees them.
/// </summary>
public static class CedarNative
{
    const string Lib = "cedar_dotnet_ffi";
    [DllImport(Lib, EntryPoint = "is_authorized")] static extern IntPtr IsAuthorized([MarshalAs(UnmanagedType.LPUTF8Str)] string s);
    [DllImport(Lib, EntryPoint = "is_authorized_partial")] static extern IntPtr IsAuthorizedPartial([MarshalAs(UnmanagedType.LPUTF8Str)] string s);
    [DllImport(Lib, EntryPoint = "validate")] static extern IntPtr Validate([MarshalAs(UnmanagedType.LPUTF8Str)] string s);
    [DllImport(Lib, EntryPoint = "check_parse_policy_set")] static extern IntPtr CheckParsePolicySet([MarshalAs(UnmanagedType.LPUTF8Str)] string s);
    [DllImport(Lib, EntryPoint = "check_parse_schema")] static extern IntPtr CheckParseSchema([MarshalAs(UnmanagedType.LPUTF8Str)] string s);
    [DllImport(Lib, EntryPoint = "check_parse_entities")] static extern IntPtr CheckParseEntities([MarshalAs(UnmanagedType.LPUTF8Str)] string s);
    [DllImport(Lib, EntryPoint = "check_parse_context")] static extern IntPtr CheckParseContext([MarshalAs(UnmanagedType.LPUTF8Str)] string s);
    [DllImport(Lib, EntryPoint = "format")] static extern IntPtr Format([MarshalAs(UnmanagedType.LPUTF8Str)] string s);
    [DllImport(Lib, EntryPoint = "policy_format_text_to_json")] static extern IntPtr TextToJson([MarshalAs(UnmanagedType.LPUTF8Str)] string s);
    [DllImport(Lib, EntryPoint = "policy_format_json_to_text")] static extern IntPtr JsonToText([MarshalAs(UnmanagedType.LPUTF8Str)] string s);
    [DllImport(Lib, EntryPoint = "policy_set_text_to_parts")] static extern IntPtr TextToParts([MarshalAs(UnmanagedType.LPUTF8Str)] string s);
    [DllImport(Lib, EntryPoint = "get_lang_version")] static extern IntPtr LangVersion();
    [DllImport(Lib, EntryPoint = "get_sdk_version")] static extern IntPtr SdkVersion();
    [DllImport(Lib, EntryPoint = "free_string")] static extern void Free(IntPtr p);

    static string Take(IntPtr p)
    {
        try { return Marshal.PtrToStringUTF8(p) ?? ""; } finally { Free(p); }
    }

    public static string Authorize(string json) => Take(IsAuthorized(json));
    public static string AuthorizePartial(string json) => Take(IsAuthorizedPartial(json));
    public static string ValidatePolicies(string json) => Take(Validate(json));
    public static string ParsePolicySet(string json) => Take(CheckParsePolicySet(json));
    public static string ParseSchema(string json) => Take(CheckParseSchema(json));
    public static string ParseEntities(string json) => Take(CheckParseEntities(json));
    public static string ParseContext(string json) => Take(CheckParseContext(json));
    public static string FormatPolicy(string json) => Take(Format(json));
    public static string PolicyTextToJson(string text) => Take(TextToJson(text));
    public static string PolicyJsonToText(string json) => Take(JsonToText(json));
    public static string PolicySetParts(string text) => Take(TextToParts(text));
    public static string SdkVersionString() => Take(SdkVersion());
    public static string LangVersionString() => Take(LangVersion());

    public static JsonObject Obj(string json) => JsonNode.Parse(json)!.AsObject();
}
