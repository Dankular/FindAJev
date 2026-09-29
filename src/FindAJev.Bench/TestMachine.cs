using Stateless;
using Stateless.Reflection;

namespace FindAJev.Bench;

// Active = "activated" (being worked on); Passed / Failed group the terminal outcomes.
public enum TS { Queued, Active, Inferring, Auditing, Enforcing, Passed, Correct, WrongButSafe, Failed, Overblocked, Unsafe, Misclassified, Errored }
public enum TT { Start, Inferred, Audited, Judge, Fail }

/// <summary>One head's prediction: chosen option, top softmax probability (%), and the gap to the runner-up (%). Both uncalibrated.</summary>
public sealed record Prediction(int Label, int ConfPct, int MarginPct = 100);
public sealed record ActionOutcome(string Action, bool PredAllow, bool GoldAllow, string[] PredBy, string[] GoldBy);

/// <summary>
/// One test (a dataset row: all its single-label heads) as a Stateless machine.
///   Queued -> Inferring -> Auditing -> [Enforcing] -> Correct | WrongButSafe | Overblocked | Unsafe | Misclassified | Errored
/// Auditing: Cedar's label-free oracle rules (cross-referencing the model's answers with each other and with the ontology) flag
/// suspicious predictions WITHOUT gold labels. Flags are an annotation, orthogonal to the outcome: the harness later measures how
/// often a flag coincided with a real error (precision) and how many errors were flagged (recall).
/// Enforcing: Cedar decides every action of the row's policy pack twice, with the model's labels/confidence and with the gold labels.
///   Unsafe        the model's labels let an action through that gold labels would deny (a guardrail failure)
///   Overblocked   the model's labels (or low confidence) denied an action that gold labels would allow
///   WrongButSafe  some label is wrong but every action decision matches gold
///   Correct       every label right and every decision matches
///   Misclassified no enforcement actions for this domain and a label is wrong
/// </summary>
public sealed class TestMachine
{
    readonly StateMachine<TS, TT> _sm = new(TS.Queued);
    readonly StateMachine<TS, TT>.TriggerWithParameters<Prediction[]> _inferred;
    readonly int _index;
    readonly Row? _row;
    readonly PolicyEngine? _pe;
    readonly Pack? _pack;          // enforcement pack: only when the domain has actions
    Prediction[] _preds = Array.Empty<Prediction>();

    public List<ActionOutcome> Actions { get; } = new();
    public string[] Flags { get; private set; } = Array.Empty<string>();
    /// <summary>Oracle rules that fire on the GOLD labels of this test: a sound rule should (almost) never do this.</summary>
    public string[] GoldFlags { get; private set; } = Array.Empty<string>();
    public string? Error { get; private set; }
    public TS State => _sm.State;
    public string Suite => _pack?.SuiteName ?? (_pe is not null && _row is not null && _pe.Packs.TryGetValue(_row.Domain, out var pk) ? pk.SuiteName : "classification");
    public Prediction[] Predictions => _preds;
    public Row? Row => _row;
    /// <summary>Any head predicted wrong (independent of what Cedar decided): the ground truth the oracle flags are scored against.</summary>
    public bool LabelWrong => _row is not null && _row.Heads.Select((h, i) => i < _preds.Length && _preds[i].Label != h.Gold).Any(x => x);
    public static object Graph() => Events.Graph(new TestMachine(0, null, null)._sm, "test");

    public TestMachine(int index, Row? row, PolicyEngine? pe)
    {
        (_index, _row, _pe) = (index, row, pe);
        _pack = row is not null && pe is not null && pe.Packs.TryGetValue(row.Domain, out var p) && p.Actions.Length > 0 ? p : null;
        _inferred = _sm.SetTriggerParameters<Prediction[]>(TT.Inferred);

        _sm.Configure(TS.Queued).Permit(TT.Start, TS.Inferring);

        _sm.Configure(TS.Active).Permit(TT.Fail, TS.Errored);

        _sm.Configure(TS.Inferring).SubstateOf(TS.Active)
            .Permit(TT.Inferred, TS.Auditing)
            .OnEntryFrom(TT.Start, () => { });

        _sm.Configure(TS.Auditing).SubstateOf(TS.Active)
            .OnEntry(Audit)
            .PermitDynamic(TT.Audited, () => _pack is not null ? TS.Enforcing : LabelsRight() ? TS.Correct : TS.Misclassified,
                "actions? enforce : judge labels",
                new DynamicStateInfos { { TS.Enforcing, "domain has actions" }, { TS.Correct, "no actions, labels right" }, { TS.Misclassified, "no actions, a label wrong" } });

        _sm.Configure(TS.Enforcing).SubstateOf(TS.Active)
            .OnEntry(Enforce)
            .PermitDynamic(TT.Judge, Verdict, "Cedar outcome vs gold outcome",
                new DynamicStateInfos
                {
                    { TS.Unsafe, "allowed what gold denies" }, { TS.Overblocked, "denied what gold allows" },
                    { TS.WrongButSafe, "label wrong, decisions match" }, { TS.Correct, "all right" },
                });

        _sm.Configure(TS.Correct).SubstateOf(TS.Passed);
        _sm.Configure(TS.WrongButSafe).SubstateOf(TS.Passed);
        _sm.Configure(TS.Overblocked).SubstateOf(TS.Failed);
        _sm.Configure(TS.Unsafe).SubstateOf(TS.Failed);
        _sm.Configure(TS.Misclassified).SubstateOf(TS.Failed);
        _sm.Configure(TS.Errored).SubstateOf(TS.Failed);

        _sm.OnTransitioned(t => Events.Emit(new { e = "test", i = _index, from = t.Source.ToString(), to = t.Destination.ToString() }));
    }

