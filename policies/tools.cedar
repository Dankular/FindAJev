// Tools suite: the model picks which tool to call; Cedar decides whether that call may run unattended.
// tool_risk is a heuristic on the tool name's leading verb (see tools/encode.py).

@id("tools.invoke.permit-read")
permit (principal, action == Action::"InvokeTool", resource)
when { context.domain == "tools" && context has tool_risk && context.tool_risk == "read" };

@id("tools.invoke.forbid-destructive")
forbid (principal, action == Action::"InvokeTool", resource)
when { context.domain == "tools" && context has tool_risk && context.tool_risk == "destructive" };

@id("tools.approval.permit-mutating")
permit (principal, action == Action::"RequireApproval", resource)
when { context.domain == "tools" && context has tool_risk && ["write", "destructive"].contains(context.tool_risk) };

@id("tools.escalate.permit-destructive")
permit (principal, action == Action::"EscalateHuman", resource)
when { context.domain == "tools" && context has tool_risk && context.tool_risk == "destructive" };
