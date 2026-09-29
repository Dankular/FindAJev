using System.Text.Json;
using CedarDotNet.Values;

namespace FindAJev.Bench;

/// <summary>The learning-loop checks: exhaustive properties of the admission and promotion policies, and the curation simulator.</summary>
public static class LearningChecks
{
    static readonly string[] AttackDomains = { "injection", "jailbreak", "harmful_request" };

    static Dictionary<string, Value> Ctx(string domain, string src, string consent, string lic, string pii, bool flagged, string cons, long conf, long margin) => new()
    {
        ["domain"] = domain, ["label_source"] = src, ["consent"] = consent, ["license_class"] = lic, ["pii_risk"] = pii,
        ["flagged"] = flagged, ["consensus"] = cons, ["confidence"] = conf, ["margin"] = margin,
    };

    /// <summary>Every combination of the admission/review attributes through Cedar versus a hand-written recomputation that shares no code with the policies.</summary>
    public static void AdmissionProperties(string root, CheckOutcome o)
    {
        var dir = Path.Combine(root, "policies");
        var findings = new List<string>(); long total = 0, bad = 0;
        var dom = new[] { "email_triage", "injection" };
        var src = new[] { "human", "outcome", "model" };
        var cons = new[] { "unanimous", "majority", "split" };
        foreach (var self in new long[] { 0, 1 })
        {
            var pe = new PolicyEngine(dir, new Dictionary<string, long> { ["admit_self_labels"] = self });
            long minConf = pe.Params["admit_min_conf"], reviewMargin = pe.Params["review_margin"];
            var confs = new long[] { minConf - 1, minConf }; var margins = new long[] { reviewMargin - 1, reviewMargin };
            var invariants = new Dictionary<string, int>();
            void Viol(string inv, string detail) { bad++; invariants[inv] = invariants.GetValueOrDefault(inv) + 1; if (findings.Count < 12) findings.Add($"[self={self}] {inv}: {detail}"); }
            foreach (var d in dom) foreach (var s in src) foreach (var consent in new[] { "yes", "no" }) foreach (var lic in new[] { "open", "research_only", "unverified" })
            foreach (var pii in new[] { "none", "low", "high" }) foreach (var fl in new[] { false, true }) foreach (var cs in cons) foreach (var cf in confs) foreach (var mg in margins)
            {
                var ctx = Ctx(d, s, consent, lic, pii, fl, cs, cf, mg);
                var tag = $"{d}/{s}/consent={consent}/{lic}/pii={pii}/flag={fl}/{cs}/conf={cf}/margin={mg}";
                var adm = pe.AuthorizeTrain("Admit", d, ctx); var rev = pe.AuthorizeTrain("Review", d, ctx);
                total += 2;
                if (adm.Error is not null || rev.Error is not null) { Viol("no-engine-error", tag + " " + adm.Error + rev.Error); continue; }
                var selfOk = s == "model" && self == 1 && cs == "unanimous" && cf >= minConf && mg >= reviewMargin && !fl && !AttackDomains.Contains(d);
                var wantAdmit = consent != "no" && lic == "open" && pii != "high" && (s is "human" or "outcome" || selfOk);
                if (adm.Allow != wantAdmit) Viol("admit-matches-recomputation", $"{tag}: cedar={adm.Allow} expected={wantAdmit}");
                if (adm.Allow && consent == "no") Viol("never-admit-without-consent", tag);
                if (adm.Allow && lic != "open") Viol("never-admit-non-open-source", tag);
                if (adm.Allow && pii == "high") Viol("never-admit-high-pii", tag);
                if (adm.Allow && s == "model" && fl) Viol("never-admit-flagged-self-label", tag);
                if (adm.Allow && s == "model" && AttackDomains.Contains(d)) Viol("never-self-label-attack-text", tag);
                if (adm.Allow && s == "model" && (cs != "unanimous")) Viol("self-label-needs-unanimity", tag);
                var wantReview = consent != "no" && pii != "high" && (fl || cs == "split" || mg < reviewMargin);
                if (rev.Allow != wantReview) Viol("review-matches-recomputation", $"{tag}: cedar={rev.Allow} expected={wantReview}");
                if (rev.Allow && (consent == "no" || pii == "high")) Viol("never-show-private-content-to-reviewers", tag);
                if (fl && consent != "no" && pii != "high" && !rev.Allow) Viol("flagged-always-reviewable", tag);
            }
        }
        o.Total = (int)total; o.Pass = (int)(total - Math.Min(total, bad)); o.Passed = bad == 0;
        o.Summary = $"{total} requests (2 settings of admit_self_labels x every combination), {bad} disagreement(s)";
        o.Findings = findings;
    }

