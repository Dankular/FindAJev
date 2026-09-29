"""Generates policies/conformance/*.json (Cedar language conformance cases). Re-run after editing: python tools/build_conformance.py
Each category file is an array of cases; kinds: authorize, validate, parse-schema/-policies/-entities/-context, format, roundtrip, parts, partial."""
import json
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "policies/conformance"
OUT.mkdir(parents=True, exist_ok=True)
U = lambda t, i: f'{t}::"{i}"'
ALICE, BOB, DOC = U("User", "alice"), U("User", "bob"), U("Doc", "d1")
ent = lambda uid, attrs=None, parents=None, tags=None: {k: v for k, v in dict(uid=uid, attrs=attrs or {}, parents=parents or [], tags=tags or {}).items() if v != {} and v != [] or k == "uid"}
ref = lambda t, i: {"__entity": {"type": t, "id": i}}
extn = lambda fn, arg: {"__extn": {"fn": fn, "arg": arg}}
REQ = lambda p=ALICE, a=U("Action", "view"), r=DOC, ctx=None: dict(principal=p, action=a, resource=r, context=ctx or {})


def auth(name, policies, expect, req=None, entities=None, schema=None, **kw):
    c = dict(name=name, kind="authorize", policies=policies, request=req or REQ(), entities=entities or [], expect=expect)
    if schema: c["schema"] = schema
    c.update(kw); return c


ALLOW, DENY = dict(decision="allow"), dict(decision="deny")
cats = {}

