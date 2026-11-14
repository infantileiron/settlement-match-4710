import os
import sys

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config.database import Base
from src.models import StatementBatch, SettlementItem, User
from src.models.ledger import (  # noqa: F401
    BalanceLedger,
    LedgerAllocation,
    LedgerBalance,
    LedgerCommission,
    LedgerPeriod,
    LedgerReceipt,
    LedgerVariance,
)
from src.schemas.ledger import LedgerCreate, PeriodClose
from src.services import ledger_service
from src.services.ledger_engine import build_plan


def item(ident, side, party, reference, amount, day):
    return {
        "external_id": ident,
        "side": side,
        "party": party,
        "reference": reference,
        "amount_cents": amount,
        "currency": "CNY",
        "booking_date": day,
    }


def commission(ident="M1", amount=10000, day="2026-10-05", party="客户甲", reference="JOB-1"):
    return item(ident, "milestone", party, reference, amount, day)


def receipt(ident, amount, day, party="客户甲", reference="JOB-1"):
    return item(ident, "receipt", party, reference, amount, day)


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def register_collation(connection, record):
        connection.create_collation("utf8mb4_bin", lambda left, right: (left > right) - (left < right))

    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as session:
        user = User(id="user-admin", email="admin@settlement.demo", password_hash="x", role="admin")
        session.add(user)
        session.add(StatementBatch(id="batch", name="测试批次", description=""))
        session.commit()
        yield session


def add_items(db, rows):
    for row in rows:
        db.add(SettlementItem(id="item-" + row["external_id"], batch_id="batch", **row))
    db.commit()


def make_ledger(db, rows, through=None):
    add_items(db, rows)
    return ledger_service.create(
        db,
        LedgerCreate(batch_id="batch", name="台账", description="", through_period=through),
        db.get(User, "user-admin"),
    )


def test_installments_do_not_need_whole_receipt_match():
    plan = build_plan(
        [commission(amount=10000)],
        [
            receipt("R1", 3000, "2026-10-20"),
            receipt("R2", 3000, "2026-11-10"),
            receipt("R3", 4000, "2026-12-05"),
        ],
    )
    assert [(row["period"], row["applied_cents"], row["closing_cents"]) for row in plan["periods"]] == [
        ("2026-10", 3000, 7000),
        ("2026-11", 3000, 4000),
        ("2026-12", 4000, 0),
    ]
    assert plan["checks"]["commissions"]["ok"]
    assert plan["checks"]["receipts"]["ok"]
    assert plan["commission_status"][0]["allocated_cents"] == 10000
    assert plan["commission_status"][0]["closing_cents"] == 0
    assert [row["amount_cents"] for row in plan["allocations"]] == [3000, 3000, 4000]


def test_receipt_larger_than_commission_leaves_receipt_remainder():
    plan = build_plan(
        [commission(amount=5000)],
        [receipt("R1", 8000, "2026-11-02")],
    )
    status = plan["receipt_status"][0]
    assert status["allocated_cents"] == 5000
    assert status["remaining_cents"] == 3000
    assert status["status"] == "unallocated"
    assert plan["commission_status"][0]["closing_cents"] == 0
    assert plan["checks"]["receipts"]["ok"]


def test_multiple_commissions_grouped_receipt_fifo():
    plan = build_plan(
        [
            commission("M1", 3000, "2026-10-01", reference="JOB-A"),
            commission("M2", 2000, "2026-10-02", reference="JOB-A"),
        ],
        [receipt("R1", 4500, "2026-11-01", reference="JOB-A")],
    )
    assert plan["commission_status"][0]["allocated_cents"] == 3000
    assert plan["commission_status"][1]["allocated_cents"] == 1500
    assert plan["commission_status"][1]["closing_cents"] == 500
    assert plan["checks"]["commissions"]["original_cents"] == 5000


