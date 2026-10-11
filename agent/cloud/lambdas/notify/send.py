from .runtime import service


def handler(event, context):
    """Direct SendMessageRequest; Scheduler wraps it in {request, scheduled: true}."""
    scheduled = event.get('scheduled') is True
    request = event['request'] if scheduled else event
    # OpenTelemetry's botocore instrumentation (on in the AgentCore Runtime) injects trace context into Lambda
    # payloads as a top-level "headers" key, which the strict SendMessageRequest rejects.
    request = {k: v for k, v in request.items() if k != 'headers'}
    return service().send(request, scheduled=scheduled).model_dump()
