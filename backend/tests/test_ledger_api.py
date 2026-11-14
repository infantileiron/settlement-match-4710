import os
import sys
import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config.database import Base, get_db
import src.config.database as database_module
from src.main import app
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
from src.utils.security import hash_password


@pytest.fixture()
def client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def register_collation(connection, record):
        connection.create_collation("utf8mb4_bin", lambda left, right: (left > right) - (left < right))

    Base.metadata.create_all(engine)
    database_module.engine = engine
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    with Session() as db:
        for role in ("admin", "analyst", "viewer"):
            db.add(
                User(
                    id="user-" + role,
                    email=role + "@settlement.demo",
                    role=role,
                    password_hash=hash_password("MatchDemo2026!"),
                )
            )
        db.add(StatementBatch(id="batch", name="API批次", description=""))
        rows = [
            ("M1", "milestone", "客户甲", "JOB-1", 10000, "2026-10-05"),
            ("M2", "milestone", "客户乙", "JOB-2", 6000, "2026-10-09"),
            ("R1", "receipt", "客户甲", "JOB-1", 3000, "2026-10-20"),
            ("R2", "receipt", "客户甲", "JOB-1", 3000, "2026-11-10"),
            ("R3", "receipt", "客户乙", "JOB-2", 6000, "2026-11-15"),
        ]
        for ident, side, party, reference, amount, day in rows:
            db.add(
                SettlementItem(
                    id="item-" + ident,
                    batch_id="batch",
                    external_id=ident,
                    side=side,
                    party=party,
                    reference=reference,
                    amount_cents=amount,
                    currency="CNY",
                    booking_date=day,
                )
            )
        db.commit()

    def override():
        with Session() as db:
            yield db

    app.dependency_overrides[get_db] = override
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def login(client, email):
    response = client.post("/auth/login", json={"email": email, "password": "MatchDemo2026!"})
    assert response.status_code == 200, response.text
    return {"Authorization": "Bearer " + response.json()["token"]}


def create(client, headers, name="台账", through=None):
    return client.post(
        "/ledgers",
        headers=headers,
        json={"batch_id": "batch", "name": name, "description": "", "through_period": through},
    )


def test_viewer_cannot_write(client):
    viewer = login(client, "viewer@settlement.demo")
    assert create(client, viewer).status_code == 403


def test_ledger_lifecycle(client):
    analyst = login(client, "analyst@settlement.demo")
    admin = login(client, "admin@settlement.demo")

    created = create(client, analyst, through="2026-11")
    assert created.status_code == 201, created.text
    ledger = created.json()
    assert [row["period"] for row in ledger["periods"]] == ["2026-10", "2026-11"]
    assert ledger["checks"]["commissions"]["ok"] and ledger["checks"]["receipts"]["ok"]
    assert ledger["checks"]["commissions"]["original_cents"] == 16000
    assert ledger["checks"]["commissions"]["closing_cents"] == 4000

    duplicate = create(client, analyst)
    assert duplicate.status_code == 409

    listing = client.get("/ledgers", headers=analyst)
    assert len(listing.json()) == 1

    month = client.get("/ledgers/%s/months/2026-11" % ledger["id"], headers=analyst)
    assert month.status_code == 200, month.text
    body = month.json()
    by_id = {row["commission_external_id"]: row for row in body["balances"]}
    assert by_id["M1"]["opening_cents"] == 7000 and by_id["M1"]["closing_cents"] == 4000
    assert by_id["M2"]["opening_cents"] == 6000 and by_id["M2"]["closing_cents"] == 0
    assert by_id["M1"]["prior_allocations"][0]["period"] == "2026-10"
    assert by_id["M1"]["allocations"][0]["receipt_external_id"] == "R2"

    for _ in range(2):
        recalculation = client.post(
            "/ledgers/%s/recalculate" % ledger["id"], headers=analyst, json={"through_period": "2026-11"}
        )
        assert recalculation.status_code == 200
    after = recalculation.json()
    assert len(after["periods"]) == 2
    assert after["outstanding_commissions"][0]["closing_cents"] == 4000

    assert client.post("/ledgers/%s/close" % ledger["id"], headers=analyst, json={"period": "2026-10"}).status_code == 403
    closed = client.post("/ledgers/%s/close" % ledger["id"], headers=admin, json={"period": "2026-10"})
    assert closed.status_code == 200 and closed.json()["changed"] is True
    again = client.post("/ledgers/%s/close" % ledger["id"], headers=admin, json={"period": "2026-10"})
    assert again.status_code == 200 and again.json()["changed"] is False
    out_of_order = client.post("/ledgers/%s/close" % ledger["id"], headers=admin, json={"period": "2026-12"})
    assert out_of_order.status_code == 404

    assert client.delete("/ledgers/%s" % ledger["id"], headers=analyst).status_code == 403
    assert client.delete("/ledgers/%s" % ledger["id"], headers=admin).status_code == 204
    assert client.get("/ledgers", headers=analyst).json() == []


