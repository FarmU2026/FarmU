"""Lazy AWS construction. Importing handlers performs no network or credential lookup."""
import os
from functools import lru_cache

from fieldwatch import config
from fieldwatch.notify import Notifications
from fieldwatch.notify.aws import DynamoStore, EventEmitter, Scheduler
from fieldwatch.notify.providers import MirrorProvider, TwilioProvider, EndUserMessagingStub


def record_decision(**kwargs):
    # WS8 owns all conditional decision writes. Fail closed until that module lands.
    from fieldwatch.approvals import record_decision as decide
    return decide(**kwargs)


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
