// Learning loop, part 1: which logged interactions may become training data (Admit) and which go to a human (Review).
// Principle: the model's own labels are never evidence of correctness. Human labels and verified outcomes are; the model's labels are
// admitted only when explicitly enabled (admit_self_labels = 1) and independently corroborated. `forbid` always wins.

@id("train.admit.human-label")
permit (principal, action == Action::"Admit", resource)
when { context.label_source == "human" };

@id("train.admit.verified-outcome")
permit (principal, action == Action::"Admit", resource)
when { context.label_source == "outcome" };

@id("train.admit.self-label-corroborated")
permit (principal, action == Action::"Admit", resource)
when { {{admit_self_labels}} == 1 && context.label_source == "model" && context.consensus == "unanimous"
    && context.confidence >= {{admit_min_conf}} && context.margin >= {{review_margin}} };

@id("train.forbid.no-consent")
forbid (principal, action == Action::"Admit", resource)
when { context.consent == "no" };

@id("train.forbid.source-not-open")
forbid (principal, action == Action::"Admit", resource)
when { context.license_class != "open" };

@id("train.forbid.pii")
forbid (principal, action == Action::"Admit", resource)
when { context.pii_risk == "high" };

@id("train.forbid.flagged-self-label")
forbid (principal, action == Action::"Admit", resource)
when { context.label_source == "model" && context.flagged };

@id("train.forbid.attack-text-self-label")
forbid (principal, action == Action::"Admit", resource)
when { context.label_source == "model" && ["injection", "jailbreak", "harmful_request"].contains(context.domain) };

// ---- what a human should look at
@id("train.review.flagged")
permit (principal, action == Action::"Review", resource)
when { context.flagged };

@id("train.review.split-vote")
permit (principal, action == Action::"Review", resource)
when { context.consensus == "split" };

@id("train.review.close-call")
permit (principal, action == Action::"Review", resource)
when { context.margin < {{review_margin}} };

// people must not be shown non-consented or high-PII content either
@id("train.forbid.review-privacy")
forbid (principal, action == Action::"Review", resource)
when { context.consent == "no" || context.pii_risk == "high" };
