"""Outbound transports. No polling; delivery errors must not look successful."""


class MirrorProvider:
    name = 'mirror-only'

    def send(self, phone, body):
        return None


class TwilioProvider:
    name = 'twilio'

    def __init__(self, account_sid, auth_token, from_phone, *, transport=None):
        if not all((account_sid, auth_token, from_phone)):
            raise ValueError('Twilio credentials and sending number are required')
        if transport is None:
            import requests
            transport = requests
        self.account_sid, self.auth_token, self.from_phone = account_sid, auth_token, from_phone
        self.transport = transport

    def send(self, phone, body):
        response = self.transport.post(
            f'https://api.twilio.com/2010-04-01/Accounts/{self.account_sid}/Messages.json',
            auth=(self.account_sid, self.auth_token),
            data={'From': self.from_phone, 'To': phone, 'Body': body}, timeout=20)
        response.raise_for_status()
        return response.json()['sid']


class EndUserMessagingStub:
    name = 'end-user-messaging'

    def send(self, phone, body):
        raise NotImplementedError('End User Messaging requires a registered sending number')