# ------------------------------------------------------------------------------------------------ authorization: core semantics
A = cats["authorization"] = []
A += [
 auth("no policies -> default deny", [], dict(decision="deny", reasons=[])),
 auth("permit-all allows", ["permit(principal, action, resource);"], dict(decision="allow", reasons=["p0"])),
 auth("forbid overrides permit", ["permit(principal, action, resource);", "forbid(principal, action, resource);"], dict(decision="deny", reasons=["p1"])),
 auth("several satisfied permits are all reported", ["permit(principal, action, resource);", "permit(principal == User::\"alice\", action, resource);"], dict(decision="allow", reasons=["p0", "p1"])),
 auth("principal == matches", [f"permit(principal == {ALICE}, action, resource);"], ALLOW),
 auth("principal == mismatch denies", [f"permit(principal == {BOB}, action, resource);"], DENY),
 auth("principal in group (direct parent)", [f"permit(principal in Group::\"eng\", action, resource);"], ALLOW,
      entities=[ent(ALICE, parents=[U("Group", "eng")]), ent(U("Group", "eng"))]),
 auth("principal in group (transitive)", [f"permit(principal in Org::\"acme\", action, resource);"], ALLOW,
      entities=[ent(ALICE, parents=[U("Team", "t1")]), ent(U("Team", "t1"), parents=[U("Org", "acme")]), ent(U("Org", "acme"))]),
 auth("not in group denies", [f"permit(principal in Group::\"eng\", action, resource);"], DENY, entities=[ent(ALICE), ent(U("Group", "eng"))]),
 auth("action in action group", [f"permit(principal, action in Action::\"read\", resource);"], ALLOW,
      entities=[ent(U("Action", "view"), parents=[U("Action", "read")]), ent(U("Action", "read"))]),
 auth("action == exact", ["permit(principal, action == Action::\"view\", resource);"], ALLOW),
 auth("action set membership", ["permit(principal, action in [Action::\"view\", Action::\"edit\"], resource);"], ALLOW),
 auth("resource is Doc", ["permit(principal, action, resource is Doc);"], ALLOW),
 auth("resource is wrong type denies", ["permit(principal, action, resource is Folder);"], DENY),
 auth("principal is User in Group", ["permit(principal is User in Group::\"eng\", action, resource);"], ALLOW,
      entities=[ent(ALICE, parents=[U("Group", "eng")]), ent(U("Group", "eng"))]),
 auth("when on resource attribute", ["permit(principal, action, resource) when { resource.public == true };"], ALLOW, entities=[ent(DOC, attrs=dict(public=True))]),
 auth("when false denies", ["permit(principal, action, resource) when { resource.public == true };"], DENY, entities=[ent(DOC, attrs=dict(public=False))]),
 auth("unless inverts", ["permit(principal, action, resource) unless { resource.locked };"], ALLOW, entities=[ent(DOC, attrs=dict(locked=False))]),
 auth("unless true denies", ["permit(principal, action, resource) unless { resource.locked };"], DENY, entities=[ent(DOC, attrs=dict(locked=True))]),
 auth("has: attribute present", ["permit(principal, action, resource) when { resource has owner };"], ALLOW, entities=[ent(DOC, attrs=dict(owner="x"))]),
 auth("has: attribute absent", ["permit(principal, action, resource) when { resource has owner };"], DENY, entities=[ent(DOC)]),
 auth("has guard prevents an error", ["permit(principal, action, resource) when { resource has owner && resource.owner == \"x\" };"], dict(decision="deny", errors=0), entities=[ent(DOC)]),
 auth("like wildcard", ["permit(principal, action, resource) when { resource.name like \"report-*\" };"], ALLOW, entities=[ent(DOC, attrs=dict(name="report-2026"))]),
 auth("like non-match", ["permit(principal, action, resource) when { resource.name like \"report-*\" };"], DENY, entities=[ent(DOC, attrs=dict(name="memo"))]),
 auth("set contains", ["permit(principal, action, resource) when { resource.tags.contains(\"a\") };"], ALLOW, entities=[ent(DOC, attrs=dict(tags=["a", "b"]))]),
 auth("set containsAll", ["permit(principal, action, resource) when { resource.tags.containsAll([\"a\", \"b\"]) };"], ALLOW, entities=[ent(DOC, attrs=dict(tags=["a", "b", "c"]))]),
 auth("set containsAny", ["permit(principal, action, resource) when { resource.tags.containsAny([\"z\", \"b\"]) };"], ALLOW, entities=[ent(DOC, attrs=dict(tags=["a", "b"]))]),
 auth("set isEmpty", ["permit(principal, action, resource) when { resource.tags.isEmpty() };"], ALLOW, entities=[ent(DOC, attrs=dict(tags=[]))]),
 auth("arithmetic + comparison", ["permit(principal, action, resource) when { context.n * 2 + 1 > 6 };"], ALLOW, req=REQ(ctx=dict(n=3))),
 auth("if-then-else", ["permit(principal, action, resource) when { if context.vip then true else context.n > 10 };"], ALLOW, req=REQ(ctx=dict(vip=True, n=1))),
 auth("string equality is case sensitive", ["permit(principal, action, resource) when { context.s == \"Yes\" };"], DENY, req=REQ(ctx=dict(s="yes"))),
 auth("entity-valued attribute equals principal (ReBAC owner)", ["permit(principal, action, resource) when { resource.owner == principal };"], ALLOW,
      entities=[ent(DOC, attrs=dict(owner=ref("User", "alice")))]),
 auth("nested record attribute", ["permit(principal, action, resource) when { context.user.dept == \"eng\" };"], ALLOW, req=REQ(ctx=dict(user=dict(dept="eng")))),
 auth("annotations do not change evaluation", ["@id(\"x\") @note(\"y\") permit(principal, action, resource);"], ALLOW),
 auth("tags: hasTag / getTag", ["permit(principal, action, resource) when { resource.hasTag(\"env\") && resource.getTag(\"env\") == \"prod\" };"], ALLOW,
      entities=[ent(DOC, tags=dict(env="prod"))]),
 auth("tags: missing tag is false", ["permit(principal, action, resource) when { resource.hasTag(\"env\") };"], DENY, entities=[ent(DOC)]),
]

# ------------------------------------------------------------------------------------------------ errors: evaluation-error semantics
E = cats["errors"] = []
E += [
 auth("missing attribute (no has) -> policy errors, deny", ["permit(principal, action, resource) when { resource.owner == \"x\" };"], dict(decision="deny", errors=1), entities=[ent(DOC)]),
 auth("an erroring forbid does not block a good permit", ["permit(principal, action, resource);", "forbid(principal, action, resource) when { resource.owner == \"x\" };"],
      dict(decision="allow", errors=1), entities=[ent(DOC)]),
 auth("an erroring permit is skipped, another permit still allows", ["permit(principal, action, resource) when { resource.owner == \"x\" };", "permit(principal, action, resource);"],
      dict(decision="allow", reasons=["p1"], errors=1), entities=[ent(DOC)]),
 auth("integer overflow is an error, not a wrap", ["permit(principal, action, resource) when { context.n + 1 > 0 };"], dict(decision="deny", errors=1), req=REQ(ctx=dict(n=9223372036854775807))),
 auth("type error in condition is an error", ["permit(principal, action, resource) when { context.s + 1 > 0 };"], dict(decision="deny", errors=1), req=REQ(ctx=dict(s="text"))),
 auth("division-free arithmetic near the limit is fine", ["permit(principal, action, resource) when { context.n - 1 < context.n };"], dict(decision="allow", errors=0), req=REQ(ctx=dict(n=9223372036854775807))),
]

