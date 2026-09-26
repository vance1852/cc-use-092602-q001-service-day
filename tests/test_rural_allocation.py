from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from rural_allocation.api import JsonApplication
from rural_allocation.clock import FrozenClock
from rural_allocation.errors import Conflict, Forbidden, ValidationFailed
from rural_allocation.planning import AllocationRequest, PricePoint, allocate_capacity, latest_streak
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
        self.service.announce_outage("risk", "pool-a-b", "2026-09-25T00:00:00Z", "2026-09-25T23:59:59Z", "50", "检修")
        for number, requested, priority in ((1, "40000", 10), (2, "30000", 20)):
            self.service.submit_nomination("dispatch", {"nomination_id": f"nom-{number}", "route_id": "pool-a-b", "shipper_id": f"shipper-{number}", "service_date": "2026-09-25", "requested_mu": requested, "priority": priority, "idempotency_key": f"key-{number}"})
        allocation = self.service.allocate("dispatch", "pool-a-b", "2026-09-25")
        # 业务日按安置片区时区 Asia/Shanghai 折算，停用时段只与业务日重叠 16 小时
        self.assertEqual(allocation["available_capacity"], "66666.667")
        self.assertEqual(allocation["allocations"][1]["allocated_mu"], "26666.667")
        self.service.add_inventory_lot("dispatch", {"lot_id": "lot-1", "facility_id": "village-a", "product": "cultivated-land", "grade": "PEAK_VALLEY", "quantity_mu": "60000", "unit_cost_cny": "91", "received_at": "2026-09-24T06:00:00Z"})
        transfer = self.service.dispatch_transfer("dispatch", "transfer-1", "nom-1", "lot-1", 2)
        self.assertEqual(transfer["surveyed_mu"], "40000.000")
        self.assertEqual(self.service.inventory_lot("lot-1")["available_mu"], "20000.000")

    def test_business_day_follows_settlement_timezone(self) -> None:
        self.service.create_facility("plan", {"facility_id": "kashgar-camp", "name": "喀什安置片区", "kind": "settlement", "timezone": "Asia/Urumqi", "capacity_mu": "300000"})
        self.service.create_route("plan", {"route_id": "pool-a-kashgar", "origin_id": "village-a", "destination_id": "kashgar-camp", "product": "cultivated-land", "daily_capacity": "24000", "loss_basis_points": 0, "transit_hours": 48})
        # 喀什当地 2026-09-24 23:00 至 2026-09-25 01:00（Asia/Urumqi，UTC+6）的跨午夜停用
        self.service.announce_outage("risk", "pool-a-kashgar", "2026-09-24T17:00:00Z", "2026-09-24T19:00:00Z", "0", "夜间检修")
        first = self.service.capacity_preview("dispatch", "pool-a-kashgar", "2026-09-24")
        second = self.service.capacity_preview("dispatch", "pool-a-kashgar", "2026-09-25")
        self.assertEqual(first["timezone"], "Asia/Urumqi")
        self.assertEqual(first["business_day_starts_at"], "2026-09-23T18:00:00Z")
        self.assertEqual(second["business_day_starts_at"], "2026-09-24T18:00:00Z")
        # 跨日限制在两个自然日各只扣减真正重叠的一小时
        self.assertEqual(first["available_capacity"], "23000.000")
        self.assertEqual(second["available_capacity"], "23000.000")
        self.assertEqual(first["nominal_capacity"], "24000.000")
        self.assertEqual(len(second["outages"]), 1)
        self.assertEqual(second["outages"][0]["overlap_seconds"], "3600")
        self.assertEqual(second["outages"][0]["deducted_mu"], "1000.000")

    def test_open_ended_outage_keeps_full_day_effect(self) -> None:
        self.service.announce_outage("risk", "pool-a-b", "2026-09-20T00:00:00Z", None, "25", "长期限电")
        for day in ("2026-09-25", "2026-09-26"):
            preview = self.service.capacity_preview("dispatch", "pool-a-b", day)
            self.assertEqual(preview["available_capacity"], "25000.000")
            self.assertEqual(preview["deducted_capacity"], "75000.000")
            self.assertEqual(preview["outages"][0]["overlap_seconds"], "86400")

    def test_allocate_replays_and_never_rewrites_confirmed_run(self) -> None:
        self.service.submit_nomination("dispatch", {"nomination_id": "nom-1", "route_id": "pool-a-b", "shipper_id": "household", "service_date": "2026-09-25", "requested_mu": "80000", "priority": 10, "idempotency_key": "key-1"})
        first = self.service.allocate("dispatch", "pool-a-b", "2026-09-25")
        self.assertFalse(first["replayed"])
        second = self.service.allocate("dispatch", "pool-a-b", "2026-09-25")
        self.assertTrue(second["replayed"])
        self.assertEqual(first["allocation_id"], second["allocation_id"])
        self.assertEqual(first["available_capacity"], second["available_capacity"])
        self.assertEqual(first["allocations"], second["allocations"])
        runs = self.connection.execute("SELECT * FROM allocation_runs").fetchall()
        self.assertEqual(len(runs), 1)
        self.service.announce_outage("risk", "pool-a-b", "2026-09-25T00:00:00Z", "2026-09-25T06:00:00Z", "50", "临时停用")
        with self.assertRaises(Conflict):
            self.service.allocate("dispatch", "pool-a-b", "2026-09-25")
        stored = self.connection.execute("SELECT available_capacity FROM allocation_runs").fetchone()
        self.assertEqual(stored["available_capacity"], first["available_capacity"])

    def test_facility_rejects_unknown_timezone(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.create_facility("plan", {"facility_id": "nowhere", "name": "未知片区", "kind": "storage", "timezone": "Mars/Olympus", "capacity_mu": "1"})

    def test_api_capacity_preview_matches_batch_allocation(self) -> None:
        app = JsonApplication(self.service)
        self.service.announce_outage("risk", "pool-a-b", "2026-09-24T15:00:00Z", "2026-09-24T17:00:00Z", "0", "跨午夜停用")
        self.service.submit_nomination("dispatch", {"nomination_id": "nom-1", "route_id": "pool-a-b", "shipper_id": "household", "service_date": "2026-09-25", "requested_mu": "80000", "priority": 10, "idempotency_key": "key-1"})
        preview = app.handle("GET", "/routes/pool-a-b/capacity?service_date=2026-09-25", {"X-Actor-Id": "dispatch"})
        self.assertEqual(preview.status, 200)
        allocation = self.service.allocate("dispatch", "pool-a-b", "2026-09-25")
        # 群众白天查询到的可选额度与夜间批量分配结果一致
        self.assertEqual(preview.body["available_capacity"], allocation["available_capacity"])
        self.assertEqual(preview.body["nominal_capacity"], allocation["nominal_capacity"])
        self.assertEqual(preview.body["outages"], allocation["outages"])

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
