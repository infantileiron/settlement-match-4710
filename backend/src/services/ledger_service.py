from threading import Lock
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from ..models.batch import StatementBatch
from ..models.item import SettlementItem
from ..models.ledger import (
    BalanceLedger,
    LedgerAllocation,
    LedgerBalance,
    LedgerCommission,
    LedgerPeriod,
    LedgerReceipt,
    LedgerVariance,
)
from ..models.common import now
from .ledger_engine import build_plan, next_period, validate_period

ITEM_FIELDS = ["external_id", "side", "party", "reference", "amount_cents", "currency", "booking_date"]

# Serialises recalculation of one ledger inside this process; the row lock and
# unique keys cover other workers.  Both together keep repeated/concurrent
# recalculation idempotent even on engines that ignore SELECT ... FOR UPDATE.
_LEDGER_LOCKS: dict[str, Lock] = {}
_LEDGER_LOCKS_GUARD = Lock()


def _ledger_lock(ledger_id):
    with _LEDGER_LOCKS_GUARD:
        return _LEDGER_LOCKS.setdefault(ledger_id, Lock())


def _require(db, ledger_id, lock=False):
    statement = select(BalanceLedger).where(BalanceLedger.id == ledger_id)
    if lock:
        statement = statement.with_for_update()
    row = db.scalar(statement)
    if row is None:
        raise HTTPException(404, "余额台账不存在")
    return row


def _batch_items(db, batch_id):
    rows = db.scalars(
        select(SettlementItem)
        .where(SettlementItem.batch_id == batch_id)
        .order_by(SettlementItem.external_id)
    ).all()
    return [{key: getattr(row, key) for key in ITEM_FIELDS} for row in rows]


def _frozen(db, ledger_id):
    closed_periods = db.scalars(
        select(LedgerPeriod.period).where(
            LedgerPeriod.ledger_id == ledger_id, LedgerPeriod.status == "closed"
        )
    ).all()
    balances = db.scalars(
        select(LedgerBalance).where(
            LedgerBalance.ledger_id == ledger_id, LedgerBalance.period.in_(closed_periods)
        )
    ).all()
    allocations = db.scalars(
        select(LedgerAllocation).where(
            LedgerAllocation.ledger_id == ledger_id, LedgerAllocation.period.in_(closed_periods)
        )
    ).all()
    return list(closed_periods), balances, allocations


def _clear_open(db, ledger_id, closed_through):
    for model in (LedgerBalance, LedgerAllocation):
        statement = db.query(model).filter(model.ledger_id == ledger_id)
        if closed_through:
            statement = statement.filter(model.period > closed_through)
        statement.delete(synchronize_session=False)


def _replace_status(db, ledger_id, plan):
    for model in (LedgerCommission, LedgerReceipt, LedgerVariance):
        db.query(model).filter(model.ledger_id == ledger_id).delete(synchronize_session=False)
    for row in plan["commission_status"]:
        db.add(
            LedgerCommission(
                id=str(uuid4()),
                ledger_id=ledger_id,
                commission_external_id=row["commission_external_id"],
                party=row["party"],
                reference=row["reference"],
                booking_date=row["booking_date"],
                original_cents=row["original_cents"],
                allocated_cents=row["allocated_cents"],
                closing_cents=row["closing_cents"],
                unexplained_cents=row["original_cents"] - row["allocated_cents"] - row["closing_cents"],
                equation_ok=row["equation_ok"],
            )
        )
    for row in plan["receipt_status"]:
        db.add(
            LedgerReceipt(
                id=str(uuid4()),
                ledger_id=ledger_id,
                receipt_external_id=row["receipt_external_id"],
                party=row["party"],
                reference=row["reference"],
                booking_date=row["booking_date"],
                amount_cents=row["amount_cents"],
                allocated_cents=row["allocated_cents"],
                remaining_cents=row["remaining_cents"],
                status=row["status"],
            )
        )
    for row in plan["variances"]:
        db.add(LedgerVariance(id=str(uuid4()), ledger_id=ledger_id, **row))