# ------------------------------------------------------------------------------------------------ patterns: RBAC / ABAC / ReBAC / guardrails
P = cats["patterns"] = []
P += [
 auth("RBAC: admin role may delete", ["permit(principal in Role::\"admin\", action == Action::\"delete\", resource);"], ALLOW,
      req=REQ(a=U("Action", "delete")), entities=[ent(ALICE, parents=[U("Role", "admin")]), ent(U("Role", "admin"))]),
 auth("RBAC: viewer may not delete", ["permit(principal in Role::\"admin\", action == Action::\"delete\", resource);"], DENY,
      req=REQ(a=U("Action", "delete")), entities=[ent(ALICE, parents=[U("Role", "viewer")]), ent(U("Role", "viewer"))]),
 auth("ABAC: clearance >= sensitivity", ["permit(principal, action, resource) when { principal.clearance >= resource.sensitivity };"], ALLOW,
      entities=[ent(ALICE, attrs=dict(clearance=3)), ent(DOC, attrs=dict(sensitivity=2))]),
 auth("ABAC: clearance too low", ["permit(principal, action, resource) when { principal.clearance >= resource.sensitivity };"], DENY,
      entities=[ent(ALICE, attrs=dict(clearance=1)), ent(DOC, attrs=dict(sensitivity=2))]),
 auth("ReBAC: shared-with set contains principal", ["permit(principal, action, resource) when { resource.sharedWith.contains(principal) };"], ALLOW,
      entities=[ent(DOC, attrs=dict(sharedWith=[ref("User", "alice")]))]),
 auth("multi-tenant isolation: same tenant", ["permit(principal, action, resource) when { principal.tenant == resource.tenant };"], ALLOW,
      entities=[ent(ALICE, attrs=dict(tenant="t1")), ent(DOC, attrs=dict(tenant="t1"))]),
 auth("multi-tenant isolation: cross-tenant denied", ["permit(principal, action, resource) when { principal.tenant == resource.tenant };"], DENY,
      entities=[ent(ALICE, attrs=dict(tenant="t1")), ent(DOC, attrs=dict(tenant="t2"))]),
 auth("break-glass: forbid unless context.breakGlass", ["permit(principal, action, resource);", "forbid(principal, action, resource) unless { context.breakGlass };"], DENY,
      req=REQ(ctx=dict(breakGlass=False))),
 auth("break-glass: flag lifts the forbid", ["permit(principal, action, resource);", "forbid(principal, action, resource) unless { context.breakGlass };"], ALLOW,
      req=REQ(ctx=dict(breakGlass=True))),
 auth("separation of duties: author cannot approve", ["permit(principal, action, resource);", "forbid(principal, action == Action::\"approve\", resource) when { resource.author == principal };"], DENY,
      req=REQ(a=U("Action", "approve")), entities=[ent(DOC, attrs=dict(author=ref("User", "alice")))]),
 auth("least privilege: only listed actions", ["permit(principal, action in [Action::\"view\"], resource);"], DENY, req=REQ(a=U("Action", "edit"))),
 auth("data classification: forbid export of PII", ["permit(principal, action, resource);", "forbid(principal, action == Action::\"export\", resource) when { resource.labels.contains(\"pii\") };"], DENY,
      req=REQ(a=U("Action", "export")), entities=[ent(DOC, attrs=dict(labels=["pii", "hr"]))]),
 auth("classifier-driven: low confidence blocks autonomous action", ["permit(principal, action, resource);", "forbid(principal, action, resource) when { context.confidence < 60 };"], DENY,
      req=REQ(ctx=dict(confidence=59))),
]

