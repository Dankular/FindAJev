@id("email.autoreply.permit")
permit (principal, action == Action::"AutoReply", resource)
when { context.domain == "email_triage"
    && context has email_needs_reply && context.email_needs_reply == "yes"
    && context has email_action && context.email_action == "reply"
    && context has email_phishing && context.email_phishing == "no" };

@id("email.autoreply.forbid-phishing")
forbid (principal, action == Action::"AutoReply", resource)
when { context.domain == "email_triage" && context has email_phishing && context.email_phishing == "yes" };

@id("email.autoreply.forbid-sensitive-category")
forbid (principal, action == Action::"AutoReply", resource)
when { context.domain == "email_triage" && context has email_category
    && ["legal", "security"].contains(context.email_category) };

@id("email.quarantine.permit")
permit (principal, action == Action::"Quarantine", resource)
when { context.domain == "email_triage" && context has email_phishing && context.email_phishing == "yes" };

@id("email.escalate.permit")
permit (principal, action == Action::"EscalateHuman", resource)
when { context.domain == "email_triage" &&
    ((context has email_action && context.email_action == "escalate")
     || (context has email_category && ["legal", "security"].contains(context.email_category))) };
