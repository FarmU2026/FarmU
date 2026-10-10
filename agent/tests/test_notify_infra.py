"""Offline synth checks: inspect templates without bundling/downloading Lambda wheels."""
import json
import sys
from pathlib import Path

def test_notification_stack_offline(monkeypatch):
    import aws_cdk as cdk
    from aws_cdk.assertions import Template, Match
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'infra'))
    from stacks.data_plane import DataPlane
    from stacks.notifications import Notifications, synthesis_app

    monkeypatch.setenv('CDK_CONTEXT_JSON', json.dumps({'aws:cdk:bundling-stacks': ['**']}))
    app = synthesis_app(offline=True)
    env = cdk.Environment(account='123456789012', region='us-west-2')
    data = DataPlane(app, 'FieldWatch-TestData', env=env)
    stack = Notifications(app, 'FieldWatch-Notifications', data=data, env=env)
    assert not stack.bundling_required
    template = Template.from_stack(stack)
    template.resource_count_is('AWS::Lambda::Function', 3)
    template.resource_count_is('AWS::Scheduler::ScheduleGroup', 1)
    for resource in template.find_resources('AWS::Lambda::Function').values():
        props = resource['Properties']
        assert props['Runtime'] == 'python3.12' and props['Architectures'] == ['x86_64']
        assert props['Environment']['Variables']['SMS_ENABLED'] == {'Ref': 'SmsEnabled'}
        assert props['TracingConfig']['Mode'] == 'Active'
    for resource in template.find_resources('AWS::IAM::Role').values():
        assert 'fieldwatch-boundary' in str(resource['Properties']['PermissionsBoundary'])
    template.has_resource_properties('AWS::IAM::Policy', {
        'PolicyDocument': {'Statement': Match.array_with([Match.object_like({
            'Action': 'iam:PassRole', 'Condition': {'StringEquals': {'iam:PassedToService': 'scheduler.amazonaws.com'}}})])}})
    assert not template.find_resources('AWS::IAM::OIDCProvider')
    assert template.to_json()['Parameters']['SmsEnabled']['Default'] == 'false'
    app.synth()