# ------------------------------------------------------------------------------------------------ extensions: ip / decimal / datetime / duration
X = cats["extensions"] = []
X += [
 auth("ip: address in CIDR range", ["permit(principal, action, resource) when { context.ip.isInRange(ip(\"10.0.0.0/8\")) };"], ALLOW, req=REQ(ctx=dict(ip=extn("ip", "10.1.2.3")))),
 auth("ip: address outside range", ["permit(principal, action, resource) when { context.ip.isInRange(ip(\"10.0.0.0/8\")) };"], DENY, req=REQ(ctx=dict(ip=extn("ip", "192.168.1.1")))),
 auth("ip: isLoopback", ["permit(principal, action, resource) when { context.ip.isLoopback() };"], ALLOW, req=REQ(ctx=dict(ip=extn("ip", "127.0.0.1")))),
 auth("ip: isIpv6", ["permit(principal, action, resource) when { context.ip.isIpv6() };"], ALLOW, req=REQ(ctx=dict(ip=extn("ip", "::1")))),
 auth("ip: isMulticast", ["permit(principal, action, resource) when { context.ip.isMulticast() };"], ALLOW, req=REQ(ctx=dict(ip=extn("ip", "224.0.0.1")))),
 auth("decimal: lessThan", ["permit(principal, action, resource) when { context.risk.lessThan(decimal(\"0.5\")) };"], ALLOW, req=REQ(ctx=dict(risk=extn("decimal", "0.25")))),
 auth("decimal: greaterThanOrEqual boundary", ["permit(principal, action, resource) when { context.risk.greaterThanOrEqual(decimal(\"0.5\")) };"], ALLOW, req=REQ(ctx=dict(risk=extn("decimal", "0.5")))),
 auth("decimal: precision beyond 4 places rejected", ["permit(principal, action, resource) when { decimal(\"0.12345\") == decimal(\"0.1234\") };"], dict(decision="deny", errors=1)),
 auth("datetime: literal comparison", ["permit(principal, action, resource) when { datetime(\"2026-09-29T00:00:00Z\") < datetime(\"2026-10-01T00:00:00Z\") };"], ALLOW),
 auth("datetime: context timestamp inside window", ["permit(principal, action, resource) when { context.now > datetime(\"2026-01-01T00:00:00Z\") && context.now < datetime(\"2027-01-01T00:00:00Z\") };"], ALLOW,
      req=REQ(ctx=dict(now=extn("datetime", "2026-06-15T12:00:00Z")))),
 auth("datetime: offset by duration", ["permit(principal, action, resource) when { datetime(\"2026-01-01T00:00:00Z\").offset(duration(\"1d\")) == datetime(\"2026-01-02T00:00:00Z\") };"], ALLOW),
 auth("duration: comparison", ["permit(principal, action, resource) when { duration(\"90m\") > duration(\"1h\") };"], ALLOW),
 auth("invalid ip literal is an error", ["permit(principal, action, resource) when { ip(\"999.1.1.1\").isLoopback() };"], dict(decision="deny", errors=1)),
]

# ------------------------------------------------------------------------------------------------ templates
T = cats["templates"] = []
TPL = {"t0": "permit(principal == ?principal, action, resource);"}
T += [
 auth("template linked to alice allows alice", [], ALLOW, templates=TPL, links=[dict(templateId="t0", newId="l0", values={"?principal": dict(type="User", id="alice")})]),
 auth("template linked to bob does not allow alice", [], DENY, templates=TPL, links=[dict(templateId="t0", newId="l0", values={"?principal": dict(type="User", id="bob")})]),
 auth("an unlinked template grants nothing", [], DENY, templates=TPL),
 auth("two links to one template", [], dict(decision="allow", reasons=["l1"]), templates=TPL,
      links=[dict(templateId="t0", newId="l0", values={"?principal": dict(type="User", id="bob")}), dict(templateId="t0", newId="l1", values={"?principal": dict(type="User", id="alice")})]),
 auth("resource slot", [], ALLOW, templates={"t1": "permit(principal, action, resource == ?resource);"},
      links=[dict(templateId="t1", newId="l0", values={"?resource": dict(type="Doc", id="d1")})]),
 auth("link with a slot the template lacks fails", [], dict(failure=True), templates=TPL,
      links=[dict(templateId="t0", newId="l0", values={"?resource": dict(type="Doc", id="d1")})]),
]

