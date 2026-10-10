# Notifications (WS9)

`Notifications.send(SendMessageRequest) -> SendMessageResult` and
`Notifications.reply(InboundMessage) -> dict` implement the cloud messaging path.
The old `fieldwatch/sms.py` and frozen local web harness are unchanged because those
paths belong to other owners. No polling is used by this package.

The service has injected storage, provider, clock, event publisher, scheduler,
link key, and approval decision function. Unit tests use moto and fakes.

## Rules and storage

- A person is `fw-farms`: `{farm_id, item: "PERSON#<person_id>", person_id,
  phone_e164, role, opted_out, opt_in_at}`. Real SMS requires `opt_in_at` and no opt-out.
  Mirror-only messages require no real-SMS consent, but still respect opt-out.
- Farm settings are `{farm_id, item: "FARM", timezone: "America/Chicago",
  quiet_hours: {start: "21:00", end: "07:00"}}`. An absent quiet-hours setting
  disables deferral; equal endpoints also disable it. Time windows are start-inclusive,
  end-exclusive, with daylight saving handled in the farm's IANA time zone.
- Two nonurgent messages per farm per local calendar day, as specified in core-features.
  Urgent messages bypass quiet hours and the cap. Deferred sends recheck both rules
  and opt-out, and generate fresh links/approval expirations at delivery time.
- `person_for_role(farm_id, "repairs")` resolves a unique role choice without an opt-out;
  callers must choose a person explicitly if several people have the same role.
  Send always targets the supplied `person_id`; it never substitutes the operator.
- Request idempotency is farm-scoped. Reusing a key for different content is an error.
  Conditional leases prevent concurrent processing; daily quota reservations are atomic.
  Notification state and quota records use namespaced keys in `fw-idempotency`.
- Delivery receipt, message row, and pending events are checkpointed. After a successful
  provider response, retries can finish database/event writes without resending SMS.
  A timeout or crash during the provider call leaves an uncertain delivery marked
  `delivery_started`; reconcile its provider outcome before clearing this state.
  An immediate provider rejection also stays blocked, deliberately avoiding blind retries.
- Event delivery is at-least-once. Consumers dedupe `message_id`, `reply_id`, or
  `approval_id`, as required by the event catalog. These Lambdas never publish to AppSync.
  Completed idempotency records do not expire automatically; quota/lease records do.
- STOP/START/HELP are handled locally. An unknown phone gets the JOIN response only.
  Shared phone numbers across multiple people are ambiguous and cannot approve anything.
  Exact keywords are classified; arbitrary farmer text is passed as data.
- Explicit reply targets never fall back to a different approval. Without a target,
  select the latest pending, unexpired approval for that person and farm. Expiration
  uses processing time, not a caller-supplied `received_at` timestamp.

## Integration for owners of shared files

**WS6/registry:** call `cloud.lambdas.notify.send.handler` with the unchanged
`SendMessageRequest` JSON. It returns `SendMessageResult` JSON. The Scheduler wrapper
is `{request: <SendMessageRequest>, scheduled: true}` and is only for private invocations.

**WS8:** the lazy adapter in `cloud/lambdas/notify/runtime.py` calls:

```python
fieldwatch.approvals.record_decision(
    approval_id=approval_id, person_id=person_id,
    decision="approved",  # or "declined"
    channel="sms",
)  # returns contracts.Approval (or its exact dict)
```

WS8's module is absent in this checkout. Until it lands, approval replies fail closed.
The function must enforce pending status, recipient and expiry atomically, returning
the existing decision on retries/races. WS9 emits `ApprovalRecorded` for the returned
decision and never also emits `ReplyReceived` for that reply. WS8 must not independently
publish that same event from this function. The adapter is the only place to reconcile
a different WS8 function signature. Pending approvals created by send include the
contract fields plus `farm_id`, `created_at`, and `expires_at_epoch` table metadata.
External approvals without `farm_id` are scoped through their investigation record.

**WS12:** route `POST /sms/inbound` unchanged to `reply.handler`. Preserve the original
form body, `isBase64Encoded`, and headers. Set the exact externally configured Twilio
URL, including any query string; never derive it from a user-supplied Host header.
The signature is checked over all form fields before any reply effects, following
[Twilio's signature specification](https://www.twilio.com/docs/usage/security).
Twilio control responses use escaped TwiML; duplicate provider IDs get empty TwiML.

For `/v1/mirror/reply`, authenticate/authorize the farm and person in WS12, resolve their
phone server-side, and privately invoke `reply.mirror_handler` with `InboundMessage`.
Use a stable `mirror:<uuid>` provider ID. Never route the mirror Lambda as a public,
unauthenticated endpoint. Its returned `response_text` can be shown in the mirror.
`CALL` maps to contract intent `ask` (the contract has no `call` intent).

For ordinary replies, investigation lookup currently uses a paginated scan because
the shared tables have no person index. It selects open/running/waiting investigations
from the past seven days in the same farm, with a matching person when stored.

**Signed links:** WS12 can use `fieldwatch.notify.links.verify_token`. Token format is
`base64url(canonical JSON).base64url(HMAC-SHA256(encoded payload))`, without padding.
Claims are exactly `{farm_id, scope, subject_id, exp}`; allowed scopes are `report`,
`upload`, `work_order`, `onboard`. WS12 must also enforce the route's scope, subject,
and farm after verifying the signature/expiry. URLs use `/reports/<id>`,
`/work-orders/<id>`, `/uploads/<id>`, or `/onboarding/<id>`, with query parameter `t`.

**Caleb/infra:** import `stacks.notifications.Notifications` and instantiate it with
the shared `data` object in `infra/app.py`. This file is intentionally not edited by WS9.
The stack exposes `.send`, `.reply`, and `.mirror` for API/Gateway grants and integrations.
Deploy parameters: `PublicUrl`, `WebhookUrl`, `TwilioAccountSid`, `TwilioFrom`, and
`SmsEnabled` (default `false`). No credentials are embedded in templates.
SSM SecureStrings `/fieldwatch/link-signing-key` and `/fieldwatch/twilio-auth-token`
must exist; use the standard SSM key (a custom KMS key needs an explicit decrypt grant).
`FIELDWATCH_SMS=end-user-messaging` is an explicit stub that refuses delivery.

The stack includes a Scheduler group, invocation role and failure queue. One-time
schedules use UTC `at(...)`, deterministic names, retry limits and automatic deletion,
as described in the [Scheduler API](https://docs.aws.amazon.com/scheduler/latest/APIReference/API_CreateSchedule.html).

## Offline validation

Install the repo's declared `agent/requirements-dev.txt` and `infra/requirements.txt`
in the test environment. From `agent/`:

```text
python -m pytest tests -q -m "not live"
```

From `infra/`, with CDK already installed:

```text
cdk synth --app "python -m stacks.notifications --offline-synth" --no-lookups
```

The offline entry point includes both DataPlane and Notifications without editing
the shared app. It skips dependency-wheel bundling, so its artifacts are for template
validation only. Normal synthesis uses the shared `python_lambda` builder to package
Python 3.12 Linux x86_64 dependencies. Do not deploy offline validation artifacts.
Only Caleb or the deploy workflow deploys.
