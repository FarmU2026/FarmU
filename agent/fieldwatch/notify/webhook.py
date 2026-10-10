"""Verify Twilio's form signature using the configured public URL, never a Host header.

Signature algorithm: https://www.twilio.com/docs/usage/security
All form parameters participate, including parameters unknown to this version.
"""
import base64
import hashlib
import hmac
from datetime import datetime, timezone
from urllib.parse import parse_qs
from xml.sax.saxutils import escape

from fieldwatch.contracts import InboundMessage


def handle_webhook(event, service, *, public_url, auth_token):
    headers = {k.lower(): v for k, v in (event.get('headers') or {}).items()}
    if not public_url or not auth_token:
        return {'statusCode': 503, 'body': 'Webhook is not configured'}
    if headers.get('content-type', '').split(';')[0].strip().lower() != 'application/x-www-form-urlencoded':
        return {'statusCode': 415, 'body': 'Expected form data'}
    try:
        raw = event.get('body') or ''
        if event.get('isBase64Encoded'):
            raw = base64.b64decode(raw, validate=True).decode('utf-8')
        params = parse_qs(raw, keep_blank_values=True, max_num_fields=100)
        signed = public_url + ''.join(k + v for k in sorted(params) for v in sorted(set(params[k])))
        expected = base64.b64encode(hmac.new(auth_token.encode(), signed.encode(), hashlib.sha1).digest()).decode()
        if not hmac.compare_digest(expected, headers.get('x-twilio-signature', '')):
            return {'statusCode': 403, 'body': 'Invalid signature'}
        if any(len(params.get(k, [])) != 1 for k in ('MessageSid', 'From', 'Body')):
            raise ValueError('Missing or repeated message fields')
        if not params['MessageSid'][0] or not params['From'][0]:
            raise ValueError('Missing sender or message ID')
        message = InboundMessage(provider_message_id=params['MessageSid'][0], from_phone=params['From'][0],
            body=params['Body'][0], received_at=datetime.now(timezone.utc).isoformat(),
            in_reply_to=(params.get('OriginalRepliedMessageSid') or [None])[0])
    except (ValueError, UnicodeError):
        return {'statusCode': 400, 'body': 'Invalid webhook payload'}
    result = service.reply(message)
    text = result.get('response_text', '')
    # The service suppresses response_text on duplicate webhook IDs.
    return {'statusCode': 200, 'headers': {'content-type': 'application/xml'},
            'body': '<Response>' + (f'<Message>{escape(text)}</Message>' if text else '') + '</Response>'}
