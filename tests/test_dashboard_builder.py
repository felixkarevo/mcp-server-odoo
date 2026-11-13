# SPDX-License-Identifier: MPL-2.0
# SPDX-FileCopyrightText: 2025-2026 Pantalytics B.V.
"""Tests for the declarative dashboard builder."""

import json

import pytest

from mcp_server_odoo.dashboard_builder import (
    DATA_VERSION,
    DashboardSpecError,
    build_dashboard,
    collect_date_hints,
    collect_models,
)

INVOICE_FIELDS = {
    "name": {"type": "char", "store": True, "string": "Number"},
    "invoice_date": {"type": "date", "store": True, "string": "Invoice Date"},
    "create_date": {"type": "datetime", "store": True, "string": "Created on"},
    "partner_id": {"type": "many2one", "relation": "res.partner", "string": "Partner"},
    "create_uid": {"type": "many2one", "relation": "res.users", "string": "Created by"},
    "invoice_user_id": {"type": "many2one", "relation": "res.users", "string": "Salesperson"},
    "amount_total": {"type": "monetary", "store": True, "string": "Total"},
    "amount_residual": {"type": "monetary", "store": True, "string": "Amount Due"},
}

FIELDS = {"account.move": INVOICE_FIELDS}


def kpi_spec():
    return {
        "title": "Cash",
        "filters": [
            {"type": "date", "label": "Period", "default": "last_90_days"},
            {"type": "relation", "label": "Customer", "model": "res.partner"},
        ],
        "widgets": [
            {
                "type": "kpi",
                "title": "Open receivables",
                "model": "account.move",
                "measure": "amount_residual",
                "domain": [["move_type", "=", "out_invoice"]],
            },
            {"type": "kpi", "title": "Invoices", "model": "account.move", "measure": "__count"},
        ],
    }


class TestDocumentShape:
    def test_minimal_document_is_json_serialisable(self):
        doc = build_dashboard({"widgets": []})
        assert json.loads(json.dumps(doc)) == doc

    def test_declares_the_migratable_format_version(self):
        doc = build_dashboard({"widgets": []})
        assert doc["version"] == DATA_VERSION
        assert doc["revisionId"] == "START_REVISION"

    def test_has_a_visible_dashboard_sheet_and_a_hidden_data_sheet(self):
        doc = build_dashboard(kpi_spec(), FIELDS)
        dashboard, data = doc["sheets"]
        assert dashboard["name"] == "Dashboard"
        assert dashboard["isVisible"] is True
        assert data["name"] == "Data"
        assert data["isVisible"] is False

    def test_rejects_an_unknown_widget_type(self):
        with pytest.raises(DashboardSpecError, match="unknown widget type"):
            build_dashboard({"widgets": [{"type": "gauge", "model": "account.move"}]})

    def test_rejects_a_widget_without_a_model(self):
        with pytest.raises(DashboardSpecError, match="needs model"):
            build_dashboard({"widgets": [{"type": "kpi", "title": "x"}]})


class TestKpis:
    def test_each_kpi_gets_a_pivot_a_formula_and_a_scorecard(self):
        doc = build_dashboard(kpi_spec(), FIELDS)
        assert len(doc["pivots"]) == 2
        scorecards = [f for f in doc["sheets"][0]["figures"] if f["data"]["type"] == "scorecard"]
        assert len(scorecards) == 2
        assert all(f["data"]["keyValue"].startswith("Data!") for f in scorecards)

    def test_kpi_pivot_aggregates_without_grouping(self):
        doc = build_dashboard(kpi_spec(), FIELDS)
        pivot = doc["pivots"]["1"]
        assert pivot["rows"] == []
        assert pivot["columns"] == []
        assert pivot["measures"] == [{"id": "amount_residual", "fieldName": "amount_residual"}]

    def test_the_formula_addresses_the_pivot_measure(self):
        doc = build_dashboard(kpi_spec(), FIELDS)
        cells = doc["sheets"][1]["cells"]
        assert cells["B1"] == '=PIVOT.VALUE(1,"amount_residual")'
        assert cells["B2"] == '=PIVOT.VALUE(2,"__count")'

    def test_consecutive_kpis_are_laid_out_side_by_side(self):
        doc = build_dashboard(kpi_spec(), FIELDS)
        scorecards = [f for f in doc["sheets"][0]["figures"] if f["data"]["type"] == "scorecard"]
        # Row 0 holds the page title; the two cards share the width below it.
        assert {f["row"] for f in scorecards} == {1}
        assert [f["offset"]["x"] for f in scorecards] == [0, 507]
        assert {f["width"] for f in scorecards} == {492}


