import unittest
from types import SimpleNamespace

from momoi.tools.contracts.agenda import AGENDA_TOOL_SPECS
from momoi.tools.contracts.builtin import BUILTIN_TOOL_SPECS
from momoi.contracts import OWNER_PROGRESS_BEFORE_FIRST_CALL, OWNER_PROGRESS_FIELD
from momoi.runtime.agent.progress import (
    public_tool_spec,
    requires_owner_progress,
)
from momoi.runtime.agent.tool_surface import ToolSurface
from momoi.runtime.tool_contracts.conversation import send_bubbles_tool_spec
from momoi.runtime.tool_contracts.runtime import tool_enable_spec


class OwnerProgressPolicyTest(unittest.TestCase):
    def test_tools_declare_owner_progress_without_a_name_registry(self) -> None:
        builtins = {spec["name"]: spec for spec in BUILTIN_TOOL_SPECS}
        agenda = {spec["name"]: spec for spec in AGENDA_TOOL_SPECS}
        self.assertTrue(requires_owner_progress(builtins["curl"]))
        self.assertTrue(requires_owner_progress(agenda["goal_create"]))
        self.assertTrue(requires_owner_progress(agenda["goal_cancel"]))
        self.assertFalse(requires_owner_progress(agenda["goal_update"]))
        self.assertFalse(requires_owner_progress(agenda["goal_finish"]))
        self.assertFalse(requires_owner_progress(builtins["sleep"]))
        self.assertFalse(requires_owner_progress(builtins["read_file"]))
        self.assertTrue(
            requires_owner_progress(
                {
                    "name": "mcp__demo__lookup",
                    OWNER_PROGRESS_FIELD: OWNER_PROGRESS_BEFORE_FIRST_CALL,
                    "input_schema": {"type": "object"},
                }
            )
        )

    def test_public_schema_removes_progress_metadata_without_adding_arguments(
        self,
    ) -> None:
        curl = next(spec for spec in BUILTIN_TOOL_SPECS if spec["name"] == "curl")
        public = public_tool_spec(curl)
        self.assertNotIn(OWNER_PROGRESS_FIELD, public)
        self.assertEqual(public["input_schema"], curl["input_schema"])
        self.assertIn(OWNER_PROGRESS_FIELD, curl)

    def test_send_bubbles_schema_preserves_delivery_contract(
        self,
    ) -> None:
        spec = send_bubbles_tool_spec(["napcat", "weixin"], "napcat")
        self.assertEqual(
            spec["input_schema"]["properties"]["channel"]["enum"],
            ["napcat", "weixin"],
        )
        self.assertEqual(
            spec["input_schema"]["properties"]["channel"]["default"],
            "napcat",
        )
        bubbles = spec["input_schema"]["properties"]["bubbles"]
        self.assertIn(
            "each item is delivered as one separate chat bubble",
            bubbles["description"],
        )
        text_description = bubbles["items"]["oneOf"][0]["description"]
        self.assertIn("Assistant text is not delivered", text_description)
        self.assertIn("Only way to send owner-visible content", spec["description"])

    def test_tool_enable_catalog_uses_group_descriptions(self) -> None:
        spec = tool_enable_spec(
            {
                "demo": "Operate demo records.",
                "other": "Look up external records.",
            }
        )
        description = spec["description"]
        self.assertIn("demo: Operate demo records.", description)
        self.assertIn("other: Look up external records.", description)
        self.assertEqual(
            spec["input_schema"]["properties"]["groups"]["items"]["enum"],
            ["demo", "other"],
        )

    def test_public_schema_keeps_native_message_argument(self) -> None:
        spec = {
            "name": "mcp__demo__post",
            OWNER_PROGRESS_FIELD: OWNER_PROGRESS_BEFORE_FIRST_CALL,
            "description": "demo",
            "input_schema": {
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            },
        }
        public = public_tool_spec(spec)
        self.assertEqual(public["input_schema"]["required"], ["message"])
        self.assertEqual(
            public["input_schema"]["properties"]["message"],
            {"type": "string"},
        )

    def test_surface_keeps_progress_list_private_to_harness(self) -> None:
        channels = {"napcat": SimpleNamespace(name="napcat")}
        mcp = SimpleNamespace(
            tool_specs=[
                {
                    "name": "mcp__brave-search__brave_web_search",
                    OWNER_PROGRESS_FIELD: OWNER_PROGRESS_BEFORE_FIRST_CALL,
                    "description": "search",
                    "input_schema": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                }
            ]
        )
        config = SimpleNamespace(autonomy=SimpleNamespace(allowed_tools=["curl"]))
        surface = ToolSurface(config, mcp, channels, "napcat")
        owner = {
            spec["name"]: spec
            for spec in surface.owner_specs()
        }
        mcp_specs = {
            spec["name"]: spec
            for spec in surface.mcp_server_groups()["brave-search"]
        }
        self.assertEqual(
            surface.owner_progress_tool_names(),
            frozenset(
                {
                    "curl",
                    "goal_create",
                    "goal_cancel",
                    "mcp__brave-search__brave_web_search",
                }
            ),
        )
        for spec in [*owner.values(), *mcp_specs.values()]:
            self.assertNotIn(OWNER_PROGRESS_FIELD, spec)
