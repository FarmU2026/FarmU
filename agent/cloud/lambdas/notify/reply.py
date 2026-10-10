import os

from fieldwatch.contracts import InboundMessage
from fieldwatch.notify.webhook import handle_webhook
from .runtime import service, twilio_token


def handler(event, context):
    """Public /sms/inbound: always verify the Twilio signature, even in mirror mode."""
    return handle_webhook(event, service(), public_url=os.getenv('FIELDWATCH_TWILIO_WEBHOOK_URL', ''),
                          auth_token=twilio_token())


def mirror_handler(event, context):
    """Private Lambda invocation from WS12 AFTER dashboard auth + person/farm checks.

    Pass the exact InboundMessage contract; use a stable `mirror:<uuid>` provider ID.
    WS12 resolves from_phone from the authenticated farm's person record, not client input.
    This is intentionally a separate handler, never an unauthenticated webhook bypass.
    """
    message = InboundMessage.model_validate(event)
    if not message.provider_message_id.startswith('mirror:'):
        raise ValueError('Mirror IDs must be namespaced with mirror:')
    return service().reply(message)
