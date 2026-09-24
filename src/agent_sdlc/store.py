from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Literal

from sqlalchemy import JSON, ForeignKey, String, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from agent_sdlc.types import (
    Calibration,
    Decision,
    EventInput,
    Item,
    ParkReason,
    Stage,
    Usage,
    WorkItem,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _db_ts(ts: datetime | None) -> datetime:
    """Stored as naive UTC (SQLite keeps no tzinfo)."""
    return (ts or _now()).astimezone(UTC).replace(tzinfo=None)


def _aware(ts: datetime) -> datetime:
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)


class Base(DeclarativeBase):
    pass


class ItemRow(Base):
    __tablename__ = "items"
    id: Mapped[int] = mapped_column(primary_key=True)
    target: Mapped[str] = mapped_column(String(100))
    title: Mapped[str]
    branch: Mapped[str]
    stage: Mapped[str]
    park_reason: Mapped[str | None]
    parked_from: Mapped[str | None]
    attempt: Mapped[int] = mapped_column(default=0)
    replans: Mapped[int] = mapped_column(default=0)
    pr_rounds: Mapped[int] = mapped_column(default=0)
    infra_failures: Mapped[int] = mapped_column(default=0)
    pr_id: Mapped[int | None]
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    usage: Mapped[dict[str, int]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=_now)
    updated_at: Mapped[datetime] = mapped_column(default=_now)


class DecisionRow(Base):
    __tablename__ = "decisions"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"))
    gate: Mapped[str]
    question: Mapped[str]
    answer: Mapped[str]
    probs: Mapped[dict[str, float]] = mapped_column(JSON)
    raw_probs: Mapped[dict[str, float]] = mapped_column(JSON)
    confidence: Mapped[float]
    shadow: Mapped[bool]
    actionable: Mapped[bool]
    state: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(default=_now)


class LabelRow(Base):
    __tablename__ = "labels"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    gate: Mapped[str]
    question: Mapped[str]
    raw_probs: Mapped[dict[str, float]] = mapped_column(JSON)
    gold: Mapped[str]
    source: Mapped[str]
    decision_id: Mapped[int | None]
    created_at: Mapped[datetime] = mapped_column(default=_now)


class CalibrationRow(Base):
    __tablename__ = "calibrations"
    key: Mapped[str] = mapped_column(primary_key=True)
    temperature: Mapped[float]
    threshold: Mapped[float]
    mode: Mapped[str]
    ece: Mapped[float | None]
    n: Mapped[int]


class FlagRow(Base):
    __tablename__ = "flags"
    key: Mapped[str] = mapped_column(primary_key=True)
    value: Mapped[str]


class DailyUsageRow(Base):
    __tablename__ = "daily_usage"
    day: Mapped[str] = mapped_column(primary_key=True)
    turns: Mapped[int] = mapped_column(default=0)
    input_tokens: Mapped[int] = mapped_column(default=0)
    output_tokens: Mapped[int] = mapped_column(default=0)