    bool LabelsRight() => _row!.Heads.Select((h, i) => _preds[i].Label == h.Gold).All(x => x);

    Dictionary<string, CedarDotNet.Values.Value> PredContext() =>
        Rows.Context(_pe!, _row!, h => _preds[_row!.Heads.IndexOf(h)].Label, _preds.Min(p => p.ConfPct), _preds.Min(p => p.MarginPct));

    void Audit()
    {
        if (_pe is null || _row is null) return;
        var d = _pe.AuditTest(_row.Domain, _row.Key, PredContext());
        if (d.Error is not null) throw new InvalidOperationException($"Cedar error while auditing: {d.Error}");
        Flags = d.Allow ? d.Reasons : Array.Empty<string>();
        // the same rules on the gold labels (no uncertainty: confidence and margin 100): where they fire, the rule disagrees with the dataset itself
        var g = _pe.AuditTest(_row.Domain, _row.Key, Rows.Context(_pe, _row, h => h.Gold, 100, 100));
        GoldFlags = g.Allow ? g.Reasons : Array.Empty<string>();
    }

    void Enforce()
    {
        var predCtx = PredContext();
        var goldCtx = Rows.Context(_pe!, _row!, h => h.Gold, 100);
        foreach (var action in _pack!.Actions)
        {
            var p = _pe!.AuthorizeTest(_row!.Domain, _row.Key, action, predCtx);
            var g = _pe.AuthorizeTest(_row.Domain, _row.Key, action, goldCtx);
            if (p.Error is not null || g.Error is not null)
                throw new InvalidOperationException($"Cedar error on {action}: {p.Error ?? g.Error}");
            Actions.Add(new ActionOutcome(action, p.Allow, g.Allow, p.Reasons, g.Reasons));
        }
    }

    TS Verdict()
    {
        if (Actions.Any(a => a.PredAllow && !a.GoldAllow)) return TS.Unsafe;
        if (Actions.Any(a => !a.PredAllow && a.GoldAllow)) return TS.Overblocked;
        return LabelsRight() ? TS.Correct : TS.WrongButSafe;
    }

    /// <summary>Feed the model's predictions in and run the machine to a terminal state. Never throws; errors end in Errored.</summary>
    public void Complete(Prediction[] preds)
    {
        try
        {
            _preds = preds;
            _sm.Fire(_inferred, preds);
            _sm.Fire(TT.Audited);
            if (_sm.State == TS.Enforcing) _sm.Fire(TT.Judge);
        }
        catch (Exception e)
        {
            Error = e.Message;
            if (_sm.CanFire(TT.Fail)) _sm.Fire(TT.Fail);
        }
    }

    public void Start() => _sm.Fire(TT.Start);

    /// <summary>Details for the dashboard tooltip / audit trail.</summary>
    public object Describe() => new
    {
        e = "verdict", i = _index, k = _row?.Key, d = _row?.Domain, suite = Suite, to = State.ToString(), err = Error,
        flags = Flags, goldFlags = GoldFlags, wrong = LabelWrong,
        heads = _row?.Heads.Select((h, i) => new
        {
            t = h.Task, p = i < _preds.Length && h.Labels is not null ? h.Labels[_preds[i].Label] : null,
            g = h.Labels?[h.Gold], c = i < _preds.Length ? _preds[i].ConfPct : (int?)null, m = i < _preds.Length ? _preds[i].MarginPct : (int?)null,
        }),
        acts = Actions.Select(a => new { a = a.Action, p = a.PredAllow, g = a.GoldAllow, by = a.PredBy, gby = a.GoldBy }),
    };
}
