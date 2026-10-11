"""Lazy AWS construction. Importing handlers performs no network or credential lookup."""
import os
from functools import lru_cache

from fieldwatch import config
from fieldwatch.notify import Notifications
from fieldwatch.notify.aws import DynamoStore, EventEmitter, Scheduler
from fieldwatch.notify.providers import MirrorProvider, TwilioProvider, EndUserMessagingStub


def _person_phone(approval_id, person_id):
    """The number on file for the person the approval was asked of. The reply handler found this person by
    the number the text came from, so this is that number."""
    import boto3
    from fieldwatch import interfaces
    db = boto3.resource('dynamodb', region_name=config.REGION)
    approval = db.Table(interfaces.table_name('approvals')).get_item(Key={'approval_id': approval_id}).get('Item') or {}
    farm_id = approval.get('farm_id')
    if not farm_id and approval.get('investigation_id'):
        inv = db.Table(interfaces.table_name('investigations')).get_item(
            Key={'investigation_id': approval['investigation_id']}).get('Item') or {}
        farm_id = inv.get('farm_id')
    if not farm_id:
        return None
    person = db.Table(interfaces.table_name('farms')).get_item(
        Key={'farm_id': farm_id, 'item': interfaces.person_item_key(person_id)}).get('Item') or {}
    return person.get('phone_e164')


def record_decision(*, approval_id, person_id, decision, channel):
    """Notifications.reply -> WS8, through interfaces.ApprovalsAPI.

    WS8's signature is record_decision(approval_id, decision, decided_by_person_id, channel, from_phone, *, emit).
    emit=False: Notifications.reply publishes ApprovalRecorded itself, so one YES makes exactly one event.
    A decision WS8 refuses (already decided, expired, wrong person) returns the stored approval; the reply
    handler then answers "Already decided by ..." instead of failing the webhook.
    """
    from fieldwatch import interfaces
    approvals = interfaces.approvals()
    try:
        return approvals.record_decision(approval_id, decision, person_id, channel,
                                         from_phone=_person_phone(approval_id, person_id), emit=False)
    except interfaces.FieldwatchError as exc:
        if exc.code not in ('ALREADY_DECIDED', 'EXPIRED', 'POLICY_DENIED'):
            raise
        current = approvals.get(approval_id)
        if current is None:
            raise
        return current


@lru_cache(maxsize=8)
def parameter(name):
    import boto3
    return boto3.client('ssm', region_name=config.REGION).get_parameter(
        Name=name, WithDecryption=True)['Parameter']['Value']


def twilio_token():
    return parameter(os.getenv('FIELDWATCH_TWILIO_TOKEN_PARAM', '/fieldwatch/twilio-auth-token'))


@lru_cache(maxsize=1)
def service():
    import boto3
    provider_name = os.getenv('FIELDWATCH_SMS', 'twilio').lower()
    enabled = os.getenv('SMS_ENABLED', 'false').lower() == 'true'
    provider = MirrorProvider()
    if enabled:
        if provider_name == 'twilio':
            provider = TwilioProvider(os.environ['TWILIO_ACCOUNT_SID'], twilio_token(), os.environ['TWILIO_FROM'])
        elif provider_name == 'end-user-messaging':
            provider = EndUserMessagingStub()
        elif provider_name not in {'none', 'mirror-only'}:
            raise ValueError('Unsupported notification provider')
    client = lambda name: boto3.client(name, region_name=config.REGION)
    return Notifications(store=DynamoStore(boto3.resource('dynamodb', region_name=config.REGION), config.TABLE_PREFIX),
        provider=provider, emit=EventEmitter(client('events'), config.EVENT_BUS),
        defer=Scheduler(client('scheduler'), os.environ.get('FIELDWATCH_SEND_ARN', ''),
            os.environ.get('FIELDWATCH_SCHEDULER_ROLE_ARN', ''),
            os.getenv('FIELDWATCH_SCHEDULE_GROUP', 'fieldwatch-notifications'),
            os.getenv('FIELDWATCH_NOTIFY_DLQ_ARN')),
        signing_key=lambda: parameter(config.LINK_KEY_PARAM).encode(),
        base_url=os.getenv('FIELDWATCH_PUBLIC_URL', ''), record_decision=record_decision)
