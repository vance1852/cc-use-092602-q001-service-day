from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from rural_allocation.api import JsonApplication
from rural_allocation.clock import FrozenClock
from rural_allocation.errors import Conflict, Forbidden
from rural_allocation.planning import (
    AllocationRequest,
    PricePoint,
    RestrictionWindow,
    allocate_capacity,
    allocation_day_breakdown,
    business_day_window,
    latest_streak,
)
from rural_allocation.service import SupplyService
from rural_allocation.risk import DemandBucket, inventory_coverage, mark_to_market, supply_gap


class PlanningTests(unittest.TestCase):
    def test_latest_down_streak_uses_first_close_as_base(self) -> None:
        streak = latest_streak([
            PricePoint("2026-09-18", Decimal("108")),
            PricePoint("2026-09-19", Decimal("105")),
            PricePoint("2026-09-20", Decimal("102")),
            PricePoint("2026-09-21", Decimal("98")),
        ])
        self.assertEqual(streak.direction, "down")
        self.assertEqual(streak.sessions, 4)
        self.assertEqual(streak.start_date, "2026-09-18")
        self.assertEqual(streak.end_close, Decimal("98"))

    def test_allocation_is_stable_and_does_not_exceed_capacity(self) -> None:
        rows = allocate_capacity(Decimal("100"), [
            AllocationRequest("later", Decimal("80"), 20, "2026-09-24T09:00:00Z"),
            AllocationRequest("first", Decimal("70"), 10, "2026-09-24T10:00:00Z"),
        ])
        self.assertEqual(rows[0]["nomination_id"], "first")
        self.assertEqual(rows[0]["allocated_mu"], "70.000")
        self.assertEqual(rows[1]["allocated_mu"], "30.000")

    def test_inventory_coverage_and_supply_gap(self) -> None:
        coverage = inventory_coverage(
            [{"facility_id": "settlement", "product": "homestead", "available_mu": "250"}],
            [DemandBucket("settlement", "homestead", Decimal("100"), Decimal("20"))],
        )
        self.assertEqual(coverage[0]["coverage_days"], "2.30")
        self.assertTrue(coverage[0]["below_three_days"])
        gap = supply_gap(
            opening_inventory=Decimal("100"),
            confirmed_inbound=Decimal("30"),
            forecast_demand=Decimal("120"),
            protected_reserve=Decimal("40"),
        )
        self.assertEqual(gap["supply_gap"], "30.000")

    def test_mark_to_market_groups_deterministically(self) -> None:
        result = mark_to_market(
            [{"position_id": "p1", "market_index": "PEAK_VALLEY", "quantity_mu": "100", "entry_price_cny": "105"}],
            {"PEAK_VALLEY": Decimal("98")},
        )
        self.assertEqual(result["unrealized_pnl_cny"], "-700.00")


class SupplyServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = SupplyService(self.connection, self.clock)
        for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"), ("audit", "auditor")):
            self.service.create_user(user_id, user_id, role)
        self.service.create_facility("plan", {"facility_id": "village-a", "name": "北部示范村", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_mu": "500000"})
        self.service.create_facility("plan", {"facility_id": "settlement-b", "name": "东部安置片区", "kind": "settlement", "timezone": "Asia/Shanghai", "capacity_mu": "800000"})
        self.service.create_route("plan", {"route_id": "pool-a-b", "origin_id": "village-a", "destination_id": "settlement-b", "product": "cultivated-land", "daily_capacity": "100000", "loss_basis_points": 25, "transit_hours": 36})

    def tearDown(self) -> None:
        self.connection.close()

    def quote(self, day: int, close: str) -> dict[str, object]:
        return self.service.record_quote("plan", {"market_index": "PEAK_VALLEY", "trade_date": f"2026-09-{day}", "close_cny": close, "source_revision": f"r-{day}", "observed_at": f"2026-09-{day}T21:00:00Z"})

    def test_quote_revisions_preserve_history(self) -> None:
        first = self.quote(23, "98")
        second = self.service.record_quote("plan", {"market_index": "PEAK_VALLEY", "trade_date": "2026-09-23", "close_cny": "97.8", "source_revision": "r-23-corrected", "observed_at": "2026-09-23T22:00:00Z"})
        self.assertNotEqual(first["quote_id"], second["quote_id"])
        rows = self.connection.execute("SELECT * FROM market_index_quotes ORDER BY quote_id").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["supersedes_quote_id"], rows[0]["quote_id"])

    def test_nomination_replay_and_payload_conflict(self) -> None:
        payload = {"nomination_id": "nom-1", "route_id": "pool-a-b", "shipper_id": "household", "service_date": "2026-09-25", "requested_mu": "80000", "priority": 10, "idempotency_key": "key-1"}
        first = self.service.submit_nomination("dispatch", payload)
        self.assertEqual(first, self.service.submit_nomination("dispatch", payload))
        changed = dict(payload, requested_mu="81000")
        with self.assertRaises(Conflict):
            self.service.submit_nomination("dispatch", changed)

    def test_outage_reduces_allocation_and_transfer_consumes_inventory(self) -> None:
        # 安置片区 Asia/Shanghai 的 2026-09-25 业务日为 09-24T16:00Z 至 09-25T16:00Z，
        # 限制需覆盖整个本地业务日才是整段五折，而非按 UTC 自然日登记。
        self.service.announce_outage("risk", "pool-a-b", "2026-09-24T16:00:00Z", "2026-09-25T16:00:00Z", "50", "检修")
        for number, requested, priority in ((1, "40000", 10), (2, "30000", 20)):
            self.service.submit_nomination("dispatch", {"nomination_id": f"nom-{number}", "route_id": "pool-a-b", "shipper_id": f"shipper-{number}", "service_date": "2026-09-25", "requested_mu": requested, "priority": priority, "idempotency_key": f"key-{number}"})
        allocation = self.service.allocate("dispatch", "pool-a-b", "2026-09-25")
        self.assertEqual(allocation["available_capacity"], "50000.000")
        self.assertEqual(allocation["allocations"][1]["allocated_mu"], "10000.000")
        self.service.add_inventory_lot("dispatch", {"lot_id": "lot-1", "facility_id": "village-a", "product": "cultivated-land", "grade": "PEAK_VALLEY", "quantity_mu": "60000", "unit_cost_cny": "91", "received_at": "2026-09-24T06:00:00Z"})
        transfer = self.service.dispatch_transfer("dispatch", "transfer-1", "nom-1", "lot-1", 2)
        self.assertEqual(transfer["surveyed_mu"], "40000.000")
        self.assertEqual(self.service.inventory_lot("lot-1")["available_mu"], "20000.000")

    def test_local_midnight_outage_is_split_by_utc_day_but_counted_once(self) -> None:
        # 喀什片区（UTC+6 无 DST）：在本地午夜附近登记半天停用。
        self.service.create_facility("plan", {"facility_id": "kashi", "name": "喀什安置片区", "kind": "settlement", "timezone": "Asia/Urumqi", "capacity_mu": "0"})
        self.service.create_route("plan", {"route_id": "pool-kashi", "origin_id": "village-a", "destination_id": "kashi", "product": "homestead", "daily_capacity": "100000", "loss_basis_points": 0, "transit_hours": 12})
        # 本地 09-25 00:00 起停用 12 小时（= 09-24T18:00Z 至 09-25T06:00Z），
        # 该限制横跨两个 UTC 自然日，但只应在所属本地业务日内扣半天。
        self.service.announce_outage("risk", "pool-kashi", "2026-09-24T18:00:00Z", "2026-09-25T06:00:00Z", "0", "午夜临时停用")
        breakdown = self.service.capacity_breakdown("dispatch", "pool-kashi", "2026-09-25")
        self.assertEqual(breakdown["window_start"], "2026-09-24T18:00:00Z")
        self.assertEqual(breakdown["window_end"], "2026-09-25T18:00:00Z")
        self.assertEqual(len(breakdown["restrictions"]), 1)
        self.assertEqual(breakdown["restrictions"][0]["overlap_seconds"], "43200.000000")
        self.assertEqual(breakdown["available_capacity"], "50000.000")
        # 同一限制不应泄漏到相邻本地业务日。
        next_day = self.service.capacity_breakdown("dispatch", "pool-kashi", "2026-09-26")
        self.assertEqual(next_day["restrictions"], [])
        self.assertEqual(next_day["available_capacity"], "100000.000")

    def test_open_ended_outage_remains_in_force(self) -> None:
        # 未填写结束时间：自本地业务日中点起持续停用至业务日结束（扣半天）。
        self.service.announce_outage("risk", "pool-a-b", "2026-09-25T08:00:00Z", None, "0", "长期停用")
        # Shanghai 业务日 09-24T16:00Z 至 09-25T16:00Z：自 08:00Z 起剩余 8 小时停用。
        breakdown = self.service.capacity_breakdown("dispatch", "pool-a-b", "2026-09-25")
        self.assertEqual(breakdown["restrictions"][0]["ends_at"], None)
        self.assertEqual(breakdown["restrictions"][0]["overlap_seconds"], "28800.000000")
        self.assertEqual(breakdown["available_capacity"], "66666.667")
        # 之后的业务日整日停用。
        later = self.service.capacity_breakdown("dispatch", "pool-a-b", "2026-09-26")
        self.assertEqual(later["available_capacity"], "0.000")

    def test_capacity_recomputation_is_stable_regardless_of_order(self) -> None:
        window = business_day_window("2026-09-25", "Asia/Shanghai")
        base = dict(
            nominal_capacity=Decimal("100000"),
            service_date="2026-09-25",
            timezone_name="Asia/Shanghai",
        )
        r1 = RestrictionWindow("a", window[0], window[0] + timedelta(hours=12), Decimal("50"))
        r2 = RestrictionWindow("b", window[0] + timedelta(hours=6), window[1], Decimal("25"))
        first = allocation_day_breakdown(restrictions=[r1, r2], **base)
        second = allocation_day_breakdown(restrictions=[r2, r1], **base)
        third = allocation_day_breakdown(restrictions=[r1, r2], **base)
        self.assertEqual(first, second)
        self.assertEqual(first, third)
        # 各限制扣减贡献之和恰等于原始容量与可分配量之差（不重不漏）。
        deducted = sum(Decimal(item["deducted_capacity"]) for item in first["restrictions"])
        self.assertEqual(
            Decimal(first["nominal_capacity"]) - Decimal(first["available_capacity"]),
            deducted,
        )

    def test_business_day_boundary_shifts_the_window(self) -> None:
        self.service.create_facility("plan", {"facility_id": "xian", "name": "西安安置片区", "kind": "settlement", "timezone": "Asia/Shanghai", "business_day_boundary": "06:00", "capacity_mu": "0"})
        self.service.create_route("plan", {"route_id": "pool-xian", "origin_id": "village-a", "destination_id": "xian", "product": "homestead", "daily_capacity": "100000", "loss_basis_points": 0, "transit_hours": 12})
        # 本地 06:00 切日：09-25 业务日 = 09-24T22:00Z 至 09-25T22:00Z。
        self.service.announce_outage("risk", "pool-xian", "2026-09-24T22:00:00Z", "2026-09-25T10:00:00Z", "50", "凌晨检修")
        breakdown = self.service.capacity_breakdown("dispatch", "pool-xian", "2026-09-25")
        self.assertEqual(breakdown["business_day_boundary"], "06:00")
        self.assertEqual(breakdown["window_start"], "2026-09-24T22:00:00Z")
        # 限制重叠 12 小时，半天五折 => 75000。
        self.assertEqual(breakdown["available_capacity"], "75000.000")

    def test_allocation_records_breakdown_and_does_not_silently_rewrite_history(self) -> None:
        self.service.announce_outage("risk", "pool-a-b", "2026-09-24T16:00:00Z", "2026-09-25T16:00:00Z", "50", "检修")
        self.service.submit_nomination("dispatch", {"nomination_id": "nom-1", "route_id": "pool-a-b", "shipper_id": "h1", "service_date": "2026-09-25", "requested_mu": "80000", "priority": 10, "idempotency_key": "k1"})
        allocation = self.service.allocate("dispatch", "pool-a-b", "2026-09-25")
        self.assertEqual(allocation["available_capacity"], "50000.000")
        stored = self.service.allocation_run("dispatch", allocation["allocation_id"])
        self.assertEqual(stored["available_capacity"], "50000.000")
        self.assertEqual(stored["capacity_breakdown"]["available_capacity"], "50000.000")
        self.assertEqual(stored["capacity_breakdown"]["nominal_capacity"], "100000.000")
        # 事后新增/延长限制不得改写已确认结果的落库值。
        self.service.announce_outage("risk", "pool-a-b", "2026-09-24T16:00:00Z", "2026-09-25T16:00:00Z", "0", "追加全停")
        again = self.service.allocation_run("dispatch", allocation["allocation_id"])
        self.assertEqual(again["available_capacity"], "50000.000")
        self.assertEqual(again["allocations"][0]["allocated_mu"], "50000.000")

    def test_scenario_is_approved_and_replayed_by_input(self) -> None:
        self.quote(23, "98")
        self.service.add_inventory_lot("dispatch", {"lot_id": "lot-1", "facility_id": "village-a", "product": "cultivated-land", "grade": "PEAK_VALLEY", "quantity_mu": "60000", "unit_cost_cny": "91", "received_at": "2026-09-24T06:00:00Z"})
        self.service.create_scenario("plan", {"scenario_id": "restart", "name": "机组检修恢复", "market_index_drop_percent": "9", "route_capacity_changes": {"pool-a-b": "20"}, "demand_changes": {"village-a:cultivated-land": "-5"}})
        with self.assertRaises(Forbidden):
            self.service.approve_scenario("plan", "restart", 1)
        self.service.approve_scenario("risk", "restart", 1)
        first = self.service.run_scenario("plan", "restart", "2026-09-23")
        second = self.service.run_scenario("plan", "restart", "2026-09-23")
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["run_id"], second["run_id"])

    def test_audit_chain_detects_tampering(self) -> None:
        self.assertTrue(self.service.audit_chain("audit")["valid"])
        self.connection.execute("UPDATE supply_audit_events SET payload_json='{}' WHERE event_id=1")
        self.assertFalse(self.service.audit_chain("audit")["valid"])

    def test_api_exposes_browser_free_boundary(self) -> None:
        app = JsonApplication(self.service)
        self.assertEqual(app.handle("GET", "/health").status, 200)
        response = app.handle("GET", "/quotes/summary/PEAK_VALLEY", {"X-Actor-Id": "plan"})
        self.assertEqual(response.status, 404)
        self.assertEqual(response.body["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
