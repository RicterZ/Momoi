import unittest

from momoi.runtime.heartbeat_planner import (
    HEARTBEAT_PLAN_TOOL_NAME,
    HEARTBEAT_PLAN_TOOL_SPEC,
    HeartbeatPlanError,
    degraded_heartbeat_plan,
    parse_heartbeat_plan,
)
from momoi.runtime.turn_support import HEARTBEAT_PLANNER_SYSTEM_PROMPT


class DeprecatedHeartbeatPlannerTest(unittest.TestCase):
    def test_is_explicitly_marked_for_future_replacement(self) -> None:
        self.assertIn("deprecated compatibility stage", HEARTBEAT_PLANNER_SYSTEM_PROMPT)
        self.assertIn("Owner context routing does not use", HEARTBEAT_PLANNER_SYSTEM_PROMPT)

    def test_tool_name_and_schema_remain_stable(self) -> None:
        self.assertEqual(HEARTBEAT_PLAN_TOOL_NAME, "submit_heartbeat_plan")
        self.assertEqual(
            HEARTBEAT_PLAN_TOOL_SPEC["name"],
            HEARTBEAT_PLAN_TOOL_NAME,
        )
        self.assertEqual(
            HEARTBEAT_PLAN_TOOL_SPEC["input_schema"]["properties"]["version"]["enum"],
            [3],
        )

    def test_degraded_plan_still_round_trips_through_validation(self) -> None:
        plan = degraded_heartbeat_plan("resting", "test fallback")
        self.assertEqual(parse_heartbeat_plan(plan), plan)

    def test_invalid_plan_is_rejected(self) -> None:
        with self.assertRaisesRegex(HeartbeatPlanError, "invalid_heartbeat_plan"):
            parse_heartbeat_plan({"version": 3})


if __name__ == "__main__":
    unittest.main()