class TestCharts:
    def test_builds_an_odoo_chart_figure(self):
        doc = build_dashboard(
            {
                "widgets": [
                    {
                        "type": "chart",
                        "chart": "line",
                        "title": "Revenue",
                        "model": "account.move",
                        "measure": "amount_total",
                        "group_by": ["invoice_date:month"],
                    }
                ]
            },
            FIELDS,
        )
        figure = doc["sheets"][0]["figures"][0]
        assert figure["data"]["type"] == "odoo_line"
        assert figure["data"]["metaData"] == {
            "groupBy": ["invoice_date:month"],
            "measure": "amount_total",
            "order": None,
            "resModel": "account.move",
            "mode": "line",
        }
        assert figure["data"]["searchParams"]["groupBy"] == ["invoice_date"]

    def test_rejects_a_chart_without_a_group_by(self):
        with pytest.raises(DashboardSpecError, match="needs a group_by"):
            build_dashboard(
                {"widgets": [{"type": "chart", "model": "account.move", "measure": "x"}]}
            )

    def test_rejects_an_unknown_chart_kind(self):
        with pytest.raises(DashboardSpecError, match="unknown chart type"):
            build_dashboard(
                {
                    "widgets": [
                        {
                            "type": "chart",
                            "chart": "donut",
                            "model": "account.move",
                            "group_by": ["partner_id"],
                        }
                    ]
                }
            )


class TestPivotsAndLists:
    def test_pivot_spills_from_a_single_formula(self):
        doc = build_dashboard(
            {
                "widgets": [
                    {
                        "type": "pivot",
                        "title": "Top customers",
                        "model": "account.move",
                        "rows": ["partner_id"],
                        "measures": ["amount_total"],
                        "sort_by": "amount_total",
                        "limit": 5,
                    }
                ]
            },
            FIELDS,
        )
        cells = doc["sheets"][0]["cells"]
        assert cells["A1"].startswith("[Top customers](odoo://view/")
        assert cells["A2"] == "=PIVOT(1, 5, FALSE, FALSE)"
        assert doc["pivots"]["1"]["sortedColumn"] == {
            "measure": "amount_total",
            "order": "desc",
            "domain": [],
        }

    def test_list_writes_headers_and_one_formula_per_line(self):
        doc = build_dashboard(
            {
                "widgets": [
                    {
                        "type": "list",
                        "title": "Overdue",
                        "model": "account.move",
                        "columns": ["name", {"name": "amount_residual", "string": "Due"}],
                        "order": ["amount_residual desc"],
                        "limit": 2,
                    }
                ]
            },
            FIELDS,
        )
        cells = doc["sheets"][0]["cells"]
        assert cells["A2"] == '=ODOO.LIST.HEADER(1,"name")'
        assert cells["B2"] == '=ODOO.LIST.HEADER(1,"amount_residual","Due")'
        assert cells["A3"] == '=ODOO.LIST(1,1,"name")'
        assert cells["B4"] == '=ODOO.LIST(1,2,"amount_residual")'
        assert doc["lists"]["1"]["orderBy"] == [{"name": "amount_residual", "asc": False}]

    def test_list_columns_are_stored_as_plain_field_names(self):
        """An object here fails the o-spreadsheet migration on load."""
        doc = build_dashboard(
            {
                "widgets": [
                    {
                        "type": "list",
                        "model": "account.move",
                        "columns": ["name", {"name": "amount_residual", "string": "Due"}],
                    }
                ]
            },
            FIELDS,
        )
        assert doc["lists"]["1"]["columns"] == ["name", "amount_residual"]

    def test_rejects_a_list_without_columns(self):
        with pytest.raises(DashboardSpecError, match="needs columns"):
            build_dashboard({"widgets": [{"type": "list", "model": "account.move"}]})


