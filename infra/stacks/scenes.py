"""FieldWatch-Scenes (workstream 2; plan T14, T34): scene queue -> filter -> Step Functions Express -> events, plus replay.

    SQS fw-scene-queue (+ DLQ)
      <- Earth Search SNS (optional, context `scenes_sns_topic_arn`, off by default)
      <- daily "check latest" Lambda (EventBridge Scheduler)
      <- replay Lambda (invoked by the API lane)
      -> scene filter Lambda -> Express state machine fw-scene-analysis:
           Map over fields (max concurrency 10, per-field catch):
             LoadFieldInputs -> AnalyzeField (geo container image, registry analyze_field) -> Decide
           Decide writes fw-scene-analyses and emits SceneAnalyzed / AnomalyDetected on the `fieldwatch` bus.
    Reset Lambda clears what replays created for the demo farm.

Handlers live in agent/cloud/lambdas/scenes/. Outputs are exported as FieldWatch-Scenes-<Name> for the API lane.
Context: `-c scenes_map_concurrency=N` (default 10; lower it while the account's Lambda concurrency is capped at 10,
spike finding 19), `-c scenes_sns_topic_arn=arn:...` (live Sentinel-2 subscription; not us-east-1).
"""
from __future__ import annotations

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack, Tags
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda_event_sources as event_sources
from aws_cdk import aws_logs as logs
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks
from constructs import Construct

from .common import geo_lambda, python_lambda
from .data_plane import DataPlane

HANDLER = "cloud.lambdas.scenes."
LAMBDA_RETRYABLE = ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.SdkClientException"]
THROTTLED = ["Lambda.TooManyRequestsException"]


