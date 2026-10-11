# WS9 live Twilio setup and acceptance run

Only Caleb or the deploy workflow provisions AWS configuration (decisions D3).
Kurt assumes `fieldwatch-dev` for read/invoke/data work. Do not modify IAM to
get around a missing permission. Confirm the receiving phone, sender, Account
SID, and readiness before sending any text.

## Caleb provisioning handoff

The Oct 10 inspection found active send/reply Lambdas, `SMS_ENABLED=false`,
empty SID/from/webhook settings, and `ParameterNotFound` for
`/fieldwatch/twilio-auth-token`. No real SMS was sent. The dev role has SSM
read access, not `PutParameter` or deployment permissions.

From an interactive PowerShell terminal, use Caleb's authorized profile:

```powershell
@'
import boto3
import getpass
import warnings
warnings.simplefilter('error', getpass.GetPassWarning)
profile = input('Authorized Caleb AWS profile: ').strip()
ssm = boto3.Session(profile_name=profile, region_name='us-west-2').client('ssm')
token = getpass.getpass('Twilio Auth Token (hidden): ').strip()
if not token:
    raise SystemExit('Empty token; nothing changed')
try:
    result = ssm.put_parameter(
        Name='/fieldwatch/twilio-auth-token', Type='SecureString',
        Value=token, Overwrite=True)
    print('Stored SecureString version', result['Version'])
finally:
    del token
'@ | Set-Content -LiteralPath "$env:TEMP\fieldwatch-set-twilio-token.py"
python "$env:TEMP\fieldwatch-set-twilio-token.py"
Remove-Item -LiteralPath "$env:TEMP\fieldwatch-set-twilio-token.py"
```

This script prompts without echo and aborts if hidden input is unavailable.
The token never appears in shell arguments, on disk, or in output. Disable
boto3 wire/debug logging. Use the standard SSM key; a custom KMS key needs an
explicit decrypt grant. Confirm `/fieldwatch/link-signing-key` also exists
without displaying its value. From `infra/`, with Caleb's deploy credentials:

```powershell
npx aws-cdk@2 deploy FieldWatch-Notifications `
  --parameters 'FieldWatch-Notifications:SmsEnabled=true' `
  --parameters 'FieldWatch-Notifications:TwilioAccountSid=<confirmed-account-sid>' `
  --parameters 'FieldWatch-Notifications:TwilioFrom=<confirmed-E.164-sender>' `
  --parameters 'FieldWatch-Notifications:WebhookUrl=https://psvr1ov6bg.execute-api.us-west-2.amazonaws.com/sms/inbound' `
  --parameters 'FieldWatch-Notifications:PublicUrl=https://dx8jyi91tw5ey.cloudfront.net'
```

Replace placeholders. Use normal bundled artifacts, never offline-synth
artifacts. Read back the live Lambda settings after deployment.
`FieldWatch-Frontend` output `SiteUrl` supplied the CloudFront URL above on
Oct 10; the current notify `FIELDWATCH_PUBLIC_URL` is empty. Recheck that output
if the frontend stack changes before provisioning.

Configure only the confirmed Twilio number's incoming SMS URL to the exact
webhook above, using POST; read back `sms_url` and `sms_method`. The authorized
IncomingPhoneNumber API update accepts `SmsUrl` and `SmsMethod`; resolve the
number SID from the confirmed sender first. See [Twilio number configuration](https://www.twilio.com/docs/phone-numbers/api/incomingphonenumber-resource).
Keep the token in memory or gitignored `agent/.env`, without printing it.
A trial recipient must be verified.

## Live run

Assume `arn:aws:iam::615237236062:role/fieldwatch-dev` with the
`fieldwatch-kurt` profile's STS client. Construct a boto3 Session from the
returned credentials in memory; never print or globally export credentials.
Use `us-west-2` throughout. Finish the disease stream before external test
mutations. Choose one run ID, such as `ws9-live-<UTC timestamp>`.

1. Query `fw-farms` by-phone for the confirmed recipient; require exactly one
   person. Use `interfaces.FarmItem`, `PersonItem`, table names, and
   `person_item_key` for authorized fixtures. Require `opt_in_at`,
   `opted_out=false`, and an accurate timezone. If Kurt is both operator and
   repair assignee, use one person row with that phone; duplicate rows make
   inbound approvals ambiguous. Never overwrite another stream's records.
2. Confirm the deployed investigation path and actual field/investigation IDs.
   Do not fabricate a disease diagnosis or a running agent session. A direct
   notification test does not prove investigation resumption.
3. Configure `FIELDWATCH_SEND_FN=fw-notify-send`, then use
   `interfaces.notify().send(SendMessageRequest(...))` with the confirmed IDs,
   a plain test alert under 300 characters, normal `actionable` urgency,
   `approval={"action_type":"work_order","ttl_hours":24}`, and unique
   `<run-id>#alert` idempotency key. Respect quiet hours and the daily cap;
   wait or use an authorized isolated test farm rather than forcing urgency.
