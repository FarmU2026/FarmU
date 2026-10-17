"""Facts a run takes from code, never from the model: where the problem is, how big it is, what it may cost.

The model writes words; the location of a scouting pin or work order, the acres and the dollars come from here
(`tools.create_scout_pin`, `tools.create_work_order`, `agents.DiagnosisGuard`). One RunFacts is made when an
investigation starts, kept on the investigation record at a pause, and put back when the farmer's reply resumes it, so
a pin proposed in the morning is the pin sent in the afternoon even if a newer satellite pass has landed since.
"""
from __future__ import annotations

import contextlib
import contextvars
from dataclasses import asdict, dataclass, field, fields
from typing import Iterator


@dataclass
class RunFacts:
    field_id: str = ""
    field_name: str = ""
    where: str | None = None  # the flagged region's plain-words location
    centroid: tuple[float, float] | None = None  # (lon, lat) of the flagged region
    region_acres: float | None = None
    outcome: str | None = None  # likely | possible | not_determined, from the scorer
    scout_for: str = ""  # what a person walking the spot should look for, from the catalog
    acres: float | None = None  # from the impact tool, set when it runs
    low_usd: float | None = None
    high_usd: float | None = None
    possible: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["centroid"] = list(self.centroid) if self.centroid else None
        return d

    @classmethod
    def from_dict(cls, d: dict | None) -> "RunFacts | None":
        if not isinstance(d, dict):
            return None
        known = {f.name for f in fields(cls)}
        kw = {k: v for k, v in d.items() if k in known}
        if kw.get("centroid"):
            kw["centroid"] = (float(kw["centroid"][0]), float(kw["centroid"][1]))
        return cls(**kw)


_CURRENT: contextvars.ContextVar[RunFacts | None] = contextvars.ContextVar("fieldwatch_run_facts", default=None)


@contextlib.contextmanager
def use(facts: RunFacts | None) -> Iterator[RunFacts | None]:
    token = _CURRENT.set(facts)
    try:
        yield facts
    finally:
        _CURRENT.reset(token)


def current() -> RunFacts | None:
    return _CURRENT.get()
