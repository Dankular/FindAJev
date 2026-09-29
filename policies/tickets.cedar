@id("ticket.autoroute.permit")
permit (principal, action == Action::"AutoRoute", resource)
when { context.domain == "ticket_route"
    && context has ticket_pii && context.ticket_pii == "no"
    && context has ticket_urgency && ["low", "normal"].contains(context.ticket_urgency) };

@id("ticket.autoroute.forbid-sensitive-queue")
forbid (principal, action == Action::"AutoRoute", resource)
when { context.domain == "ticket_route" && context has ticket_queue
    && ["fraud_review", "identity", "compliance", "account_closure", "billing_disputes"].contains(context.ticket_queue) };

@id("ticket.redact.permit")
permit (principal, action == Action::"RedactPII", resource)
when { context.domain == "ticket_route" && context has ticket_pii && context.ticket_pii == "yes" };

@id("ticket.page.permit")
permit (principal, action == Action::"PageOnCall", resource)
when { context.domain == "ticket_route" && context has ticket_urgency && context.ticket_urgency == "critical" };

@id("ticket.escalate.permit")
permit (principal, action == Action::"EscalateHuman", resource)
when { context.domain == "ticket_route" && context has ticket_queue
    && ["fraud_review", "compliance", "identity", "account_closure", "billing_disputes"].contains(context.ticket_queue) };
