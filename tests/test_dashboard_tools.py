# SPDX-License-Identifier: MPL-2.0
# SPDX-FileCopyrightText: 2025-2026 Pantalytics B.V.
"""Tests for the dashboard tools (list_dashboards, get_dashboard, write_dashboard)."""

import json
from unittest.mock import MagicMock

import pytest
from mcp.server.mcpserver import MCPServer

from mcp_server_odoo.access_control import AccessControlError, AccessController
from mcp_server_odoo.config import OdooConfig
from mcp_server_odoo.dashboard_builder import SPEC_KEY, build_dashboard
from mcp_server_odoo.dashboards import OdooDashboardHandler, summarize_document
from mcp_server_odoo.error_handling import ValidationError
from mcp_server_odoo.odoo_connection import OdooConnection

FIELDS = {
    "mrp.production": {
        "name": {"type": "char", "string": "Reference"},
        "state": {"type": "selection", "string": "State"},
        "date_start": {"type": "datetime", "store": True, "string": "Start"},
    }
}

SPEC = {
    "title": "Manufacturing",
    "widgets": [
        {"type": "kpi", "title": "Open", "model": "mrp.production", "measure": "__count"},
        {"type": "list", "title": "Orders", "model": "mrp.production", "columns": ["name"]},
    ],
}


def stored(document):
    return json.dumps(document)


@pytest.fixture
def connection():
    connection = MagicMock(spec=OdooConnection)
    connection.is_authenticated = True
    connection.get_major_version.return_value = 19
    connection.fields_get.side_effect = lambda model, attributes=None: FIELDS[model]
    connection.search_count.return_value = 0
    connection.search.return_value = [6]
    connection.create.return_value = 42
    connection.read.return_value = [
        {
            "name": "Manufacturing",
            "dashboard_group_id": [6, "Logistics"],
            "is_published": True,
            "spreadsheet_data": stored(build_dashboard(SPEC, FIELDS)),
        }
    ]
    return connection


@pytest.fixture
def access():
    return MagicMock(spec=AccessController)


@pytest.fixture
def handler(connection, access):
    config = OdooConfig(url="http://localhost:8069/", api_key="k", database="d")
    return OdooDashboardHandler(MagicMock(spec=MCPServer), connection, access, config)


class TestListDashboards:
    async def test_lists_sections_and_dashboards(self, handler, connection):
        connection.search_read.side_effect = [
            [{"id": 2, "name": "Sales"}, {"id": 1, "name": "Finance"}],
            [
                {
                    "id": 5,
                    "name": "Cash",
                    "dashboard_group_id": [1, "Finance"],
                    "sequence": 10,
                    "is_published": True,
                }
            ],
        ]

        result = await handler._handle_list_dashboards_tool("Finance")

        assert [g["name"] for g in result["groups"]] == ["Finance", "Sales"]
        assert result["dashboards"][0]["group"] == "Finance"
        assert connection.search_read.call_args_list[1][0][1] == [
            ["dashboard_group_id.name", "=", "Finance"]
        ]


class TestGetDashboard:
    async def test_returns_summary_spec_and_link(self, handler):
        result = await handler._handle_get_dashboard_tool(9)

        assert result["spec"] == SPEC
        assert result["summary"]["scorecards"] == ["Open"]
        assert result["url"] == "http://localhost:8069/odoo/dashboards?dashboard_id=9"
        assert result["edited_in_odoo"] is False
        assert "document" not in result

    async def test_reports_editor_changes(self, handler, connection):
        connection.search_count.return_value = 3
        result = await handler._handle_get_dashboard_tool(9, raw=True)
        assert result["edited_in_odoo"] is True
        assert result["document"][SPEC_KEY] == SPEC

    async def test_unknown_dashboard(self, handler, connection):
        connection.read.return_value = []
        with pytest.raises(ValidationError, match="No dashboard with ID 9"):
            await handler._handle_get_dashboard_tool(9)