def _write_plan(db, ledger, plan):
    closed_through = ledger.closed_through or ""
    _clear_open(db, ledger.id, closed_through)

    existing_periods = {
        row.period: row
        for row in db.scalars(select(LedgerPeriod).where(LedgerPeriod.ledger_id == ledger.id)).all()
    }
    planned = {row["period"] for row in plan["periods"]}
    for period, stored in existing_periods.items():
        if period not in planned and stored.status == "open":
            db.delete(stored)
    for row in plan["periods"]:
        stored = existing_periods.get(row["period"])
        if stored is None:
            db.add(
                LedgerPeriod(
                    id=str(uuid4()),
                    ledger_id=ledger.id,
                    period=row["period"],
                    status="closed" if row["period"] <= closed_through else "open",
                    opening_cents=row["opening_cents"],
                    new_cents=row["new_cents"],
                    applied_cents=row["applied_cents"],
                    closing_cents=row["closing_cents"],
                    commission_count=row["commission_count"],
                    allocation_count=row["allocation_count"],
                )
            )
        elif stored.status != "closed":
            stored.opening_cents = row["opening_cents"]
            stored.new_cents = row["new_cents"]
            stored.applied_cents = row["applied_cents"]
            stored.closing_cents = row["closing_cents"]
            stored.commission_count = row["commission_count"]
            stored.allocation_count = row["allocation_count"]

    for row in plan["balances"]:
        db.add(LedgerBalance(id=str(uuid4()), ledger_id=ledger.id, **row))
    for row in plan["allocations"]:
        db.add(LedgerAllocation(id=str(uuid4()), ledger_id=ledger.id, **row))
    _replace_status(db, ledger.id, plan)

    ledger.through_period = plan["through_period"]
    ledger.checks = plan["checks"]
    ledger.recalculated_at = now()


def recalculate(db, ledger_id, through_period=None):
    """Recompute a ledger for the open periods.

    The ledger row is locked so two operators cannot interleave delete/insert
    on the same open periods.  If the storage engine cannot honour that lock
    (or a duplicate slips through anyway), the unique keys reject the second
    write and the whole transaction is retried once; retrying deletes and
    rewrites the same deterministic rows, so repeated or concurrent calls
    converge on one balance set instead of adding a second one.
    """

    for attempt in range(2):
        try:
            with _ledger_lock(ledger_id):
                return _recalculate_once(db, ledger_id, through_period)
        except IntegrityError:
            db.rollback()
            if attempt:
                raise HTTPException(409, "余额台账正被其他操作更新，请稍后重试")
        except OperationalError as error:
            db.rollback()
            if attempt:
                raise HTTPException(409, f"余额台账暂时无法锁定：{error}")
    raise HTTPException(409, "余额台账重算失败，请重试")


def _recalculate_once(db, ledger_id, through_period=None):
    ledger = _require(db, ledger_id, lock=True)
    items = _batch_items(db, ledger.batch_id)
    commissions = [row for row in items if row["side"] == "milestone"]
    receipts = [row for row in items if row["side"] == "receipt"]
    if not commissions or not receipts:
        raise HTTPException(409, "批次需同时存在委托里程碑和入账凭证")
    closed_periods, balances, allocations = _frozen(db, ledger.id)
    try:
        plan = build_plan(
            commissions,
            receipts,
            closed_periods=closed_periods,
            frozen_balances=[
                {
                    "period": row.period,
                    "commission_external_id": row.commission_external_id,
                    "closing_cents": row.closing_cents,
                }
                for row in balances
            ],
            frozen_allocations=[
                {
                    "period": row.period,
                    "commission_external_id": row.commission_external_id,
                    "receipt_external_id": row.receipt_external_id,
                    "amount_cents": row.amount_cents,
                }
                for row in allocations
            ],
            through_period=through_period or ledger.through_period or None,
        )
    except ValueError as error:
        raise HTTPException(422, str(error))
    _write_plan(db, ledger, plan)
    db.commit()
    db.refresh(ledger)
    return ledger


def recalculate_for_batch(db, batch_id):
    """Refresh every active ledger in a batch after a backfill.

    A backfill is saved even when a ledger cannot yet be rebuilt (for example a
    batch with no milestones); the next explicit recalculation surfaces it.
    """

    import logging

    ledgers = db.scalars(
        select(BalanceLedger).where(
            BalanceLedger.batch_id == batch_id, BalanceLedger.status == "active"
        )
    ).all()
    for ledger in ledgers:
        try:
            recalculate(db, ledger.id)
        except HTTPException as error:
            db.rollback()
            logging.warning("Ledger %s not recalculated after backfill: %s", ledger.id, error.detail)


def create(db, body, user):
    if db.get(StatementBatch, body.batch_id) is None:
        raise HTTPException(422, "对账批次不存在")
    existing = db.scalar(
        select(BalanceLedger).where(
            BalanceLedger.batch_id == body.batch_id, BalanceLedger.name == body.name
        )
    )
    if existing:
        raise HTTPException(409, "同批次余额台账名称已存在")
    ledger = BalanceLedger(
        id=str(uuid4()),
        batch_id=body.batch_id,
        name=body.name,
        description=body.description,
        status="active",
        through_period="",
        closed_through="",
        checks={},
        created_by=user.email,
    )
    db.add(ledger)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "同批次余额台账名称已存在")
    db.refresh(ledger)
    try:
        recalculate(db, ledger.id, body.through_period)
    except HTTPException:
        delete_ledger(db, ledger.id)
        raise
    return ledger