    // ---------------------------------------------------------------------------------------------------- promotion gate
    static Dictionary<string, Value> Pctx(bool appr, bool comp, long core, long worst, long unsafeBp, long held, long err, long lat) => new()
    {
        ["human_approved"] = appr, ["comparable"] = comp, ["d_core_acc_bp"] = core, ["d_worst_suite_acc_bp"] = worst, ["d_unsafe_bp"] = unsafeBp,
        ["d_overblocked_bp"] = 0L, ["held_out_delta_bp"] = held, ["latency_ratio_pct"] = lat, ["errored"] = err, ["tests"] = 100L,
    };

    public static void PromotionGate(string root, CheckOutcome o)
    {
        var pe = new PolicyEngine(Path.Combine(root, "policies"));
        long maxUnsafe = pe.Params["promote_max_unsafe_rise_bp"], maxDrop = pe.Params["promote_max_suite_drop_bp"], maxLat = pe.Params["promote_max_latency_pct"];
        var findings = new List<string>(); int total = 0, bad = 0, allowed = 0;
        foreach (var pr in new[] { "Human", "Learner" }) foreach (var appr in new[] { true, false }) foreach (var comp in new[] { true, false })
        foreach (var core in new long[] { -1, 0, 50 }) foreach (var worst in new[] { -maxDrop - 1, -maxDrop, 0 }) foreach (var un in new[] { 0L, maxUnsafe, maxUnsafe + 1 })
        foreach (var held in new long[] { -1, 0 }) foreach (var err in new long[] { 0, 1 }) foreach (var lat in new[] { 100L, maxLat, maxLat + 1 })
        {
            var d = pe.AuthorizePromote(pr, "cand", Pctx(appr, comp, core, worst, un, held, err, lat)); total++;
            var want = pr == "Human" && appr && comp && core >= 0 && un <= maxUnsafe && worst >= -maxDrop && held >= 0 && err == 0 && lat <= maxLat;
            if (want) allowed++;
            if (d.Error is not null || d.Allow != want)
            { bad++; if (findings.Count < 12) findings.Add($"{pr} approved={appr} comparable={comp} core={core} worst={worst} unsafe={un} held={held} err={err} lat={lat}: cedar={d.Allow} expected={want} {d.Error}"); }
        }
        // demo on real results: each scored run against the most accurate other run of the default variant (informational)
        var demo = new List<object>();
        try
        {
            var runs = Promotion.LoadResults(root).Where(r => r.Variant == "" && r.Suites.Count > 0).ToList();
            var held = Data.Registry(root).Where(s => s.HeldOut).Select(s => s.Id).ToHashSet();
            foreach (var cand in runs) foreach (var champ in runs.Where(r => r.Id != cand.Id && r.Threads == cand.Threads).OrderByDescending(r => r.Accuracy).Take(1))
            {
                var ctx = Promotion.Context(cand, champ, true, held);
                var d = pe.AuthorizePromote("Human", cand.Id, ctx);
                demo.Add(new { candidate = cand.Id, champion = champ.Id, allow = d.Allow, by = d.Reasons, error = d.Error, comparable = ((BoolValue)ctx["comparable"]).Value });
            }
        }
        catch (Exception e) { findings.Add("real-result demo skipped: " + e.Message); }
        o.Total = total; o.Pass = total - bad; o.Passed = bad == 0;
        o.Summary = $"{total} boundary combinations ({allowed} expected to pass), {bad} disagreement(s); {demo.Count} real candidate/champion pair(s) evaluated";
        o.Findings = findings; o.Details = demo;
    }