class TestWriteDashboard:
    async def test_creates_a_dashboard_in_an_existing_section(self, handler, connection, access):
        result = await handler._handle_write_dashboard_tool(SPEC, "Manufacturing", "Logistics")

        model, values = connection.create.call_args[0]
        assert model == "spreadsheet.dashboard"
        assert values["name"] == "Manufacturing"
        assert values["dashboard_group_id"] == 6
        assert json.loads(values["spreadsheet_data"])[SPEC_KEY] == SPEC
        access.validate_model_access.assert_any_call("spreadsheet.dashboard", "create")
        access.validate_model_access.assert_any_call("mrp.production", "read")
        assert result["created"] is True
        assert result["dashboard_id"] == 42

    async def test_creates_a_missing_section(self, handler, connection, access):
        connection.search.return_value = []
        connection.create.side_effect = [7, 42]

        await handler._handle_write_dashboard_tool(SPEC, "Manufacturing", "Production")

        assert connection.create.call_args_list[0][0] == (
            "spreadsheet.dashboard.group",
            {"name": "Production"},
        )
        assert connection.create.call_args_list[1][0][1]["dashboard_group_id"] == 7
        access.validate_model_access.assert_any_call("spreadsheet.dashboard.group", "create")

    async def test_replaces_a_dashboard_it_built(self, handler, connection, access):
        result = await handler._handle_write_dashboard_tool(SPEC, dashboard_id=9)

        connection.write.assert_called_once()
        assert connection.write.call_args[0][1] == [9]
        access.validate_model_access.assert_any_call("spreadsheet.dashboard", "write")
        assert result["created"] is False

    async def test_dry_run_writes_nothing(self, handler, connection, access):
        result = await handler._handle_write_dashboard_tool(SPEC, dry_run=True)

        connection.create.assert_not_called()
        connection.write.assert_not_called()
        assert result["summary"]["widgets"] == 2
        assert ("spreadsheet.dashboard", "create") not in [
            c.args for c in access.validate_model_access.call_args_list
        ]

    async def test_new_dashboard_needs_name_and_group(self, handler):
        with pytest.raises(ValidationError, match="needs a name"):
            await handler._handle_write_dashboard_tool(SPEC, group="Logistics")
        with pytest.raises(ValidationError, match="needs a group"):
            await handler._handle_write_dashboard_tool(SPEC, name="Manufacturing")

    async def test_refuses_to_replace_editor_changes(self, handler, connection):
        connection.search_count.return_value = 2
        with pytest.raises(ValidationError, match="changed in Odoo's editor"):
            await handler._handle_write_dashboard_tool(SPEC, dashboard_id=9)
        connection.write.assert_not_called()

        await handler._handle_write_dashboard_tool(SPEC, dashboard_id=9, overwrite=True)
        connection.write.assert_called_once()

    async def test_refuses_to_replace_a_dashboard_it_did_not_build(self, handler, connection):
        connection.read.return_value[0]["spreadsheet_data"] = stored({"sheets": []})
        with pytest.raises(ValidationError, match="not built by write_dashboard"):
            await handler._handle_write_dashboard_tool(SPEC, dashboard_id=9)
        connection.write.assert_not_called()

    async def test_refuses_when_revisions_are_unreadable(self, handler, connection):
        connection.search_count.side_effect = RuntimeError("no access")
        with pytest.raises(ValidationError, match="spreadsheet.revision is not readable"):
            await handler._handle_write_dashboard_tool(SPEC, dashboard_id=9)

    async def test_refuses_odoo_17(self, handler, connection):
        connection.get_major_version.return_value = 17
        with pytest.raises(ValidationError, match="needs Odoo 18 or later"):
            await handler._handle_write_dashboard_tool(SPEC, "Manufacturing", "Logistics")
        connection.create.assert_not_called()

    async def test_refuses_without_access_to_a_spec_model(self, handler, access, connection):
        def deny(model, operation):
            if model == "mrp.production":
                raise AccessControlError("Access denied to model 'mrp.production'")

        access.validate_model_access.side_effect = deny
        with pytest.raises(ValidationError, match="mrp.production"):
            await handler._handle_write_dashboard_tool(SPEC, "Manufacturing", "Logistics")
        connection.create.assert_not_called()

    async def test_bad_spec_is_a_validation_error(self, handler):
        with pytest.raises(ValidationError, match="Invalid dashboard spec"):
            await handler._handle_write_dashboard_tool(
                {"widgets": [{"type": "gauge"}]}, dry_run=True
            )

    async def test_accepts_the_spec_as_json_text(self, handler):
        result = await handler._handle_write_dashboard_tool(json.dumps(SPEC), dry_run=True)
        assert result["summary"]["lists"][0]["name"] == "Orders"


class TestSummarize:
    def test_lists_stored_as_plain_field_names(self):
        doc = build_dashboard(
            {
                "widgets": [
                    {"type": "list", "title": "MOs", "model": "mrp.production", "columns": ["name"]}
                ]
            },
            FIELDS,
        )
        assert summarize_document(doc)["lists"] == [
            {"name": "MOs", "model": "mrp.production", "columns": ["name"]}
        ]

    def test_accepts_the_object_column_form(self):
        doc = {
            "lists": {"1": {"name": "L", "model": "m", "columns": [{"name": "x", "string": "X"}]}}
        }
        assert summarize_document(doc)["lists"][0]["columns"] == ["x"]

    def test_names_an_untitled_chart_after_the_heading_above_it(self):
        doc = build_dashboard(
            {
                "widgets": [
                    {"type": "kpi", "title": "Open", "model": "mrp.production"},
                    {
                        "type": "chart",
                        "title": "By state",
                        "note": "Draft orders excluded.",
                        "model": "mrp.production",
                        "group_by": ["state"],
                    },
                ]
            },
            FIELDS,
        )
        summary = summarize_document(doc)
        assert [c["title"] for c in summary["charts"]] == ["By state"]
        assert summary["scorecards"] == ["Open"]
