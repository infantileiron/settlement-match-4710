"""Cross-period commission balance engine.

The engine has no ORM or database imports so the accounting rules can be
tested on their own.  It consumes plain dictionaries:

* commissions (milestone items) with their original integer-cent amount;
* receipts with their booking month;
* allocations/balances already frozen in closed periods;
* the requested reporting horizon.

It returns the open-period allocations, monthly balance rows, the
closed-period differences, per-commission and per-receipt equations, and a
small set of aggregate checks.  Persistence is the service layer's job.
"""

from collections import defaultdict

from .input_records import normalize


def month_of(day):
    return day[:7]


def _index(period):
    return int(period[:4]) * 12 + int(period[5:7]) - 1


def _period(index):
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def next_period(period):
    return _period(_index(period) + 1)


def month_range(start, end):
    if _index(start) > _index(end):
        return []
    return [_period(value) for value in range(_index(start), _index(end) + 1)]


def validate_period(value):
    if not isinstance(value, str) or len(value) != 7 or value[4] != "-":
        raise ValueError("账期须为YYYY-MM")
    if not (value[:4] + value[5:]).isdigit() or not 1 <= int(value[5:]) <= 12:
        raise ValueError("账期须为有效YYYY-MM")
    return value


def link_key(row):
    """Hard attribution key for an installment.

    Same currency, same normalised party, same normalised reference.  This is
    stricter than the candidate-scoring engine on purpose: an installment can
    only offset a commission it can be attributed to, not one that merely has
    a similar amount.
    """

    return (row["currency"], normalize(row["party"]), normalize(row["reference"]))


def _item_key(row):
    return (row["booking_date"], row["external_id"])


def _validate_items(commissions, receipts):
    seen = set()
    for row in commissions + receipts:
        ident = row["external_id"]
        if ident in seen:
            raise ValueError(f"分录ID重复：{ident}")
        seen.add(ident)
        amount = row["amount_cents"]
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            raise ValueError("金额须为正整数分")
        if row["currency"] != "CNY":
            raise ValueError("余额台账仅支持CNY")
        if len(row["booking_date"]) != 10:
            raise ValueError("业务日期须为YYYY-MM-DD")


