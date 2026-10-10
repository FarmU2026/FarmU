from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone, time
from zoneinfo import ZoneInfo

from fieldwatch.contracts import (
    Approval, ApprovalRecorded, InboundMessage, MessageReceived, MessageSent,
    ReplyReceived, SendMessageRequest, SendMessageResult,
)
from .links import signed_link

UTC = timezone.utc
EVENT_TYPES = {c.__name__: c for c in (ApprovalRecorded, ReplyReceived, MessageReceived, MessageSent)}


def iso(value):
    return value.astimezone(UTC).isoformat(timespec='seconds').replace('+00:00', 'Z')


def parse(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Timestamp must include a time zone')
    return result


def identifier(prefix, value):
    return prefix + hashlib.sha256(value.encode()).hexdigest()[:32]


def classify_intent(text):
    return {'YES': 'yes', 'Y': 'yes', 'NO': 'no', 'N': 'no', 'MORE': 'more',
            'PHOTO': 'photo', 'CALL': 'ask', 'ASK': 'ask', 'STOP': 'stop',
            'START': 'start', 'HELP': 'help'}.get(text.strip().upper(), 'free_text')


def quiet_until(now, zone, hours):
    if not hours:
        return None
    start, end = time.fromisoformat(hours['start']), time.fromisoformat(hours['end'])
    local = now.astimezone(ZoneInfo(zone))
    clock = local.time().replace(tzinfo=None)
    quiet = (clock >= start or clock < end) if start > end else start <= clock < end
    if not quiet or start == end:
        return None
    day = local.date() + (timedelta(days=1) if start > end and clock >= start else timedelta())
    # fold=1 ends a fall-back window after the second occurrence; UTC round-trip
    # moves a nonexistent spring-forward end time forward across the gap.
    until = datetime.combine(day, end, tzinfo=local.tzinfo).replace(fold=1)
    if until.astimezone(UTC).astimezone(local.tzinfo).replace(tzinfo=None) != until.replace(tzinfo=None):
        until = until.replace(fold=0)
    return iso(until)


class Notifications:
    def __init__(self, *, store, provider, emit, defer, signing_key, base_url,
                 record_decision, clock=None, daily_cap=2):
        self.store, self.provider, self.emit, self.defer = store, provider, emit, defer
        self.signing_key, self.base_url = signing_key, base_url
        self.record_decision = record_decision
        self.clock = clock or (lambda: datetime.now(UTC))
        self.daily_cap = daily_cap

    def person_for_role(self, farm_id, role):
        people = [p for p in self.store.query('farms', 'farm_id', farm_id)
                  if p['item'].startswith('PERSON#') and p.get('role') == role and not p.get('opted_out')]
        if len(people) != 1:
            raise ValueError('Select an explicit person when a role has zero or multiple recipients')
        return people[0]

    def _save(self, state):
        self.store.put('idempotency', state)

    def _flush(self, state):
        # Events are at-least-once: projector/session starter dedupe their contract IDs.
        if state.get('message'):
            self.store.put('messages', state['message'])
        while state.get('events'):
            event = state['events'][0]
            self.emit(EVENT_TYPES[event['type']].model_validate(event['detail']))
            state['events'].pop(0)
            self._save(state)

    @staticmethod
    def _events(events):
        return [{'type': type(e).__name__, 'detail': e.model_dump()} for e in events]

    def send(self, request, *, scheduled=False):
        request = SendMessageRequest.model_validate(request)
        now = self.clock()
        person = self.store.get('farms', {'farm_id': request.farm_id, 'item': 'PERSON#' + request.person_id})
        farm = self.store.get('farms', {'farm_id': request.farm_id, 'item': 'FARM'})
        if not person or not farm:
            raise ValueError('Unknown farm or recipient')
        key = 'notify-send#' + identifier('', request.farm_id + '\0' + request.idempotency_key)
        message_id = identifier('msg-', key)
        if person.get('opted_out') or (self.provider.name != 'mirror-only' and not person.get('opt_in_at')):
            return SendMessageResult(message_id=message_id, status='opted_out', provider=self.provider.name)
        fingerprint = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
        with self.store.lock(key, now):
            state = self.store.get('idempotency', {'key': key}) or {'key': key, 'fingerprint': fingerprint}
            if state['fingerprint'] != fingerprint:
                raise ValueError('Idempotency key already belongs to a different request')
            if state.get('result'):
                result = SendMessageResult.model_validate(state['result'])
                if result.status != 'deferred_quiet_hours' or not scheduled or parse(result.scheduled_for) > now:
                    self._flush(state)
                    return result
            if state.get('delivery_started'):
                raise RuntimeError('Delivery outcome is uncertain; reconcile provider status before retrying')
            body = request.body
            if request.link:
                body += ' ' + signed_link(self.base_url, self.signing_key(), request.farm_id, request.link, now)
            if request.approval:
                if not request.investigation_id or not 0 < float(request.approval.get('ttl_hours', 24)) <= 168:
                    raise ValueError('Approval requires an investigation and a lifetime of up to 168 hours')
                # Validate before any provider/Scheduler effects.
                Approval(approval_id='pending', investigation_id=request.investigation_id,
                    action_type=request.approval['action_type'], summary=request.body,
                    sent_to_person_id=request.person_id, status='pending', expires_at=iso(now))
            until = quiet_until(now, farm.get('timezone', 'America/Chicago'), farm.get('quiet_hours'))
            if request.urgency != 'urgent' and until:
                self.defer(message_id, until, request)
                result = SendMessageResult(message_id=message_id, status='deferred_quiet_hours',
                                           provider=self.provider.name, scheduled_for=until)
                state['result'] = result.model_dump()
                self._save(state)
                return result
            day = now.astimezone(ZoneInfo(farm.get('timezone', 'America/Chicago'))).date().isoformat()
            if request.urgency != 'urgent' and not self.store.reserve_daily(
                    request.farm_id, day, message_id, self.daily_cap, now):
                result = SendMessageResult(message_id=message_id, status='rate_limited', provider=self.provider.name)
                state['result'] = result.model_dump()
                self._save(state)
                return result
            approval_id = None
            if request.approval:
                approval_id = identifier('apr-', message_id)
                approval = Approval(approval_id=approval_id, investigation_id=request.investigation_id,
                    action_type=request.approval['action_type'], summary=request.body,
                    sent_to_person_id=request.person_id, status='pending',
                    expires_at=iso(now + timedelta(hours=float(request.approval.get('ttl_hours', 24)))))
                self.store.create('approvals', 'approval_id', {**approval.model_dump(),
                    'farm_id': request.farm_id, 'created_at': iso(now),
                    'expires_at_epoch': int(parse(approval.expires_at).timestamp())})
            state.pop('result', None)
            state['delivery_started'] = True
            self._save(state)
            provider_id = self.provider.send(person['phone_e164'], body)
            status = 'mirror_only' if self.provider.name == 'mirror-only' else 'sent'
            result = SendMessageResult(message_id=message_id, approval_id=approval_id,
                                       status=status, provider=self.provider.name)
            row = {**result.model_dump(), 'farm_id': request.farm_id, 'person_id': request.person_id,
                   'direction': 'out', 'body': body, 'created_at': iso(now),
                   'investigation_id': request.investigation_id, 'idempotency_key': request.idempotency_key}
            if provider_id:
                row['provider_message_id'] = provider_id
            # Save delivery receipt before writing the mirror row, allowing safe retries of both.
            state['result'], state['message'] = result.model_dump(), row
            state['events'] = self._events([MessageSent(occurred_at=iso(now), message_id=message_id,
                farm_id=request.farm_id, person_id=request.person_id, body=body,
                investigation_id=request.investigation_id, status=status, provider=self.provider.name)])
            self._save(state)
            self._flush(state)
            return result

    def _person_by_phone(self, phone):
        people = self.store.query('farms', 'phone_e164', phone, 'by-phone')
        # A shared phone is ambiguous: never guess which farm/person can approve.
        if len(people) != 1:
            return None
        return self.store.get('farms', {'farm_id': people[0]['farm_id'], 'item': people[0]['item']})

    def _approval(self, message, person, now):
        if message.in_reply_to:
            rows = self.store.query('messages', 'provider_message_id', message.in_reply_to, 'by-provider-id')
            direct = self.store.get('messages', {'message_id': message.in_reply_to})
            if direct:
                rows.append(direct)
            rows = [r for r in rows if r.get('farm_id') == person['farm_id'] and
                    r.get('person_id') == person['person_id'] and r.get('direction') == 'out']
            candidates = [self.store.get('approvals', {'approval_id': r['approval_id']})
                          for r in rows if r.get('approval_id')]
        else:
            candidates = self.store.query('approvals', 'sent_to_person_id', person['person_id'], 'by-person-status')
            candidates = [self.store.get('approvals', {'approval_id': r['approval_id']}) for r in candidates]
            candidates = [r for r in candidates if r and r.get('status') == 'pending']
        def belongs(a):
            if a.get('farm_id'):
                return a['farm_id'] == person['farm_id']
            investigation = self.store.get('investigations', {'investigation_id': a['investigation_id']}) or {}
            return investigation.get('farm_id') == person['farm_id']
        candidates = [a for a in candidates if a and belongs(a) and
                      a['sent_to_person_id'] == person['person_id'] and parse(a['expires_at']) > now]
        return max(candidates, key=lambda a: (a.get('created_at', a['expires_at']), a['approval_id']), default=None)

    def _recent_investigation(self, person, now):
        # No by-person index in the shared data-plane contract; paginated scan until WS7 adds one.
        candidates = [r for r in self.store.scan('investigations') if
            r.get('farm_id') == person['farm_id'] and r.get('person_id', person['person_id']) == person['person_id']
            and r.get('status') in {'open', 'running', 'waiting', 'waiting_for_farmer'}
            and r.get('created_at') and now - timedelta(days=7) <= parse(r['created_at']) <= now]
        return max(candidates, key=lambda r: r['created_at'], default={}).get('investigation_id')

    def reply(self, message):
        message = InboundMessage.model_validate(message)
        now = self.clock()
        key = 'notify-in#' + identifier('', message.provider_message_id)
        with self.store.lock(key, now):
            state = self.store.get('idempotency', {'key': key}) or {'key': key}
            if state.get('result'):
                self._flush(state)
                return {**state['result'], 'response_text': ''}
            person = self._person_by_phone(message.from_phone)
            if not person:
                state['result'] = {'status': 'unknown_person', 'response_text': 'Text JOIN to sign up for FieldWatch.'}
                self._save(state)
                return state['result']
            intent = classify_intent(message.body)
            response_text, events, investigation_id = '', [], None
            if intent in {'stop', 'start', 'help'}:
                if intent != 'help':
                    self.store.set_optout(person, intent == 'stop', iso(now))
                response_text = {'stop': 'FieldWatch texts stopped. Reply START to resume.',
                    'start': 'FieldWatch texts resumed. Reply STOP to stop.',
                    'help': 'FieldWatch watches your fields. Reply MORE for details or STOP to stop texts.'}[intent]
            elif person.get('opted_out'):
                response_text = 'FieldWatch texts are stopped. Reply START to resume.'
            else:
                approval = state.get('decision_request') or (
                    self._approval(message, person, now) if intent in {'yes', 'no'} else None)
                if approval:
                    investigation_id = approval['investigation_id']
                    if approval['status'] != 'pending':
                        response_text = 'Already decided by ' + str(approval.get('decided_by') or 'another person') + '.'
                    else:
                        decision = 'approved' if intent == 'yes' else 'declined'
                        state['decision_request'] = approval
                        self._save(state)
                        decided = self.record_decision(approval_id=approval['approval_id'],
                            person_id=person['person_id'], decision=decision, channel='sms')
                        decided = Approval.model_validate(decided)
                        if (decided.status == decision and decided.decided_by == person['person_id'] and
                                decided.approval_id == approval['approval_id'] and
                                decided.investigation_id == investigation_id):
                            events.append(ApprovalRecorded(occurred_at=decided.decided_at or iso(now),
                                approval_id=approval['approval_id'], investigation_id=investigation_id,
                                decision=decision, decided_by=person['person_id'], channel='sms'))
                        else:
                            response_text = 'Already decided by ' + str(decided.decided_by or 'another person') + '.'
                else:
                    investigation_id = self._recent_investigation(person, now)
                    events.append(ReplyReceived(occurred_at=iso(now), reply_id=identifier('reply-', key),
                        farm_id=person['farm_id'], person_id=person['person_id'],
                        intent='free_text' if intent in {'yes', 'no'} else intent, text=message.body,
                        investigation_id=investigation_id))
            message_id = identifier('msg-in-', key)
            events.append(MessageReceived(occurred_at=iso(now), message_id=message_id,
                farm_id=person['farm_id'], person_id=person['person_id'], body=message.body,
                investigation_id=investigation_id))
            state['message'] = dict(message_id=message_id, farm_id=person['farm_id'], person_id=person['person_id'],
                direction='in', body=message.body, provider_message_id=message.provider_message_id,
                received_at=message.received_at, investigation_id=investigation_id)
            state['events'] = self._events(events)
            state['result'] = {'status': 'received', 'message_id': message_id, 'response_text': response_text}
            self._save(state)
            self._flush(state)
            return state['result']
