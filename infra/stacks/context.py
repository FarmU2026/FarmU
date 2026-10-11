"""FieldWatch-Context: the context builder (workstream 15, plan ticket T08).

A Step Functions STANDARD state machine, started by the web API after fields are saved:

    input   {schema_version: "1", farm_id, field_ids: [..]}
    Map over field_ids (max concurrency 5), per field:
        LoadBoundary  -> normalize polygon, acres, centroid, Sentinel-2 tiles -> fw-tile-index, status=building
        Parallel      -> soil | terrain | crop | disease | economics | baseline   (each with its own Catch)
        Finalize      -> status ready / partial / failed, fw-fields.status
    output  {farm_id, results: [{field_id, status, failed_stages, stub_stages}]}

Lambda code: agent/cloud/lambdas/context/. Stage Lambdas call the registry/adapters, so Kurt's and Aaron's
implementations land without changing this stack. The baseline stage is a light Lambda while WS1's baseline
is a stub; when it needs rasterio, switch `BASELINE_USES_GEO_IMAGE` (needs Docker at deploy time).
"""
from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack, Tags
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks
from constructs import Construct

from .access import BOUNDARY_NAME
from .common import geo_lambda, python_lambda

BASELINE_USES_GEO_IMAGE = False
MAP_CONCURRENCY = 5
HANDLER_PREFIX = "cloud.lambdas.context"
# One bundle for all eight Lambdas (CDK caches the bundle by source path, so per-function lists would not
# be honored): shapely for LoadBoundary on top of common.BASE_REQUIREMENTS.
REQUIREMENTS = ["shapely>=2"]

# stage name -> handler attribute in cloud.lambdas.context.stages
STAGE_LAMBDAS = {
    "soil": "soil_handler",
    "terrain": "terrain_handler",
    "crop": "crop_handler",
    "disease": "disease_handler",
    "economics": "economics_handler",
    "baseline": "baseline_handler",
}