4. Require `provider=twilio`, `status=sent`, and an approval ID. Read the message
   row's `provider_message_id` and fetch that exact Twilio Message resource.
   Record provider status/error code. Repeat the same request once: require
   identical message/approval/provider IDs and no second text. FieldWatch
   `sent` means provider acceptance; require provider evidence and Kurt's
   handset receipt before claiming live delivery.
5. Kurt sends **YES from the confirmed handset** to the confirmed sender.
   Do not substitute a mirror reply. Read the inbound row and approval:
   require the same investigation, `approved`, the confirmed `decided_by`,
   `channel=sms`, and a decision timestamp. Check reply errors without
   dumping message bodies or secrets.
6. Observe deployed session-starter/runtime resumption and one `fw-actions`
   work order for that approval. Require work-order SMS to the confirmed
   assignee; capture its provider ID/status and Kurt's receipt. A direct
   `interfaces.actions().create_work_order(...)` call can separately test
   the approved seam, but label it as direct if automatic resume is absent.
   Repeat the same action request: require one action and one SMS. Never
   create an action before the approval is recorded.
7. Send **HELP** from the handset: require help text with no approval/action
   change. Send **STOP**: require `opted_out=true`. Invoke a new-key normal
   send: require `opted_out` and no provider message. Twilio also processes
   control keywords; distinguish provider replies from application replies.
   Send **START** from the handset, then verify restored consent and updated
   `opt_in_at` before further sends.
8. Test quiet hours on an authorized dedicated farm, or during actual quiet
   hours. Save original settings. Choose a short local quiet window ending
   a few minutes ahead; send normal urgency with no approval/link and unique
   `<run-id>#quiet` key. Require `deferred_quiet_hours`, future
   `scheduled_for`, and no immediate provider call/message row. At the
   scheduled time require exactly one delivery and message row. Scheduler
   may lag slightly. Repeat the request to verify idempotency. Restore farm
   settings after scheduled delivery, since restoring early changes the test.

If a provider call times out or is rejected, `delivery_started` remains.
Reconcile its outcome before retrying; do not clear state or generate new
keys to force duplicate delivery. Twilio `queued`/`sent` are intermediate;
`delivered` is a carrier receipt. Investigate `failed`/`undelivered`.
See [Twilio Message statuses](https://www.twilio.com/docs/messaging/api/message-resource).

## Evidence

Record only nonsecret IDs, deployment revision, timestamps, statuses, and
redacted errors. Acceptance needs alert SID/status and handset receipt;
live YES inbound SID and approved decision; work-order action/SID/status and
receipt; HELP response; STOP suppression; START restoration; quiet-hours
deferral and scheduled delivery times. Missing credentials, provisioning,
recipient confirmation, or runtime resume are explicit blockers. No CDK
or Lambda deployment is authorized for Kurt.