class Scenes(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, data: DataPlane, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        Tags.of(self).add("project", "fieldwatch")
        t = data.tables

        base_env = {
            "FIELDWATCH_TABLE_PREFIX": "fw-",
            "FIELDWATCH_BUCKET": data.bucket.bucket_name,
            "FIELDWATCH_EVENT_BUS": data.bus.event_bus_name,
        }

        # ---------- queue ----------
        self.dlq = sqs.Queue(self, "SceneDlq", queue_name="fw-scene-dlq", retention_period=Duration.days(14),
                             enforce_ssl=True)
        self.queue = sqs.Queue(
            self, "SceneQueue", queue_name="fw-scene-queue",
            visibility_timeout=Duration.seconds(180),  # 6x the filter's 30 s
            retention_period=Duration.days(4), enforce_ssl=True,
            dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=5, queue=self.dlq),
        )

        # ---------- per-field Lambdas ----------
        load_fn = python_lambda(self, "LoadInputs", handler=HANDLER + "load_inputs.handler", environment=base_env,
                                timeout_s=30, memory_mb=256, function_name="fw-scene-load-inputs")
        self.analyze_fn = geo_lambda(self, "AnalyzeField", handler=HANDLER + "analyze.handler", environment=base_env,
                                     timeout_s=90, memory_mb=3008, function_name="fw-scene-analyze-field")
        decide_fn = python_lambda(self, "Decide", handler=HANDLER + "decide.handler", environment=base_env,
                                  timeout_s=30, memory_mb=256, function_name="fw-scene-decide")

        t["fw-fields"].grant_read_data(load_fn)
        t["fw-watches"].grant_read_data(load_fn)
        data.bucket.grant_read(load_fn, "fields/*")
        data.bucket.grant_read_write(self.analyze_fn)
        for name in ("fw-fields", "fw-watches", "fw-field-context"):
            t[name].grant_read_data(self.analyze_fn)
        t["fw-scene-analyses"].grant_read_write_data(decide_fn)
        t["fw-idempotency"].grant_read_write_data(decide_fn)
        data.bus.grant_put_events_to(decide_fn)

        # ---------- state machine ----------
        self.load_fn, self.decide_fn = load_fn, decide_fn  # FieldWatch-Monitoring alarms on them
        self.state_machine = self._state_machine(load_fn, self.analyze_fn, decide_fn)

        # ---------- scene filter ----------
        scene_filter = python_lambda(
            self, "SceneFilter", handler=HANDLER + "scene_filter.handler", function_name="fw-scene-filter",
            environment={**base_env, "STATE_MACHINE_ARN": self.state_machine.state_machine_arn},
            timeout_s=30, memory_mb=512)
        t["fw-tile-index"].grant_read_data(scene_filter)
        t["fw-idempotency"].grant_read_write_data(scene_filter)
        self.state_machine.grant_start_execution(scene_filter)
        scene_filter.add_event_source(event_sources.SqsEventSource(
            self.queue, batch_size=10, report_batch_item_failures=True))
        self.scene_filter_fn = scene_filter

        # ---------- replay and reset ----------
        queue_env = {**base_env, "SCENE_QUEUE_URL": self.queue.queue_url}
        self.replay_fn = python_lambda(self, "Replay", handler=HANDLER + "replay.handler", function_name="fw-scene-replay",
                                       environment=queue_env, timeout_s=60, memory_mb=512)
        t["fw-fields"].grant_read_data(self.replay_fn)
        t["fw-tile-index"].grant_read_data(self.replay_fn)
        data.bucket.grant_read(self.replay_fn, "fields/*")
        self.queue.grant_send_messages(self.replay_fn)
        data.bus.grant_put_events_to(self.replay_fn)

        self.reset_fn = python_lambda(self, "Reset", handler=HANDLER + "reset.handler", function_name="fw-scene-reset",
                                      environment={**base_env, "DEMO_FARM_ID": "wohleb-farm"}, timeout_s=120,
                                      memory_mb=256)
        t["fw-fields"].grant_read_data(self.reset_fn)
        for name in ("fw-scene-analyses", "fw-idempotency", "fw-investigations", "fw-approvals", "fw-actions"):
            t[name].grant_read_write_data(self.reset_fn)

        # ---------- scheduled "check latest" (T34's cheap alternative to the SNS subscription) ----------
        self.check_fn = python_lambda(self, "CheckLatest", handler=HANDLER + "check_latest.handler",
                                      function_name="fw-scene-check-latest", environment=queue_env,
                                      timeout_s=120, memory_mb=256)
        t["fw-tile-index"].grant_read_data(self.check_fn)
        self.queue.grant_send_messages(self.check_fn)

        scheduler_role = iam.Role(self, "CheckLatestSchedulerRole",
                                  assumed_by=iam.ServicePrincipal("scheduler.amazonaws.com"))
        self.check_fn.grant_invoke(scheduler_role)
        scheduler.CfnSchedule(
            self, "CheckLatestDaily", name="fw-scene-check-latest-daily", state="ENABLED",
            schedule_expression="cron(30 13 * * ? *)", schedule_expression_timezone="UTC",  # 8:30 am US Central
            flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(mode="OFF"),
            target=scheduler.CfnSchedule.TargetProperty(
                arn=self.check_fn.function_arn, role_arn=scheduler_role.role_arn, input="{}",
                retry_policy=scheduler.CfnSchedule.RetryPolicyProperty(maximum_retry_attempts=2)),
        )

        # ---------- optional live subscription (off unless the topic ARN is given) ----------
        topic_arn = self.node.try_get_context("scenes_sns_topic_arn")
        if topic_arn:
            topic_region = topic_arn.split(":")[3]
            if topic_region == "us-east-1":
                raise ValueError("docs/decisions.md D3: no resources in us-east-1")
            self.queue.add_to_resource_policy(iam.PolicyStatement(
                actions=["sqs:SendMessage"], resources=[self.queue.queue_arn],
                principals=[iam.ServicePrincipal("sns.amazonaws.com")],
                conditions={"ArnEquals": {"aws:SourceArn": topic_arn}}))
            sns.CfnSubscription(self, "LiveSceneSubscription", topic_arn=topic_arn, protocol="sqs",
                                endpoint=self.queue.queue_arn, region=topic_region)

        # ---------- outputs (exported for the API lane) ----------
        for name, value in {
            "SceneQueueUrl": self.queue.queue_url, "SceneQueueArn": self.queue.queue_arn,
            "SceneDlqUrl": self.dlq.queue_url, "ReplayFunctionName": self.replay_fn.function_name,
            "ReplayFunctionArn": self.replay_fn.function_arn, "ResetFunctionName": self.reset_fn.function_name,
            "ResetFunctionArn": self.reset_fn.function_arn, "StateMachineArn": self.state_machine.state_machine_arn,
            "CheckLatestFunctionName": self.check_fn.function_name,
        }.items():
            CfnOutput(self, name, value=value, export_name=f"FieldWatch-Scenes-{name}")

    # ------------------------------------------------------------------

    def _state_machine(self, load_fn, analyze_fn, decide_fn) -> sfn.StateMachine:
        concurrency = int(self.node.try_get_context("scenes_map_concurrency") or 10)

        def invoke(sid: str, fn, payload: dict | None = None, result_path: str | None = None, **kw) -> tasks.LambdaInvoke:
            # payload=None sends the (InputPath-selected) state input as is; a dict is an ASL Parameters object.
            task = tasks.LambdaInvoke(
                self, f"{sid}Step", lambda_function=fn, state_name=sid, retry_on_service_exceptions=False,
                payload=sfn.TaskInput.from_object(payload) if payload is not None else None,
                payload_response_only=True, result_path=result_path, **kw)
            task.add_retry(errors=LAMBDA_RETRYABLE, interval=Duration.seconds(2), max_attempts=3, backoff_rate=2)
            task.add_retry(errors=THROTTLED, interval=Duration.seconds(3), max_attempts=6, backoff_rate=1.5)
            return task

        carry = {"scene.$": "$.scene", "replay.$": "$.replay", "replay_id.$": "$.replay_id"}
        load = invoke("LoadFieldInputs", load_fn, result_path="$.request")
        analyze = invoke("AnalyzeField", analyze_fn, result_path="$.result", input_path="$.request", task_timeout=sfn.Timeout.duration(Duration.seconds(120)))
        decide = invoke("Decide", decide_fn, {"request.$": "$.request", "result.$": "$.result", **carry})

        field_failed = sfn.Pass(self, "FieldFailed", parameters={"field_id.$": "$.field.field_id", "status": "error"})
        record_error = invoke("RecordError", decide_fn, {"error.$": "$.error", "field.$": "$.field", **carry})
        record_error.add_catch(field_failed, errors=["States.ALL"], result_path="$.record_error")
        for step in (load, analyze, decide):
            step.add_catch(record_error, errors=["States.ALL"], result_path="$.error")

        each_field = sfn.Map(
            self, "ForEachField", items_path="$.fields", max_concurrency=concurrency, result_path="$.results",
            item_selector={"field.$": "$$.Map.Item.Value", **carry},
        ).item_processor(load.next(analyze).next(decide))
        summarize = sfn.Pass(self, "Summarize", parameters={"scene_id.$": "$.scene.scene_id", "results.$": "$.results"})

        log_group = logs.LogGroup(self, "SceneAnalysisLogs", retention=logs.RetentionDays.ONE_WEEK,
                                  removal_policy=RemovalPolicy.DESTROY)
        return sfn.StateMachine(
            self, "SceneAnalysis", state_machine_name="fw-scene-analysis",
            state_machine_type=sfn.StateMachineType.EXPRESS,
            definition_body=sfn.DefinitionBody.from_chainable(each_field.next(summarize)),
            logs=sfn.LogOptions(destination=log_group, level=sfn.LogLevel.ERROR),
            timeout=Duration.minutes(5),
        )