class Context(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, data, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        Tags.of(self).add("project", "fieldwatch")
        iam.PermissionsBoundary.of(self).apply(
            iam.ManagedPolicy.from_managed_policy_name(self, "Boundary", BOUNDARY_NAME))

        fields, tiles, ctx_table = (data.tables[n] for n in ("fw-fields", "fw-tile-index", "fw-field-context"))
        env = {
            "FIELDS_TABLE": fields.table_name,
            "TILE_INDEX_TABLE": tiles.table_name,
            "FIELD_CONTEXT_TABLE": ctx_table.table_name,
            "BUCKET_NAME": data.bucket.bucket_name,
        }

        # --- Lambdas ---
        load = python_lambda(self, "LoadBoundary", handler=f"{HANDLER_PREFIX}.load_boundary.handler",
                             requirements=REQUIREMENTS, environment=env, timeout_s=60)
        fields.grant_read_write_data(load)
        tiles.grant_read_write_data(load)

        stage_fns = {}
        for name, attr in STAGE_LAMBDAS.items():
            cid = f"Stage{name.capitalize()}"
            handler = f"{HANDLER_PREFIX}.stages.{attr}"
            if name in ("soil", "terrain", "crop") or (name == "baseline" and BASELINE_USES_GEO_IMAGE):
                fn = geo_lambda(self, cid, handler=handler, environment=env, timeout_s=300)
            else:
                fn = python_lambda(self, cid, handler=handler, environment=env, requirements=REQUIREMENTS,
                                   timeout_s=300 if name == "baseline" else 120)
            fields.grant_read_data(fn)
            ctx_table.grant_read_write_data(fn)  # UpdateItem on the stage's own attributes only (enforced in code)
            if name in ("soil", "terrain", "baseline"):
                data.bucket.grant_read_write(fn, "context/*")
            if name == "crop":
                data.bucket.grant_read_write(fn, "static/csb/*")
            stage_fns[name] = fn

        finalize = python_lambda(self, "Finalize", handler=f"{HANDLER_PREFIX}.finalize.handler",
                                 requirements=REQUIREMENTS, environment=env, timeout_s=30)
        fields.grant_read_write_data(finalize)
        ctx_table.grant_write_data(finalize)

        # --- state machine ---
        def invoke(cid, fn, payload, result_path=None):
            task = tasks.LambdaInvoke(
                self, cid, lambda_function=fn, payload=sfn.TaskInput.from_object(payload),
                payload_response_only=True, result_path=result_path,
            )
            # Concurrency caps (spike finding 19: 10 on new accounts) show up as throttles: wait and retry.
            task.add_retry(errors=["Lambda.TooManyRequestsException"], interval=Duration.seconds(2),
                           max_attempts=8, backoff_rate=2)
            # Network blips in the lookups (the handlers raise TransientError for them).
            task.add_retry(errors=["TransientError"], interval=Duration.seconds(3), max_attempts=2, backoff_rate=2)
            return task

        stage_payload = {
            "schema_version": "1",
            "farm_id.$": "$.farm_id",
            "field_id.$": "$.field_id",
            "boundary.$": "$.boundary",
        }

        load_task = invoke("LoadBoundaryTask", load, {
            "schema_version.$": "$.schema_version", "farm_id.$": "$.farm_id", "field_id.$": "$.field_id",
        }, result_path="$.boundary")

        parallel = sfn.Parallel(self, "BuildContext", result_path="$.stages")
        for name, fn in stage_fns.items():
            task = invoke(f"{name.capitalize()}Task", fn, stage_payload)
            task.add_catch(
                sfn.Pass(self, f"{name.capitalize()}Failed", parameters={
                    "stage": name, "ok": False, "error.$": "$.error.Error",
                }),
                errors=["States.ALL"], result_path="$.error",
            )
            parallel.branch(task)

        finalize_task = invoke("FinalizeTask", finalize, {
            "farm_id.$": "$.farm_id", "field_id.$": "$.field_id", "stages.$": "$.stages",
        })
        finalize_task.add_catch(
            sfn.Pass(self, "FinalizeFailed", parameters={
                "field_id.$": "$.field_id", "status": "failed", "failed_stages": ["finalize"], "stub_stages": [],
            }),
            errors=["States.ALL"], result_path="$.error",
        )

        # A field whose boundary can't be loaded skips the stages and goes straight to Finalize as `failed`.
        load_task.add_catch(
            sfn.Pass(self, "BoundaryFailed", parameters={
                "farm_id.$": "$.farm_id", "field_id.$": "$.field_id",
                "stages": [{"stage": "load_boundary", "ok": False}],
            }).next(finalize_task),
            errors=["States.ALL"], result_path="$.error",
        )

        per_field = load_task.next(parallel).next(finalize_task)
        fan_out = sfn.Map(
            self, "ForEachField", items_path="$.field_ids", max_concurrency=MAP_CONCURRENCY,
            item_selector={"schema_version.$": "$.schema_version", "farm_id.$": "$.farm_id",
                           "field_id.$": "$$.Map.Item.Value"},
            result_path="$.results",
        ).item_processor(per_field)
        definition = fan_out.next(sfn.Pass(self, "Output", parameters={
            "farm_id.$": "$.farm_id", "results.$": "$.results",
        }))

        log_group = logs.LogGroup(self, "MachineLogs", retention=logs.RetentionDays.ONE_WEEK,
                                  removal_policy=RemovalPolicy.DESTROY)
        self.state_machine = sfn.StateMachine(
            self, "ContextBuilder",
            state_machine_name="fieldwatch-context-builder",
            state_machine_type=sfn.StateMachineType.STANDARD,
            definition_body=sfn.DefinitionBody.from_chainable(definition),
            timeout=Duration.minutes(30),
            tracing_enabled=True,
            logs=sfn.LogOptions(destination=log_group, level=sfn.LogLevel.ERROR),
        )

        CfnOutput(self, "StateMachineArn", value=self.state_machine.state_machine_arn,
                  description="The web API calls states:StartExecution on this after fields are saved")
