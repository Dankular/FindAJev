// Label-free oracle rules. A permit on Action::"Audit" flags the prediction as suspicious; the reasons name the rules that fired.
// They never see gold labels. The harness measures each rule afterwards: how often its flags are real errors (precision) and how many errors
// it catches (recall).

@id("oracle.low-margin")
permit (principal, action == Action::"Audit", resource)
when { context.minMargin < {{oracle_margin}} };
