"""WS9 acceptance checks. All AWS calls are intercepted by moto; no SMS/network."""
import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import urlencode, urlparse, parse_qs

import boto3
import pytest
from moto import mock_aws

from fieldwatch.contracts import (
    Approval, ApprovalRecorded, InboundMessage, MessageReceived, MessageSent,
    ReplyReceived, SendMessageRequest, SendMessageResult,
)
from fieldwatch.notify import Notifications, classify_intent, quiet_until
from fieldwatch.notify.aws import DynamoStore
from fieldwatch.notify.links import verify_token
from fieldwatch.notify.providers import MirrorProvider, TwilioProvider, EndUserMessagingStub
from fieldwatch.notify.webhook import handle_webhook


FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures' / 'contracts'
NOW = datetime(2026, 7, 18, 17, 24, 40, tzinfo=timezone.utc)


@pytest.fixture
def env():
    with mock_aws():
        db = boto3.resource('dynamodb', region_name='us-west-2')
        specs = [
            ('farms', 'farm_id', 'item', [('by-phone', 'phone_e164', None)]),
            ('messages', 'message_id', None, [('by-provider-id', 'provider_message_id', None)]),
            ('approvals', 'approval_id', None, [('by-person-status', 'sent_to_person_id', 'status')]),
            ('investigations', 'investigation_id', None, []),
            ('idempotency', 'key', None, []),
        ]
        for name, pk, sk, indexes in specs:
            keys = lambda p, s: ([{'AttributeName': p, 'KeyType': 'HASH'}] +
                                ([{'AttributeName': s, 'KeyType': 'RANGE'}] if s else []))
            attrs = {pk} | ({sk} if sk else set())
            for _, p, s in indexes:
                attrs |= {p} | ({s} if s else set())
            kwargs = dict(TableName='fw-' + name, BillingMode='PAY_PER_REQUEST',
                          KeySchema=keys(pk, sk), AttributeDefinitions=[
                              {'AttributeName': a, 'AttributeType': 'S'} for a in sorted(attrs)])
            if indexes:
                kwargs['GlobalSecondaryIndexes'] = [dict(IndexName=i, KeySchema=keys(p, s),
                    Projection={'ProjectionType': 'ALL'}) for i, p, s in indexes]
            db.create_table(**kwargs)
        store = DynamoStore(db)
        farm = dict(farm_id='wohleb-farm', item='FARM', timezone='America/Chicago',
                    quiet_hours={'start': '21:00', 'end': '07:00'})
        person = dict(farm_id='wohleb-farm', item='PERSON#dad', person_id='dad',
                      phone_e164='+14025550100', role='operator', opted_out=False,
                      opt_in_at='2026-07-01T12:00:00Z')
        store.put('farms', farm)
        store.put('farms', person)
        events, schedules, deliveries = [], [], []
        provider = Mock(name='provider')
        provider.name = 'twilio'
        provider.send.side_effect = lambda phone, body: deliveries.append((phone, body)) or 'SM-out'
        decision = Mock()
        clock = Mock(return_value=NOW)
        service = Notifications(store=store, provider=provider, emit=events.append,
            defer=lambda *args: schedules.append(args), signing_key=lambda: b'test-key',
            base_url='https://fieldwatch.example', record_decision=decision, clock=clock)
        request = SendMessageRequest.model_validate_json((FIXTURES / 'send_message_request.json').read_text())
        yield locals()


def test_contract_sample_send_is_persisted_and_deduped(env):
    s, req = env['service'], env['request']
    first = s.send(req)
    assert SendMessageResult.model_validate(first.model_dump()) == first
    assert first.status == 'sent' and first.approval_id
    assert s.send(req) == first
    assert len(env['deliveries']) == 1
    row = env['store'].get('messages', {'message_id': first.message_id})
    assert row['provider_message_id'] == 'SM-out' and row['body'] == req.body
    approval = env['store'].get('approvals', {'approval_id': first.approval_id})
    assert approval['sent_to_person_id'] == 'dad' and approval['status'] == 'pending'
    assert len(env['events']) == 1 and isinstance(env['events'][0], MessageSent)