def test_backfill_after_close_reports_variance(client):
    analyst = login(client, "analyst@settlement.demo")
    admin = login(client, "admin@settlement.demo")
    ledger = create(client, analyst).json()
    client.post("/ledgers/%s/close" % ledger["id"], headers=admin, json={"period": "2026-10"})

    backfill = client.post(
        "/items",
        headers=analyst,
        json={
            "batch_id": "batch",
            "external_id": "R0",
            "side": "receipt",
            "party": "客户甲",
            "reference": "JOB-1",
            "amount_cents": 1000,
            "currency": "CNY",
            "booking_date": "2026-10-28",
        },
    )
    assert backfill.status_code == 201, backfill.text

    detail = client.get("/ledgers/%s" % ledger["id"], headers=analyst).json()
    october = next(row for row in detail["periods"] if row["period"] == "2026-10")
    november = next(row for row in detail["periods"] if row["period"] == "2026-11" or row["period"] == "2026-12")
    assert october["status"] == "closed" and october["closing_cents"] == 13000
    assert november["opening_cents"] == 13000
    assert detail["checks"]["commissions"]["ok"]
    variances = detail["variances"]
    assert variances and variances[0]["period"] == "2026-10"
    assert variances[0]["kept_cents"] == 7000
    assert variances[0]["recomputed_cents"] == 6000
    assert variances[0]["reason"] == "backdated_receipt"

    month = client.get("/ledgers/%s/months/2026-11" % ledger["id"], headers=analyst).json()
    row = next(item for item in month["balances"] if item["commission_external_id"] == "M1")
    sources = [allocation["source"] for allocation in row["allocations"]]
    assert "backfill_receipt" in sources


def test_concurrent_recalculation_does_not_duplicate(client):
    analyst = login(client, "analyst@settlement.demo")
    ledger = create(client, analyst).json()
    errors = []

    def run():
        try:
            response = client.post("/ledgers/%s/recalculate" % ledger["id"], headers=analyst, json={})
            if response.status_code not in (200, 409):
                errors.append(response.text)
        except Exception as error:  # pragma: no cover - surfaced by assertion
            errors.append(str(error))

    threads = [threading.Thread(target=run) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []

    detail = client.get("/ledgers/%s" % ledger["id"], headers=analyst).json()
    assert len(detail["periods"]) == 2
    allocations = sum(row["allocation_count"] for row in detail["periods"])
    assert allocations == 3
    assert detail["checks"]["commissions"]["ok"]


def test_duplicate_ledger_name_is_rejected(client):
    analyst = login(client, "analyst@settlement.demo")
    assert create(client, analyst, name="同名台账").status_code == 201
    duplicate = create(client, analyst, name="同名台账")
    assert duplicate.status_code == 409
    assert len(client.get("/ledgers", headers=analyst).json()) == 1


def test_invalid_period_is_rejected(client):
    analyst = login(client, "analyst@settlement.demo")
    bad = client.post(
        "/ledgers",
        headers=analyst,
        json={"batch_id": "batch", "name": "坏账期", "description": "", "through_period": "2026-13"},
    )
    assert bad.status_code == 422


def test_unknown_month_is_not_found(client):
    analyst = login(client, "analyst@settlement.demo")
    ledger = create(client, analyst).json()
    assert client.get("/ledgers/%s/months/2026-09" % ledger["id"], headers=analyst).status_code == 404