class EventRow(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id"), index=True)
    ts: Mapped[datetime] = mapped_column(index=True)
    kind: Mapped[str] = mapped_column(String(40), index=True)
    stage: Mapped[str | None]
    attempt: Mapped[int | None]
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


@dataclass(frozen=True)
class Event:
    id: int
    item_id: int | None
    ts: datetime          # aware UTC
    kind: str
    stage: str | None
    attempt: int | None
    payload: dict[str, Any]


def _to_event(r: EventRow) -> Event:
    return Event(r.id, r.item_id, _aware(r.ts), r.kind, r.stage, r.attempt, dict(r.payload or {}))


def _event_row(ev: EventInput, at: Item | None, ts: datetime | None = None) -> EventRow:
    return EventRow(item_id=at.id if at else None, ts=_db_ts(ts), kind=ev.kind,
                    stage=at.stage.value if at else None, attempt=at.attempt if at else None,
                    payload=dict(ev.payload))


@dataclass(frozen=True)
class LabelInput:
    gate: str
    question: str
    raw_probs: dict[str, float]
    gold: str
    source: str
    decision_id: int | None = None


def _to_item(r: ItemRow) -> Item:
    return Item(
        id=r.id, target=r.target, title=r.title, branch=r.branch, stage=Stage(r.stage),
        park_reason=ParkReason(r.park_reason) if r.park_reason else None,
        parked_from=Stage(r.parked_from) if r.parked_from else None,
        attempt=r.attempt, replans=r.replans, pr_rounds=r.pr_rounds,
        infra_failures=r.infra_failures, pr_id=r.pr_id, data=dict(r.data or {}),
        usage=Usage.from_dict(r.usage),
    )


def _to_decision(r: DecisionRow) -> Decision:
    return Decision(r.gate, r.question, r.answer, dict(r.probs), dict(r.raw_probs),
                    r.confidence, r.shadow, r.actionable)


class Store:
    def __init__(self, url: str) -> None:
        self._engine = create_engine(url)
        Base.metadata.create_all(self._engine)

    def _session(self) -> Session:
        return Session(self._engine, expire_on_commit=False)

    # items -----------------------------------------------------------------
    def add_item(self, target: str, wi: WorkItem, branch: str) -> bool:
        with self._session() as s, s.begin():
            if s.get(ItemRow, wi.id) is not None:
                return False
            s.add(ItemRow(id=wi.id, target=target, title=wi.title, branch=branch,
                          stage=Stage.TRIAGE.value, data={}, usage={}))
            return True

    def get(self, item_id: int) -> Item:
        with self._session() as s:
            row = s.get(ItemRow, item_id)
            if row is None:
                raise KeyError(item_id)
            return _to_item(row)

    def items(self, target: str, stages: Iterable[Stage] | None = None) -> list[Item]:
        with self._session() as s:
            q = select(ItemRow).where(ItemRow.target == target)
            if stages is not None:
                q = q.where(ItemRow.stage.in_([st.value for st in stages]))
            q = q.order_by(ItemRow.created_at, ItemRow.id)
            return [_to_item(r) for r in s.scalars(q)]

    def _write_item(self, s: Session, item: Item) -> None:
        row = s.get(ItemRow, item.id)
        if row is None:
            raise KeyError(item.id)
        row.title, row.branch, row.stage = item.title, item.branch, item.stage.value
        row.park_reason = item.park_reason.value if item.park_reason else None
        row.parked_from = item.parked_from.value if item.parked_from else None
        row.attempt, row.replans, row.pr_rounds = item.attempt, item.replans, item.pr_rounds
        row.infra_failures, row.pr_id = item.infra_failures, item.pr_id
        row.data, row.usage, row.updated_at = dict(item.data), item.usage.to_dict(), _now()

    def save(self, item: Item, events: Sequence[EventInput] = (), at: Item | None = None) -> None:
        with self._session() as s, s.begin():
            self._write_item(s, item)
            for ev in events:
                s.add(_event_row(ev, at or item))

    def commit_step(self, item: Item, decisions: list[tuple[Decision, dict[str, Any]]],
                    usage: Usage, day: date, labels: list[LabelInput],
                    events: Sequence[EventInput] = (), at: Item | None = None) -> None:
        """One step, one transaction: item state, decisions, labels, usage and the events
        describing the step. `at` is the item as it was when the step ran (event context)."""
        with self._session() as s, s.begin():
            self._write_item(s, item)
            for d, state in decisions:
                s.add(DecisionRow(item_id=item.id, gate=d.gate, question=d.question,
                                  answer=d.answer, probs=d.probs, raw_probs=d.raw_probs,
                                  confidence=d.confidence, shadow=d.shadow,
                                  actionable=d.actionable, state=state))
            for lab in labels:
                s.add(self._label_row(lab))
            for ev in events:
                s.add(_event_row(ev, at or item))
            self._add_usage(s, day, usage)

    # decisions & labels ----------------------------------------------------
    def decisions_for(self, item_id: int, gate: str | None = None) -> list[Decision]:
        with self._session() as s:
            q = select(DecisionRow).where(DecisionRow.item_id == item_id)
            if gate is not None:
                q = q.where(DecisionRow.gate == gate)
            return [_to_decision(r) for r in s.scalars(q.order_by(DecisionRow.id))]

    def unlabeled_decisions(self, gate: str,
                            limit: int) -> list[tuple[int, Decision, dict[str, Any]]]:
        with self._session() as s:
            labeled = select(LabelRow.decision_id).where(
                LabelRow.decision_id.is_not(None)).scalar_subquery()
            q = (select(DecisionRow).where(DecisionRow.gate == gate)
                 .where(DecisionRow.id.not_in(labeled)).order_by(DecisionRow.id).limit(limit))
            return [(r.id, _to_decision(r), dict(r.state)) for r in s.scalars(q)]

    @staticmethod
    def _label_row(lab: LabelInput) -> LabelRow:
        return LabelRow(gate=lab.gate, question=lab.question, raw_probs=lab.raw_probs,
                        gold=lab.gold, source=lab.source, decision_id=lab.decision_id)

    def add_label(self, label: LabelInput) -> None:
        with self._session() as s, s.begin():
            s.add(self._label_row(label))

    def labels(self, gate: str, question: str) -> list[tuple[dict[str, float], str]]:
        with self._session() as s:
            q = (select(LabelRow).where(LabelRow.gate == gate, LabelRow.question == question)
                 .order_by(LabelRow.id))
            return [(dict(r.raw_probs), r.gold) for r in s.scalars(q)]

    def labeled_decisions(self, gate: str, question: str) -> list[tuple[str, str]]:
        """(logged answer, human gold) for every label tied to a logged decision."""
        with self._session() as s:
            q = (select(DecisionRow.answer, LabelRow.gold)
                 .join(LabelRow, LabelRow.decision_id == DecisionRow.id)
                 .where(DecisionRow.gate == gate, DecisionRow.question == question)
                 .order_by(LabelRow.id))
            return [(str(a), str(g)) for a, g in s.execute(q).tuples()]

    def unlabeled_decisions_for_items(
        self, gate: str, item_ids: set[int], limit: int
    ) -> list[tuple[int, int, Decision, dict[str, Any]]]:
        if not item_ids:
            return []
        with self._session() as s:
            labeled = select(LabelRow.decision_id).where(
                LabelRow.decision_id.is_not(None)).scalar_subquery()
            q = (select(DecisionRow).where(DecisionRow.gate == gate)
                 .where(DecisionRow.item_id.in_(item_ids))
                 .where(DecisionRow.id.not_in(labeled))
                 .order_by(DecisionRow.item_id, DecisionRow.id).limit(limit))
            return [(r.id, r.item_id, _to_decision(r), dict(r.state)) for r in s.scalars(q)]

    def decision_states(self, item_id: int, gate: str) -> list[dict[str, Any]]:
        with self._session() as s:
            q = (select(DecisionRow).where(DecisionRow.item_id == item_id,
                                           DecisionRow.gate == gate).order_by(DecisionRow.id))
            return [dict(r.state) for r in s.scalars(q)]

    def decisions_with_ts(self, item_id: int) -> list[tuple[datetime, Decision]]:
        with self._session() as s:
            q = select(DecisionRow).where(DecisionRow.item_id == item_id).order_by(DecisionRow.id)
            return [(_aware(r.created_at), _to_decision(r)) for r in s.scalars(q)]

    # events ------------------------------------------------------------------------------------
    def add_event(self, kind: str, payload: dict[str, Any] | None = None, *,
                  item: Item | None = None, ts: datetime | None = None) -> None:
        with self._session() as s, s.begin():
            s.add(_event_row(EventInput(kind, payload or {}), item, ts))

    def events_for(self, item_id: int) -> list[Event]:
        with self._session() as s:
            q = select(EventRow).where(EventRow.item_id == item_id).order_by(EventRow.id)
            return [_to_event(r) for r in s.scalars(q)]

    def events_since(self, since: datetime, kinds: Iterable[str] | None = None) -> list[Event]:
        with self._session() as s:
            q = select(EventRow).where(EventRow.ts >= _db_ts(since))
            if kinds is not None:
                q = q.where(EventRow.kind.in_(list(kinds)))
            return [_to_event(r) for r in s.scalars(q.order_by(EventRow.id))]

    def last_event_ts(self, item_id: int) -> datetime | None:
        with self._session() as s:
            q = (select(EventRow.ts).where(EventRow.item_id == item_id)
                 .order_by(EventRow.id.desc()).limit(1))
            ts = s.scalars(q).first()
            return _aware(ts) if ts is not None else None

    def abandoned_item_ids(self) -> set[int]:
        with self._session() as s:
            q = select(EventRow).where(EventRow.kind == "outcome")
            return {r.item_id for r in s.scalars(q)
                    if r.item_id is not None and (r.payload or {}).get("result") == "abandoned"}

    # calibration -----------------------------------------------------------
    def calibration(self, gate: str, question: str) -> Calibration | None:
        with self._session() as s:
            r = s.get(CalibrationRow, f"{gate}.{question}")
            if r is None:
                return None
            mode: Literal["shadow", "active"] = "active" if r.mode == "active" else "shadow"
            return Calibration(r.temperature, r.threshold, mode, r.ece, r.n)

    def set_calibration(self, gate: str, question: str, cal: Calibration) -> None:
        with self._session() as s, s.begin():
            s.merge(CalibrationRow(key=f"{gate}.{question}", temperature=cal.temperature,
                                   threshold=cal.threshold, mode=cal.mode, ece=cal.ece, n=cal.n))

    # flags & usage ---------------------------------------------------------
    def get_flag(self, key: str) -> str | None:
        with self._session() as s:
            r = s.get(FlagRow, key)
            return r.value if r else None

    def set_flag(self, key: str, value: str | None) -> None:
        with self._session() as s, s.begin():
            r = s.get(FlagRow, key)
            if value is None:
                if r is not None:
                    s.delete(r)
            elif r is None:
                s.add(FlagRow(key=key, value=value))
            else:
                r.value = value

    @staticmethod
    def _add_usage(s: Session, day: date, usage: Usage) -> None:
        r = s.get(DailyUsageRow, day.isoformat())
        if r is None:
            r = DailyUsageRow(day=day.isoformat(), turns=0, input_tokens=0, output_tokens=0)
            s.add(r)
        r.turns += usage.turns
        r.input_tokens += usage.input_tokens
        r.output_tokens += usage.output_tokens

    def add_daily_usage(self, day: date, usage: Usage) -> None:
        with self._session() as s, s.begin():
            self._add_usage(s, day, usage)

    def daily_usage(self, day: date) -> Usage:
        with self._session() as s:
            r = s.get(DailyUsageRow, day.isoformat())
            return Usage(r.turns, r.input_tokens, r.output_tokens) if r else Usage()
