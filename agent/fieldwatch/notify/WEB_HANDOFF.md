# Web conversation handoff (WS12 / WS13 / WS14)

The user chose web conversation instead of SMS. Twilio is preserved for future
use. `SmsEnabled=false` remains the default and the external transport kill
switch. Root verified `fw-notify-send`, `fw-notify-reply`, and `fw-notify-mirror`
Active with `SMS_ENABLED=false` on Oct 11, 2026. The SSM Twilio token was
created earlier as SecureString; web conversation does not read it. This change
does not deploy, change AWS/Twilio settings, or remove any credentials/resources.

## WS9 behavior ready for integration

- `interfaces.notify().send(SendMessageRequest)` uses the send Lambda configured
  by `FIELDWATCH_SEND_FN=fw-notify-send`. With SMS disabled it stores mirror-only
  messages and emits `MessageSent`, including pending approvals when requested.
  Mirror sends bypass SMS consent/opt-out, quiet hours, and the two-text daily cap
  so interactive responses and work orders stay immediate. Real SMS retains all
  those rules. Recipient lookup and idempotency still apply to both paths.
  Existing rate-limited/deferred results remain cached under their original
  idempotency keys; use new keys for new web messages. Do not reset historical
  records or pending schedules blindly to force redelivery.
- `interfaces.notify().reply(InboundMessage)` invokes the private Lambda when
  `FIELDWATCH_MIRROR_FN=fw-notify-mirror`. It requires `mirror:` provider IDs.
  The private handler selects its own mirror runtime, even if external SMS is
  enabled later, and never constructs Twilio or loads its token. Web YES/NO uses
  `interfaces.approvals().record_decision(..., channel="web", emit=False)` via
  the WS9 adapter; one `ApprovalRecorded` event resumes the investigation.
  SMS opt-out does not prevent web replies or approval. The public webhook
  cannot choose web semantics by putting `mirror:` in a MessageSid.
- Work orders use `interfaces.actions().create_work_order(...)` after approval;
  its existing sender calls `interfaces.notify().send`. Mirror delivery remains
  idempotent. Links still require `/fieldwatch/link-signing-key` and a valid
  HTTPS `FIELDWATCH_PUBLIC_URL`; missing signing credentials fail rather than
  weakening report/work-order authentication. This key is web infrastructure,
  independent of `/fieldwatch/twilio-auth-token`.
- Public `/sms/inbound` returns 503 without token/service access while SMS is
  disabled. If enabled later it verifies the Twilio signature; missing transport
  credentials also fail closed. Existing transports and templates remain intact.

## Next owner's work

1. WS12: keep both notify function names wired and invoke only through
   `fieldwatch.interfaces`. Existing routes are
   `POST /api/farms/{farm_id}/reply` (used by `web/app.js`) and authenticated
   `POST /v1/mirror/reply` (dashboard). Resolve the canonical recipient phone
   server-side after farm/person authorization. The unchanged InboundMessage
   contract still identifies a person by phone; require an unambiguous person
   row and do not accept client-supplied identity as authorization. This patch
   does not implement a new phone-free identity contract.
2. WS12: choose a stable reply ID per client submission if retries must dedupe.
   `_reply.handle` currently generates a new mirror ID each time. Return/show
   `result.response_text` for immediate control/error replies; normal agent
   responses arrive asynchronously. Prefer the configured WS9 path. The fallback
   in `cloud/api/_reply.py` still records approval as `sms` and STOP changes
   `farm.muted`; reconcile it with web semantics in the API lane. Do not route
   the mirror Lambda publicly or bypass dashboard/farm authorization.
3. WS13/WS14: show MessageSent/MessageReceived and approval/action changes through
   the existing projector and AppSync farm channels, with stored thread history
   for reconnect. Keep notification events flowing while SMS is disabled. WS9
   never publishes to AppSync directly. Present YES/NO/MORE as web conversation
   actions and render signed report/work-order links in the authenticated UI.
4. WS14: clarify STOP/START/HELP in the text tab. WS9 still treats exact control
   keywords as SMS-preference commands and records them in the web thread;
   STOP does not stop subsequent web messages. Rename or explain the controls
   in the web UX instead of implying that SMS opt-out closes the conversation.
5. Caleb: deploy the reviewed WS9 changes with `SmsEnabled=false`. No frontend
   completion or live deployed web loop is claimed by this branch. Verify the
   authenticated web alert -> YES -> single work-order flow after integration.

Offline tests cover credential-free send/private reply, SMS opt-out/quiet-hour/
cap separation, canonical approval/work-order mirror flow, dedupe, signed-link
failure, dormant public endpoint, and enabled invalid-signature rejection.
