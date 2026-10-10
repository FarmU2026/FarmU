"""WS9 send/reply Lambdas. Integration into infra/app.py is owned by Caleb.

Offline template checks: python -m stacks.notifications --offline-synth
This skips wheel bundling only; normal synthesis uses the shared Lambda builder.
"""
from aws_cdk import App, CfnOutput, CfnParameter, Duration, Environment, Stack, Tags
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from .common import python_lambda


class Notifications(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, data, **kwargs):
        super().__init__(scope, construct_id, **kwargs)
        Tags.of(self).add('project', 'fieldwatch')
        boundary = iam.ManagedPolicy.from_managed_policy_name(self, 'Boundary', 'fieldwatch-boundary')
        public_url = CfnParameter(self, 'PublicUrl', type='String', default='',
                                  description='HTTPS frontend URL for signed links')
        webhook_url = CfnParameter(self, 'WebhookUrl', type='String', default='',
                                   description='Exact public Twilio webhook URL, including query string')
        sid = CfnParameter(self, 'TwilioAccountSid', type='String', default='')
        sender = CfnParameter(self, 'TwilioFrom', type='String', default='')
        enabled = CfnParameter(self, 'SmsEnabled', type='String', default='false', allowed_values=['true', 'false'])
        self.dlq = sqs.Queue(self, 'Failures', queue_name='fw-notify-failures',
                             retention_period=Duration.days(14), enforce_ssl=True)
        group = scheduler.CfnScheduleGroup(self, 'ScheduleGroup', name='fieldwatch-notifications')
        scheduler_role = iam.Role(self, 'SchedulerRole', role_name='fieldwatch-notify-scheduler',
            permissions_boundary=boundary, assumed_by=iam.ServicePrincipal('scheduler.amazonaws.com', conditions={
                'StringEquals': {'aws:SourceAccount': self.account},
                'ArnEquals': {'aws:SourceArn': group.attr_arn}}))
        self.dlq.grant_send_messages(scheduler_role)
        # Construct ARN from the fixed name to avoid a self-reference in Lambda environment.
        send_arn = self.format_arn(service='lambda', resource='function', resource_name='fw-notify-send')
        environment = {
            'FIELDWATCH_TABLE_PREFIX': 'fw-', 'FIELDWATCH_EVENT_BUS': data.bus.event_bus_name,
            'FIELDWATCH_LINK_KEY_PARAM': '/fieldwatch/link-signing-key',
            'FIELDWATCH_TWILIO_TOKEN_PARAM': '/fieldwatch/twilio-auth-token',
            'FIELDWATCH_PUBLIC_URL': public_url.value_as_string,
            'FIELDWATCH_TWILIO_WEBHOOK_URL': webhook_url.value_as_string,
            'TWILIO_ACCOUNT_SID': sid.value_as_string, 'TWILIO_FROM': sender.value_as_string,
            'SMS_ENABLED': enabled.value_as_string, 'FIELDWATCH_SMS': 'twilio',
            'FIELDWATCH_SEND_ARN': send_arn, 'FIELDWATCH_SCHEDULER_ROLE_ARN': scheduler_role.role_arn,
            'FIELDWATCH_SCHEDULE_GROUP': group.ref, 'FIELDWATCH_NOTIFY_DLQ_ARN': self.dlq.queue_arn,
        }
        functions = {}
        for name, handler in [('send', 'send.handler'), ('reply', 'reply.handler'), ('mirror', 'reply.mirror_handler')]:
            role = iam.Role(self, name.title() + 'Role', role_name='fieldwatch-notify-' + name,
                assumed_by=iam.ServicePrincipal('lambda.amazonaws.com'), permissions_boundary=boundary,
                managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name('service-role/AWSLambdaBasicExecutionRole')])
            functions[name] = python_lambda(self, name.title(), function_name='fw-notify-' + name,
                handler='cloud.lambdas.notify.' + handler, role=role, environment=environment,
                requirements=['tzdata>=2024'], memory_mb=256, timeout_s=30,
                tracing=lambda_.Tracing.ACTIVE, dead_letter_queue=self.dlq, retry_attempts=2)
            for table in ('messages', 'idempotency', 'approvals'):
                data.tables['fw-' + table].grant_read_write_data(role)
            data.tables['fw-farms'].grant_read_data(role)
            data.bus.grant_put_events_to(role)
            role.add_to_policy(iam.PolicyStatement(actions=['ssm:GetParameter'], resources=[
                self.format_arn(service='ssm', resource='parameter', resource_name='fieldwatch/link-signing-key'),
                self.format_arn(service='ssm', resource='parameter', resource_name='fieldwatch/twilio-auth-token')]))
            if name != 'send':
                data.tables['fw-farms'].grant_write_data(role)
                data.tables['fw-investigations'].grant_read_data(role)
            else:
                role.add_to_policy(iam.PolicyStatement(actions=['scheduler:CreateSchedule'], resources=[
                    self.format_arn(service='scheduler', resource='schedule', resource_name='fieldwatch-notifications/fw-notify-*')]))
                role.add_to_policy(iam.PolicyStatement(actions=['iam:PassRole'], resources=[scheduler_role.role_arn],
                    conditions={'StringEquals': {'iam:PassedToService': 'scheduler.amazonaws.com'}}))
        self.send, self.reply, self.mirror = functions['send'], functions['reply'], functions['mirror']
        self.send.grant_invoke(scheduler_role)
        for name, fn in functions.items():
            CfnOutput(self, name.title() + 'Arn', value=fn.function_arn)


def synthesis_app(*, offline=False):
    app = App()
    if offline:
        # CDK CLI context overrides App(context=...). Set after construction so
        # the CLI's default ['**'] cannot enable pip bundling in offline checks.
        app.node.set_context('aws:cdk:bundling-stacks', [])
    return app


if __name__ == '__main__':
    import sys
    from .data_plane import DataPlane
    offline = '--offline-synth' in sys.argv
    app = synthesis_app(offline=offline)
    env = Environment(account='123456789012' if offline else None, region='us-west-2')
    data = DataPlane(app, 'FieldWatch-DataPlane', env=env)
    Notifications(app, 'FieldWatch-Notifications', data=data, env=env)
    app.synth()
