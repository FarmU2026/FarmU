"""Small AWS adapters. Clients are injected, so tests never need credentials."""
import hashlib
import json
import uuid
from contextlib import contextmanager
from decimal import Decimal

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError


def _conditional(exc):
    return exc.response['Error']['Code'] == 'ConditionalCheckFailedException'


def _dynamo(value):
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_dynamo(v) for v in value]
    return value


class DynamoStore:
    def __init__(self, resource, prefix='fw-'):
        self.db, self.prefix = resource, prefix

    def table(self, name):
        return self.db.Table(self.prefix + name)

    def get(self, name, key):
        return self.table(name).get_item(Key=key, ConsistentRead=True).get('Item')

    def put(self, name, item):
        self.table(name).put_item(Item=_dynamo(item))

    def create(self, name, pk, item):
        try:
            self.table(name).put_item(Item=item, ConditionExpression='attribute_not_exists(#pk)',
                                      ExpressionAttributeNames={'#pk': pk})
        except ClientError as exc:
            if not _conditional(exc):
                raise

    def query(self, name, attr, value, index=None):
        kwargs = {'KeyConditionExpression': Key(attr).eq(value)}
        if index:
            kwargs['IndexName'] = index
        else:
            kwargs['ConsistentRead'] = True
        return self._pages(self.table(name).query, kwargs)

    def scan(self, name):
        return self._pages(self.table(name).scan, {})

    @staticmethod
    def _pages(fn, kwargs):
        items = []
        while True:
            result = fn(**kwargs)
            items.extend(result.get('Items', []))
            if not result.get('LastEvaluatedKey'):
                return items
            kwargs['ExclusiveStartKey'] = result['LastEvaluatedKey']

    @contextmanager
    def lock(self, key, now):
        # Lease exceeds the Lambda's 30-second timeout; an expired process can be retried.
        owner = str(uuid.uuid4())
        lock_key = 'notify-lock#' + key
        try:
            self.table('idempotency').put_item(
                Item={'key': lock_key, 'owner': owner, 'expires_at': int(now.timestamp()) + 120},
                ConditionExpression='attribute_not_exists(#k) OR expires_at < :now',
                ExpressionAttributeNames={'#k': 'key'},
                ExpressionAttributeValues={':now': int(now.timestamp())})
        except ClientError as exc:
            if _conditional(exc):
                raise RuntimeError('Notification is already processing; retry later') from exc
            raise
        try:
            yield
        finally:
            try:
                self.table('idempotency').delete_item(Key={'key': lock_key},
                    ConditionExpression='#o = :owner', ExpressionAttributeNames={'#o': 'owner'},
                    ExpressionAttributeValues={':owner': owner})
            except ClientError as exc:
                if not _conditional(exc):
                    raise

    def reserve_daily(self, farm_id, day, message_id, cap, now):
        # Atomic counter + membership in one item: retrying this message consumes no extra slot.
        key = 'notify-cap#' + farm_id + '#' + day
        table = self.table('idempotency')
        existing = self.get('idempotency', {'key': key}) or {}
        if message_id in existing.get('messages', set()):
            return True
        try:
            table.update_item(Key={'key': key},
                UpdateExpression='SET expires_at = :ttl ADD #n :one, messages :message',
                ConditionExpression='(attribute_not_exists(#n) OR #n < :cap) AND '
                                    '(attribute_not_exists(messages) OR NOT contains(messages, :id))',
                ExpressionAttributeNames={'#n': 'count'},
                ExpressionAttributeValues={':ttl': int(now.timestamp()) + 3 * 86400,
                    ':one': 1, ':cap': cap, ':message': {message_id}, ':id': message_id})
            return True
        except ClientError as exc:
            if not _conditional(exc):
                raise
            existing = self.get('idempotency', {'key': key}) or {}
            return message_id in existing.get('messages', set())

    def set_optout(self, person, opted_out, now):
        self.table('farms').update_item(
            Key={'farm_id': person['farm_id'], 'item': person['item']},
            UpdateExpression='SET opted_out = :out, opt_in_at = :at' if not opted_out else 'SET opted_out = :out',
            ExpressionAttributeValues={':out': opted_out, **({':at': now} if not opted_out else {})})


class EventEmitter:
    def __init__(self, client, bus='fieldwatch'):
        self.client, self.bus = client, bus

    def __call__(self, event):
        response = self.client.put_events(Entries=[{'EventBusName': self.bus,
            'Source': 'fieldwatch.notify', 'DetailType': type(event).__name__, 'Detail': event.model_dump_json()}])
        if response.get('FailedEntryCount', 0):
            raise RuntimeError('EventBridge rejected notification event')


class Scheduler:
    def __init__(self, client, target_arn, role_arn, group='fieldwatch-notifications', dlq_arn=None):
        self.client, self.target_arn, self.role_arn = client, target_arn, role_arn
        self.group, self.dlq_arn = group, dlq_arn

    def __call__(self, message_id, scheduled_for, request):
        name = 'fw-notify-' + hashlib.sha256((message_id + scheduled_for).encode()).hexdigest()[:40]
        target = {'Arn': self.target_arn, 'RoleArn': self.role_arn,
                  'Input': json.dumps({'request': request.model_dump(), 'scheduled': True}),
                  'RetryPolicy': {'MaximumEventAgeInSeconds': 86400, 'MaximumRetryAttempts': 10}}
        if self.dlq_arn:
            target['DeadLetterConfig'] = {'Arn': self.dlq_arn}
        try:
            self.client.create_schedule(Name=name, GroupName=self.group,
                ClientToken=hashlib.sha256(name.encode()).hexdigest(),
                ScheduleExpression=f"at({scheduled_for.removesuffix('Z')})", ScheduleExpressionTimezone='UTC',
                FlexibleTimeWindow={'Mode': 'OFF'}, ActionAfterCompletion='DELETE', Target=target)
        except ClientError as exc:
            # Same deterministic schedule can be retried after a crash before saving state.
            if exc.response['Error']['Code'] != 'ConflictException':
                raise