    // ---------------------------------------------------------------------------------------------------- curation simulator
    /// <summary>Deterministic synthetic interactions, so the simulator's safety claims are tested even when no benchmark run has been recorded.</summary>
    public static List<SimExample> Synthetic(PolicyEngine pe, int n)
    {
        var rnd = new Random(20260929); var doms = pe.Packs.Keys.Concat(new[] { "pii", "retrieval" }).OrderBy(x => x, StringComparer.Ordinal).ToArray();
        var cons = new[] { "single", "unanimous", "unanimous", "majority", "split" };
        var res = new List<SimExample>();
        for (var i = 0; i < n; i++)
        {
            var wrong = rnd.NextDouble() < 0.25; var conf = wrong ? rnd.Next(20, 95) : rnd.Next(50, 100);
            res.Add(new SimExample($"syn-{i}", doms[rnd.Next(doms.Length)], "syn", new[] { wrong ? 1 : 0 }, new[] { 0 }, wrong, conf, rnd.Next(0, Math.Max(1, conf)),
                                   rnd.NextDouble() < (wrong ? 0.35 : 0.08), cons[rnd.Next(cons.Length)], 2, i % 3 == 0 ? "none" : "email"));
        }
        return res;
    }

    public static List<Curation.Scenario> Scenarios(double budgetPct, double annotatorAcc) => new()
    {
        new("naive self-training (admit everything)", new(), 0, 1, UseCedar: false),
        new("Cedar gate, humans review only", new(), budgetPct, annotatorAcc),
        new("Cedar gate + corroborated self-labels", new() { ["admit_self_labels"] = 1 }, budgetPct, annotatorAcc),
    };

    public static void CurationSim(string root, CheckOutcome o)
    {
        var findings = new List<string>(); var details = new List<object>(); int leaks = 0; int runs = 0;
        var pe = new PolicyEngine(Path.Combine(root, "policies"));
        var sets = new List<(string name, List<SimExample> ex)> { ("synthetic (1500 interactions)", Synthetic(pe, 1500)) };
        try
        {
            var preds = Curation.LoadPreds(root);
            if (preds.Count > 0)
            {
                var (ex, skipped) = Curation.BuildExamples(preds);
                sets.Add(($"recorded runs ({preds.Count} model(s): {string.Join(", ", preds.Keys)}; {skipped} test(s) skipped: options differ)", ex));
            }
            else findings.Add("no results/*.preds.jsonl yet: only the synthetic set was simulated (predictions are written by `run` from this version on)");
        }
        catch (Exception e) { findings.Add("recorded runs skipped: " + e.Message); }
        foreach (var (name, ex) in sets)
        {
            var rows = new List<object>();
            foreach (var sc in Scenarios(10, 0.98))
            {
                var r = Curation.Simulate(root, ex, sc, out _); runs++;
                var l = r.LeakNoConsent + r.LeakNotOpen + r.LeakPii + r.LeakAttackSelf;
                if (sc.UseCedar) leaks += l;
                if (sc.UseCedar && !sc.Overrides.ContainsKey("admit_self_labels") && r.WrongAdmittedSelf > 0) { leaks++; findings.Add($"{name} / {sc.Name}: {r.WrongAdmittedSelf} wrong model labels admitted although the default policy admits none"); }
                rows.Add(new { scenario = sc.Name, r.Examples, r.Reviewed, r.Admitted, r.AdmittedHuman, r.AdmittedSelf, r.WrongAdmitted, r.WrongAdmittedSelf,
                               labelNoisePct = r.Admitted == 0 ? 0 : Math.Round(100.0 * r.WrongAdmitted / r.Admitted, 2), yieldPct = r.Examples == 0 ? 0 : Math.Round(100.0 * r.Admitted / r.Examples, 1),
                               baseErrorPct = Math.Round(100 * r.BaseError, 2), errorsCaughtPct = Math.Round(100 * r.WrongErrorsCaught, 1), r.LeakNoConsent, r.LeakNotOpen, r.LeakPii, r.LeakAttackSelf });
            }
            details.Add(new { set = name, rows });
        }
        o.Total = runs; o.Pass = runs - (leaks > 0 ? 1 : 0); o.Passed = leaks == 0; o.Details = details; o.Findings = findings;
        o.Summary = $"{runs} scenario runs over {sets.Count} set(s); leaks through the Cedar gate: {leaks} (must be 0)";
    }

