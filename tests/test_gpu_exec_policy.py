from __future__ import annotations

import unittest

from scripts.gpu_exec import _decide_execution_mode, _median_cold_start_seconds


class GpuExecPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = {
            "promote_attempts": 4,
            "promote_window_seconds": 900,
            "promote_cold_start_median_seconds": 45,
            "demote_idle_seconds": 1800,
        }

    def test_override_on_forces_hot(self) -> None:
        state = {"mode": "hybrid", "last_activity_epoch": 0, "history": []}
        decision = _decide_execution_mode(
            state=state,
            policy=self.policy,
            override="on",
            now_ts=1000,
        )
        self.assertEqual(decision.mode, "hot")
        self.assertEqual(decision.reason, "forced_by_flag")

    def test_override_off_forces_hybrid(self) -> None:
        state = {"mode": "hot", "last_activity_epoch": 0, "history": []}
        decision = _decide_execution_mode(
            state=state,
            policy=self.policy,
            override="off",
            now_ts=1000,
        )
        self.assertEqual(decision.mode, "hybrid")
        self.assertEqual(decision.reason, "forced_by_flag")

    def test_promote_by_attempt_burst(self) -> None:
        now_ts = 10_000
        history = [
            {"finished_at_epoch": now_ts - 100},
            {"finished_at_epoch": now_ts - 200},
            {"finished_at_epoch": now_ts - 300},
            {"finished_at_epoch": now_ts - 400},
        ]
        state = {"mode": "hybrid", "last_activity_epoch": now_ts - 100, "history": history}
        decision = _decide_execution_mode(
            state=state,
            policy=self.policy,
            override="auto",
            now_ts=now_ts,
        )
        self.assertEqual(decision.mode, "hot")
        self.assertEqual(decision.reason, "promoted_by_attempt_burst")

    def test_demote_hot_after_idle(self) -> None:
        now_ts = 10_000
        state = {
            "mode": "hot",
            "last_activity_epoch": now_ts - 3_600,
            "history": [],
        }
        decision = _decide_execution_mode(
            state=state,
            policy=self.policy,
            override="auto",
            now_ts=now_ts,
        )
        self.assertEqual(decision.mode, "hybrid")
        self.assertEqual(decision.reason, "default_hybrid")

    def test_promote_by_cold_start_median(self) -> None:
        now_ts = 10_000
        policy = dict(self.policy)
        policy["promote_attempts"] = 10
        history = [
            {"finished_at_epoch": now_ts - 300, "cold_start": True, "startup_latency_ms": 60_000},
            {"finished_at_epoch": now_ts - 240, "cold_start": True, "startup_latency_ms": 50_000},
            {"finished_at_epoch": now_ts - 180, "cold_start": True, "startup_latency_ms": 70_000},
            {"finished_at_epoch": now_ts - 120, "cold_start": True, "startup_latency_ms": 65_000},
            {"finished_at_epoch": now_ts - 60, "cold_start": True, "startup_latency_ms": 55_000},
        ]
        state = {"mode": "hybrid", "last_activity_epoch": now_ts - 30, "history": history}
        decision = _decide_execution_mode(
            state=state,
            policy=policy,
            override="auto",
            now_ts=now_ts,
        )
        self.assertEqual(decision.mode, "hot")
        self.assertEqual(decision.reason, "promoted_by_cold_start_latency")

    def test_median_cold_start_seconds_ignores_non_cold(self) -> None:
        history = [
            {"cold_start": False, "startup_latency_ms": 90000},
            {"cold_start": True, "startup_latency_ms": 50000},
            {"cold_start": True, "startup_latency_ms": 60000},
            {"cold_start": True, "startup_latency_ms": 55000},
        ]
        value = _median_cold_start_seconds(history)
        self.assertAlmostEqual(value, 55.0)


if __name__ == "__main__":
    unittest.main()