# ------------------------------------------------------------------------------------------------ validation (schema-aware)
S = """entity User { dept: String, level: Long };
entity Doc { owner: User, tags: Set<String>, note?: String };
action view appliesTo { principal: [User], resource: [Doc], context: { ip?: String } };
action edit appliesTo { principal: [User], resource: [Doc] };"""


def val(name, pol, valid, contains=None, schema=S, mode="strict", warning=None):
    c = dict(name=name, kind="validate", policies=[pol] if isinstance(pol, str) else pol, schema=schema, mode=mode, expect=dict(valid=valid))
    if contains: c["expect"]["contains"] = contains
    if warning: c["expect"]["warning"] = warning
    return c


V = cats["validation"] = []
V += [
 val("well-typed policy validates", "permit(principal, action == Action::\"view\", resource) when { principal.level > 2 && resource.tags.contains(\"a\") };", True),
 val("attribute typo is rejected (with a suggestion)", "permit(principal, action, resource) when { principal.levl > 2 };", False, "level"),
 val("comparing String to Long is a type error", "permit(principal, action, resource) when { principal.dept > 2 };", False),
 val("optional attribute needs a has guard (strict)", "permit(principal, action, resource) when { resource.note == \"x\" };", False, "note"),
 val("has guard makes optional access valid", "permit(principal, action, resource) when { resource has note && resource.note == \"x\" };", True),
 val("undefined entity type in scope", "permit(principal == Robot::\"r2\", action, resource);", False, "Robot"),
 val("unknown action", "permit(principal, action == Action::\"launch\", resource);", False, "launch"),
 val("attribute that does not exist on the entity", "permit(principal, action, resource) when { principal.email == \"x\" };", False, "email"),
 val("context attribute not in the schema", "permit(principal, action == Action::\"view\", resource) when { context.nope == 1 };", False, "nope"),
 val("optional context attribute needs has", "permit(principal, action == Action::\"view\", resource) when { context.ip == \"1.1.1.1\" };", False),
 val("entity-typed attribute comparison is valid", "permit(principal, action, resource) when { resource.owner == principal };", True),
 val("set method on a non-set", "permit(principal, action, resource) when { principal.dept.contains(\"a\") };", False),
 val("scope that no request can satisfy is flagged as impossible (a warning, not an error)", "permit(principal is Doc, action == Action::\"view\", resource);", True, warning="impossible"),
 val("validation of an empty policy set is fine", [], True),
]

# ------------------------------------------------------------------------------------------------ parsing / formatting
SCHEMA_TEXT = "entity User; entity Doc; action view appliesTo { principal: [User], resource: [Doc] };"
SCHEMA_JSON = {"": {"entityTypes": {"User": {}, "Doc": {}}, "actions": {"view": {"appliesTo": {"principalTypes": ["User"], "resourceTypes": ["Doc"]}}}}}
PA = cats["parsing"] = []
PA += [
 dict(name="valid schema (text)", kind="parse-schema", schema=SCHEMA_TEXT, expect=dict(ok=True)),
 dict(name="valid schema (JSON) is accepted too", kind="parse-schema", schema=SCHEMA_JSON, expect=dict(ok=True)),
 dict(name="schema syntax error", kind="parse-schema", schema="entity User {", expect=dict(ok=False)),
 dict(name="schema referencing an undefined type", kind="parse-schema", schema="entity User { boss: Manager };", expect=dict(ok=False, contains="Manager")),
 dict(name="valid policy set", kind="parse-policies", policies=["permit(principal, action, resource);"], expect=dict(ok=True)),
 dict(name="policy syntax error", kind="parse-policies", policies=["permit(principal, action resource);"], expect=dict(ok=False)),
 dict(name="policy with an unknown method parses (checked later)", kind="parse-policies", policies=["permit(principal, action, resource) when { context.x.frobnicate() };"], expect=dict(ok=False)),
 dict(name="entities: well formed", kind="parse-entities", entities=[ent(ALICE, attrs=dict(dept="eng"))], expect=dict(ok=True)),
 dict(name="entities: validated against the schema (missing attr)", kind="parse-entities", schema=S, entities=[ent(ALICE, attrs=dict(dept="eng"))], expect=dict(ok=False, contains="level")),
 dict(name="entities: validated against the schema (ok)", kind="parse-entities", schema=S, entities=[ent(ALICE, attrs=dict(dept="eng", level=3))], expect=dict(ok=True)),
 dict(name="context: valid for the action", kind="parse-context", schema=S, action=U("Action", "view"), context=dict(ip="1.2.3.4"), expect=dict(ok=True)),
 dict(name="context: wrong attribute type for the action", kind="parse-context", schema=S, action=U("Action", "view"), context=dict(ip=5), expect=dict(ok=False)),
 dict(name="format: messy policy is normalised", kind="format", policy="permit(principal,action,resource)when{ context.a==1&&context.b==2 };", expect=dict(contains="permit")),
 dict(name="format: idempotent on an annotated multi-clause policy", kind="format",
      policy="@id(\"x\") forbid(principal, action, resource) when { context.n > 1 } unless { context.ok };", expect={}),
 dict(name="text -> JSON -> text keeps the policy's meaning", kind="roundtrip", policy="permit(principal == User::\"alice\", action in [Action::\"view\", Action::\"edit\"], resource is Doc) when { context.n > 1 && resource.tags.contains(\"a\") };", expect={}),
 dict(name="roundtrip with unless + like", kind="roundtrip", policy="forbid(principal, action, resource) unless { resource.name like \"a*b\" };", expect={}),
 dict(name="policy set text splits into policies + templates", kind="parts",
      text="permit(principal, action, resource); forbid(principal, action, resource); permit(principal == ?principal, action, resource);", expect=dict(policies=2, templates=1)),
]

