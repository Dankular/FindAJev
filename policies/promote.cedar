// Learning loop, part 2: may a candidate model replace the champion? Deltas are measured by this harness (basis points).
// Separation of duties: the learner may only propose; a human's approval is a required input.

@id("promote.permit-no-regression")
permit (principal, action == Action::"Promote", resource)
when { context.human_approved && context.comparable && context.d_core_acc_bp >= 0 };

@id("promote.forbid-learner")
forbid (principal, action == Action::"Promote", resource)
when { principal is Learner };

@id("promote.forbid-no-approval")
forbid (principal, action == Action::"Promote", resource)
when { !context.human_approved };

@id("promote.forbid-not-comparable")
forbid (principal, action == Action::"Promote", resource)
when { !context.comparable };

@id("promote.forbid-unsafe-rise")
forbid (principal, action == Action::"Promote", resource)
when { context.d_unsafe_bp > {{promote_max_unsafe_rise_bp}} };

@id("promote.forbid-suite-drop")
forbid (principal, action == Action::"Promote", resource)
when { context.d_worst_suite_acc_bp < 0 - {{promote_max_suite_drop_bp}} };

@id("promote.forbid-heldout-drop")
forbid (principal, action == Action::"Promote", resource)
when { context.held_out_delta_bp < 0 };

@id("promote.forbid-errors")
forbid (principal, action == Action::"Promote", resource)
when { context.errored > 0 };

@id("promote.forbid-slower")
forbid (principal, action == Action::"Promote", resource)
when { context.latency_ratio_pct > {{promote_max_latency_pct}} };