def test_optout_precedes_dedupe_and_has_no_effects(env):
    env['person']['opted_out'] = True
    env['store'].put('farms', env['person'])
    assert env['service'].send(env['request']).status == 'opted_out'
    assert not env['deliveries'] and not env['events'] and not env['schedules']


def test_quiet_deferral_reenters_once_and_rechecks_optout(env):
    env['clock'].return_value = datetime(2026, 7, 19, 3, tzinfo=timezone.utc)
    result = env['service'].send(env['request'])
    assert result.status == 'deferred_quiet_hours'
    assert result.scheduled_for == '2026-07-19T12:00:00Z'
    assert not env['deliveries']
    assert env['service'].send(env['request']) == result
    assert len(env['schedules']) == 1
    env['clock'].return_value = datetime(2026, 7, 19, 12, tzinfo=timezone.utc)
    sent = env['service'].send(env['request'], scheduled=True)
    assert sent.message_id == result.message_id and sent.status == 'sent'
    env['service'].send(env['request'], scheduled=True)
    assert len(env['deliveries']) == 1


def test_deferred_optout(env):
    env['clock'].return_value = datetime(2026, 7, 19, 3, tzinfo=timezone.utc)
    env['service'].send(env['request'])
    env['person']['opted_out'] = True
    env['store'].put('farms', env['person'])
    env['clock'].return_value = datetime(2026, 7, 19, 12, tzinfo=timezone.utc)
    assert env['service'].send(env['request'], scheduled=True).status == 'opted_out'
    assert not env['deliveries']


def test_cap_is_per_farm_and_urgent_bypasses_it(env):
    for i in range(2):
        assert env['service'].send(env['request'].model_copy(update={'idempotency_key': str(i)})).status == 'sent'
    req = env['request'].model_copy(update={'idempotency_key': 'third'})
    assert env['service'].send(req).status == 'rate_limited'
    env['clock'].return_value = datetime(2026, 7, 19, 3, tzinfo=timezone.utc)
    assert env['service'].send(req.model_copy(update={'urgency': 'urgent', 'idempotency_key': 'urgent'})).status == 'sent'
    assert not env['schedules']


def test_mirror_and_repairs_recipient(env):
    env['service'].provider = MirrorProvider()
    repair = dict(env['person'], person_id='son', item='PERSON#son', role='repairs', phone_e164='+14025550101')
    env['store'].put('farms', repair)
    assert env['service'].person_for_role('wohleb-farm', 'repairs')['person_id'] == 'son'
    result = env['service'].send(env['request'].model_copy(update={'person_id': 'son', 'approval': None}))
    assert result.status == 'mirror_only'
    assert env['events'][0].person_id == 'son' and not env['deliveries']


def test_mirror_send_is_immediate_despite_sms_optout_quiet_hours_and_cap(env):
    env['service'].provider = MirrorProvider()
    env['person'].update(opted_out=True, role='repairs', opt_in_at=None)
    env['store'].put('farms', env['person'])
    env['clock'].return_value = datetime(2026, 7, 19, 3, tzinfo=timezone.utc)
    assert env['service'].person_for_role('wohleb-farm', 'repairs')['person_id'] == 'dad'
    for number in range(4):
        req = env['request'].model_copy(update={'approval': None, 'idempotency_key': f'web-{number}'})
        assert env['service'].send(req).status == 'mirror_only'
    assert not env['deliveries'] and not env['schedules']
    assert len(env['events']) == 4


def test_mirror_still_requires_signing_key_for_link_requests(env):
    env['service'].provider = MirrorProvider()
    env['service'].signing_key = lambda: b''
    req = env['request'].model_copy(update={'link': {'scope': 'work_order', 'subject_id': 'act-test'}})
    with pytest.raises(ValueError, match='signing key'):
        env['service'].send(req)
    assert not env['events'] and not env['deliveries']