# ------------------------------------------------------------------------------------------------ request validation against a schema
R = cats["requests"] = []
RS = "entity User; entity Doc; action view appliesTo { principal: [User], resource: [Doc], context: { n: Long } };"
R += [
 auth("valid request passes validation", ["permit(principal, action, resource);"], ALLOW, req=REQ(ctx=dict(n=1)), schema=RS, validateRequest=True),
 auth("context of the wrong type fails validation", ["permit(principal, action, resource);"], dict(failure=True), req=REQ(ctx=dict(n="one")), schema=RS, validateRequest=True),
 auth("missing required context attribute fails validation", ["permit(principal, action, resource);"], dict(failure=True), req=REQ(ctx={}), schema=RS, validateRequest=True),
 auth("principal type not allowed by the action fails validation", ["permit(principal, action, resource);"], dict(failure=True),
      req=REQ(p=U("Robot", "r2"), ctx=dict(n=1)), schema=RS, validateRequest=True),
 auth("unknown action fails validation", ["permit(principal, action, resource);"], dict(failure=True), req=REQ(a=U("Action", "launch"), ctx=dict(n=1)), schema=RS, validateRequest=True),
 auth("the same bad request passes when validation is off", ["permit(principal, action, resource);"], ALLOW, req=REQ(ctx=dict(n="one")), schema=RS, validateRequest=False),
]

# ------------------------------------------------------------------------------------------------ partial evaluation
def part(name, policies, req, expect, entities=None):
    return dict(name=name, kind="partial", policies=policies, request=req, entities=entities or [], expect=expect)


PE = cats["partial"] = []
PE += [
 part("known request evaluates fully", ["permit(principal == User::\"alice\", action, resource);"], dict(principal=ALICE, action=U("Action", "view"), resource=DOC), dict(decision="allow")),
 part("unknown principal leaves a residual", ["permit(principal == User::\"alice\", action, resource);"], dict(action=U("Action", "view"), resource=DOC), dict(decision="unknown", nontrivialResiduals=1)),
 part("a satisfied unconditional forbid decides despite unknowns", ["forbid(principal, action, resource);", "permit(principal == User::\"alice\", action, resource);"], dict(action=U("Action", "view"), resource=DOC), dict(decision="deny")),
 part("unknown resource attribute condition stays open", ["permit(principal, action, resource) when { resource.public == true };"], dict(principal=ALICE, action=U("Action", "view")), dict(decision="unknown", nontrivialResiduals=1)),
]

for name, cases in cats.items():
    (OUT / f"{name}.json").write_text(json.dumps(cases, indent=1))
print({k: len(v) for k, v in cats.items()}, "total", sum(map(len, cats.values())))
