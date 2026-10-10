"""FieldWatch hazards: west-region queue subscribed directly to NOAA's east-region topic.

Only the SNS Subscription API call uses us-east-1. Every owned resource is in
us-west-2; no forwarder or topic is created in NOAA's region (decision D3).
"""
from aws_cdk import Stack, Duration, Tags
from aws_cdk import aws_sns as sns, aws_sqs as sqs, aws_iam as iam
from aws_cdk import aws_lambda_event_sources as sources
from aws_cdk import aws_events as events, aws_events_targets as targets
from constructs import Construct
from .common import geo_lambda, python_lambda
from .data_plane import DataPlane

MRMS_TOPIC = 'arn:aws:sns:us-east-1:123901341784:NewMRMSObject'


class Hazards(Stack):
    def __init__(self, scope:Construct, construct_id:str, *, data:DataPlane, **kwargs):
        super().__init__(scope,construct_id,**kwargs)
        Tags.of(self).add('project','fieldwatch')
        env={'FIELDWATCH_TABLE_PREFIX':'fw-', 'FIELDWATCH_BUCKET':data.bucket.bucket_name,
             'FIELDWATCH_EVENT_BUS':data.bus.event_bus_name}
        self.dlq=sqs.Queue(self,'HailDlq',queue_name='fw-hail-dlq',retention_period=Duration.days(14),enforce_ssl=True)
        self.queue=sqs.Queue(self,'HailQueue',queue_name='fw-hail-queue',visibility_timeout=Duration.seconds(720),
            retention_period=Duration.days(4),enforce_ssl=True,
            dead_letter_queue=sqs.DeadLetterQueue(queue=self.dlq,max_receive_count=5))
        self.queue.add_to_resource_policy(iam.PolicyStatement(principals=[iam.ServicePrincipal('sns.amazonaws.com')],
            actions=['sqs:SendMessage'],resources=[self.queue.queue_arn],conditions={'ArnEquals':{'aws:SourceArn':MRMS_TOPIC}}))
        sns.CfnSubscription(self,'MrmsSubscription',protocol='sqs',endpoint=self.queue.queue_arn,
            topic_arn=MRMS_TOPIC,region='us-east-1',raw_message_delivery=True,
            filter_policy_scope='MessageBody',filter_policy={'Records':{'s3':{'object':{'key':[
                {'prefix':'CONUS/MESH_Max_60min_00.50/'},{'prefix':'CONUS/MESH_Max_1440min_00.50/'}]}}}})
        self.hail_filter=geo_lambda(self,'HailFilter',handler='cloud.lambdas.hazards.hail_filter.handler',
            function_name='fw-hail-filter',environment=env,timeout_s=120,memory_mb=3008)
        self.hail_filter.add_event_source(sources.SqsEventSource(self.queue,batch_size=1,report_batch_item_failures=True))
        events.Rule(self,'ReplayHail',event_bus=data.bus,event_pattern=events.EventPattern(detail_type=['MrmsFileAvailable']),
            targets=[targets.LambdaFunction(self.hail_filter)])
        self.drought_poller=python_lambda(self,'DroughtPoller',handler='cloud.lambdas.hazards.drought_poller.handler',
            function_name='fw-drought-poller',environment=env,timeout_s=120)
        events.Rule(self,'WeeklyDrought',schedule=events.Schedule.cron(minute='0',hour='14',week_day='THU'),
            targets=[targets.LambdaFunction(self.drought_poller)])
        for fn in (self.hail_filter,self.drought_poller):
            data.tables['fw-fields'].grant_read_data(fn)
            data.tables['fw-watches'].grant_read_write_data(fn)
            data.bus.grant_put_events_to(fn)
        data.bucket.grant_read(self.hail_filter,'fields/*')
        data.bucket.grant_write(self.hail_filter,'hazards/*')
        data.tables['fw-farms'].grant_read_data(self.drought_poller)
        data.tables['fw-field-context'].grant_read_write_data(self.drought_poller)
