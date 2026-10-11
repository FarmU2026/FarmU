"""Shared Lambda builders. Every stack uses these instead of packaging its own way.

python_lambda(): plain Python 3.12 x86_64 Lambda. The asset is agent/fieldwatch, agent/cloud,
    agent/catalog and agent/fixtures/contracts, plus pip-installed Linux wheels for `requirements`
    (boto3 is already in the Lambda runtime). Bundles locally with pip, no Docker needed.
geo_lambda(): container-image Lambda with the geospatial stack (rasterio, numpy, pillow, pystac-client,
    shapely) from infra/docker/geo/Dockerfile, for the field analyzer and anything that reads rasters.
    Needs Docker at deploy time (not at synth).

Handlers live under agent/cloud/..., e.g. handler="cloud.api.farms.handler".
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import jsii
from aws_cdk import BundlingOptions, DockerImage, Duration, ILocalBundling, RemovalPolicy
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from constructs import Construct

REPO = Path(__file__).resolve().parents[2]
AGENT = REPO / "agent"
_COPY = ["fieldwatch", "cloud", "catalog", "fixtures/contracts"]
_IGNORE_NAMES = shutil.ignore_patterns("__pycache__", "*.pyc", "tests", "data", "sessions", ".env", "node_modules",
                                       ".cli-tools", "cdk.out")
# Paths (relative to agent/) never copied into a Lambda bundle: the AgentCore CLI project (its own CDK app,
# node_modules and staged copies of fieldwatch/) is the Runtime's, not a Lambda's.
_IGNORE_PATHS = {"cloud/agentcore"}


def _IGNORE(directory: str, names: list[str]) -> set[str]:  # noqa: N802 - shutil.copytree `ignore` callable
    ignored = set(_IGNORE_NAMES(directory, names))
    try:
        rel = Path(directory).resolve().relative_to(AGENT.resolve()).as_posix()
    except ValueError:
        return ignored
    prefix = "" if rel == "." else rel + "/"
    return ignored | {n for n in names if prefix + n in _IGNORE_PATHS}


_EXCLUDE = ["data", "sessions", "tests", "web", ".venv", "**/__pycache__", "**/*.pyc", ".env",
            "cloud/agentcore", "**/node_modules", "**/.cli-tools", "**/cdk.out"]
# What every handler needs on top of the Lambda runtime's boto3.
BASE_REQUIREMENTS = ["pydantic>=2", "python-dotenv>=1", "requests>=2.31"]


@jsii.implements(ILocalBundling)
class _LocalPip:
    def __init__(self, requirements: list[str]):
        self.requirements = requirements

    def try_bundle(self, output_dir: str, *, image=None, **_kwargs) -> bool:  # noqa: ARG002
        out = Path(output_dir)
        for rel in _COPY:
            src = AGENT / rel
            if src.exists():
                shutil.copytree(src, out / rel, ignore=_IGNORE, dirs_exist_ok=True)
        if self.requirements:
            subprocess.check_call([
                sys.executable, "-m", "pip", "install", "--quiet", "--no-cache-dir", "--target", str(out),
                "--platform", "manylinux2014_x86_64", "--implementation", "cp", "--python-version", "3.12",
                "--only-binary=:all:", *self.requirements,
            ])
        return True


def python_lambda(scope: Construct, cid: str, *, handler: str, requirements: list[str] | None = None,
                  environment: dict[str, str] | None = None, timeout_s: int = 30, memory_mb: int = 512,
                  **kwargs) -> lambda_.Function:
    reqs = BASE_REQUIREMENTS + (requirements or [])
    code = lambda_.Code.from_asset(
        str(AGENT),
        exclude=_EXCLUDE,
        bundling=BundlingOptions(
            image=DockerImage.from_registry("public.ecr.aws/sam/build-python3.12:latest"),
            local=_LocalPip(reqs),
            command=["bash", "-c", "echo local bundling only && exit 1"],
        ),
    )
    return lambda_.Function(
        scope, cid,
        runtime=lambda_.Runtime.PYTHON_3_12,
        architecture=lambda_.Architecture.X86_64,
        handler=handler,
        code=code,
        timeout=Duration.seconds(timeout_s),
        memory_size=memory_mb,
        environment={"PYTHONPATH": "/var/task", **(environment or {})},
        log_group=logs.LogGroup(scope, f"{cid}Logs", retention=logs.RetentionDays.ONE_WEEK,
                                removal_policy=RemovalPolicy.DESTROY),
        **kwargs,
    )


def geo_lambda(scope: Construct, cid: str, *, handler: str, environment: dict[str, str] | None = None,
               timeout_s: int = 90, memory_mb: int = 3008, **kwargs) -> lambda_.DockerImageFunction:
    code = lambda_.DockerImageCode.from_image_asset(
        str(REPO),
        file="infra/docker/geo/Dockerfile",
        cmd=[handler],
        platform=ecr_assets.Platform.LINUX_AMD64,
        exclude=["**/node_modules", "**/cdk.out", "**/__pycache__", "**/.venv", "agent/data", "agent/sessions",
                 "web", "docs", ".git", "**/*.pyc", "agent/.env"],
    )
    return lambda_.DockerImageFunction(
        scope, cid,
        code=code,
        architecture=lambda_.Architecture.X86_64,
        timeout=Duration.seconds(timeout_s),
        memory_size=memory_mb,
        environment=environment or {},
        log_group=logs.LogGroup(scope, f"{cid}Logs", retention=logs.RetentionDays.ONE_WEEK,
                                removal_policy=RemovalPolicy.DESTROY),
        **kwargs,
    )
