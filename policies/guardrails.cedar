// Cross-domain guardrails. `forbid` always wins over `permit`.
// Confidence is the top softmax probability (0-100), NOT calibrated. A fixed bar punishes decisions with many options (a 40% top
// probability over ten passages is strong; over two options it is nothing), so the bar depends on the option count.
// The thresholds are heuristic starting points and are runtime parameters: see policies/params.json and `--param name=value`.

@id("guard.low-confidence")
forbid (principal, action in Action::"AutoAct", resource)
when { context.options <= {{few_max_options}} && context.minConfidence < {{min_conf_few}} };

@id("guard.low-confidence-many")
forbid (principal, action in Action::"AutoAct", resource)
when { context.options > {{few_max_options}} && context.minConfidence < {{min_conf_many}} };