def test_closed_period_is_kept_and_variance_listed():
    base_rows = [
        commission("M1", 10000, "2026-10-05"),
        receipt("R1", 3000, "2026-10-20"),
        receipt("R2", 3000, "2026-11-10"),
    ]
    initial = build_plan([base_rows[0]], base_rows[1:])
    assert initial["periods"][0]["closing_cents"] == 7000
    frozen_balances = [
        {
            "period": row["period"],
            "commission_external_id": row["commission_external_id"],
            "closing_cents": row["closing_cents"],
        }
        for row in initial["balances"]
    ]
    frozen_allocations = [
        {
            "period": row["period"],
            "commission_external_id": row["commission_external_id"],
            "receipt_external_id": row["receipt_external_id"],
            "amount_cents": row["amount_cents"],
        }
        for row in initial["allocations"]
    ]
    # A prior-period receipt arrives after October was closed.
    plan = build_plan(
        [commission("M1", 10000, "2026-10-05")],
        [receipt("R1", 3000, "2026-10-20"), receipt("R0", 1000, "2026-10-25"), receipt("R2", 3000, "2026-11-10")],
        closed_periods=["2026-10"],
        frozen_balances=frozen_balances,
        frozen_allocations=frozen_allocations,
    )
    assert plan["periods"][0]["period"] == "2026-11"
    assert plan["periods"][0]["opening_cents"] == 7000
    assert plan["allocations"][0]["receipt_external_id"] == "R0"
    assert plan["allocations"][0]["source"] == "backfill_receipt"
    reasons = {(row["period"], row["entity_type"], row["entity_id"]): row for row in plan["variances"]}
    assert ("2026-10", "commission", "M1") in reasons
    assert reasons[("2026-10", "commission", "M1")]["kept_cents"] == 7000
    assert reasons[("2026-10", "commission", "M1")]["recomputed_cents"] == 6000
    assert reasons[("2026-10", "commission", "M1")]["delta_cents"] == -1000
    assert plan["checks"]["commissions"]["ok"]


def test_repeated_calculation_is_idempotent(db):
    ledger = make_ledger(
        db,
        [
            commission("M1", 10000, "2026-10-05"),
            receipt("R1", 3000, "2026-10-20"),
            receipt("R2", 3000, "2026-11-10"),
        ],
    )
    first = ledger_service.summary(db, ledger.id)
    ledger_service.recalculate(db, ledger.id)
    ledger_service.recalculate(db, ledger.id)
    second = ledger_service.summary(db, ledger.id)
    assert [(row["period"], row["closing_cents"]) for row in first["periods"]] == [
        (row["period"], row["closing_cents"]) for row in second["periods"]
    ]
    assert len(second["periods"]) == 2
    assert db.query(LedgerAllocation).count() == 2
    assert db.query(LedgerBalance).count() == 2


def test_closing_then_backfill_keeps_closed_month_and_reports_variance(db):
    ledger = make_ledger(
        db,
        [
            commission("M1", 10000, "2026-10-05"),
            receipt("R1", 3000, "2026-10-20"),
        ],
    )
    ledger, period, changed = ledger_service.close_period(db, ledger.id, "2026-10", db.get(User, "user-admin"))
    assert changed and period.status == "closed" and ledger.closed_through == "2026-10"

    add_items(
        db,
        [
            receipt("R0", 1000, "2026-10-28"),
            receipt("R2", 3000, "2026-11-12"),
        ],
    )
    ledger_service.recalculate_for_batch(db, "batch")
    summary = ledger_service.summary(db, ledger.id)
    october = next(row for row in summary["periods"] if row["period"] == "2026-10")
    november = next(row for row in summary["periods"] if row["period"] == "2026-11")
    assert october["status"] == "closed"
    assert october["closing_cents"] == 7000
    assert october["applied_cents"] == 3000
    assert november["opening_cents"] == 7000
    assert november["applied_cents"] == 4000
    assert november["closing_cents"] == 3000
    variance = next(row for row in summary["variances"] if row["period"] == "2026-10")
    assert variance["kept_cents"] == 7000
    assert variance["recomputed_cents"] == 6000
    assert variance["delta_cents"] == -1000
    assert summary["checks"]["commissions"]["ok"]


def test_month_view_traces_each_balance_and_prior_allocations(db):
    ledger = make_ledger(
        db,
        [
            commission("M1", 10000, "2026-10-05"),
            receipt("R1", 3000, "2026-10-20"),
            receipt("R2", 3000, "2026-11-10"),
        ],
    )
    view = ledger_service.month_view(db, ledger.id, "2026-11")
    row = view["balances"][0]
    assert row["opening_cents"] == 7000
    assert row["closing_cents"] == 4000
    assert len(row["prior_allocations"]) == 1
    assert row["prior_allocations"][0]["period"] == "2026-10"
    assert [(item["receipt_external_id"], item["amount_cents"]) for item in row["allocations"]] == [("R2", 3000)]
    assert row["equation_ok"]


def test_close_must_be_sequential_and_idempotent(db):
    ledger = make_ledger(
        db,
        [
            commission("M1", 10000, "2026-10-05"),
            receipt("R1", 3000, "2026-10-20"),
            receipt("R2", 3000, "2026-11-10"),
        ],
    )
    user = db.get(User, "user-admin")
    with pytest.raises(Exception) as error:
        ledger_service.close_period(db, ledger.id, "2026-11", user)
    assert getattr(error.value, "status_code", None) == 409
    ledger_service.close_period(db, ledger.id, "2026-10", user)
    _, period, changed = ledger_service.close_period(db, ledger.id, "2026-10", user)
    assert changed is False and period.status == "closed"
    ledger_service.close_period(db, ledger.id, "2026-11", user)
    summary = ledger_service.summary(db, ledger.id)
    assert summary["closed_through"] == "2026-11"


