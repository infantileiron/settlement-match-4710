from pydantic import BaseModel, Field, field_validator

from ..services.ledger_engine import validate_period


def _optional_period(value):
    if value in (None, ""):
        return None
    return validate_period(value)


class LedgerCreate(BaseModel):
    batch_id: str
    name: str = Field(min_length=2, max_length=180)
    description: str = Field(default="", max_length=2000)
    through_period: str | None = None

    @field_validator("through_period", mode="before")
    @classmethod
    def check_through(cls, value):
        return _optional_period(value)


class LedgerRecalculate(BaseModel):
    through_period: str | None = None

    @field_validator("through_period", mode="before")
    @classmethod
    def check_through(cls, value):
        return _optional_period(value)


class PeriodClose(BaseModel):
    period: str

    @field_validator("period", mode="before")
    @classmethod
    def check_period(cls, value):
        if value in (None, ""):
            raise ValueError("账期须为YYYY-MM")
        return validate_period(value)
