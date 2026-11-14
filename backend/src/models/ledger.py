from sqlalchemy import String, Text, JSON, ForeignKey, BigInteger, Boolean, DateTime, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from .common import RecordMixin, now
from ..config.database import Base


class BalanceLedger(RecordMixin, Base):
    """A cross-period balance book built from one batch's milestones/receipts."""

    __tablename__ = "ledgers"
    __table_args__ = (UniqueConstraint("batch_id", "name", name="uq_ledger_batch_name"),)
    batch_id: Mapped[str] = mapped_column(ForeignKey("batches.id"))
    name: Mapped[str] = mapped_column(String(180))
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="active")
    through_period: Mapped[str] = mapped_column(String(7), default="")
    closed_through: Mapped[str] = mapped_column(String(7), default="")
    checks: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[str] = mapped_column(String(160), default="")
    recalculated_at: Mapped[object] = mapped_column(DateTime, default=now, nullable=True)


class LedgerPeriod(RecordMixin, Base):
    """Monthly summary.  ``status=closed`` makes its rows immutable."""

    __tablename__ = "ledger_periods"
    __table_args__ = (UniqueConstraint("ledger_id", "period", name="uq_ledger_period"),)
    ledger_id: Mapped[str] = mapped_column(ForeignKey("ledgers.id"))
    period: Mapped[str] = mapped_column(String(7))
    status: Mapped[str] = mapped_column(String(20), default="open")
    opening_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    new_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    applied_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    closing_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    commission_count: Mapped[int] = mapped_column(Integer, default=0)
    allocation_count: Mapped[int] = mapped_column(Integer, default=0)
    closed_by: Mapped[str] = mapped_column(String(160), default="")
    closed_at: Mapped[object] = mapped_column(DateTime, nullable=True)


class LedgerBalance(RecordMixin, Base):
    """One commission's month-end balance row; the monthly audit trail."""

    __tablename__ = "ledger_balances"
    __table_args__ = (UniqueConstraint("ledger_id", "period", "commission_external_id", name="uq_ledger_balance"),)
    ledger_id: Mapped[str] = mapped_column(ForeignKey("ledgers.id"))
    period: Mapped[str] = mapped_column(String(7))
    commission_external_id: Mapped[str] = mapped_column(String(80))
    party: Mapped[str] = mapped_column(String(160))
    reference: Mapped[str] = mapped_column(String(100))
    booking_date: Mapped[str] = mapped_column(String(10))
    original_cents: Mapped[int] = mapped_column(BigInteger)
    opening_cents: Mapped[int] = mapped_column(BigInteger)
    new_cents: Mapped[int] = mapped_column(BigInteger)
    applied_cents: Mapped[int] = mapped_column(BigInteger)
    closing_cents: Mapped[int] = mapped_column(BigInteger)
    applied_to_date_cents: Mapped[int] = mapped_column(BigInteger)
    carried_from_period: Mapped[str] = mapped_column(String(7), default="")


class LedgerAllocation(RecordMixin, Base):
    """One installment: receipt -> commission, the smallest auditable step."""

    __tablename__ = "ledger_allocations"
    __table_args__ = (
        UniqueConstraint(
            "ledger_id",
            "period",
            "commission_external_id",
            "receipt_external_id",
            "sequence",
            name="uq_ledger_allocation",
        ),
    )
    ledger_id: Mapped[str] = mapped_column(ForeignKey("ledgers.id"))
    period: Mapped[str] = mapped_column(String(7))
    commission_external_id: Mapped[str] = mapped_column(String(80))
    receipt_external_id: Mapped[str] = mapped_column(String(80))
    amount_cents: Mapped[int] = mapped_column(BigInteger)
    sequence: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(30), default="normal")
    receipt_booking_date: Mapped[str] = mapped_column(String(10))


class LedgerVariance(RecordMixin, Base):
    """Difference between frozen closed-period books and a fresh recomputation."""

    __tablename__ = "ledger_variances"
    __table_args__ = (UniqueConstraint("ledger_id", "period", "entity_type", "entity_id", name="uq_ledger_variance"),)
    ledger_id: Mapped[str] = mapped_column(ForeignKey("ledgers.id"))
    period: Mapped[str] = mapped_column(String(7))
    entity_type: Mapped[str] = mapped_column(String(20))
    entity_id: Mapped[str] = mapped_column(String(80))
    kept_cents: Mapped[int] = mapped_column(BigInteger)
    recomputed_cents: Mapped[int] = mapped_column(BigInteger)
    delta_cents: Mapped[int] = mapped_column(BigInteger)
    reason: Mapped[str] = mapped_column(String(30))


class LedgerCommission(RecordMixin, Base):
    """Commission-level equation: original = applied + closing."""

    __tablename__ = "ledger_commissions"
    __table_args__ = (UniqueConstraint("ledger_id", "commission_external_id", name="uq_ledger_commission"),)
    ledger_id: Mapped[str] = mapped_column(ForeignKey("ledgers.id"))
    commission_external_id: Mapped[str] = mapped_column(String(80))
    party: Mapped[str] = mapped_column(String(160))
    reference: Mapped[str] = mapped_column(String(100))
    booking_date: Mapped[str] = mapped_column(String(10))
    original_cents: Mapped[int] = mapped_column(BigInteger)
    allocated_cents: Mapped[int] = mapped_column(BigInteger)
    closing_cents: Mapped[int] = mapped_column(BigInteger)
    unexplained_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    equation_ok: Mapped[bool] = mapped_column(Boolean, default=True)


class LedgerReceipt(RecordMixin, Base):
    """Receipt-level equation: amount = applied + remaining."""

    __tablename__ = "ledger_receipts"
    __table_args__ = (UniqueConstraint("ledger_id", "receipt_external_id", name="uq_ledger_receipt"),)
    ledger_id: Mapped[str] = mapped_column(ForeignKey("ledgers.id"))
    receipt_external_id: Mapped[str] = mapped_column(String(80))
    party: Mapped[str] = mapped_column(String(160))
    reference: Mapped[str] = mapped_column(String(100))
    booking_date: Mapped[str] = mapped_column(String(10))
    amount_cents: Mapped[int] = mapped_column(BigInteger)
    allocated_cents: Mapped[int] = mapped_column(BigInteger)
    remaining_cents: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(20))
