using System.Diagnostics;
using System.Text.Json;
using Microsoft.ML.OnnxRuntime;
using Microsoft.ML.OnnxRuntime.Tensors;
using Stateless;

namespace FindAJev.Bench;

public enum RunState { Running, Pending, ModelReady, SessionLoaded, WarmedUp, Measured, Scored, Failed }
public enum RunTrigger { Verify, Load, Warm, Measure, Score, Fail }

/// <summary>
/// The lifecycle of benchmarking one model, as a Stateless machine.
/// Every working state is a substate of Running, so a single Running.Permit(Fail) covers all of them.
/// </summary>
public sealed class RunMachine
{
    readonly ModelSpec _spec;
    readonly string _root;
    readonly int _threads, _warmup, _repeats;
    readonly List<Item> _items;
    readonly StateMachine<RunState, RunTrigger> _sm;
    readonly PolicyEngine? _pe;
    readonly int _cpus;
    string? _policyDenial;
    readonly List<TestMachine> _tests = new();

    InferenceSession? _session;
    List<Dictionary<string, OrtValue>>? _inputs;
    readonly List<double> _ms = new();
    readonly List<float[]> _logits = new();
    readonly RunResult _r = new();

    public RunState State => _sm.State;
    public RunResult Result => _r;

    public RunMachine(ModelSpec spec, string root, List<Item> items, int threads, int warmup, int repeats,
                      PolicyEngine? pe = null, int cpus = 0)
    {
        (_spec, _root, _items, _threads, _warmup, _repeats) = (spec, root, items, threads, warmup, repeats);
        (_pe, _cpus) = (pe, cpus > 0 ? cpus : Environment.ProcessorCount);
        _r.Id = spec.Id; _r.Family = spec.Family; _r.Precision = spec.Precision; _r.Threads = threads;
        _r.Items = items.Count; _r.OrtVersion = OrtEnv.Instance().GetVersionString();
        _r.Cpu = CpuName();

        _sm = new StateMachine<RunState, RunTrigger>(RunState.Pending);

        _sm.Configure(RunState.Running).Permit(RunTrigger.Fail, RunState.Failed);

        _sm.Configure(RunState.Pending).SubstateOf(RunState.Running)
            .PermitIf(RunTrigger.Verify, RunState.ModelReady, () => File.Exists(OnnxPath) && _policyDenial is null,
                      "model file present and Cedar allows FetchModel + RunModel");

        _sm.Configure(RunState.ModelReady).SubstateOf(RunState.Running)
            .Permit(RunTrigger.Load, RunState.SessionLoaded);

        _sm.Configure(RunState.SessionLoaded).SubstateOf(RunState.Running)
            .OnEntry(CreateSession)
            .Permit(RunTrigger.Warm, RunState.WarmedUp);

        _sm.Configure(RunState.WarmedUp).SubstateOf(RunState.Running)
            .OnEntry(WarmUp)
            .Permit(RunTrigger.Measure, RunState.Measured);

        _sm.Configure(RunState.Measured).SubstateOf(RunState.Running)
            .OnEntry(Measure)
            .Permit(RunTrigger.Score, RunState.Scored);

        _sm.Configure(RunState.Scored)
            .OnEntry(Score);

        _sm.Configure(RunState.Failed)
            .OnEntry(t => { _r.State = "Failed"; });

        _sm.OnTransitioned(t => Events.Emit(new { e = "run", model = _spec.Id, from = t.Source.ToString(), to = t.Destination.ToString() }));
    }

    /// <summary>Ask Cedar whether this model may be fetched and run here. Sets _policyDenial when it may not.</summary>
    void EnforceRunPolicy()
    {
        if (_pe is null) return;
        foreach (var action in new[] { "FetchModel", "RunModel" })
        {
            var d = _pe.AuthorizeRun(action, _spec, _threads, _cpus);
            Events.Emit(new { e = "policy", scope = "run", model = _spec.Id, action, allow = d.Allow, by = d.Reasons });
            if (d.Error is not null) throw new InvalidOperationException($"Cedar error on {action}: {d.Error}");
            if (!d.Allow)
            {
                _policyDenial = $"Cedar denied {action} for {_spec.Id} ({(d.Reasons.Length > 0 ? string.Join(", ", d.Reasons) : "no permit policy matched")})";
                return;
            }
        }
    }

    string OnnxPath => Path.Combine(_root, _spec.Onnx);

