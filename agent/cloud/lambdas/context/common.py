"""Shared helpers for the context-builder Lambdas: table access, DynamoDB number conversion, errors."""
from __future__ import annotations

import json
import os
from decimal import Decimal
from typing import Any

import boto3

SCHEMA_VERSION = "1"

# Table names are fixed by infra/stacks/data_plane.py; the stack also passes them as environment variables.
FIELDS_TABLE = "FIELDS_TABLE"
TILE_INDEX_TABLE = "TILE_INDEX_TABLE"
FIELD_CONTEXT_TABLE = "FIELD_CONTEXT_TABLE"
_DEFAULTS = {FIELDS_TABLE: "fw-fields", TILE_INDEX_TABLE: "fw-tile-index", FIELD_CONTEXT_TABLE: "fw-field-context"}


class BoundaryError(Exception):
    """The field or its polygon can't be used. Not retried."""


class StageError(Exception):
    """A stage could not produce its part of the context. Not retried; the field becomes `partial`."""


class TransientError(Exception):
    """A network or throttling problem worth one more try. The state machine retries this error name."""


def table(env_name: str):
    """A DynamoDB Table resource (created per call so tests under moto always get a fresh client)."""
    return boto3.resource("dynamodb").Table(os.environ.get(env_name, _DEFAULTS[env_name]))


def to_ddb(obj: Any) -> Any:
    """JSON-able Python -> DynamoDB-safe (floats become Decimal)."""
    return json.loads(json.dumps(obj), parse_float=Decimal)


def from_ddb(obj: Any) -> Any:
    """DynamoDB item -> plain JSON-able Python (Decimal -> int or float)."""
    if isinstance(obj, Decimal):
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    if isinstance(obj, dict):
        return {k: from_ddb(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [from_ddb(v) for v in obj]
    return obj


_CROPS = {"corn": "corn", "maize": "corn", "soybeans": "soybeans", "soybean": "soybeans", "soy": "soybeans",
          "wheat": "wheat", "milo": "milo", "sorghum": "milo", "alfalfa": "alfalfa", "hay": "alfalfa"}


def normalize_crop(raw: Any) -> str:
    """Map whatever the farmer typed to contracts.Crop (corn, soybeans, wheat, milo, alfalfa, other)."""
    return _CROPS.get(str(raw or "").strip().lower(), "other")
