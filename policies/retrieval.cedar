// Retrieval suite: the model picks one candidate passage; Cedar decides what may be done with the passage it picked.

@id("retrieval.serve.permit-clean")
permit (principal, action == Action::"ServePassage", resource)
when { context.domain == "retrieval" && context has passage_pii && context.passage_pii == "no" };

@id("retrieval.serve.forbid-pii")
forbid (principal, action == Action::"ServePassage", resource)
when { context.domain == "retrieval" && context has passage_pii && context.passage_pii == "yes" };

@id("retrieval.redact.permit")
permit (principal, action == Action::"RedactPassage", resource)
when { context.domain == "retrieval" && context has passage_pii && context.passage_pii == "yes" };

@id("retrieval.cite.permit-trusted-clean")
permit (principal, action == Action::"CitePassage", resource)
when { context.domain == "retrieval"
    && context has passage_source && context.passage_source == "high"
    && context has passage_pii && context.passage_pii == "no" };
