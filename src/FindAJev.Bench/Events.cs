using System.Text.Json;
using Stateless;

namespace FindAJev.Bench;

/// <summary>
/// Machine-readable events on stderr (one "EVENT {json}" line each), consumed by space/server.py and streamed to the dashboard.
/// Inference timing never includes emitting: events are written outside the stopwatch.
/// </summary>
public static class Events
{
    static readonly object Gate = new();
    public static bool Enabled { get; set; } = true;

    public static void Emit(object payload)
    {
        if (!Enabled) return;
        var line = "EVENT " + JsonSerializer.Serialize(payload);
        lock (Gate) Console.Error.WriteLine(line);
    }

    /// <summary>Structure of a Stateless machine (states, hierarchy, transitions with guard text) straight from GetInfo().</summary>
    public static object Graph<TS, TT>(StateMachine<TS, TT> sm, string name) where TS : notnull where TT : notnull
    {
        var info = sm.GetInfo();
        var states = info.States.Select(s => new { id = s.UnderlyingState.ToString(), parent = s.Superstate?.UnderlyingState.ToString() }).ToList();
        var edges = new List<object>();
        foreach (var s in info.States)
        {
            var from = s.UnderlyingState.ToString();
            foreach (var f in s.FixedTransitions)
                edges.Add(new
                {
                    from, to = f.DestinationState.UnderlyingState.ToString(), trigger = f.Trigger.UnderlyingTrigger.ToString(),
                    guard = string.Join("; ", f.GuardConditionsMethodDescriptions.Select(g => g.Description)),
                });
            foreach (var d in s.DynamicTransitions)
                foreach (var p in d.PossibleDestinationStates ?? new())
                    edges.Add(new { from, to = p.DestinationState, trigger = d.Trigger.UnderlyingTrigger.ToString(), guard = p.Criterion });
        }
        return new { e = "graph", name, states, edges };
    }
}
