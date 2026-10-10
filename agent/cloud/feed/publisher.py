"""Publish FeedMessages to AppSync Events over the HTTP publish API, signed with SigV4 (service `appsync`).

POST https://<api-id>.appsync-api.<region>.amazonaws.com/event
    {"channel": "/fieldwatch/farms/<farm>/feed", "events": ["<json string>", ...]}     (max 5 events)
Each event is a JSON *string*. A 200 response is {"successful": [...], "failed": [...]}.

No dependency beyond botocore. The transport is injectable so tests never touch the network.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Callable

from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

from fieldwatch.contracts import FeedMessage

MAX_EVENTS_PER_REQUEST = 5
# (url, headers, body) -> (status, response text)
Transport = Callable[[str, dict[str, str], bytes], tuple[int, str]]


class PublishError(RuntimeError):
    pass


def urllib_transport(url: str, headers: dict[str, str], body: bytes) -> tuple[int, str]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 (https endpoint from config)
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def signed_request(url: str, body: bytes, credentials, region: str) -> dict[str, str]:
    """Headers for a SigV4-signed POST (content-type, host, x-amz-date, x-amz-security-token, authorization)."""
    req = AWSRequest(method="POST", url=url, data=body, headers={"Content-Type": "application/json"})
    SigV4Auth(credentials, "appsync", region).add_auth(req)
    return {k: str(v) for k, v in req.headers.items()}


class FeedPublisher:
    def __init__(self, http_host: str, region: str, credentials, transport: Transport = urllib_transport):
        self.url = f"https://{http_host}/event"
        self.region = region
        self.credentials = credentials
        self.transport = transport

    def publish(self, channel: str, messages: list[FeedMessage]) -> int:
        """Publish messages (in order) to one channel; returns how many were accepted. Raises PublishError."""
        sent = 0
        for i in range(0, len(messages), MAX_EVENTS_PER_REQUEST):
            batch = messages[i:i + MAX_EVENTS_PER_REQUEST]
            body = json.dumps({"channel": channel,
                               "events": [json.dumps(m.model_dump(mode="json"), separators=(",", ":")) for m in batch]},
                              separators=(",", ":")).encode()
            headers = signed_request(self.url, body, self.credentials.get_frozen_credentials()
                                     if hasattr(self.credentials, "get_frozen_credentials") else self.credentials,
                                     self.region)
            status, text = self.transport(self.url, headers, body)
            if status != 200:
                raise PublishError(f"AppSync Events publish to {channel} returned {status}: {text[:300]}")
            failed = (json.loads(text) if text else {}).get("failed") or []
            if failed:
                raise PublishError(f"AppSync Events rejected {len(failed)} event(s) on {channel}: {failed[:2]}")
            sent += len(batch)
        return sent