class TestGlobalFilters:
    def test_date_filter_binds_to_the_charted_date_field(self):
        doc = build_dashboard(
            {
                "filters": [{"type": "date", "label": "Period"}],
                "widgets": [
                    {
                        "type": "chart",
                        "model": "account.move",
                        "measure": "amount_total",
                        "group_by": ["invoice_date:month"],
                    }
                ],
            },
            FIELDS,
        )
        filter_id = doc["globalFilters"][0]["id"]
        matching = doc["sheets"][0]["figures"][0]["data"]["fieldMatching"]
        assert matching[filter_id] == {"chain": "invoice_date", "type": "date", "offset": 0}

    def test_relation_filter_prefers_the_field_named_after_its_model(self):
        doc = build_dashboard(
            {
                "filters": [{"type": "relation", "label": "Customer", "model": "res.partner"}],
                "widgets": [{"type": "kpi", "model": "account.move", "measure": "amount_total"}],
            },
            FIELDS,
        )
        filter_id = doc["globalFilters"][0]["id"]
        assert doc["pivots"]["1"]["fieldMatching"][filter_id]["chain"] == "partner_id"

    def test_relation_filter_skips_bookkeeping_fields(self):
        doc = build_dashboard(
            {
                "filters": [{"type": "relation", "label": "Salesperson", "model": "res.users"}],
                "widgets": [{"type": "kpi", "model": "account.move", "measure": "amount_total"}],
            },
            FIELDS,
        )
        filter_id = doc["globalFilters"][0]["id"]
        assert doc["pivots"]["1"]["fieldMatching"][filter_id]["chain"] == "invoice_user_id"

    def test_an_explicit_chain_wins_over_the_automatic_match(self):
        doc = build_dashboard(
            {
                "filters": [{"type": "relation", "label": "Customer", "model": "res.partner"}],
                "widgets": [
                    {
                        "type": "kpi",
                        "model": "account.move",
                        "measure": "amount_total",
                        "filters": {"Customer": "partner_id.parent_id"},
                    }
                ],
            },
            FIELDS,
        )
        filter_id = doc["globalFilters"][0]["id"]
        chain = doc["pivots"]["1"]["fieldMatching"][filter_id]["chain"]
        assert chain == "partner_id.parent_id"

    def test_filters_false_detaches_the_widget(self):
        doc = build_dashboard(
            {
                "filters": [{"type": "date", "label": "Period"}],
                "widgets": [
                    {
                        "type": "kpi",
                        "model": "account.move",
                        "measure": "amount_total",
                        "filters": False,
                    }
                ],
            },
            FIELDS,
        )
        assert doc["pivots"]["1"]["fieldMatching"] == {}

    def test_without_field_metadata_nothing_is_matched(self):
        doc = build_dashboard(
            {
                "filters": [{"type": "date", "label": "Period"}],
                "widgets": [{"type": "kpi", "model": "account.move", "measure": "amount_total"}],
            }
        )
        assert doc["pivots"]["1"]["fieldMatching"] == {}

    def test_relation_filter_needs_a_model(self):
        with pytest.raises(DashboardSpecError, match="needs a model"):
            build_dashboard({"filters": [{"type": "relation", "label": "Customer"}], "widgets": []})


class TestCollectModels:
    def test_lists_every_model_the_spec_touches(self):
        spec = {
            "widgets": [
                {"type": "kpi", "model": "account.move", "measure": "x"},
                {
                    "type": "chart",
                    "model": "sale.report",
                    "measure": "x",
                    "group_by": ["date"],
                },
                {"type": "text", "text": "hello"},
            ]
        }
        assert collect_models(spec) == ["account.move", "sale.report"]