def test_signed_link_scoped_and_tamper_expiry_checked(env):
    request = env['request'].model_copy(update={'approval': None,
        'link': {'scope': 'report', 'subject_id': 'inv-1', 'ttl_hours': 24}})
    env['service'].send(request)
    url = env['deliveries'][0][1].split()[-1]
    token = parse_qs(urlparse(url).query)['t'][0]
    claims = verify_token(token, b'test-key', now=NOW)
    assert claims['farm_id'] == 'wohleb-farm' and claims['subject_id'] == 'inv-1'
    assert claims['scope'] == 'report'
    with pytest.raises(ValueError):
        verify_token(token + 'x', b'test-key', now=NOW)
    with pytest.raises(ValueError):
        verify_token(token, b'test-key', now=datetime(2026, 7, 20, tzinfo=timezone.utc))


def test_event_retry_does_not_resend_sms(env):
    emit = Mock(side_effect=RuntimeError('bus unavailable'))
    env['service'].emit = emit
    with pytest.raises(RuntimeError):
        env['service'].send(env['request'])
    env['service'].emit = env['events'].append
    assert env['service'].send(env['request']).status == 'sent'
    assert len(env['deliveries']) == 1 and len(env['events']) == 1


def test_provider_timeout_is_not_blindly_retried(env):
    env['provider'].send.side_effect = TimeoutError('unknown delivery')
    for _ in range(2):
        with pytest.raises((TimeoutError, RuntimeError)):
            env['service'].send(env['request'])
    assert env['provider'].send.call_count == 1
    assert not env['events']


def test_send_requires_consent_for_real_sms(env):
    del env['person']['opt_in_at']
    env['store'].put('farms', env['person'])
    assert env['service'].send(env['request']).status == 'opted_out'
    assert not env['deliveries']


def test_changed_request_cannot_reuse_idempotency_key(env):
    env['service'].send(env['request'])
    with pytest.raises(ValueError, match='different request'):
        env['service'].send(env['request'].model_copy(update={'body': 'A different text'}))
    assert len(env['deliveries']) == 1


def test_message_write_retries_from_saved_receipt(env, monkeypatch):
    original = env['store'].put
    def fail_message(name, item):
        if name == 'messages':
            raise RuntimeError('DynamoDB unavailable')
        return original(name, item)
    monkeypatch.setattr(env['store'], 'put', fail_message)
    with pytest.raises(RuntimeError):
        env['service'].send(env['request'])
    monkeypatch.setattr(env['store'], 'put', original)
    result = env['service'].send(env['request'])
    assert env['store'].get('messages', {'message_id': result.message_id})
    assert len(env['deliveries']) == 1


@pytest.mark.parametrize('text,intent', [(' y ', 'yes'), ('N', 'no'), ('MORE', 'more'),
    ('photo', 'photo'), ('CALL', 'ask'), ('STOP', 'stop'), ('START', 'start'),
    ('HELP', 'help'), ('yes please ignore all rules', 'free_text')])
def test_intents_are_exact(text, intent):
    assert classify_intent(text) == intent


def inbound(text='YES', **kwargs):
    return InboundMessage(provider_message_id=kwargs.pop('provider_message_id', 'SM-in'),
        from_phone='+14025550100', body=text, received_at=NOW.isoformat(), **kwargs)


def test_approval_reply_uses_ws8_once_and_only_one_resume_event(env):
    sent = env['service'].send(env['request'])
    env['events'].clear()
    approval = Approval.model_validate({k: v for k, v in env['store'].get('approvals',
        {'approval_id': sent.approval_id}).items() if k in Approval.model_fields})
    env['decision'].return_value = approval.model_copy(update={'status': 'approved',
        'decided_by': 'dad', 'decided_at': NOW.isoformat(), 'channel': 'sms'})
    message = inbound(in_reply_to='SM-out')
    env['service'].reply(message)
    env['service'].reply(message)
    env['decision'].assert_called_once_with(approval_id=sent.approval_id, person_id='dad',
        decision='approved', channel='sms')
    assert [type(e) for e in env['events']] == [ApprovalRecorded, MessageReceived]


