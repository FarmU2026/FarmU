import os

from fieldwatch.contracts import InboundMessage
from fieldwatch.notify.webhook import handle_webhook
from botocore.exceptions import ClientError
from .runtime import service, twilio_token


def handler(event, context):
    """Dormant when SMS is disabled; otherwise every public request needs a Twilio signature."""
    if os.getenv('SMS_ENABLED', 'false').lower() != 'true':
        return {'statusCode': 503, 'headers': {'content-type': 'text/plain'},
                'body': 'SMS is disabled.'}
    try:
        token = twilio_token()
        notifications = service()
    except (ClientError, KeyError, ValueError):
        # Missing or unavailable transport credentials must not process a public reply.
        return {'statusCode': 503, 'headers': {'content-type': 'text/plain'},
                'body': 'SMS is unavailable.'}
    return handle_webhook(event, notifications, public_url=os.getenv('FIELDWATCH_TWILIO_WEBHOOK_URL', ''),
                          auth_token=token)


def mirror_handler(event, context):
    """Private Lambda invocation from WS12 AFTER dashboard auth + person/farm checks.

    Pass the exact InboundMessage contract; use a stable `mirror:<uuid>` provider ID.
    WS12 resolves from_phone from the authenticated farm's person record, not client input.
    This is intentionally a separate handler, never an unauthenticated webhook bypass.
    """
    message = InboundMessage.model_validate(event)
    if not message.provider_message_id.startswith('mirror:'):
        raise ValueError('Mirror IDs must be namespaced with mirror:')
    return service(mirror=True).reply(message, channel='mirror')