def close_period(db, ledger_id, period, user):
    validate_period(period)
    ledger = _require(db, ledger_id, lock=True)
    target = db.scalar(
        select(LedgerPeriod).where(
            LedgerPeriod.ledger_id == ledger_id, LedgerPeriod.period == period
        )
    )
    if target is None:
        raise HTTPException(404, "账期不存在，请先重算余额台账")
    if target.status == "closed":
        return ledger, target, False
    earliest = db.scalar(
        select(LedgerPeriod.period)
        .where(LedgerPeriod.ledger_id == ledger_id)
        .order_by(LedgerPeriod.period)
        .limit(1)
    )
    expected = next_period(ledger.closed_through) if ledger.closed_through else earliest
    if period != expected:
        raise HTTPException(409, f"请按月份顺序关账，下一可关账期为{expected}")
    later_open = db.scalar(
        select(LedgerPeriod).where(
            LedgerPeriod.ledger_id == ledger_id,
            LedgerPeriod.period < period,
            LedgerPeriod.status == "open",
        )
    )
    if later_open:
        raise HTTPException(409, "存在更早未关账月份")
    target.status = "closed"
    target.closed_by = user.email
    target.closed_at = now()
    ledger.closed_through = period
    db.commit()
    db.refresh(ledger)
    return ledger, target, True


def delete_ledger(db, ledger_id):
    ledger = _require(db, ledger_id)
    for model in (LedgerPeriod, LedgerBalance, LedgerAllocation, LedgerVariance, LedgerCommission, LedgerReceipt):
        db.query(model).filter(model.ledger_id == ledger_id).delete(synchronize_session=False)
    db.delete(ledger)
    db.commit()


def month_view(db, ledger_id, period):
    validate_period(period)
    ledger = _require(db, ledger_id)
    month = db.scalar(
        select(LedgerPeriod).where(
            LedgerPeriod.ledger_id == ledger_id, LedgerPeriod.period == period
        )
    )
    if month is None:
        raise HTTPException(404, "该月份不在本台账范围内")
    balances = db.scalars(
        select(LedgerBalance)
        .where(LedgerBalance.ledger_id == ledger_id, LedgerBalance.period == period)
        .order_by(LedgerBalance.commission_external_id)
    ).all()
    allocations = db.scalars(
        select(LedgerAllocation)
        .where(LedgerAllocation.ledger_id == ledger_id, LedgerAllocation.period == period)
        .order_by(LedgerAllocation.commission_external_id, LedgerAllocation.sequence)
    ).all()
    prior = db.scalars(
        select(LedgerAllocation)
        .where(
            LedgerAllocation.ledger_id == ledger_id,
            LedgerAllocation.period < period,
            LedgerAllocation.commission_external_id.in_([row.commission_external_id for row in balances]),
        )
        .order_by(LedgerAllocation.period, LedgerAllocation.sequence)
    ).all()
    receipts = {
        row.receipt_external_id: row
        for row in db.scalars(
            select(LedgerReceipt).where(LedgerReceipt.ledger_id == ledger_id)
        ).all()
    }
    variances = db.scalars(
        select(LedgerVariance)
        .where(LedgerVariance.ledger_id == ledger_id, LedgerVariance.period <= period)
        .order_by(LedgerVariance.period, LedgerVariance.entity_type, LedgerVariance.entity_id)
    ).all()
    return {
        "ledger": {
            "id": ledger.id,
            "name": ledger.name,
            "batch_id": ledger.batch_id,
            "closed_through": ledger.closed_through,
            "through_period": ledger.through_period,
            "checks": ledger.checks,
        },
        "period": {
            "period": month.period,
            "status": month.status,
            "opening_cents": month.opening_cents,
            "new_cents": month.new_cents,
            "applied_cents": month.applied_cents,
            "closing_cents": month.closing_cents,
            "commission_count": month.commission_count,
            "allocation_count": month.allocation_count,
            "closed_by": month.closed_by,
            "closed_at": month.closed_at.isoformat() + "Z" if month.closed_at else "",
        },
        "balances": [
            {
                **{key: getattr(row, key) for key in (
                    "commission_external_id", "party", "reference", "booking_date",
                    "original_cents", "opening_cents", "new_cents", "applied_cents",
                    "closing_cents", "applied_to_date_cents", "carried_from_period",
                )},
                "allocations": [
                    {
                        "receipt_external_id": item.receipt_external_id,
                        "amount_cents": item.amount_cents,
                        "sequence": item.sequence,
                        "source": item.source,
                        "receipt_booking_date": item.receipt_booking_date,
                        "receipt_amount_cents": receipts[item.receipt_external_id].amount_cents
                        if item.receipt_external_id in receipts else None,
                        "receipt_status": receipts[item.receipt_external_id].status
                        if item.receipt_external_id in receipts else "",
                    }
                    for item in allocations
                    if item.commission_external_id == row.commission_external_id
                ],
                "prior_allocations": [
                    {
                        "period": item.period,
                        "receipt_external_id": item.receipt_external_id,
                        "amount_cents": item.amount_cents,
                        "source": item.source,
                    }
                    for item in prior
                    if item.commission_external_id == row.commission_external_id
                ],
                "equation_ok": row.original_cents
                == row.applied_to_date_cents + row.closing_cents,
            }
            for row in balances
        ],
        "allocations": [
            {
                "period": row.period,
                "commission_external_id": row.commission_external_id,
                "receipt_external_id": row.receipt_external_id,
                "amount_cents": row.amount_cents,
                "sequence": row.sequence,
                "source": row.source,
            }
            for row in allocations
        ],
        "variances": [
            {
                "period": row.period,
                "entity_type": row.entity_type,
                "entity_id": row.entity_id,
                "kept_cents": row.kept_cents,
                "recomputed_cents": row.recomputed_cents,
                "delta_cents": row.delta_cents,
                "reason": row.reason,
            }
            for row in variances
        ],
    }


