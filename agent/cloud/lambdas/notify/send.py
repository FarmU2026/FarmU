from .runtime import service


def handler(event, context):
    """Direct SendMessageRequest; Scheduler wraps it in {request, scheduled: true}."""
    scheduled = event.get('scheduled') is True
    request = event['request'] if scheduled else event
    return service().send(request, scheduled=scheduled).model_dump()