def test_same_party_different_reference_is_not_offset(db):
    ledger = make_ledger(
        db,
        [
            commission("M1", 5000, "2026-10-05", reference="JOB-1"),
            commission("M2", 5000, "2026-10-06", reference="JOB-2"),
            receipt("R1", 5000, "2026-10-20", reference="JOB-2"),
        ],
    )
    summary = ledger_service.summary(db, ledger.id)
    by_id = {row["commission_external_id"]: row for row in summary["outstanding_commissions"]}
    assert by_id["M1"]["closing_cents"] == 5000
    assert "M2" not in by_id
    assert summary["periods"][0]["closing_cents"] == 5000


def test_receipt_carried_from_closed_period_is_tagged(db):
    ledger = make_ledger(
        db,
        [
            commission("M1", 10000, "2026-10-05"),
            receipt("R1", 8000, "2026-10-20"),
            receipt("R2", 3000, "2026-11-10"),
        ],
    )
    user = db.get(User, "user-admin")
    ledger_service.close_period(db, ledger.id, "2026-10", user)
    view = ledger_service.month_view(db, ledger.id, "2026-11")
    row = view["balances"][0]
    assert row["opening_cents"] == 2000
    assert row["allocations"][0]["receipt_external_id"] == "R2"
    assert row["allocations"][0]["amount_cents"] == 2000
    detail = next(item for item in ledger_service.summary(db, ledger.id)["receipts"] if item["receipt_external_id"] == "R2")
    assert detail["remaining_cents"] == 1000


def test_through_period_never_truncates_newer_entries(db):
    ledger = make_ledger(
        db,
        [
            commission("M1", 10000, "2026-10-05"),
            receipt("R1", 3000, "2026-10-20"),
            receipt("R2", 3000, "2026-12-10"),
        ],
        through="2026-10",
    )
    summary = ledger_service.summary(db, ledger.id)
    assert summary["through_period"] == "2026-12"
    december = next(row for row in summary["periods"] if row["period"] == "2026-12")
    assert december["applied_cents"] == 3000


def test_closed_books_cross_foot_difference_is_reported_not_absorbed():
    plan = build_plan(
        [commission("M1", 10000, "2026-10-05")],
        [receipt("R1", 5000, "2026-10-20"), receipt("R2", 2000, "2026-11-10")],
        closed_periods=["2026-10"],
        frozen_balances=[{"period": "2026-10", "commission_external_id": "M1", "closing_cents": 7000}],
        frozen_allocations=[
            {"period": "2026-10", "commission_external_id": "M1", "receipt_external_id": "R1", "amount_cents": 5000}
        ],
        through_period="2026-11",
    )
    # The kept closing balance wins and the equation still holds; the 2000c
    # mismatch between balances and allocation detail is surfaced separately.
    assert plan["checks"]["commissions"]["ok"]
    assert plan["checks"]["commissions"]["closed_cross_foot_cents"] == 2000
    assert plan["commission_status"][0]["allocated_cents"] == 5000
    assert plan["commission_status"][0]["closing_cents"] == 5000
    assert plan["periods"][0]["opening_cents"] == 7000


def test_consistent_closed_books_have_zero_cross_foot():
    first = build_plan(
        [commission("M1", 10000, "2026-10-05")],
        [receipt("R1", 5000, "2026-10-20")],
        through_period="2026-10",
    )
    plan = build_plan(
        [commission("M1", 10000, "2026-10-05")],
        [receipt("R1", 5000, "2026-10-20"), receipt("R2", 2000, "2026-11-10")],
        closed_periods=["2026-10"],
        frozen_balances=[
            {"period": row["period"], "commission_external_id": row["commission_external_id"], "closing_cents": row["closing_cents"]}
            for row in first["balances"]
        ],
        frozen_allocations=[
            {"period": row["period"], "commission_external_id": row["commission_external_id"], "receipt_external_id": row["receipt_external_id"], "amount_cents": row["amount_cents"]}
            for row in first["allocations"]
        ],
        through_period="2026-11",
    )
    assert plan["checks"]["commissions"]["closed_cross_foot_cents"] == 0
    assert plan["variances"] == []
    assert plan["commission_status"][0]["allocated_cents"] == 7000