class TestDateHints:
    def test_one_date_field_per_model_across_widgets(self):
        """A KPI must filter on the same date field as the chart beside it."""
        doc = build_dashboard(
            {
                "filters": [{"type": "date", "label": "Period"}],
                "widgets": [
                    {"type": "kpi", "model": "account.move", "measure": "amount_total"},
                    {
                        "type": "chart",
                        "model": "account.move",
                        "measure": "amount_total",
                        "group_by": ["invoice_date:month"],
                    },
                ],
            },
            FIELDS,
        )
        filter_id = doc["globalFilters"][0]["id"]
        kpi_chain = doc["pivots"]["1"]["fieldMatching"][filter_id]["chain"]
        chart_chain = doc["sheets"][0]["figures"][1]["data"]["fieldMatching"][filter_id]["chain"]
        assert kpi_chain == chart_chain == "invoice_date"

    def test_an_explicit_date_field_sets_the_hint(self):
        hints = collect_date_hints(
            {"widgets": [{"model": "account.move", "date_field": "invoice_date_due"}]}
        )
        assert hints == {"account.move": "invoice_date_due"}


def list_spec(**overrides):
    widget = {
        "type": "list",
        "title": "Overdue",
        "model": "account.move",
        "columns": [
            "name",
            "partner_id",
            {"name": "invoice_date", "string": "Date"},
            {"name": "amount_residual", "string": "Due", "highlight": "negative", "width": 130},
        ],
        "domain": [["state", "=", "posted"]],
        "limit": 3,
    }
    widget.update(overrides)
    return {"widgets": [widget]}


