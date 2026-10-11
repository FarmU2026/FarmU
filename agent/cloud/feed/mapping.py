"""Pure mapping: one EventBridge event -> the FeedMessages it produces, plus thread-history items.

No AWS here. `project()` takes the bus event (`detail-type`, `detail`, `id`) and a `farm_lookup`
callable (investigation_id -> farm_id) and returns a `Projection`.

Channels (inside AppSync namespace `fieldwatch`):
    /fieldwatch/farms/{farm_id}/feed    dashboard: everything
    /fieldwatch/farms/{farm_id}/phone   phone mirror: exactly the messages that are also written to thread
                                        history (message_out, message_in, investigation_step, concluded,
                                        action_created), so a page reload rebuilds what the live feed showed.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from fieldwatch import contracts as C
from fieldwatch.interfaces import EVENT_REGISTRY, ThreadItem  # noqa: F401  (ThreadItem re-exported for importers)

NAMESPACE = "fieldwatch"
FEED = "feed"
PHONE = "phone"

# A message goes to the phone channel (and has a thread item) when its payload carries a `kind`
# ("message", "step", "concluded" or "action"); the payload then also has `who` and `text`.
MAX_PAYLOAD_BYTES = 200_000  # AppSync Events limit is 240 KB per event

# detail-type -> contract model (validates the event before it is shown); models come from interfaces.EVENT_REGISTRY
SHOWN = ["SceneAnalyzed", "AnomalyDetected", "WatchOpened", "InvestigationProgress", "InvestigationConcluded",
         "ReplyReceived", "ApprovalRecorded", "ActionCreated", "ActionCompleted", "MessageSent", "MessageReceived"]
EVENT_MODELS: dict[str, type[C.EventBase]] = {name: EVENT_REGISTRY[name].model for name in SHOWN}
DETAIL_TYPES = list(EVENT_MODELS)


class UnknownFarm(RuntimeError):
    """The farm for an event could not be found (yet); raise so EventBridge retries."""


def channel(farm_id: str, kind: str) -> str:
    return f"/{NAMESPACE}/farms/{farm_id}/{kind}"


@dataclass
class Projection:
    messages: list[C.FeedMessage] = field(default_factory=list)
    thread: list[ThreadItem] = field(default_factory=list)

    def publishes(self) -> dict[str, list[C.FeedMessage]]:
        """channel -> messages, in order."""
        out: dict[str, list[C.FeedMessage]] = {}
        for m in self.messages:
            out.setdefault(channel(m.farm_id, FEED), []).append(m)
            if "kind" in m.payload:
                out.setdefault(channel(m.farm_id, PHONE), []).append(m)
        return out


def _msg(type_: str, ev: C.EventBase, farm_id: str, event_id: str, *, investigation_id: str | None = None,
         agent: str | None = None, **payload: Any) -> C.FeedMessage:
    payload = {"event_id": event_id, **payload}
    if len(json.dumps(payload, default=str)) > MAX_PAYLOAD_BYTES:
        payload = {k: v for k, v in payload.items() if k not in ("regions", "overlays", "detail")} | {"trimmed": True}
    return C.FeedMessage(type=type_, ts=ev.occurred_at, farm_id=farm_id, investigation_id=investigation_id,
                         agent=agent, backend="cloud", payload=payload)


def _no_lookup(investigation_id: str) -> str:
    raise UnknownFarm(investigation_id)


def project(detail_type: str, detail: dict[str, Any], event_id: str,
            farm_lookup: Callable[[str], str] | None = None) -> Projection:
    """Map one bus event. `event_id` is the EventBridge id (used when the event has no natural id).
    Raises KeyError for an event the feed does not show, pydantic.ValidationError for a bad detail."""
    ev = EVENT_MODELS[detail_type].model_validate(detail)
    lookup = farm_lookup or _no_lookup
    p = Projection()
    at = ev.occurred_at

    if isinstance(ev, C.SceneAnalyzed):
        f = ev.field
        p.messages.append(_msg("scene_analyzed", ev, f.farm_id, event_id, field_id=f.field_id,
                               scene_id=ev.scene_id, date=ev.date, status=ev.status, max_score=ev.max_score,
                               region_count=ev.region_count, analysis_key=ev.analysis_key))
        p.messages.append(_msg("field_status", ev, f.farm_id, event_id, field_id=f.field_id,
                               status="ok" if ev.status == "ok" else ev.status, latest_score=ev.max_score,
                               scene_date=ev.date))

    elif isinstance(ev, C.AnomalyDetected):
        f = ev.field
        regions = [{"region_id": r.region_id, "geometry": r.geometry, "area_acres": r.area_acres,
                    "centroid": list(r.centroid), "score": r.score} for r in ev.regions]
        p.messages.append(_msg(
            "field_status", ev, f.farm_id, ev.anomaly_id, field_id=f.field_id, status="anomaly",
            anomaly_id=ev.anomaly_id, scene_id=ev.scene_id, date=ev.date, region_count=len(regions),
            regions=regions, overlays=ev.overlays.model_dump(), open_watch_ids=ev.open_watch_ids,
            replay=ev.replay))

    elif isinstance(ev, C.WatchOpened):
        w = ev.watch
        for fr in w.fields:
            p.messages.append(_msg(
                "field_status", ev, fr.farm_id, f"{w.watch_id}#{fr.field_id}", field_id=fr.field_id,
                status="watching", watch_id=w.watch_id, watch_type=w.type, expires_at=w.expires_at,
                detail=w.detail))

    elif isinstance(ev, C.InvestigationProgress):
        farm_id = lookup(ev.investigation_id)
        eid = f"{ev.investigation_id}#{ev.seq}"
        p.messages.append(_msg("investigation_step", ev, farm_id, eid, investigation_id=ev.investigation_id,
                               agent=ev.agent, seq=ev.seq, step=ev.step, text=ev.text, tool=ev.tool,
                               latency_ms=ev.latency_ms, who="agent", kind="step"))
        p.thread.append(ThreadItem(farm_id=farm_id, id=eid, at=at, who="agent", text=ev.text, kind="step",
                                   investigation_id=ev.investigation_id))
        if ev.evidence:
            p.messages.append(_msg("evidence_added", ev, farm_id, eid, investigation_id=ev.investigation_id,
                                   agent=ev.agent, seq=ev.seq, evidence=ev.evidence.model_dump()))
        if ev.hypotheses:
            p.messages.append(_msg("hypotheses_updated", ev, farm_id, eid, investigation_id=ev.investigation_id,
                                   agent=ev.agent, seq=ev.seq,
                                   hypotheses=[h.model_dump() for h in ev.hypotheses]))

    elif isinstance(ev, C.InvestigationConcluded):
        f = ev.field
        eid = f"{ev.investigation_id}#concluded"
        p.messages.append(_msg(
            "concluded", ev, f.farm_id, eid, investigation_id=ev.investigation_id, field_id=f.field_id,
            condition=ev.condition, summary=ev.summary, hypotheses=[h.model_dump() for h in ev.hypotheses],
            impact=ev.impact.model_dump() if ev.impact else None,
            proposed_action=ev.proposed_action.model_dump(), message_id=ev.message_id, fast_path=ev.fast_path,
            who="fieldwatch", kind="concluded", text=ev.summary))
        p.thread.append(ThreadItem(farm_id=f.farm_id, id=eid, at=at, who="fieldwatch", text=ev.summary,
                                   kind="concluded", investigation_id=ev.investigation_id, field_id=f.field_id))

    elif isinstance(ev, C.ReplyReceived):
        # The farmer's text itself reaches the phone as MessageReceived; this only tells the dashboard how
        # the reply was understood. No thread item (MessageReceived writes it) and not on the phone channel.
        p.messages.append(_msg("investigation_step", ev, ev.farm_id, ev.reply_id,
                               investigation_id=ev.investigation_id, agent="supervisor", step="reply",
                               text=f"Farmer replied ({ev.intent}).", intent=ev.intent, reply_id=ev.reply_id,
                               approval_id=ev.approval_id, person_id=ev.person_id))

    elif isinstance(ev, C.ApprovalRecorded):
        farm_id = ev.farm_id or lookup(ev.investigation_id)
        text = f"{ev.decided_by} {'approved' if ev.decision == 'approved' else 'declined'} the plan."
        p.messages.append(_msg("action_created", ev, farm_id, ev.approval_id, investigation_id=ev.investigation_id,
                               approval_id=ev.approval_id, status=ev.decision, decided_by=ev.decided_by,
                               channel=ev.channel, who="fieldwatch", kind="action", text=text))
        p.thread.append(ThreadItem(farm_id=farm_id, id=f"{ev.approval_id}#approval", at=at, who="fieldwatch",
                                   text=text, kind="action", investigation_id=ev.investigation_id))

    elif isinstance(ev, C.ActionCreated):
        farm_id = ev.farm_id or lookup(ev.investigation_id)
        what = {"work_order": "Repair ticket", "scout_pin": "Scouting pin", "photo_request": "Photo request",
                "insurance_notice": "Insurance note draft"}[ev.type]
        to = f" to {ev.assignee_person_id}" if ev.assignee_person_id and ev.type != "insurance_notice" else ""
        text = f"{what} sent{to}: {ev.summary}"
        p.messages.append(_msg("action_created", ev, farm_id, f"{ev.action_id}#created",
                               investigation_id=ev.investigation_id, action_id=ev.action_id,
                               approval_id=ev.approval_id, action_type=ev.type, status="sent",
                               assignee_person_id=ev.assignee_person_id, field_name=ev.field_name,
                               delivered=ev.delivered, who="fieldwatch", kind="action", text=text))
        p.thread.append(ThreadItem(farm_id=farm_id, id=f"{ev.action_id}#created", at=at, who="fieldwatch",
                                   text=text, kind="action", investigation_id=ev.investigation_id))

    elif isinstance(ev, C.ActionCompleted):
        farm_id = ev.farm_id or lookup(ev.investigation_id)
        text = f"{ev.completed_by} finished the job."
        p.messages.append(_msg("action_created", ev, farm_id, ev.action_id, investigation_id=ev.investigation_id,
                               action_id=ev.action_id, status="done", completed_by=ev.completed_by,
                               photo_upload_id=ev.photo_upload_id,
                               who="fieldwatch", kind="action", text=text))
        p.thread.append(ThreadItem(farm_id=farm_id, id=f"{ev.action_id}#done", at=at, who="fieldwatch", text=text,
                                   kind="action", investigation_id=ev.investigation_id))

    elif isinstance(ev, (C.MessageSent, C.MessageReceived)):
        out = isinstance(ev, C.MessageSent)
        who = "fieldwatch" if out else "farmer"
        extra = dict(status=ev.status, provider=ev.provider) if out else {}
        p.messages.append(_msg("message_out" if out else "message_in", ev, ev.farm_id, ev.message_id,
                               investigation_id=ev.investigation_id, message_id=ev.message_id,
                               person_id=ev.person_id, text=ev.body, who=who, kind="message", **extra))
        p.thread.append(ThreadItem(farm_id=ev.farm_id, id=ev.message_id, at=at, who=who, text=ev.body,
                                   kind="message", investigation_id=ev.investigation_id))

    return p
