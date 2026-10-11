"""The send Lambda must accept payloads from the AgentCore Runtime, whose OpenTelemetry botocore instrumentation adds
a top-level "headers" key (trace context) to every Lambda invoke payload."""
from cloud.lambdas.notify import send


class _Result:
    def model_dump(self):
        return {"status": "sent"}


class _Service:
    def __init__(self):
        self.calls = []

    def send(self, request, scheduled=False):
        self.calls.append((request, scheduled))
        return _Result()


def test_otel_headers_are_dropped(monkeypatch):
    svc = _Service()
    monkeypatch.setattr(send, "service", lambda: svc)
    out = send.handler({"farm_id": "f", "body": "hi", "headers": {"baggage": "Lineage=2"}}, None)
    assert out == {"status": "sent"}
    assert svc.calls == [({"farm_id": "f", "body": "hi"}, False)]


def test_scheduled_wrapper_still_works(monkeypatch):
    svc = _Service()
    monkeypatch.setattr(send, "service", lambda: svc)
    send.handler({"scheduled": True, "request": {"farm_id": "f", "headers": {}}}, None)
    assert svc.calls == [({"farm_id": "f"}, True)]
