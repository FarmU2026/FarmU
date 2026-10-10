// FieldWatch AgentCore Gateway policy (docs/system-design.md "Policy design", implementation-specs "Gateway tools").
//
// NOT DEPLOYED. AgentCore Policy sees only the call's arguments (context.input) and the caller's identity, never
// the Approvals table. So this policy enforces SHAPE: write tools must carry a well-formed approval_id and an
// investigation_id. The FACTS (approval exists, is approved, same investigation, same action type, unexpired,
// one action per approval) are enforced in agent/fieldwatch/actions.py, which every write-tool Lambda calls.
//
// Placeholders, filled in by the attach step (see README.md):
//   {{TARGET}}       the Gateway target name; Gateway exposes tools as <target>___<tool>
//   {{GATEWAY_ARN}}  arn:aws:bedrock-agentcore:us-west-2:<account>:gateway/<gateway-id>
//
// Cedar is default-deny, so every tool must appear in exactly one policy below. Adding a tool to
// agent/fieldwatch/registry.py without listing it here makes the Gateway refuse it, and
// agent/tests/test_approvals.py::test_cedar_policy_covers_registry fails until you add it.
//
// Each `permit` below is one AgentCore Policy resource; the attach step splits this file on the
// "// ---- policy:" marker lines.

// ---- policy: fieldwatch-read-tools ----
// Read tools are always permitted.
permit(
  principal,
  action in [
    AgentCore::Action::"{{TARGET}}___list_fields",
    AgentCore::Action::"{{TARGET}}___look_at_field",
    AgentCore::Action::"{{TARGET}}___get_scene_analysis",
    AgentCore::Action::"{{TARGET}}___get_field_context",
    AgentCore::Action::"{{TARGET}}___get_index_history",
    AgentCore::Action::"{{TARGET}}___compare_to_cohort",
    AgentCore::Action::"{{TARGET}}___get_weather",
    AgentCore::Action::"{{TARGET}}___get_hazards",
    AgentCore::Action::"{{TARGET}}___get_hail",
    AgentCore::Action::"{{TARGET}}___get_drought",
    AgentCore::Action::"{{TARGET}}___get_county_stats",
    AgentCore::Action::"{{TARGET}}___get_soil",
    AgentCore::Action::"{{TARGET}}___get_soil_profile",
    AgentCore::Action::"{{TARGET}}___get_disease_reports",
    AgentCore::Action::"{{TARGET}}___test_overlap",
    AgentCore::Action::"{{TARGET}}___search_agronomy",
    AgentCore::Action::"{{TARGET}}___analyze_field",
    AgentCore::Action::"{{TARGET}}___score_hypotheses",
    AgentCore::Action::"{{TARGET}}___estimate_impact",
    AgentCore::Action::"{{TARGET}}___estimate_loss"
  ],
  resource == AgentCore::Gateway::"{{GATEWAY_ARN}}"
);

// ---- policy: fieldwatch-write-tools-need-approval ----
// Tools that act on the farm's behalf: only with a well-formed approval_id (apr-...) and an investigation_id.
// Cedar "like" supports only the * wildcard, so the check is the "apr-" prefix plus "something after it"; the
// registry input model additionally pins the full pattern ^apr-[A-Za-z0-9_-]{3,64}$.
permit(
  principal,
  action in [
    AgentCore::Action::"{{TARGET}}___create_work_order",
    AgentCore::Action::"{{TARGET}}___create_scout_pin",
    AgentCore::Action::"{{TARGET}}___draft_insurance_notice"
  ],
  resource == AgentCore::Gateway::"{{GATEWAY_ARN}}"
) when {
  context.input has approval_id &&
  context.input.approval_id like "apr-*" &&
  context.input.approval_id != "apr-" &&
  context.input has investigation_id &&
  context.input.investigation_id != ""
};

// ---- policy: fieldwatch-messaging-tools ----
// Messaging needs no approval (quiet hours and rate limits are enforced in the send Lambda) but must name its
// investigation so every outbound message is auditable.
permit(
  principal,
  action in [
    AgentCore::Action::"{{TARGET}}___send_farmer_message",
    AgentCore::Action::"{{TARGET}}___request_photo"
  ],
  resource == AgentCore::Gateway::"{{GATEWAY_ARN}}"
) when {
  context.input has investigation_id &&
  context.input.investigation_id != ""
};
