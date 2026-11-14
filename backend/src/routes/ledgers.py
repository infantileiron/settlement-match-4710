from fastapi import APIRouter, Depends
from sqlalchemy import select

from ..config.database import get_db
from ..utils.auth import current, write_user, admin
from ..schemas.ledger import LedgerCreate, LedgerRecalculate, PeriodClose
from ..models.ledger import BalanceLedger
from ..models.common import serialize
from ..services import ledger_service

router = APIRouter(prefix="/ledgers", tags=["ledgers"])


@router.get("")
def listing(db=Depends(get_db), user=Depends(current)):
    rows = db.scalars(select(BalanceLedger).order_by(BalanceLedger.created_at.desc()).limit(200)).all()
    return [ledger_service.summary(db, row.id) for row in rows]


@router.get("/{ident}")
def detail(ident: str, db=Depends(get_db), user=Depends(current)):
    return ledger_service.summary(db, ident)


@router.post("", status_code=201)
def create(body: LedgerCreate, db=Depends(get_db), user=Depends(write_user)):
    return ledger_service.summary(db, ledger_service.create(db, body, user).id)


@router.post("/{ident}/recalculate")
def recalculate(ident: str, body: LedgerRecalculate | None = None, db=Depends(get_db), user=Depends(write_user)):
    through = body.through_period if body else None
    return ledger_service.summary(db, ledger_service.recalculate(db, ident, through).id)


@router.post("/{ident}/close")
def close(ident: str, body: PeriodClose, db=Depends(get_db), user=Depends(admin)):
    ledger, period, changed = ledger_service.close_period(db, ident, body.period, user)
    return {"ledger_id": ledger.id, "closed_through": ledger.closed_through, "period": period.period, "changed": changed}


@router.get("/{ident}/months/{period}")
def month(ident: str, period: str, db=Depends(get_db), user=Depends(current)):
    return ledger_service.month_view(db, ident, period)


@router.delete("/{ident}", status_code=204)
def remove(ident: str, db=Depends(get_db), user=Depends(admin)):
    ledger_service.delete_ledger(db, ident)
