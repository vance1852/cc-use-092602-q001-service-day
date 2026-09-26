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
    """对整段业务日均生效的限制做连乘（保留给历史/单段场景）。"""
    result = nominal
    for percentage in capacity_percentages:
        bounded = max(ZERO, min(HUNDRED, percentage))
        result *= bounded / HUNDRED
    return quantize_volume(result)


@dataclass(frozen=True, slots=True)
class RestrictionWindow:
    """临时停用/限制时段，时间均为 UTC；ends_at 为空表示持续生效。"""

    restriction_id: str
    starts_at: datetime
    ends_at: datetime | None
    capacity_percent: Decimal

    def __post_init__(self) -> None:
        if self.starts_at.tzinfo is None or (
            self.ends_at is not None and self.ends_at.tzinfo is None
        ):
            raise ValueError("限制时段必须包含时区")
        if self.ends_at is not None and self.ends_at <= self.starts_at:
            raise ValueError("限制结束时间必须晚于开始时间")
        if self.capacity_percent < ZERO or self.capacity_percent > HUNDRED:
            raise ValueError("capacity_percent 必须在 0 到 100 之间")


def resolve_timezone(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name)
    except Exception as exc:  # ZoneInfoNotFoundError 等
        raise ValueError(f"不支持的时区: {tz_name}") from exc


def business_day_window(
    service_date: str | date,
    timezone_name: str,
    boundary_time: str = "00:00",
) -> tuple[datetime, datetime]:
    """返回乡镇业务日 [start, end) 的 UTC 时间区间。

    安置片区时区与业务日边界（乡镇采用的切日时刻）统一在此换算，
    避免把同一个分配日的额度按 UTC 自然日拆开。
    """
    if isinstance(service_date, str):
        local_date = date.fromisoformat(service_date)
    else:
        local_date = service_date
    zone = resolve_timezone(timezone_name)
    try:
        hour, minute = (int(part) for part in boundary_time.split(":"))
        boundary = time(hour=hour, minute=minute)
    except (ValueError, TypeError) as exc:
        raise ValueError("business_day_boundary 必须是 HH:MM") from exc
    start_local = datetime.combine(local_date, boundary, tzinfo=zone)
    start = start_local.astimezone(timezone.utc)
    end_local = datetime.combine(local_date + timedelta(days=1), boundary, tzinfo=zone)
    end = end_local.astimezone(timezone.utc)
    if end <= start:
        # 防御性：正常时区/边界下不会出现。
        raise ValueError("业务日时长必须为正")
    return start, end


def _overlap_seconds(
    start: datetime,
    end: datetime,
    restriction: RestrictionWindow,
) -> Decimal:
    """计算限制与业务日 [start,end) 的真实重叠秒数。

    未填写结束时间的限制自其开始起持续生效，按业务日结束收口，
    绝不把业务日开始之前或结束之后的时间计入。
    """
    overlap_start = max(start, restriction.starts_at)
    raw_end = restriction.ends_at if restriction.ends_at is not None else end
    overlap_end = min(end, raw_end)
    if overlap_end <= overlap_start:
        return ZERO
    return Decimal(str((overlap_end - overlap_start).total_seconds()))


