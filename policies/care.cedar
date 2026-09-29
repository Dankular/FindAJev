// Clinic + agent handoff: safety-critical routing.

@id("clinic.selfserve.permit")
permit (principal, action == Action::"SelfServe", resource)
when { context.domain == "clinic_request"
    && context has clinic_urgent && context.clinic_urgent == "no"
    && context has clinic_request
    && ["appointment", "cancel_visit", "billing", "insurance", "records", "lab_results"].contains(context.clinic_request) };

@id("clinic.selfserve.forbid-urgent")
forbid (principal, action == Action::"SelfServe", resource)
when { context.domain == "clinic_request" && context has clinic_urgent && context.clinic_urgent == "yes" };

@id("clinic.selfserve.forbid-clinical")
forbid (principal, action == Action::"SelfServe", resource)
when { context.domain == "clinic_request" && context has clinic_request
    && ["symptom_question", "refill"].contains(context.clinic_request) };

@id("clinic.escalate.permit")
permit (principal, action == Action::"EscalateHuman", resource)
when { context.domain == "clinic_request" &&
    ((context has clinic_urgent && context.clinic_urgent == "yes")
     || (context has clinic_request && ["symptom_question", "refill"].contains(context.clinic_request))) };

@id("handoff.bot.permit")
permit (principal, action == Action::"BotContinue", resource)
when { context.domain == "agent_handoff" && context has handoff && context.handoff == "no" };

@id("handoff.escalate.permit")
permit (principal, action == Action::"EscalateHuman", resource)
when { context.domain == "agent_handoff" && context has handoff && context.handoff == "yes" };