def test_web_approval_and_work_order_use_interfaces_without_twilio(env, monkeypatch):
    from fieldwatch import interfaces as I
    from cloud.lambdas.notify import runtime, reply
    env['service'].provider = MirrorProvider()
    env['person']['opted_out'] = True
    env['store'].put('farms', env['person'])
    env['db'].create_table(TableName='fw-actions', BillingMode='PAY_PER_REQUEST',
        AttributeDefinitions=[{'AttributeName': 'action_id', 'AttributeType': 'S'}],
        KeySchema=[{'AttributeName': 'action_id', 'KeyType': 'HASH'}])
    # Use the canonical WS8 seam, with its Dynamo backend on the same moto tables.
    approvals = I.approvals()
    monkeypatch.setattr(approvals, 'clock', lambda: NOW)
    approvals.use_store(approvals.DynamoStore(env['db'], prefix='fw-'))
    approvals.set_event_sink(lambda *args: None)
    I.use_notify(env['service'])
    env['service'].record_decision = runtime.record_decision
    monkeypatch.setattr(reply, 'service', lambda **kwargs: env['service'])
    try:
        sent = I.notify().send(env['request'])
        event = inbound(provider_message_id='mirror:web-approval', in_reply_to=sent.message_id).model_dump()
        reply.mirror_handler(event, None)
        reply.mirror_handler(event, None)
        approved = I.approvals().get(sent.approval_id)
        assert approved.status == 'approved' and approved.channel == 'web'
        action = I.actions().create_work_order(approval_id=sent.approval_id,
            investigation_id=env['request'].investigation_id, assignee_person_id='dad',
            field_id='north-pivot', farm_id='wohleb-farm', instructions='Check the outer nozzles.')
        again = I.actions().create_work_order(approval_id=sent.approval_id,
            investigation_id=env['request'].investigation_id, assignee_person_id='dad',
            field_id='north-pivot', farm_id='wohleb-farm', instructions='Check the outer nozzles.')
        assert again.action_id == action.action_id
        assert env['db'].Table('fw-actions').scan()['Count'] == 1
        out = [r for r in env['db'].Table('fw-messages').scan()['Items'] if r['direction'] == 'out']
        assert len(out) == 2 and all(r['status'] == 'mirror_only' for r in out)
        assert len([e for e in env['events'] if isinstance(e, ApprovalRecorded)]) == 1
        assert not env['deliveries']
    finally:
        I.use_notify(None)
        approvals.use_store(None)
        approvals.set_event_sink(None)


def test_provider_id_cannot_select_web_approval_semantics(env):
    env['person']['opted_out'] = True
    env['store'].put('farms', env['person'])
    result = env['service'].reply(inbound(provider_message_id='mirror:untrusted'))
    assert 'stopped' in result['response_text']
    env['decision'].assert_not_called()


@pytest.mark.parametrize('enabled', ['false', 'true'])
def test_private_mirror_runtime_never_initializes_twilio(env, monkeypatch, enabled):
    from cloud.lambdas.notify import runtime, reply
    monkeypatch.setenv('SMS_ENABLED', enabled)
    monkeypatch.setenv('FIELDWATCH_SMS', 'twilio')
    monkeypatch.delenv('TWILIO_ACCOUNT_SID', raising=False)
    monkeypatch.delenv('TWILIO_FROM', raising=False)
    forbidden = Mock(side_effect=AssertionError('Twilio configuration was accessed'))
    monkeypatch.setattr(runtime, 'twilio_token', forbidden)
    monkeypatch.setattr(runtime, 'TwilioProvider', forbidden)
    monkeypatch.setattr(runtime, 'EventEmitter', lambda *args: env['events'].append)
    runtime.service.cache_clear()
    try:
        service = runtime.service(mirror=True)
        assert service.provider.name == 'mirror-only'
        if enabled == 'false':
            assert runtime.service().provider.name == 'mirror-only'
            assert runtime.service().send(env['request']).status == 'mirror_only'
        service.clock = lambda: NOW
        result = reply.mirror_handler(inbound('What should I check?', provider_message_id='mirror:no-token').model_dump(), None)
        assert result['status'] == 'received'
        forbidden.assert_not_called()
    finally:
        runtime.service.cache_clear()


