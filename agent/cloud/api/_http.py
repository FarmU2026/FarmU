"""Tiny request/response layer for API Gateway HTTP API (payload format 2.0) Lambda proxies.

Each route-group module builds a Router, registers handlers with `@router.route("GET", "/v1/x/{id}")`,
and exposes `handler = router.handler`. Handlers take a Request and return a Response (or a dict,
sent as 200 JSON). Raise ApiError for the specs' error body `{error: {code, message, retryable}}`.
"""
from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable
from urllib.parse import parse_qsl

from pydantic import ValidationError

log = logging.getLogger("fieldwatch.api")
log.setLevel(logging.INFO)

STATUS_FOR_CODE = {
    "TOKEN_EXPIRED": 401, "TOKEN_SCOPE": 403, "NOT_FOUND": 404, "ALREADY_DECIDED": 409,
    "INVALID_INPUT": 400, "RATE_LIMITED": 429, "NOT_IMPLEMENTED": 501, "INTERNAL": 500,
}


class ApiError(Exception):
    def __init__(self, code: str, message: str, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable
        self.status = status or STATUS_FOR_CODE.get(code, 400)


def _default(o: Any):
    if isinstance(o, Decimal):
        return int(o) if o == o.to_integral_value() else float(o)
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    if hasattr(o, "model_dump"):
        return o.model_dump(mode="json")
    raise TypeError(f"not JSON serializable: {type(o).__name__}")


def dumps(obj: Any) -> str:
    return json.dumps(obj, default=_default)


@dataclass
class Response:
    status: int = 200
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    raw: str | None = None  # non-JSON body (e.g. TwiML)

    def to_lambda(self) -> dict:
        headers = {"Cache-Control": "no-store", **self.headers}
        if self.raw is not None:
            body = self.raw
            headers.setdefault("Content-Type", "text/plain")
        elif self.body is None:
            body = ""
        else:
            body = dumps(self.body)
            headers.setdefault("Content-Type", "application/json")
        return {"statusCode": self.status, "headers": headers, "body": body}


def json_response(body: Any, status: int = 200) -> Response:
    return Response(status=status, body=body)


def error_response(code: str, message: str, status: int | None = None, retryable: bool = False) -> Response:
    return Response(status=status or STATUS_FOR_CODE.get(code, 400),
                    body={"error": {"code": code, "message": message, "retryable": retryable}})


def redirect(location: str) -> Response:
    return Response(status=302, headers={"Location": location})


@dataclass
class Request:
    method: str
    path: str
    params: dict[str, str]
    query: dict[str, str]
    headers: dict[str, str]
    raw_body: str
    event: dict

    def json(self) -> dict:
        if not self.raw_body:
            return {}
        try:
            data = json.loads(self.raw_body)
        except ValueError:
            raise ApiError("INVALID_INPUT", "Body must be JSON") from None
        if not isinstance(data, dict):
            raise ApiError("INVALID_INPUT", "Body must be a JSON object")
        return data

    def form(self) -> dict[str, str]:
        return dict(parse_qsl(self.raw_body, keep_blank_values=True))

    def model(self, cls):
        """Parse the JSON body into a Pydantic model; validation errors become INVALID_INPUT."""
        try:
            return cls.model_validate(self.json())
        except ValidationError as e:
            first = e.errors()[0]
            where = ".".join(str(x) for x in first.get("loc", ()))
            raise ApiError("INVALID_INPUT", f"{where}: {first.get('msg')}" if where else str(first.get("msg"))) from None

    def bearer(self) -> str | None:
        auth = self.headers.get("authorization", "")
        return auth[7:].strip() if auth.lower().startswith("bearer ") else None


def parse_event(event: dict) -> Request:
    http = (event.get("requestContext") or {}).get("http") or {}
    method = (http.get("method") or event.get("httpMethod") or "GET").upper()
    path = event.get("rawPath") or http.get("path") or event.get("path") or "/"
    body = event.get("body") or ""
    if body and event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    return Request(method=method, path=path, params=dict(event.get("pathParameters") or {}),
                   query=dict(event.get("queryStringParameters") or {}), headers=headers,
                   raw_body=body, event=event)


Handler = Callable[[Request], "Response | dict | list"]


class Router:
    def __init__(self, name: str):
        self.name = name
        self.routes: list[tuple[str, str, re.Pattern, Handler]] = []

    def route(self, method: str, template: str):
        pattern = re.compile("^" + re.sub(r"\\\{(\w+)\\\}", r"(?P<\1>[^/]+)", re.escape(template)) + "$")

        def deco(fn: Handler) -> Handler:
            self.routes.append((method.upper(), template, pattern, fn))
            return fn
        return deco

    @property
    def route_keys(self) -> list[str]:
        return [f"{m} {t}" for m, t, _, _ in self.routes]

    def _match(self, req: Request) -> Handler | None:
        route_key = req.event.get("routeKey")
        for method, template, pattern, fn in self.routes:
            if route_key and route_key == f"{method} {template}":
                m = pattern.match(req.path)
                if m:
                    req.params = {**m.groupdict(), **req.params}
                return fn
        for method, _template, pattern, fn in self.routes:
            if method != req.method:
                continue
            m = pattern.match(req.path)
            if m:
                req.params = {**m.groupdict(), **req.params}
                return fn
        return None

    def dispatch(self, event: dict) -> dict:
        req = parse_event(event)
        fn = self._match(req)
        if fn is None:
            return error_response("NOT_FOUND", f"No route for {req.method} {req.path}").to_lambda()
        try:
            out = fn(req)
            resp = out if isinstance(out, Response) else json_response(out)
        except ApiError as e:
            resp = error_response(e.code, e.message, e.status, e.retryable)
        except Exception:  # noqa: BLE001 - last resort; details go to the logs only
            log.exception("%s %s failed", req.method, req.path)
            resp = error_response("INTERNAL", "Something went wrong on our end", 500, retryable=True)
        return resp.to_lambda()

    def handler(self, event: dict, context: Any = None) -> dict:  # noqa: ARG002 - Lambda signature
        return self.dispatch(event)