def _simulate(
    commissions,
    receipts,
    *,
    start,
    end,
    initial_outstanding,
    initial_receipt_remaining,
    initial_applied,
    carried_from,
    frozen_commissions,
    frozen_receipts,
):
    """Apply receipts FIFO inside each link group, month by month.

    Opening balances carry only the uncleared part of commissions already in
    closed periods.  New commissions enter with ``new_cents`` in their
    effective month, so an original amount is never counted twice.  A
    commission that was fully cleared in a closed period is not re-opened.
    Items dated before ``start`` (a prior-period backfill) become effective in
    ``start`` and are tagged ``backfill_*``.  A receipt that already has a
    frozen closed-period allocation is tagged ``carried_receipt``.
    """

    original = {row["external_id"]: row["amount_cents"] for row in commissions}
    by_link = defaultdict(list)
    for row in commissions:
        by_link[link_key(row)].append(row)
    for rows in by_link.values():
        rows.sort(key=_item_key)

    outstanding = {key: value for key, value in initial_outstanding.items() if value > 0}
    remaining = {row["external_id"]: value for row, value in zip(receipts, initial_receipt_remaining)}
    applied_to_date = dict(initial_applied)
    carry_from = dict(carried_from)

    new_period = {row["external_id"]: max(month_of(row["booking_date"]), start) for row in commissions}
    receipt_period = {row["external_id"]: max(month_of(row["booking_date"]), start) for row in receipts}

    periods = []
    balances = []
    allocations = []

    for period in month_range(start, end):
        opening_map = {key: value for key, value in outstanding.items() if value > 0}
        new_here = sorted(
            (
                row
                for row in commissions
                if new_period[row["external_id"]] == period and row["external_id"] not in frozen_commissions
            ),
            key=_item_key,
        )
        new_ids = {row["external_id"] for row in new_here}
        available = dict(opening_map)
        for row in new_here:
            ident = row["external_id"]
            available[ident] = available.get(ident, 0) + original[ident]

        period_allocations = []
        receipts_here = sorted(
            (
                row
                for row in receipts
                if remaining.get(row["external_id"], 0) > 0 and receipt_period[row["external_id"]] == period
            ),
            key=_item_key,
        )
        for receipt in receipts_here:
            receipt_id = receipt["external_id"]
            for commission in by_link[link_key(receipt)]:
                commission_id = commission["external_id"]
                if remaining[receipt_id] <= 0:
                    break
                if available.get(commission_id, 0) <= 0:
                    continue
                if commission["booking_date"] > receipt["booking_date"]:
                    continue
                amount = min(remaining[receipt_id], available[commission_id])
                if amount <= 0:
                    continue
                available[commission_id] -= amount
                remaining[receipt_id] -= amount
                receipt_backfill = month_of(receipt["booking_date"]) < start
                commission_backfill = (
                    month_of(commission["booking_date"]) < start
                    and commission_id not in frozen_commissions
                )
                source = "normal"
                if receipt_id in frozen_receipts:
                    source = "carried_receipt"
                elif receipt_backfill:
                    source = "backfill_receipt"
                elif commission_backfill:
                    source = "backfill_commission"
                allocation = {
                    "period": period,
                    "commission_external_id": commission_id,
                    "receipt_external_id": receipt_id,
                    "amount_cents": amount,
                    "sequence": len(period_allocations) + 1,
                    "source": source,
                    "receipt_booking_date": receipt["booking_date"],
                }
                period_allocations.append(allocation)
                allocations.append(allocation)

        active = sorted(set(opening_map) | new_ids)
        period_rows = []
        for commission_id in active:
            opening = opening_map.get(commission_id, 0)
            new = original[commission_id] if commission_id in new_ids else 0
            applied = opening + new - available[commission_id]
            source_row = next(row for row in commissions if row["external_id"] == commission_id)
            applied_to_date[commission_id] = applied_to_date.get(commission_id, 0) + applied
            row = {
                "period": period,
                "commission_external_id": commission_id,
                "party": source_row["party"],
                "reference": source_row["reference"],
                "booking_date": source_row["booking_date"],
                "original_cents": original[commission_id],
                "opening_cents": opening,
                "new_cents": new,
                "applied_cents": applied,
                "closing_cents": available[commission_id],
                "applied_to_date_cents": applied_to_date[commission_id],
                "carried_from_period": carry_from.get(commission_id, ""),
            }
            period_rows.append(row)
            balances.append(row)

        next_outstanding = {}
        for commission_id in active:
            if available[commission_id] > 0:
                next_outstanding[commission_id] = available[commission_id]
                carry_from[commission_id] = period
            else:
                carry_from.pop(commission_id, None)
        outstanding = next_outstanding

        periods.append(
            {
                "period": period,
                "opening_cents": sum(row["opening_cents"] for row in period_rows),
                "new_cents": sum(row["new_cents"] for row in period_rows),
                "applied_cents": sum(row["applied_cents"] for row in period_rows),
                "closing_cents": sum(row["closing_cents"] for row in period_rows),
                "commission_count": len(period_rows),
                "allocation_count": len(period_allocations),
            }
        )

    return {
        "periods": periods,
        "balances": balances,
        "allocations": allocations,
        "outstanding": outstanding,
        "remaining": remaining,
        "applied_to_date": applied_to_date,
    }


def _frozen_state(frozen_balances, frozen_allocations):
    stored = {}
    for row in sorted(frozen_balances, key=lambda item: item["period"]):
        stored[row["commission_external_id"]] = row["closing_cents"]
    applied_commission = defaultdict(int)
    applied_receipt = defaultdict(int)
    for row in frozen_allocations:
        applied_commission[row["commission_external_id"]] += row["amount_cents"]
        applied_receipt[row["receipt_external_id"]] += row["amount_cents"]
    return stored, dict(applied_commission), dict(applied_receipt)


