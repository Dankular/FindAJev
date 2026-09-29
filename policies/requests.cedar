// Banking, support, travel, benefits: which requests may be executed without a human.

@id("bank.execute.permit-low-risk")
permit (principal, action == Action::"AutoExecute", resource)
when { context.domain == "banking_intent" && context has bank_intent
    && ["balance", "statement", "branch_appointment", "direct_deposit", "loan_payment"].contains(context.bank_intent) };

@id("bank.execute.forbid-high-risk")
forbid (principal, action == Action::"AutoExecute", resource)
when { context.domain == "banking_intent" && context has bank_intent
    && ["fraud_alert", "card_lost", "close_account", "dispute_charge", "wire", "limit_increase", "mortgage"].contains(context.bank_intent) };

@id("bank.stepup.permit")
permit (principal, action == Action::"RequireStepUp", resource)
when { context.domain == "banking_intent" && context has bank_intent
    && ["transfer", "wire", "limit_increase", "new_account"].contains(context.bank_intent) };

@id("bank.escalate.permit")
permit (principal, action == Action::"EscalateHuman", resource)
when { context.domain == "banking_intent" && context has bank_intent
    && ["fraud_alert", "card_lost", "dispute_charge", "close_account", "wire"].contains(context.bank_intent) };

@id("support.execute.permit-self-service")
permit (principal, action == Action::"AutoExecute", resource)
when { context.domain == "support_intent" && context has support_intent
    && ["order_status", "invoice_copy", "password_reset", "tax_document", "data_export", "promo_code", "pricing_question", "product_question"].contains(context.support_intent) };

@id("support.execute.forbid-destructive")
forbid (principal, action == Action::"AutoExecute", resource)
when { context.domain == "support_intent" && context has support_intent
    && ["close_account", "cancel_subscription", "refund_request", "update_payment", "change_address"].contains(context.support_intent) };

@id("support.escalate.permit")
permit (principal, action == Action::"EscalateHuman", resource)
when { context.domain == "support_intent" && context has support_intent
    && ["speak_to_human", "complaint", "outage", "damaged_item"].contains(context.support_intent) };

@id("travel.execute.permit")
permit (principal, action == Action::"AutoExecute", resource)
when { context.domain == "travel_request" && context has travel_intent
    && ["baggage", "seat", "loyalty", "visa_docs", "hotel", "car"].contains(context.travel_intent) };

@id("travel.escalate.permit")
permit (principal, action == Action::"EscalateHuman", resource)
when { context.domain == "travel_request" && context has travel_intent
    && ["disruption", "cancel_trip", "refund"].contains(context.travel_intent) };

@id("benefits.reply.permit-status")
permit (principal, action == Action::"AutoReply", resource)
when { context.domain == "benefits_request"
    && context has benefit_status && context.benefit_status == "yes"
    && context has benefit_program && context.benefit_program != "other" };