    /// <summary>Drive the machine to a terminal state. Never throws; failures end in Failed with Result.Error set.</summary>
    public RunResult Run()
    {
        try
        {
            Events.Emit(Events.Graph(_sm, "run"));
            Events.Emit(TestMachine.Graph());
            if (_pe is not null)
            {
                var bad = _pe.Validate().Where(p => !p.Contains("warning:")).ToList();
                if (bad.Count > 0) throw new InvalidOperationException("policy validation failed: " + string.Join(" | ", bad).Replace("\n", " "));
            }
            EnforceRunPolicy();
            if (_policyDenial is not null) throw new UnauthorizedAccessException(_policyDenial);
            if (!_sm.CanFire(RunTrigger.Verify))
                throw new FileNotFoundException($"missing {OnnxPath} (run: python tools/fetch.py {_spec.Id})");
            _sm.Fire(RunTrigger.Verify);
            _sm.Fire(RunTrigger.Load);
            _sm.Fire(RunTrigger.Warm);
            _sm.Fire(RunTrigger.Measure);
            _sm.Fire(RunTrigger.Score);
        }
        catch (Exception e)
        {
            _r.Error = e.Message;
            if (_sm.CanFire(RunTrigger.Fail)) _sm.Fire(RunTrigger.Fail); else _r.State = "Failed";
        }
        finally { _session?.Dispose(); }
        return _r;
    }

    public string Dot() => Stateless.Graph.UmlDotGraph.Format(_sm.GetInfo());

    void CreateSession()
    {
        Progress("loading", 0, _items.Count, 0);
        var sw = Stopwatch.StartNew();
        var o = new SessionOptions
        {
            IntraOpNumThreads = _threads,
            InterOpNumThreads = 1,
            ExecutionMode = ExecutionMode.ORT_SEQUENTIAL,
            GraphOptimizationLevel = GraphOptimizationLevel.ORT_ENABLE_ALL,
        };
        _session = new InferenceSession(OnnxPath, o); // CPUExecutionProvider is the default; no other EP is appended
        _inputs = _items.Select(BuildInputs).ToList();
        _r.LoadSeconds = sw.Elapsed.TotalSeconds;
        Progress("loaded", 0, _items.Count, 0);
    }

    // Input names verified against the ONNX graphs with onnxruntime (Python): see README.
    Dictionary<string, OrtValue> BuildInputs(Item it)
    {
        static OrtValue L(long[] v, long[] shape) => OrtValue.CreateTensorValueFromMemory(v, shape);
        var n = it.Ids.Length;
        var ids = L(it.Ids, new long[] { 1, n });
        var att = L(Enumerable.Repeat(1L, n).ToArray(), new long[] { 1, n });
        var pos = L(it.Pos, new long[] { 1, it.Pos.Length });
        return _spec.Family switch
        {
            "gliner" => new() { ["input_ids"] = ids, ["attention_mask"] = att, ["label_positions"] = pos },
            "julia" or "laya" => new()
            {
                ["input_ids"] = ids, ["attention_mask"] = att, ["marker_pos"] = pos,
                ["marker_mask"] = OrtValue.CreateTensorValueFromMemory(Enumerable.Repeat(true, it.Pos.Length).ToArray(), new long[] { 1, it.Pos.Length }),
                ["qtype"] = L(new[] { it.QType }, new long[] { 1 }),
            },
            _ => throw new NotSupportedException($"family {_spec.Family}"),
        };
    }

    float[] Infer(int i)
    {
        using var run = new RunOptions();
        var inp = _inputs![i];
        using var outs = _session!.Run(run, inp.Keys.ToArray(), inp.Values.ToArray(), new[] { "logits" });
        return outs[0].GetTensorDataAsSpan<float>().ToArray();
    }

    void WarmUp()
    {
        Progress("warmup", 0, _warmup, 0);
        for (var k = 0; k < _warmup; k++) Infer(k % _items.Count);
    }

    static Prediction Predict(float[] logits, int n)
    {
        int arg = 0;
        for (var k = 1; k < n; k++) if (logits[k] > logits[arg]) arg = k;
        double sum = 0;
        for (var k = 0; k < n; k++) sum += Math.Exp(logits[k] - logits[arg]);
        return new Prediction(arg, (int)Math.Round(100.0 / sum)); // softmax top probability, uncalibrated
    }

    double TimedInfer(int i)
    {
        var sw = Stopwatch.GetTimestamp();
        var lg = Infer(i);
        var ms = Stopwatch.GetElapsedTime(sw).TotalMilliseconds;
        _ms.Add(ms);
        _lastLogits = lg;
        return ms;
    }
    float[] _lastLogits = Array.Empty<float>();