def build_plan(
    commissions,
    receipts,
    *,
    closed_periods=(),
    frozen_balances=(),
    frozen_allocations=(),
    through_period=None,
):
    """Build the full cross-period plan.

    Closed periods are never recalculated into the ledger.  Their frozen
    balances/allocations provide the carry-forward; their theoretical values
    are compared separately, producing ``variances`` only.  The new ledger
    always satisfies ``original = cumulative_applied + closing``; where the
    frozen account disagrees, the difference is listed rather than silently
    rewriting the closed month.
    """

    _validate_items(commissions, receipts)
    if not commissions:
        raise ValueError("余额台账至少需要一笔委托")

    closed = sorted({validate_period(value) for value in closed_periods})
    frozen_balances = [row for row in frozen_balances if row["period"] in closed]
    frozen_allocations = [row for row in frozen_allocations if row["period"] in closed]

    item_months = [month_of(row["booking_date"]) for row in commissions + receipts]
    first_open = next_period(closed[-1]) if closed else min(item_months)
    if through_period is None:
        through_period = max(item_months)
    else:
        through_period = validate_period(through_period)
    horizon = max(through_period, first_open, max(item_months))

    stored_closing, frozen_commission_applied, frozen_receipt_applied = _frozen_state(
        frozen_balances, frozen_allocations
    )

    original = {row["external_id"]: row["amount_cents"] for row in commissions}
    frozen_ids = set(stored_closing) | set(frozen_commission_applied)
    opening = {}
    carried_from = {}
    # A kept closing balance is the authority for the closed month.  Cumulative
    # applied is derived from it, so a frozen allocation set that disagrees
    # cannot break ``original = applied + closing``; the disagreement is
    # reported as a separate cross-foot row below.
    closed_applied = {}
    for commission in commissions:
        ident = commission["external_id"]
        if ident in stored_closing:
            opening[ident] = stored_closing[ident]
            carried_from[ident] = closed[-1]
            closed_applied[ident] = original[ident] - stored_closing[ident]
        elif ident in frozen_commission_applied:
            opening[ident] = original[ident] - frozen_commission_applied[ident]
            carried_from[ident] = closed[-1]
            closed_applied[ident] = frozen_commission_applied[ident]
        if opening.get(ident, 0) < 0:
            raise ValueError(f"委托{ident}的已关账余额为负")
        if opening.get(ident, 0) == 0:
            opening.pop(ident, None)

    receipt_remaining = []
    for receipt in receipts:
        ident = receipt["external_id"]
        remaining = receipt["amount_cents"] - frozen_receipt_applied.get(ident, 0)
        if remaining < 0:
            raise ValueError(f"凭证{ident}的已关账抵扣超过原额")
        receipt_remaining.append(remaining)

    actual = _simulate(
        commissions,
        receipts,
        start=first_open,
        end=horizon,
        initial_outstanding=opening,
        initial_receipt_remaining=receipt_remaining,
        initial_applied=closed_applied,
        carried_from=carried_from,
        frozen_commissions=frozen_ids,
        frozen_receipts={ident for ident, amount in frozen_receipt_applied.items() if amount > 0},
    )

    variances = []
    if closed:
        theory_start = min([*item_months, closed[0]])
        theory = _simulate(
            commissions,
            receipts,
            start=theory_start,
            end=closed[-1],
            initial_outstanding={},
            initial_receipt_remaining=[row["amount_cents"] for row in receipts],
            initial_applied={},
            carried_from={},
            frozen_commissions=set(),
            frozen_receipts=set(),
        )
        theory_balance = {(row["period"], row["commission_external_id"]): row for row in theory["balances"]}
        frozen_balance = {(row["period"], row["commission_external_id"]): row for row in frozen_balances}
        theory_receipt_period = defaultdict(int)
        for row in theory["allocations"]:
            theory_receipt_period[(row["period"], row["receipt_external_id"])] += row["amount_cents"]
        frozen_receipt_period = defaultdict(int)
        for row in frozen_allocations:
            frozen_receipt_period[(row["period"], row["receipt_external_id"])] += row["amount_cents"]
        new_commissions = {
            row["external_id"]
            for row in commissions
            if row["external_id"] not in frozen_ids
            and month_of(row["booking_date"]) <= closed[-1]
        }
        new_receipts = {
            row["external_id"]
            for row in receipts
            if row["external_id"] not in frozen_receipt_applied
            and month_of(row["booking_date"]) <= closed[-1]
        }

        for period in closed:
            commission_ids = sorted(
                {
                    key[1]
                    for key in list(theory_balance) + list(frozen_balance)
                    if key[0] == period
                }
            )
            for commission_id in commission_ids:
                kept = frozen_balance.get((period, commission_id), {}).get("closing_cents", 0)
                recomputed = theory_balance.get((period, commission_id), {}).get("closing_cents", 0)
                delta = recomputed - kept
                if delta == 0:
                    continue
                if commission_id in new_commissions:
                    reason = "new_commission"
                elif new_receipts:
                    reason = "backdated_receipt"
                else:
                    reason = "recomputed_shift"
                variances.append(
                    {
                        "period": period,
                        "entity_type": "commission",
                        "entity_id": commission_id,
                        "kept_cents": kept,
                        "recomputed_cents": recomputed,
                        "delta_cents": delta,
                        "reason": reason,
                    }
                )

            receipt_ids = sorted(
                {
                    key[1]
                    for key in list(theory_receipt_period) + list(frozen_receipt_period)
                    if key[0] == period
                }
            )
            for receipt_id in receipt_ids:
                kept = sum(
                    amount
                    for (period_key, ident), amount in frozen_receipt_period.items()
                    if ident == receipt_id and _index(period_key) <= _index(period)
                )
                recomputed = sum(
                    amount
                    for (period_key, ident), amount in theory_receipt_period.items()
                    if ident == receipt_id and _index(period_key) <= _index(period)
                )
                delta = recomputed - kept
                if delta == 0:
                    continue
                variances.append(
                    {
                        "period": period,
                        "entity_type": "receipt",
                        "entity_id": receipt_id,
                        "kept_cents": kept,
                        "recomputed_cents": recomputed,
                        "delta_cents": delta,
                        "reason": "backdated_receipt" if receipt_id in new_receipts else "recomputed_shift",
                    }
                )

    receipt_status = []
    for receipt, remaining in zip(receipts, [actual["remaining"].get(row["external_id"], 0) for row in receipts]):
        amount = receipt["amount_cents"]
        allocated = amount - remaining
        receipt_status.append(
            {
                "receipt_external_id": receipt["external_id"],
                "party": receipt["party"],
                "reference": receipt["reference"],
                "booking_date": receipt["booking_date"],
                "amount_cents": amount,
                "allocated_cents": allocated,
                "remaining_cents": remaining,
                "status": "matched" if remaining == 0 else "unallocated",
            }
        )

    commission_status = []
    for commission in commissions:
        ident = commission["external_id"]
        allocated = actual["applied_to_date"].get(ident, frozen_commission_applied.get(ident, 0))
        closing_cents = actual["outstanding"].get(ident, 0)
        commission_status.append(
            {
                "commission_external_id": ident,
                "party": commission["party"],
                "reference": commission["reference"],
                "booking_date": commission["booking_date"],
                "original_cents": original[ident],
                "allocated_cents": allocated,
                "closing_cents": closing_cents,
                "equation_ok": original[ident] == allocated + closing_cents,
            }
        )

    commission_original = sum(row["original_cents"] for row in commission_status)
    commission_allocated = sum(row["allocated_cents"] for row in commission_status)
    commission_closing = sum(row["closing_cents"] for row in commission_status)
    receipt_amount = sum(row["amount_cents"] for row in receipt_status)
    receipt_allocated = sum(row["allocated_cents"] for row in receipt_status)
    receipt_remaining_total = sum(row["remaining_cents"] for row in receipt_status)
    # Difference between the frozen closing balances and the frozen allocation
    # detail; a non-zero value means the closed books were internally
    # inconsistent, which is reported without being rewritten.
    frozen_cross_foot = sum(
        (stored_closing.get(ident, original[ident] - amount) + amount - original[ident])
        for ident, amount in frozen_commission_applied.items()
    ) if closed else 0
    frozen_cross_foot += sum(
        stored_closing.get(ident, 0) - (original[ident] - frozen_commission_applied.get(ident, 0))
        for ident in stored_closing
        if ident not in frozen_commission_applied
    )

    return {
        "first_open_period": first_open,
        "through_period": horizon,
        "closed_periods": closed,
        "periods": actual["periods"],
        "balances": actual["balances"],
        "allocations": actual["allocations"],
        "variances": variances,
        "receipt_status": receipt_status,
        "commission_status": commission_status,
        "checks": {
            "commissions": {
                "original_cents": commission_original,
                "allocated_cents": commission_allocated,
                "closing_cents": commission_closing,
                "closed_cross_foot_cents": frozen_cross_foot,
                "ok": commission_original == commission_allocated + commission_closing
                and all(row["equation_ok"] for row in commission_status),
            },
            "receipts": {
                "amount_cents": receipt_amount,
                "allocated_cents": receipt_allocated,
                "remaining_cents": receipt_remaining_total,
                "matched": sum(row["status"] == "matched" for row in receipt_status),
                "exceptions": sum(row["status"] == "unallocated" for row in receipt_status),
                "ok": receipt_amount == receipt_allocated + receipt_remaining_total,
            },
        },
    }
