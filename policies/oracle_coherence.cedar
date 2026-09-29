// Cross-referencing rules: contradictions between a model's own answers on the same item, and between an answer and the ontology.

// ---- email triage: four heads on one email
@id("oracle.email.phishing-but-reply")
permit (principal, action == Action::"Audit", resource)
when { context.domain == "email_triage" && context has email_phishing && context.email_phishing == "yes"
    && context has email_action && ["reply", "approve"].contains(context.email_action) };

@id("oracle.email.needs-reply-but-fyi")
permit (principal, action == Action::"Audit", resource)
when { context.domain == "email_triage" && context has email_needs_reply && context.email_needs_reply == "yes"
    && context has email_action && context.email_action == "fyi" };

@id("oracle.email.no-reply-needed-but-reply")
permit (principal, action == Action::"Audit", resource)
when { context.domain == "email_triage" && context has email_needs_reply && context.email_needs_reply == "no"
    && context has email_action && context.email_action == "reply" };

@id("oracle.email.newsletter-needs-reply")
permit (principal, action == Action::"Audit", resource)
when { context.domain == "email_triage" && context has email_category && context.email_category == "newsletter"
    && context has email_needs_reply && context.email_needs_reply == "yes" };

// ---- ticket routing
@id("oracle.ticket.fraud-low-urgency")
permit (principal, action == Action::"Audit", resource)
when { context.domain == "ticket_route" && context has ticket_queue && context.ticket_queue == "fraud_review"
    && context has ticket_urgency && context.ticket_urgency == "low" };

@id("oracle.ticket.outage-low-urgency")
permit (principal, action == Action::"Audit", resource)
when { context.domain == "ticket_route" && context has ticket_queue && context.ticket_queue == "outages"
    && context has ticket_urgency && context.ticket_urgency == "low" };

// ---- clinic
@id("oracle.clinic.urgent-administrative")
permit (principal, action == Action::"Audit", resource)
when { context.domain == "clinic_request" && context has clinic_urgent && context.clinic_urgent == "yes"
    && context has clinic_request && ["billing", "records", "insurance", "cancel_visit"].contains(context.clinic_request) };

@id("oracle.clinic.symptoms-not-urgent")
permit (principal, action == Action::"Audit", resource)
when { context.domain == "clinic_request" && context has clinic_request && context.clinic_request == "symptom_question"
    && context has clinic_urgent && context.clinic_urgent == "no" };

// ---- sports recap: world knowledge through the ontology (Sport entities)
@id("oracle.sports.draw-in-a-sport-without-draws")
permit (principal, action == Action::"Audit", resource)
when { context.domain == "sports_recap" && context has sport_result && context.sport_result == "draw"
    && context has sport_ref && !context.sport_ref.hasDraws };

@id("oracle.sports.upset-in-an-upcoming-game")
permit (principal, action == Action::"Audit", resource)
when { context.domain == "sports_recap" && context has sport_result && context.sport_result == "upcoming"
    && context has sport_upset && context.sport_upset == "yes" };