def summary(db, ledger_id):
    ledger = _require(db, ledger_id)
    periods = db.scalars(
        select(LedgerPeriod)
        .where(LedgerPeriod.ledger_id == ledger_id)
        .order_by(LedgerPeriod.period)
    ).all()
    commissions = db.scalars(
        select(LedgerCommission)
        .where(LedgerCommission.ledger_id == ledger_id)
        .order_by(LedgerCommission.commission_external_id)
    ).all()
    receipts = db.scalars(
        select(LedgerReceipt)
        .where(LedgerReceipt.ledger_id == ledger_id)
        .order_by(LedgerReceipt.receipt_external_id)
    ).all()
    variances = db.scalars(
        select(LedgerVariance)
        .where(LedgerVariance.ledger_id == ledger_id)
        .order_by(LedgerVariance.period)
    ).all()
    return {
        "id": ledger.id,
        "name": ledger.name,
        "description": ledger.description,
        "batch_id": ledger.batch_id,
        "status": ledger.status,
        "through_period": ledger.through_period,
        "closed_through": ledger.closed_through,
        "checks": ledger.checks,
        "created_by": ledger.created_by,
        "created_at": ledger.created_at.isoformat() + "Z",
        "recalculated_at": ledger.recalculated_at.isoformat() + "Z" if ledger.recalculated_at else "",
        "periods": [
            {
                "period": row.period,
                "status": row.status,
                "opening_cents": row.opening_cents,
                "new_cents": row.new_cents,
                "applied_cents": row.applied_cents,
                "closing_cents": row.closing_cents,
                "commission_count": row.commission_count,
                "allocation_count": row.allocation_count,
                "closed_by": row.closed_by,
                "closed_at": row.closed_at.isoformat() + "Z" if row.closed_at else "",
            }
            for row in periods
        ],
        "outstanding_commissions": [
            {
                "commission_external_id": row.commission_external_id,
                "party": row.party,
                "reference": row.reference,
                "booking_date": row.booking_date,
                "original_cents": row.original_cents,
                "allocated_cents": row.allocated_cents,
                "closing_cents": row.closing_cents,
                "unexplained_cents": row.unexplained_cents,
                "equation_ok": row.equation_ok,
            }
            for row in commissions
            if row.closing_cents > 0
        ],
        "receipts": [
            {
                "receipt_external_id": row.receipt_external_id,
                "party": row.party,
                "reference": row.reference,
                "booking_date": row.booking_date,
                "amount_cents": row.amount_cents,
                "allocated_cents": row.allocated_cents,
                "remaining_cents": row.remaining_cents,
                "status": row.status,
            }
            for row in receipts
        ],
        "variances": [
            {
                "period": row.period,
                "entity_type": row.entity_type,
                "entity_id": row.entity_id,
                "kept_cents": row.kept_cents,
                "recomputed_cents": row.recomputed_cents,
                "delta_cents": row.delta_cents,
                "reason": row.reason,
            }
            for row in variances
        ],
    }
