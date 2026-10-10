"""Route table: which Lambda (route group) serves which "METHOD /path" route key.

Plain Python with no imports so infra/stacks/api.py can load it at synth time. A unit test checks
that each group's Router registers exactly these routes.
"""

ROUTES: dict[str, list[str]] = {
    "farms": [
        "POST /api/farms",
        "GET /api/farms/{farm_id}",
        "POST /api/farms/{farm_id}/check",
        "POST /api/farms/{farm_id}/fields/{field_id}/ask",
        "POST /api/farms/{farm_id}/reply",
        "GET /api/farms/{farm_id}/fields/{field_id}/{image}",
        "GET /api/status",
    ],
    "pages": [
        "GET /v1/reports/{investigation_id}",
        "POST /v1/approvals/{approval_id}/decision",
        "POST /v1/uploads",
        "GET /v1/uploads/{upload_id}",
        "GET /v1/work-orders/{action_id}",
        "POST /v1/work-orders/{action_id}/done",
        "POST /v1/farms/{farm_id}/people",
    ],
    "dashboard": [
        "POST /v1/auth/dashboard",
        "GET /v1/farms/{farm_id}/overview",
        "GET /v1/investigations/{investigation_id}",
        "POST /v1/replay",
        "POST /v1/replay/reset",
        "POST /v1/mirror/reply",
    ],
    "sms_inbound": [
        "POST /sms/inbound",
    ],
}

HANDLERS: dict[str, str] = {group: f"cloud.api.{group}.handler" for group in ROUTES}
