// Cross-domain guardrails. `forbid` always wins over `permit`.

@id("guard.low-confidence")
forbid (principal, action in Action::"AutoAct", resource)
when { context.minConfidence < 60 };