class TestLayout:
    def test_page_title_and_description_open_the_sheet(self):
        doc = build_dashboard(
            {"title": "Cash", "description": "Where the money is.", "widgets": []}, FIELDS
        )
        sheet = doc["sheets"][0]
        assert sheet["cells"]["A1"] == "Cash"
        assert sheet["cells"]["A2"] == "Where the money is."
        assert sheet["rows"]["0"]["size"] > 23
        # The description spans the dashboard width in one merged, wrapped cell.
        assert sheet["merges"] and sheet["merges"][0].startswith("A2:")
        style = doc["styles"][sheet["styles"]["A2"]]
        assert style["wrapping"] == "wrap"

    def test_accent_colours_the_headings(self):
        doc = build_dashboard({"title": "Cash", "accent": "#7c3aed", "widgets": []}, FIELDS)
        sheet = doc["sheets"][0]
        assert doc["styles"][sheet["styles"]["A1"]]["textColor"] == "#7c3aed"

    def test_list_heading_links_to_its_records(self):
        doc = build_dashboard(list_spec(), FIELDS)
        heading = doc["sheets"][0]["cells"]["A1"]
        label, _, url = heading.partition("](")
        assert label == "[Overdue"
        payload = json.loads(url[: -len(")")].removeprefix("odoo://view/"))
        assert payload["viewType"] == "list"
        assert payload["action"]["modelName"] == "account.move"
        assert payload["action"]["domain"] == [["state", "=", "posted"]]

    def test_link_false_leaves_a_plain_heading(self):
        doc = build_dashboard(list_spec(link=False), FIELDS)
        assert doc["sheets"][0]["cells"]["A1"] == "Overdue"

    def test_heading_row_is_ruled_across_the_width(self):
        doc = build_dashboard(list_spec(), FIELDS)
        sheet = doc["sheets"][0]
        ruled = [r for r in sheet["borders"] if r.startswith("A1:")]
        assert ruled
        assert doc["borders"][sheet["borders"][ruled[0]]]["bottom"]["color"]

    def test_note_sits_under_the_heading(self):
        doc = build_dashboard(list_spec(note="Sorted by due date."), FIELDS)
        cells = doc["sheets"][0]["cells"]
        assert cells["A2"] == "Sorted by due date."
        assert cells["A3"] == '=ODOO.LIST.HEADER(1,"name")'

    def test_list_columns_get_widths_from_their_types(self):
        doc = build_dashboard(list_spec(), FIELDS)
        cols = doc["sheets"][0]["cols"]
        assert cols["0"]["size"] == 240  # name
        assert cols["1"]["size"] == 220  # many2one
        assert cols["2"]["size"] == 100  # date
        assert cols["3"]["size"] == 130  # explicit width

    def test_date_columns_are_formatted_without_the_time(self):
        doc = build_dashboard(list_spec(), FIELDS)
        sheet = doc["sheets"][0]
        fmt_id = sheet["formats"]["C3:C5"]
        assert doc["formats"][fmt_id] == "m/d/yyyy"

    def test_date_columns_follow_the_spec_locale(self):
        spec = list_spec()
        spec["locale"] = {"code": "de_DE", "dateFormat": "dd.mm.yyyy"}
        doc = build_dashboard(spec, FIELDS)
        fmt_id = doc["sheets"][0]["formats"]["C3:C5"]
        assert doc["formats"][fmt_id] == "dd.mm.yyyy"

    def test_negative_highlight_becomes_a_conditional_format(self):
        doc = build_dashboard(list_spec(), FIELDS)
        (cf,) = doc["sheets"][0]["conditionalFormats"]
        assert cf["ranges"] == ["D3:D5"]
        # 18.4.1 renamed the operators; at our format version the new name holds.
        assert cf["rule"]["operator"] == "isLessThan"
        assert cf["rule"]["values"] == ["0"]

    def test_rejects_an_unknown_highlight(self):
        spec = list_spec()
        spec["widgets"][0]["columns"][3]["highlight"] = "positive"
        with pytest.raises(DashboardSpecError, match="highlight"):
            build_dashboard(spec, FIELDS)

    def test_full_width_chart_leaves_the_title_to_its_heading(self):
        doc = build_dashboard(
            {
                "widgets": [
                    {
                        "type": "chart",
                        "title": "Revenue",
                        "model": "account.move",
                        "group_by": ["invoice_date:month"],
                    }
                ]
            },
            FIELDS,
        )
        sheet = doc["sheets"][0]
        assert sheet["cells"]["A1"].startswith("[Revenue](odoo://view/")
        assert sheet["figures"][0]["data"]["title"]["text"] == ""

    def test_row_puts_charts_side_by_side(self):
        doc = build_dashboard(
            {
                "widgets": [
                    {
                        "type": "row",
                        "widgets": [
                            {
                                "title": "By month",
                                "model": "account.move",
                                "group_by": ["invoice_date:month"],
                            },
                            {
                                "title": "By customer",
                                "chart": "pie",
                                "model": "account.move",
                                "group_by": ["partner_id"],
                                "height": 300,
                            },
                        ],
                    }
                ]
            },
            FIELDS,
        )
        figures = doc["sheets"][0]["figures"]
        assert [f["offset"]["x"] for f in figures] == [0, 507]
        assert {f["width"] for f in figures} == {492}
        assert {f["height"] for f in figures} == {345}
        assert [f["data"]["title"]["text"] for f in figures] == ["By month", "By customer"]

    def test_row_accepts_charts_only(self):
        with pytest.raises(DashboardSpecError, match="charts only"):
            build_dashboard(
                {"widgets": [{"type": "row", "widgets": [{"type": "kpi", "model": "x"}]}]}
            )

    def test_models_and_date_hints_look_inside_rows(self):
        spec = {
            "widgets": [
                {
                    "type": "row",
                    "widgets": [
                        {"model": "account.move", "group_by": ["invoice_date:month"]},
                        {"model": "sale.order", "group_by": ["state"]},
                    ],
                }
            ]
        }
        assert collect_models(spec) == ["account.move", "sale.order"]
        assert collect_date_hints(spec) == {"account.move": "invoice_date"}

    def test_kpis_wrap_after_four_cards(self):
        kpi = {"type": "kpi", "title": "n", "model": "account.move"}
        doc = build_dashboard({"widgets": [dict(kpi) for _ in range(5)]}, FIELDS)
        figures = doc["sheets"][0]["figures"]
        assert len({f["row"] for f in figures}) == 2
        assert figures[4]["offset"]["x"] == 0

    def test_kpi_tone_sets_the_card_colour(self):
        doc = build_dashboard(
            {"widgets": [{"type": "kpi", "model": "account.move", "tone": "bad"}]}, FIELDS
        )
        assert doc["sheets"][0]["figures"][0]["data"]["background"] == "#FDECEC"

    def test_rejects_an_unknown_kpi_tone(self):
        with pytest.raises(DashboardSpecError, match="tone"):
            build_dashboard(
                {"widgets": [{"type": "kpi", "model": "account.move", "tone": "loud"}]}, FIELDS
            )

    def test_default_locale_is_english(self):
        doc = build_dashboard({"widgets": []})
        assert doc["settings"]["locale"]["code"] == "en_US"

    def test_spec_locale_replaces_the_default(self):
        locale = {"code": "de_DE", "dateFormat": "dd.mm.yyyy"}
        doc = build_dashboard({"locale": locale, "widgets": []})
        assert doc["settings"]["locale"] == locale


