// Moderation suite: comment toxicity and PII handling.

@id("mod.publish.permit-clean")
permit (principal, action == Action::"Publish", resource)
when { context.domain == "toxicity" && context has tox_label && context.tox_label == "clean" };

@id("mod.publish.forbid-flagged")
forbid (principal, action == Action::"Publish", resource)
when { context.domain == "toxicity" && context has tox_label && ["toxic", "hate", "threat"].contains(context.tox_label) };

@id("mod.hide.permit-toxic-hate")
permit (principal, action == Action::"HideComment", resource)
when { context.domain == "toxicity" && context has tox_label && ["toxic", "hate"].contains(context.tox_label) };

@id("mod.hide.forbid-threat")
forbid (principal, action == Action::"HideComment", resource)
when { context.domain == "toxicity" && context has tox_label && context.tox_label == "threat" };

@id("mod.escalate.permit-hate-threat")
permit (principal, action == Action::"EscalateHuman", resource)
when { context.domain == "toxicity" && context has tox_label && ["hate", "threat"].contains(context.tox_label) };

@id("pii.share.permit-none")
permit (principal, action == Action::"ShareText", resource)
when { context.domain == "pii" && context has pii_class && context.pii_class == "none" };

@id("pii.share.forbid-any-pii")
forbid (principal, action == Action::"ShareText", resource)
when { context.domain == "pii" && context has pii_class && context.pii_class != "none" };

@id("pii.redact.permit-low-risk")
permit (principal, action == Action::"AutoRedact", resource)
when { context.domain == "pii" && context has pii_class && ["personal", "contact"].contains(context.pii_class) };

@id("pii.redact.forbid-high-risk")
forbid (principal, action == Action::"AutoRedact", resource)
when { context.domain == "pii" && context has pii_class && ["government_id", "credentials"].contains(context.pii_class) };

@id("pii.export.block-high-risk")
permit (principal, action == Action::"BlockExport", resource)
when { context.domain == "pii" && context has pii_class && ["government_id", "credentials"].contains(context.pii_class) };

@id("pii.escalate.permit-high-risk")
permit (principal, action == Action::"EscalateHuman", resource)
when { context.domain == "pii" && context has pii_class && ["government_id", "credentials"].contains(context.pii_class) };