    // ---------------------------------------------------------------------------------------------------- turn taking
    static Dictionary<string, Value> TurnCtx(long p, long sil, bool speaking, bool dangling) =>
        new() { ["p_complete"] = p, ["silence_ms"] = sil, ["user_speaking"] = speaking, ["dangling"] = dangling };

    /// <summary>Every boundary combination of the turn-taking policies vs an independent recomputation, plus monotonicity and "never interrupt speech".</summary>
    public static void TurnProperties(string root, CheckOutcome o)
    {
        var pe = new PolicyEngine(Path.Combine(root, "policies"));
        long minP = pe.Params["turn_min_p"], minSil = pe.Params["turn_min_silence_ms"], maxWait = pe.Params["turn_max_wait_ms"], bcP = pe.Params["turn_backchannel_p"];
        var ps = new[] { 0L, bcP - 1, bcP, minP - 1, minP, 100 }; var sils = new[] { 0L, minSil - 1, minSil, maxWait - 1, maxWait, maxWait * 2 };
        var findings = new List<string>(); int total = 0, bad = 0;
        void Viol(string m) { bad++; if (findings.Count < 12) findings.Add(m); }
        foreach (var sp in new[] { false, true }) foreach (var dg in new[] { false, true })
        {
            bool? prevRespond = null; // monotone in silence for a fixed p (checked below per p)
            foreach (var p in ps)
            {
                bool? last = null;
                foreach (var sil in sils)
                {
                    var c = TurnCtx(p, sil, sp, dg); var tag = $"p={p} silence={sil} speaking={sp} dangling={dg}";
                    var r = pe.AuthorizeTurn("Respond", c); var w = pe.AuthorizeTurn("Wait", c); var b = pe.AuthorizeTurn("Backchannel", c); total += 3;
                    if (r.Error is not null || w.Error is not null || b.Error is not null) { Viol("engine error " + tag); continue; }
                    var wantR = !sp && !(dg && sil < maxWait) && ((p >= minP && sil >= minSil) || sil >= maxWait);
                    var wantB = !sp && p < minP && p >= bcP && sil >= minSil;
                    if (r.Allow != wantR) Viol($"Respond {tag}: cedar={r.Allow} expected={wantR}");
                    if (b.Allow != wantB) Viol($"Backchannel {tag}: cedar={b.Allow} expected={wantB}");
                    if (!w.Allow) Viol($"Wait must always be allowed: {tag}");
                    if (sp && (r.Allow || b.Allow)) Viol($"assistant talks over the speaker: {tag}");
                    if (!sp && last == true && !r.Allow) Viol($"Respond not monotone in silence: {tag}");   // more silence never turns an allowed Respond into a denial
                    last = r.Allow || last == true;
                }
                prevRespond = null;
            }
            // monotone in p at fixed silence
            foreach (var sil in sils)
            {
                bool seen = false;
                foreach (var p in ps)
                {
                    var r = pe.AuthorizeTurn("Respond", TurnCtx(p, sil, false, dg)); total++;
                    if (seen && !r.Allow) Viol($"Respond not monotone in p_complete: p={p} silence={sil} dangling={dg}");
                    seen |= r.Allow;
                }
            }
        }
        o.Total = total; o.Pass = total - Math.Min(total, bad); o.Passed = bad == 0;
        o.Summary = $"{total} requests over the boundary grid, {bad} disagreement(s) with the independent recomputation";
        o.Findings = findings;
    }
}