class TestFieldChecks:
    def test_rejects_an_unknown_list_column_and_suggests_one(self):
        spec = list_spec(columns=["name", "amount_residal"])
        with pytest.raises(DashboardSpecError, match="did you mean amount_residual"):
            build_dashboard(spec, FIELDS)

    def test_rejects_an_unknown_measure(self):
        with pytest.raises(DashboardSpecError, match="no field 'amount'"):
            build_dashboard(
                {"widgets": [{"type": "kpi", "model": "account.move", "measure": "amount"}]},
                FIELDS,
            )

    def test_rejects_a_non_stored_measure(self):
        fields = {"account.move": dict(INVOICE_FIELDS, due={"type": "float", "store": False})}
        with pytest.raises(DashboardSpecError, match="not stored"):
            build_dashboard(
                {"widgets": [{"type": "kpi", "model": "account.move", "measure": "due:sum"}]},
                fields,
            )

    def test_a_non_stored_field_is_fine_as_a_list_column(self):
        fields = {"account.move": dict(INVOICE_FIELDS, due={"type": "float", "store": False})}
        build_dashboard(list_spec(columns=["name", "due"]), fields)

    def test_rejects_an_unknown_group_by(self):
        with pytest.raises(DashboardSpecError, match="group_by"):
            build_dashboard(
                {
                    "widgets": [
                        {
                            "type": "chart",
                            "model": "account.move",
                            "group_by": ["invoce_date:month"],
                        }
                    ]
                },
                FIELDS,
            )

    def test_models_without_metadata_are_not_checked(self):
        build_dashboard(list_spec(columns=["anything"]))


class TestSpecRoundTrip:
    def test_the_spec_is_stored_in_the_document(self):
        spec = list_spec()
        doc = build_dashboard(spec, FIELDS)
        assert doc["mcpSpec"] == spec
        # A copy, so later changes to the caller's dict do not leak in.
        spec["widgets"][0]["title"] = "changed"
        assert doc["mcpSpec"]["widgets"][0]["title"] == "Overdue"

    def test_the_stored_spec_rebuilds_the_same_dashboard(self):
        doc = build_dashboard(list_spec(note="n"), FIELDS)
        again = build_dashboard(doc["mcpSpec"], FIELDS)
        assert again["sheets"][0]["cells"] == doc["sheets"][0]["cells"]

    def test_past_highlight_compares_with_today(self):
        spec = list_spec()
        spec["widgets"][0]["columns"][2]["highlight"] = "past"
        doc = build_dashboard(spec, FIELDS)
        rules = [cf["rule"] for cf in doc["sheets"][0]["conditionalFormats"]]
        assert {"values": ["=TODAY()"], "operator": "isLessThan"}.items() <= rules[0].items()