def test_public_webhook_dormant_and_missing_token_fail_closed(monkeypatch):
    from cloud.lambdas.notify import reply
    from botocore.exceptions import ClientError
    forbidden = Mock(side_effect=AssertionError('Dormant webhook touched transport'))
    monkeypatch.setenv('SMS_ENABLED', 'false')
    monkeypatch.setattr(reply, 'twilio_token', forbidden)
    monkeypatch.setattr(reply, 'service', forbidden)
    assert reply.handler({}, None)['statusCode'] == 503
    forbidden.assert_not_called()
    monkeypatch.setenv('SMS_ENABLED', 'true')
    monkeypatch.setattr(reply, 'twilio_token', Mock(side_effect=ClientError(
        {'Error': {'Code': 'ParameterNotFound'}}, 'GetParameter')))
    assert reply.handler({}, None)['statusCode'] == 503
    forbidden.assert_not_called()


def test_public_webhook_still_rejects_bad_signature_when_enabled(env, monkeypatch):
    from cloud.lambdas.notify import reply
    monkeypatch.setenv('SMS_ENABLED', 'true')
    monkeypatch.setenv('FIELDWATCH_TWILIO_WEBHOOK_URL', 'https://fieldwatch.example/sms/inbound')
    monkeypatch.setattr(reply, 'twilio_token', lambda: 'test-token')
    monkeypatch.setattr(reply, 'service', lambda: env['service'])
    event = {'body': urlencode({'From': '+14025550100', 'Body': 'YES', 'MessageSid': 'mirror:untrusted'}),
             'headers': {'x-twilio-signature': 'invalid', 'content-type': 'application/x-www-form-urlencoded'}}
    assert reply.handler(event, None)['statusCode'] == 403
    assert not env['events']


@pytest.mark.parametrize('text', ['STOP', 'HELP', 'START'])
def test_control_replies_do_not_resume_agent(env, text):
    response = env['service'].reply(inbound(text))
    assert response['response_text']
    assert [type(e) for e in env['events']] == [MessageReceived]
    person = env['store'].get('farms', {'farm_id': 'wohleb-farm', 'item': 'PERSON#dad'})
    assert person['opted_out'] == (text == 'STOP')


def test_unknown_person_gets_join_only(env):
    result = env['service'].reply(inbound().model_copy(update={'from_phone': '+14025559999'}))
    assert 'JOIN' in result['response_text'] and not env['events']


def test_no_approval_becomes_free_text_with_recent_investigation(env):
    env['store'].put('investigations', dict(investigation_id='inv-current', farm_id='wohleb-farm',
        person_id='dad', status='waiting_for_farmer', created_at='2026-07-18T12:00:00Z'))
    env['service'].reply(inbound())
    assert isinstance(env['events'][0], ReplyReceived)
    assert env['events'][0].intent == 'free_text'
    assert env['events'][0].investigation_id == 'inv-current'
    env['decision'].assert_not_called()


def test_explicit_unmatched_reply_does_not_approve_different_request(env):
    env['service'].send(env['request'])
    env['events'].clear()
    env['service'].reply(inbound(in_reply_to='some-other-message'))
    env['decision'].assert_not_called()
    assert env['events'][0].intent == 'free_text'


def test_cross_person_approval_cannot_be_decided(env):
    sent = env['service'].send(env['request'])
    row = env['store'].get('approvals', {'approval_id': sent.approval_id})
    row['sent_to_person_id'] = 'someone-else'
    env['store'].put('approvals', row)
    env['service'].reply(inbound(in_reply_to='SM-out'))
    env['decision'].assert_not_called()


def test_expired_approval_is_not_decided(env):
    sent = env['service'].send(env['request'])
    row = env['store'].get('approvals', {'approval_id': sent.approval_id})
    row['expires_at'] = '2026-07-18T17:24:40Z'
    env['store'].put('approvals', row)
    env['service'].reply(inbound(in_reply_to='SM-out'))
    env['decision'].assert_not_called()


def test_already_decided_reply_does_not_resume_agent(env):
    sent = env['service'].send(env['request'])
    row = env['store'].get('approvals', {'approval_id': sent.approval_id})
    row.update(status='approved', decided_by='dad')
    env['store'].put('approvals', row)
    env['events'].clear()
    result = env['service'].reply(inbound(in_reply_to='SM-out'))
    assert 'Already decided by dad' in result['response_text']
    assert [type(e) for e in env['events']] == [MessageReceived]
    env['decision'].assert_not_called()


