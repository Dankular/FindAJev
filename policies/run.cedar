// Harness lifecycle policies (model gating). Evaluated with principal Runner, resource Model.

@id("run.fetch.permit-apache")
permit (principal, action == Action::"FetchModel", resource)
when { resource.license == "apache-2.0" };

@id("run.fetch.forbid-oversize")
forbid (principal, action == Action::"FetchModel", resource)
when { resource.sizeMb > 4000 };

@id("run.run.permit-within-quota")
permit (principal, action == Action::"RunModel", resource)
when { context.threads <= context.cpus };

@id("run.run.forbid-oversubscribe")
forbid (principal, action == Action::"RunModel", resource)
when { context.threads > context.cpus };