def allocation_day_breakdown(
    *,
    nominal_capacity: Decimal,
    service_date: str | date,
    timezone_name: str,
    boundary_time: str = "00:00",
    restrictions: Iterable[RestrictionWindow] = (),
) -> dict[str, object]:
    """计算单个分配（业务）日的原始容量、各段限制重叠贡献与最终可分配量。

    容量按时间推进积分：每个限制只在它与业务日真正重叠的时段内按比例扣减；
    同一时刻多段限制重叠时连乘。计算先按天积分再统一量化，因此重复核算
    （限制顺序不同、重复调用）结果稳定一致。未填写结束时间的限制持续生效。
    """
    start, end = business_day_window(service_date, timezone_name, boundary_time)
    window_seconds = Decimal(str((end - start).total_seconds()))
    if nominal_capacity < ZERO:
        raise ValueError("原始容量不能为负数")

    # 切分点：业务日边界 + 各限制在窗口内的起止时刻。
    cuts: set[datetime] = {start, end}
    relevant: list[RestrictionWindow] = []
    for restriction in sorted(restrictions, key=lambda item: item.restriction_id):
        # 无结束时间的限制只要已开始即相关。
        if restriction.ends_at is None:
            if restriction.starts_at < end:
                relevant.append(restriction)
                cuts.add(max(start, restriction.starts_at))
            continue
        if restriction.starts_at < end and restriction.ends_at > start:
            relevant.append(restriction)
            cuts.add(max(start, restriction.starts_at))
            cuts.add(min(end, restriction.ends_at))

    ordered_cuts = sorted(cuts)

    available_seconds = ZERO  # 以"满容量秒"计的可分配积分
    # 每段的实际扣减按各限制自身折减权重稳定分摊，保证多段限制重叠时
    # 各限制的扣减贡献之和恰好等于该段（及全天）真实扣减，不重不漏。
    attributed_by_id: dict[str, Decimal] = {}
    for index in range(len(ordered_cuts) - 1):
        seg_start = ordered_cuts[index]
        seg_end = ordered_cuts[index + 1]
        seg_seconds = Decimal(str((seg_end - seg_start).total_seconds()))
        if seg_seconds <= ZERO:
            continue
        active = [
            item
            for item in relevant
            if item.starts_at <= seg_start
            and (item.ends_at is None or item.ends_at >= seg_end)
        ]
        factor = Decimal(1)
        weights: list[tuple[str, Decimal]] = []
        weight_total = ZERO
        for item in active:
            reduction = Decimal(1) - max(ZERO, min(HUNDRED, item.capacity_percent)) / HUNDRED
            factor *= Decimal(1) - reduction
            weights.append((item.restriction_id, reduction))
            weight_total += reduction
        segment_deduction = seg_seconds * (Decimal(1) - factor)
        available_seconds += seg_seconds - segment_deduction
        if not weights:
            continue
        assigned = ZERO
        # 按 restriction_id 升序分摊；除最后一个外按权重比例，末位吸收舍入余差，
        # 使 attributed 之和恒等于 segment_deduction（确定性、与输入顺序无关）。
        ordered_weights = sorted(weights, key=lambda pair: pair[0])
        for position, (restriction_id, reduction) in enumerate(ordered_weights):
            if position == len(ordered_weights) - 1:
                share_deduction = segment_deduction - assigned
            else:
                share_deduction = (
                    segment_deduction * reduction / weight_total
                    if weight_total > ZERO
                    else ZERO
                )
                assigned += share_deduction
            attributed_by_id[restriction_id] = (
                attributed_by_id.get(restriction_id, ZERO) + share_deduction
            )

    contributions: list[dict[str, object]] = []
    for restriction in relevant:
        overlap = _overlap_seconds(start, end, restriction)
        # 仅在真实重叠时段内列报；重叠为零（如窗口外）时不列报。
        if overlap <= ZERO:
            continue
        lost_seconds = attributed_by_id.get(restriction.restriction_id, ZERO)
        lost_capacity = nominal_capacity * lost_seconds / window_seconds
        contributions.append({
            "restriction_id": restriction.restriction_id,
            "capacity_percent": decimal_text(restriction.capacity_percent),
            "starts_at": utc_text(restriction.starts_at),
            "ends_at": None if restriction.ends_at is None else utc_text(restriction.ends_at),
            "overlap_seconds": decimal_text(overlap.quantize(Decimal("0.000001"))),
            "overlap_share": decimal_text(
                (overlap / window_seconds).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
            ),
            "deducted_capacity": decimal_text(quantize_volume(lost_capacity)),
        })

    available_capacity = quantize_volume(
        nominal_capacity * available_seconds / window_seconds
    )
    return {
        "timezone": timezone_name,
        "business_day_boundary": boundary_time,
        "window_start": utc_text(start),
        "window_end": utc_text(end),
        "nominal_capacity": decimal_text(quantize_volume(nominal_capacity)),
        "restrictions": contributions,
        "available_capacity": decimal_text(available_capacity),
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
