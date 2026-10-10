# Cedar policy for the AgentCore Gateway (WS8, not deployed)

`fieldwatch-gateway.cedar` is the Gateway policy from `docs/system-design.md` "Policy design":

- read tools: always permitted;
- write tools (`create_work_order`, `create_scout_pin`, `draft_insurance_notice`): permitted only when
  `context.input` has an `approval_id` starting `apr-` and a non-empty `investigation_id`;
- messaging tools: permitted only with an `investigation_id`.

It checks shape only. AgentCore Policy cannot see the Approvals table, so `agent/fieldwatch/actions.py`
(called by every write-tool Lambda) enforces status, owner, type, expiry and one-action-per-approval.
This policy is the second layer; the first is the required `approval_id` / `investigation_id` fields in the
registry's input models (spike finding 23). Per `docs/decisions.md` D9 this is the first thing cut if time runs out.

## Attaching it (Caleb, after the Gateway exists; nothing here runs automatically)

1. Substitute the placeholders: `{{TARGET}}` is the Gateway target name (tools appear as `<target>___<tool>`),
   `{{GATEWAY_ARN}}` is the Gateway's ARN.
2. Split the file on the `// ---- policy: <name> ----` lines: each `permit(...)` becomes one policy named
   `<name>` (policy names use letters, digits and underscores, so write `fieldwatch_read_tools`).
3. Create a policy engine, add the three policies to it with the console or the AgentCore Policy API/CLI
   (Bedrock AgentCore, Policy), and **validate** them against the schema AgentCore generates from the Gateway's tools.
   Validation fails on unknown tool names or input fields; fix the list rather than loosening the policy.
4. Associate the engine with the Gateway in `LOG_ONLY` mode first, call each tool once with and without an
   `approval_id`, check the decision logs, then switch to `ENFORCE`.
5. Verify in `ENFORCE`: a `create_work_order` call without `approval_id` is refused by the Gateway (before any Lambda
   runs); with a made-up `apr-123` it reaches the Lambda, which returns `POLICY_DENIED`.

Unverified from here (no AWS access): the exact `principal` form AgentCore expects for IAM-authenticated callers
(the file leaves `principal` unconstrained) and whether the validator wants `has` guards on required inputs.
`agent/tests/test_approvals.py::test_cedar_policy_covers_registry` keeps the tool lists in step with the registry.