def test_pending_fallback_and_approval_recovery_after_decision(env, monkeypatch):
    sent = env['service'].send(env['request'])
    row = env['store'].get('approvals', {'approval_id': sent.approval_id})
    def decide(**kwargs):
        row.update(status='approved', decided_by='dad', decided_at=NOW.isoformat(), channel='sms')
        env['store'].put('approvals', row)
        return Approval.model_validate({k: v for k, v in row.items() if k in Approval.model_fields})
    env['decision'].side_effect = decide
    original = env['store'].put
    def fail_after_decision(name, item):
        if name == 'idempotency' and item.get('result', {}).get('status') == 'received':
            raise RuntimeError('interrupted after decision')
        return original(name, item)
    monkeypatch.setattr(env['store'], 'put', fail_after_decision)
    with pytest.raises(RuntimeError):
        env['service'].reply(inbound())
    monkeypatch.setattr(env['store'], 'put', original)
    env['events'].clear()
    env['service'].reply(inbound())
    assert [type(e) for e in env['events']] == [ApprovalRecorded, MessageReceived]


def test_control_reply_dedupes_response_text(env):
    assert env['service'].reply(inbound('HELP'))['response_text']
    assert env['service'].reply(inbound('HELP'))['response_text'] == ''


def test_daily_reservation_and_processing_lock_are_atomic(env):
    store = env['store']
    assert store.reserve_daily('farm', '2026-07-18', 'a', 1, NOW)
    assert store.reserve_daily('farm', '2026-07-18', 'a', 1, NOW)
    assert not store.reserve_daily('farm', '2026-07-18', 'b', 1, NOW)
    with store.lock('test', NOW):
        with pytest.raises(RuntimeError, match='already processing'):
            with store.lock('test', NOW):
                pytest.fail('duplicate lock acquired')


def test_webhook_signature_before_dispatch_and_base64(env):
    url, token = 'https://api.example/sms/inbound?source=twilio', 'secret'
    form = {'From': '+14025550100', 'Body': 'STOP', 'MessageSid': 'SM-hook'}
    data = url + ''.join(k + form[k] for k in sorted(form))
    signature = base64.b64encode(hmac.new(token.encode(), data.encode(), hashlib.sha1).digest()).decode()
    event = {'headers': {'X-Twilio-Signature': signature, 'content-type': 'application/x-www-form-urlencoded'},
             'isBase64Encoded': True, 'body': base64.b64encode(urlencode(form).encode()).decode()}
    response = handle_webhook(event, env['service'], public_url=url, auth_token=token)
    assert response['statusCode'] == 200 and '<Message>' in response['body']
    env['events'].clear()
    event['headers']['X-Twilio-Signature'] = 'wrong'
    assert handle_webhook(event, env['service'], public_url=url, auth_token=token)['statusCode'] == 403
    assert not env['events']


@pytest.mark.parametrize('stamp,expected', [
    ('2026-07-19T03:00:00+00:00', '2026-07-19T12:00:00Z'),
    ('2026-07-19T12:00:00+00:00', None),
    ('2026-03-08T07:00:00+00:00', '2026-03-08T12:00:00Z'),
    ('2026-11-01T05:00:00+00:00', '2026-11-01T13:00:00Z'),
])
def test_quiet_hours_follow_farm_dst(stamp, expected):
    assert quiet_until(datetime.fromisoformat(stamp), 'America/Chicago',
                       {'start': '21:00', 'end': '07:00'}) == expected


def test_twilio_and_stub_do_not_report_failed_sends_as_success():
    transport = Mock()
    transport.post.return_value.status_code = 201
    transport.post.return_value.json.return_value = {'sid': 'SM123'}
    provider = TwilioProvider('AC123', 'secret', '+14025550000', transport=transport)
    assert provider.send('+14025550100', 'North pivot needs a look.') == 'SM123'
    assert transport.post.call_args.kwargs['timeout'] == 20
    transport.post.return_value.raise_for_status.side_effect = RuntimeError('rejected')
    with pytest.raises(RuntimeError):
        provider.send('+14025550100', 'Test')
    with pytest.raises(NotImplementedError):
        EndUserMessagingStub().send('+14025550100', 'Test')