    void Measure()
    {
        // Per-decision latency at batch size 1, sequential, wall clock around session.Run only. Cedar and event emission
        // happen outside the stopwatch. Repeat 1 records logits and runs the per-test machines; every repeat feeds latency.
        var total = _repeats * _items.Count;
        Progress("measure", 0, total, 0);
        var rows = _pe is null ? null : Rows.Group(_items);
        if (rows is not null)
            Events.Emit(new
            {
                e = "plan", model = _spec.Id, tests = rows.Count,
                suites = rows.GroupBy(r => _pe!.Packs.ContainsKey(r.Domain) ? "automation" : "classification").ToDictionary(g => g.Key, g => g.Count()),
                domains = rows.GroupBy(r => r.Domain).ToDictionary(g => g.Key, g => g.Count()),
                packDomains = _pe!.Packs.Keys.ToDictionary(k => k, _ => true),
                keys = rows.Select(r => r.Key + "|" + r.Domain),
            });

        for (var rep = 0; rep < _repeats; rep++)
        {
            if (rows is null || rep > 0)
            {
                for (var i = 0; i < _items.Count; i++)
                {
                    TimedInfer(i);
                    if (rep == 0) _logits.Add(_lastLogits);
                    if (_ms.Count % 25 == 0 || _ms.Count == total) Progress("measure", _ms.Count, total, _ms.Average());
                }
                continue;
            }
            for (var ri = 0; ri < rows.Count; ri++)
            {
                var row = rows[ri];
                var tm = new TestMachine(ri, row, _pe);
                tm.Start();
                var preds = new Prediction[row.Heads.Count];
                for (var h = 0; h < row.Heads.Count; h++)
                {
                    TimedInfer(row.Start + h);
                    _logits.Add(_lastLogits);
                    preds[h] = Predict(_lastLogits, row.Heads[h].N);
                    if (_ms.Count % 25 == 0 || _ms.Count == total) Progress("measure", _ms.Count, total, _ms.Average());
                }
                tm.Complete(preds);
                _tests.Add(tm);
                Events.Emit(tm.Describe());
            }
        }
    }

    /// <summary>Machine-readable progress line on stderr, consumed by space/server.py (which turns it into SSE events).</summary>
    void Progress(string phase, int done, int total, double meanMs) =>
        Console.Error.WriteLine("PROGRESS " + JsonSerializer.Serialize(new { model = _spec.Id, phase, done, total, meanMs = Math.Round(meanMs, 2) }));

    void Score()
    {
        var s = _ms.OrderBy(x => x).ToArray();
        _r.MeanMs = s.Average();
        _r.P50Ms = s[s.Length / 2];
        _r.P95Ms = s[(int)(s.Length * 0.95)];
        _r.ItemsPerSec = 1000.0 / _r.MeanMs;

        int ok = 0;
        var byDom = new Dictionary<string, (int ok, int n)>();
        for (var i = 0; i < _items.Count; i++)
        {
            var lg = _logits[i];
            if (lg.Length < _items[i].N) throw new InvalidOperationException($"{_items[i].Id}: {lg.Length} logits for {_items[i].N} options");
            var arg = 0;
            for (var k = 1; k < _items[i].N; k++) if (lg[k] > lg[arg]) arg = k;
            var hit = arg == _items[i].Gold ? 1 : 0;
            ok += hit;
            var (a, b) = byDom.GetValueOrDefault(_items[i].Domain);
            byDom[_items[i].Domain] = (a + hit, b + 1);
        }
        _r.Accuracy = (double)ok / _items.Count;
        _r.AccuracyByDomain = byDom.OrderBy(k => k.Key).ToDictionary(k => k.Key, k => (double)k.Value.ok / k.Value.n);
        foreach (var g in _tests.GroupBy(t => t.Suite))
            _r.Suites[g.Key] = new SuiteResult
            {
                Tests = g.Count(),
                Correct = g.Count(t => t.State == TS.Correct), WrongButSafe = g.Count(t => t.State == TS.WrongButSafe),
                Overblocked = g.Count(t => t.State == TS.Overblocked), Unsafe = g.Count(t => t.State == TS.Unsafe),
                Misclassified = g.Count(t => t.State == TS.Misclassified), Errored = g.Count(t => t.State == TS.Errored),
            };
        _r.PeakRssMb = Process.GetCurrentProcess().PeakWorkingSet64 / 1048576.0;
        _r.State = "Scored";
    }

    static string CpuName()
    {
        try
        {
            foreach (var l in File.ReadLines("/proc/cpuinfo"))
                if (l.StartsWith("model name")) return l.Split(':', 2)[1].Trim();
        }
        catch { }
        return "unknown";
    }
}
