"""确定性的补偿单价、能力与土地库存计算。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from .clock import utc_text


ZERO = Decimal("0")
HUNDRED = Decimal("100")
BASIS_POINTS = Decimal("10000")


def quantize_volume(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def quantize_money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class PricePoint:
    trade_date: str
    close: Decimal


@dataclass(frozen=True, slots=True)
class Streak:
    direction: str
    sessions: int
    start_date: str
    end_date: str
    start_close: Decimal
    end_close: Decimal
    percent_change: Decimal

    def as_dict(self) -> dict[str, object]:
        return {
            "direction": self.direction,
            "sessions": self.sessions,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "start_close": decimal_text(self.start_close),
            "end_close": decimal_text(self.end_close),
            "percent_change": decimal_text(self.percent_change),
        }


def latest_streak(points: Sequence[PricePoint]) -> Streak | None:
    ordered = sorted(points, key=lambda item: item.trade_date)
    if len(ordered) < 2:
        return None
    last = ordered[-1]
    previous = ordered[-2]
    if last.close == previous.close:
        return Streak("flat", 1, last.trade_date, last.trade_date, last.close, last.close, ZERO)
    direction = "down" if last.close < previous.close else "up"
    start_index = len(ordered) - 2
    while start_index > 0:
        left = ordered[start_index - 1]
        right = ordered[start_index]
        matches = right.close < left.close if direction == "down" else right.close > left.close
        if not matches:
            break
        start_index -= 1
    start = ordered[start_index]
    change = (last.close - start.close) / start.close * HUNDRED
    return Streak(
        direction=direction,
        sessions=len(ordered) - start_index,
        start_date=start.trade_date,
        end_date=last.trade_date,
        start_close=start.close,
        end_close=last.close,
        percent_change=change.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP),
    )


def moving_average(points: Sequence[PricePoint], sessions: int) -> Decimal | None:
    if sessions <= 0:
        raise ValueError("sessions 必须大于零")
    ordered = sorted(points, key=lambda item: item.trade_date)
    if len(ordered) < sessions:
        return None
    values = [item.close for item in ordered[-sessions:]]
    return quantize_money(sum(values, ZERO) / Decimal(len(values)))


def effective_capacity(
    nominal: Decimal,
    capacity_percentages: Iterable[Decimal],
) -> Decimal:
    result = nominal
    for percentage in capacity_percentages:
        bounded = max(ZERO, min(HUNDRED, percentage))
        result *= bounded / HUNDRED
    return quantize_volume(result)


@dataclass(frozen=True, slots=True)
class OutageWindow:
    """一条地块临时限制时段；ends_at 为 None 表示未填写结束时间、持续生效。"""

    outage_id: int
    starts_at: datetime
    ends_at: datetime | None
    capacity_percent: Decimal


def business_day_window(timezone_name: str, service_date: str) -> tuple[datetime, datetime]:
    """返回业务日在 UTC 下的起止时刻。

    业务日边界跟随安置片区时区的当地午夜（含夏令时引起的 23/25 小时日），
    使临时限制时段与乡镇采用的自然日口径保持一致。
    """
    zone = ZoneInfo(timezone_name)
    day = date.fromisoformat(service_date)
    start_local = datetime.combine(day, time.min).replace(tzinfo=zone)
    end_local = datetime.combine(day + timedelta(days=1), time.min).replace(tzinfo=zone)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def _duration_seconds(delta: timedelta) -> Decimal:
    return Decimal(delta.days * 86400 + delta.seconds) + Decimal(delta.microseconds) / Decimal(1000000)


def daily_capacity_breakdown(
    nominal: Decimal,
    outages: Iterable[OutageWindow],
    day_start: datetime,
    day_end: datetime,
) -> dict[str, object]:
    """按业务日窗口折算可分配容量，并逐段解释每条限制的重叠扣减贡献。

    跨日限制只按与业务日真正重叠的时长比例扣减；未填写结束时间的限制
    持续生效；同一输入重复核算返回稳定结果。
    """
    day_seconds = _duration_seconds(day_end - day_start)
    if day_seconds <= ZERO:
        raise ValueError("业务日窗口必须为正")
    rows: list[dict[str, object]] = []
    total_deduction = ZERO
    for outage in sorted(outages, key=lambda item: item.outage_id):
        overlap_start = max(outage.starts_at, day_start)
        overlap_end = day_end if outage.ends_at is None else min(outage.ends_at, day_end)
        if overlap_end <= overlap_start:
            continue
        bounded = max(ZERO, min(HUNDRED, outage.capacity_percent))
        overlap_seconds = _duration_seconds(overlap_end - overlap_start)
        deducted = quantize_volume(nominal * (HUNDRED - bounded) / HUNDRED * overlap_seconds / day_seconds)
        total_deduction += deducted
        rows.append({
            "outage_id": outage.outage_id,
            "capacity_percent": decimal_text(bounded),
            "overlap_starts_at": utc_text(overlap_start),
            "overlap_ends_at": utc_text(overlap_end),
            "overlap_seconds": decimal_text(overlap_seconds),
            "deducted_mu": decimal_text(deducted),
        })
    available = quantize_volume(max(ZERO, nominal - total_deduction))
    return {
        "nominal_capacity": decimal_text(quantize_volume(nominal)),
        "deducted_capacity": decimal_text(quantize_volume(total_deduction)),
        "available_capacity": decimal_text(available),
        "outages": rows,
    }


@dataclass(frozen=True, slots=True)
class AllocationRequest:
    nomination_id: str
    requested: Decimal
    priority: int
    submitted_at: str


def allocate_capacity(
    available: Decimal,
    requests: Iterable[AllocationRequest],
) -> list[dict[str, str]]:
    if available < ZERO:
        raise ValueError("可用能力不能为负数")
    remaining = quantize_volume(available)
    result: list[dict[str, str]] = []
    ordered = sorted(requests, key=lambda item: (item.priority, item.submitted_at, item.nomination_id))
    for request in ordered:
        allocated = min(remaining, request.requested)
        allocated = quantize_volume(max(ZERO, allocated))
        remaining = quantize_volume(remaining - allocated)
        result.append({
            "nomination_id": request.nomination_id,
            "requested_mu": decimal_text(request.requested),
            "allocated_mu": decimal_text(allocated),
            "unfilled_mu": decimal_text(quantize_volume(request.requested - allocated)),
        })
    return result


def delivered_after_loss(loaded: Decimal, loss_basis_points: int) -> Decimal:
    if not 0 <= loss_basis_points <= 1000:
        raise ValueError("损耗基点超出范围")
    retained = Decimal(1) - Decimal(loss_basis_points) / BASIS_POINTS
    return quantize_volume(loaded * retained)


def weighted_inventory_cost(lots: Iterable[Mapping[str, object]]) -> dict[str, str]:
    quantity = ZERO
    value = ZERO
    for lot in lots:
        available = Decimal(str(lot["available_mu"]))
        unit_cost = Decimal(str(lot["unit_cost_cny"]))
        if available < ZERO or unit_cost < ZERO:
            raise ValueError("土地库存数量和成本不能为负数")
        quantity += available
        value += available * unit_cost
    average = ZERO if quantity == ZERO else value / quantity
    return {
        "available_mu": decimal_text(quantize_volume(quantity)),
        "inventory_value_cny": decimal_text(quantize_money(value)),
        "weighted_unit_cost_cny": decimal_text(quantize_money(average)),
    }


def reconcile_inventory(
    book_quantity: Decimal,
    measured_quantity: Decimal,
    tolerance_percent: Decimal,
) -> dict[str, object]:
    if book_quantity < ZERO or measured_quantity < ZERO:
        raise ValueError("土地库存数量不能为负数")
    if tolerance_percent < ZERO:
        raise ValueError("容差不能为负数")
    delta = quantize_volume(measured_quantity - book_quantity)
    ratio = ZERO if book_quantity == ZERO else abs(delta) / book_quantity * HUNDRED
    return {
        "book_quantity": decimal_text(quantize_volume(book_quantity)),
        "measured_quantity": decimal_text(quantize_volume(measured_quantity)),
        "delta_mu": decimal_text(delta),
        "variance_percent": decimal_text(ratio.quantize(Decimal("0.0001"))),
        "within_tolerance": ratio <= tolerance_percent,
    }


def scenario_projection(
    *,
    current_price: Decimal,
    market_index_drop_percent: Decimal,
    routes: Iterable[Mapping[str, object]],
    inventory: Iterable[Mapping[str, object]],
    route_capacity_changes: Mapping[str, Decimal],
    demand_changes: Mapping[str, Decimal],
) -> dict[str, object]:
    projected_price = current_price * (Decimal(1) - market_index_drop_percent / HUNDRED)
    route_rows: list[dict[str, str]] = []
    total_capacity = ZERO
    for route in sorted(routes, key=lambda item: str(item["route_id"])):
        route_id = str(route["route_id"])
        nominal = Decimal(str(route["daily_capacity"]))
        change = route_capacity_changes.get(route_id, ZERO)
        projected = max(ZERO, nominal * (Decimal(1) + change / HUNDRED))
        total_capacity += projected
        route_rows.append({
            "route_id": route_id,
            "base_capacity": decimal_text(quantize_volume(nominal)),
            "change_percent": decimal_text(change),
            "projected_capacity": decimal_text(quantize_volume(projected)),
        })
    inventory_rows: list[dict[str, str]] = []
    total_inventory = ZERO
    for row in sorted(inventory, key=lambda item: (str(item["facility_id"]), str(item["product"]))):
        key = f"{row['facility_id']}:{row['product']}"
        available = Decimal(str(row["available_mu"]))
        demand_change = demand_changes.get(key, ZERO)
        days_factor = max(Decimal("0.01"), Decimal(1) + demand_change / HUNDRED)
        adjusted = available / days_factor
        total_inventory += adjusted
        inventory_rows.append({
            "inventory_key": key,
            "base_available": decimal_text(quantize_volume(available)),
            "demand_change_percent": decimal_text(demand_change),
            "demand_adjusted_inventory": decimal_text(quantize_volume(adjusted)),
        })
    return {
        "projected_market_index_cny": decimal_text(quantize_money(projected_price)),
        "total_projected_capacity": decimal_text(quantize_volume(total_capacity)),
        "demand_adjusted_inventory": decimal_text(quantize_volume(total_inventory)),
        "routes": route_rows,
        "inventory": inventory_rows,
    }
