"""Token checks for the route groups: farmer link tokens (`t` query parameter) and the dashboard bearer."""
from __future__ import annotations

from fieldwatch import links

from ._http import ApiError, Request


def _api_error(e: links.LinkError) -> ApiError:
    return ApiError(e.code, e.message, 401 if e.code == "TOKEN_EXPIRED" else 403)


def link(req: Request, scope: str, subject_id: str | None = None) -> links.LinkClaims:
    try:
        return links.verify(req.query.get("t"), scope=scope, subject_id=subject_id)
    except links.LinkError as e:
        raise _api_error(e) from None


def dashboard(req: Request) -> links.LinkClaims:
    token = req.bearer()
    if not token:
        raise ApiError("TOKEN_SCOPE", "Sign in to the dashboard first", 401)
    try:
        return links.verify(token, scope="dashboard")
    except links.LinkError as e:
        raise _api_error(e) from None


def same_farm(claims: links.LinkClaims, farm_id: str | None) -> None:
    if claims.farm_id != "*" and farm_id and claims.farm_id != farm_id:
        raise ApiError("TOKEN_SCOPE", "This link is for a different farm", 403)
