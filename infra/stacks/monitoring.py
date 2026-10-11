"""FieldWatch-Monitoring: alarms and one dashboard across the subsystem stacks.

Alarms (all notify the SNS topic `fieldwatch-alarms`, created with NO subscription):
  - every FieldWatch dead-letter queue (scenes, session starter, notifications, projector, hail): ApproximateNumberOfMessagesVisible > 0 (something failed for good)
  - Lambda Errors > 0 in 5 minutes for the main functions (scene filter/decide/replay/reset/check-latest,
    session starter, projector, hail filter, drought poller, notify send/reply/mirror, Gateway tools, every
    API route group)
  - Step Functions ExecutionsFailed > 0 (scene analysis, context builder)
  - API Gateway 5xx > 5 in 5 minutes
Dashboard `FieldWatch`: Lambda errors and duration, Step Functions failures, API requests and 5xx.

To get alarm emails, Caleb subscribes once after the first deploy (not in code, so no address is committed):
    aws sns subscribe --topic-arn <AlarmTopicArn output> --protocol email --notification-endpoint <address>
and confirms the email.
"""
from __future__ import annotations

from aws_cdk import CfnOutput, Duration, Stack, Tags
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_sns as sns
from constructs import Construct

PERIOD = Duration.minutes(5)


class Monitoring(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, scenes=None, context=None, agent=None,
                 notifications=None, feed=None, api=None, tools=None, hazards=None, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        Tags.of(self).add("project", "fieldwatch")

        self.topic = sns.Topic(self, "AlarmTopic", topic_name="fieldwatch-alarms", display_name="FieldWatch alarms")
        action = cw_actions.SnsAction(self.topic)
        self.alarms: list[cw.Alarm] = []

        def alarm(cid: str, metric: cw.IMetric, *, threshold: float = 0, description: str) -> cw.Alarm:
            a = cw.Alarm(self, cid, metric=metric, threshold=threshold, evaluation_periods=1,
                         comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
                         treat_missing_data=cw.TreatMissingData.NOT_BREACHING, alarm_description=description)
            a.add_alarm_action(action)
            self.alarms.append(a)
            return a

        # --- dead-letter queues ---
        dlqs = {
            "SceneDlq": getattr(scenes, "dlq", None),
            "StarterDlq": getattr(agent, "dlq", None),
            "NotifyDlq": getattr(notifications, "dlq", None),
            "ProjectorDlq": getattr(feed, "dlq", None),
            "HailDlq": getattr(hazards, "dlq", None),
        }
        for name, q in dlqs.items():
            if q is not None:
                alarm(f"{name}NotEmpty", q.metric_approximate_number_of_messages_visible(period=PERIOD),
                      description=f"{name}: messages that failed for good are waiting; inspect and redrive")

        # --- Lambda errors ---
        functions: dict[str, object] = {}
        if scenes is not None:
            functions.update({"SceneFilter": scenes.scene_filter_fn, "SceneLoadInputs": scenes.load_fn,
                              "SceneAnalyze": scenes.analyze_fn, "SceneDecide": scenes.decide_fn,
                              "SceneReplay": scenes.replay_fn, "SceneReset": scenes.reset_fn,
                              "SceneCheckLatest": scenes.check_fn})
        if agent is not None:
            functions["SessionStarter"] = agent.starter
        if feed is not None:
            functions["Projector"] = feed.projector
        if notifications is not None:
            functions.update({"NotifySend": notifications.send, "NotifyReply": notifications.reply,
                              "NotifyMirror": notifications.mirror})
        if hazards is not None:
            functions.update({"HailFilter": hazards.hail_filter, "DroughtPoller": hazards.drought_poller})
        if tools is not None:
            functions["GatewayTools"] = tools.function
        if api is not None:
            for group, fn in api.functions.items():
                functions["Api" + "".join(p.capitalize() for p in group.split("_"))] = fn
        for name, fn in functions.items():
            alarm(f"{name}Errors", fn.metric_errors(period=PERIOD, statistic="Sum"),
                  description=f"{name} Lambda reported errors")

        # --- Step Functions ---
        machines = {"SceneAnalysis": getattr(scenes, "state_machine", None),
                    "ContextBuilder": getattr(context, "state_machine", None)}
        machines = {k: v for k, v in machines.items() if v is not None}
        for name, sm in machines.items():
            alarm(f"{name}Failed", sm.metric_failed(period=PERIOD, statistic="Sum"),
                  description=f"{name} executions failed")

        # --- API ---
        api_5xx = api.http_api.metric_server_error(period=PERIOD, statistic="Sum") if api is not None else None
        if api_5xx is not None:
            alarm("Api5xx", api_5xx, threshold=5, description="Web API answered 5xx more than 5 times in 5 minutes")

        # --- dashboard ---
        dash = cw.Dashboard(self, "Dashboard", dashboard_name="FieldWatch")
        fn_items = list(functions.items())
        dash.add_widgets(
            cw.GraphWidget(title="Lambda errors", width=12, stacked=False,
                           left=[fn.metric_errors(period=PERIOD, statistic="Sum", label=n) for n, fn in fn_items]),
            cw.GraphWidget(title="Lambda duration (p90)", width=12,
                           left=[fn.metric_duration(period=PERIOD, statistic="p90", label=n) for n, fn in fn_items]),
        )
        row = []
        if machines:
            row.append(cw.GraphWidget(title="Step Functions failures", width=12,
                                      left=[sm.metric_failed(period=PERIOD, statistic="Sum", label=n)
                                            for n, sm in machines.items()]))
        if api is not None:
            row.append(cw.GraphWidget(title="Web API requests and 5xx", width=12,
                                      left=[api.http_api.metric_count(period=PERIOD, statistic="Sum", label="requests")],
                                      right=[api_5xx]))
        if row:
            dash.add_widgets(*row)
        if any(q is not None for q in dlqs.values()):
            dash.add_widgets(cw.SingleValueWidget(
                title="Dead-letter queues (messages waiting)", width=24,
                metrics=[q.metric_approximate_number_of_messages_visible(period=PERIOD, label=n)
                         for n, q in dlqs.items() if q is not None]))

        CfnOutput(self, "AlarmTopicArn", value=self.topic.topic_arn,
                  description="Subscribe an email to get FieldWatch alarms (aws sns subscribe ... --protocol email)")
