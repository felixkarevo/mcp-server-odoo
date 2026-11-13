# SPDX-License-Identifier: MPL-2.0
# SPDX-FileCopyrightText: 2025-2026 Pantalytics B.V.
"""MCP tools for Odoo spreadsheet dashboards.

Odoo keeps every dashboard (``spreadsheet.dashboard``) as one o-spreadsheet
JSON document in ``spreadsheet_data``. These tools list dashboards, read one
back as a summary, and write a whole dashboard from a short declarative spec;
``dashboard_builder`` turns the spec into the document.
"""

import asyncio
import json
from typing import Any, Dict, List, Optional

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import ToolAnnotations

from .access_control import (
    AccessControlError,
    AccessController,
    AccessControlUnavailableError,
    access_denied_message,
)
from .config import OdooConfig
from .dashboard_builder import SPEC_KEY, DashboardSpecError, build_dashboard, collect_models
from .error_handling import NotFoundError, ValidationError
from .error_sanitizer import ErrorSanitizer
from .logging_config import get_logger, perf_logger
from .odoo_connection import (
    OdooConnection,
    OdooConnectionError,
    OdooRequestFault,
    OdooValidationFault,
)
from .schemas import DashboardListResult, DashboardResult, DashboardWriteResult

logger = get_logger(__name__)

DASHBOARD_MODEL = "spreadsheet.dashboard"
GROUP_MODEL = "spreadsheet.dashboard.group"
REVISION_MODEL = "spreadsheet.revision"

# The builder writes the o-spreadsheet format of Odoo 18 and later
MIN_WRITE_VERSION = 18

# fields_get is the most expensive call in a build; ask only for what the
# field matcher reads
FIELD_ATTRIBUTES = ["string", "type", "relation", "store"]


class OdooDashboardHandler:
    """list_dashboards, get_dashboard and write_dashboard."""

    def __init__(
        self,
        app: MCPServer,
        connection: OdooConnection,
        access_controller: AccessController,
        config: OdooConfig,
    ):
        self.app = app
        self.connection = connection
        self.access_controller = access_controller
        self.config = config
        self._register_tools()

    def _register_tools(self) -> None:
        @self.app.tool(
            title="List Dashboards",
            annotations=ToolAnnotations(
                read_only_hint=True,
                destructive_hint=False,
                idempotent_hint=True,
                open_world_hint=True,
            ),
        )
        async def list_dashboards(
            group: Optional[str] = None,
            ctx: Optional[Context] = None,
        ) -> DashboardListResult:
            """List the spreadsheet dashboards and the sections that hold them.

            Args:
                group: Optional section name to filter on (e.g. 'Finance')

            Returns:
                The sections and the dashboards in them.
            """
            return DashboardListResult(**await self._handle_list_dashboards_tool(group))

        @self.app.tool(
            title="Get Dashboard",
            annotations=ToolAnnotations(
                read_only_hint=True,
                destructive_hint=False,
                idempotent_hint=True,
                open_world_hint=True,
            ),
        )
        async def get_dashboard(
            dashboard_id: int,
            raw: bool = False,
            ctx: Optional[Context] = None,
        ) -> DashboardResult:
            """Read one dashboard: its filters, scorecards, charts, pivots and lists.

            To change a dashboard built by write_dashboard, take `spec` from the
            result, edit it, and pass it back to write_dashboard with the same
            dashboard_id.

            Odoo's dashboard editor saves changes as separate revisions, which
            this summary does not include; `edited_in_odoo` says whether there
            are any. Writing the dashboard again discards them.

            Args:
                dashboard_id: The spreadsheet.dashboard ID
                raw: True to also return the full o-spreadsheet document (large)

            Returns:
                A summary of the dashboard, its spec if write_dashboard built it,
                and the raw document when `raw` is set.
            """
            return DashboardResult(**await self._handle_get_dashboard_tool(dashboard_id, raw))

        @self.app.tool(
            title="Write Dashboard",
            annotations=ToolAnnotations(
                read_only_hint=False,
                destructive_hint=True,
                idempotent_hint=True,
                open_world_hint=True,
            ),
        )
        async def write_dashboard(
            spec: Dict[str, Any],
            name: Optional[str] = None,
            group: Optional[str] = None,
            dashboard_id: Optional[int] = None,
            publish: bool = True,
            dry_run: bool = False,
            overwrite: bool = False,
            ctx: Optional[Context] = None,
        ) -> DashboardWriteResult:
            """Create or replace a dashboard from a declarative spec (Odoo 18+).

            The spec says what the dashboard shows; the tool builds the
            o-spreadsheet document Odoo stores and wires each global filter to
            a matching field on every widget's model.

            Replacing a dashboard replaces its whole content. The tool refuses
            to replace one that it did not build, or that someone changed in
            Odoo's editor since, unless `overwrite` is true: those changes
            would be lost. Run with `dry_run` first to check the result.

            Args:
                spec: The dashboard spec (format below)
                name: Dashboard name. Required to create one; renames it on update
                group: Section name or ID, e.g. 'Finance'. Required to create
                    one; a section that does not exist yet is created
                dashboard_id: Replace this dashboard instead of creating one
                publish: Whether users see the dashboard
                dry_run: Build and check the document, save nothing
                overwrite: Replace a dashboard that write_dashboard did not
                    build, or that was changed in Odoo's editor

            Returns:
                The dashboard's ID and link, and a summary of what was built.

            SPEC FORMAT
                spec = {title?, description?, accent?, locale?, filters: [...], widgets: [...]}
                    title and description render as a page header and an intro paragraph;
                    accent is the heading colour ('#01666b'); locale is an o-spreadsheet
                    locale object (default English (US)).

                filters: {type: date|relation|text|numeric|boolean|selection, label,
                    model (relation/selection), field (selection), default}

                widgets, in the order they should appear:
                  {type: 'kpi', title, model, measure, domain, description,
                      tone: neutral|good|warning|bad}
                      consecutive kpis share the width, four to a row; description is
                      the small line under the number
                  {type: 'chart', chart: bar|line|pie|radar|waterfall|pyramid|scatter|
                      funnel|geo|treemap|sunburst, title, model, measure,
                      group_by: ['invoice_date:month'], domain,
                      legend: top|bottom|left|right|none, height, show_values}
                  {type: 'row', title?, note?, widgets: [chart, chart]}
                      charts side by side, each titled inside its figure
                  {type: 'pivot', title, model, rows: ['partner_id'], columns: [],
                      measures: ['amount_total'], sort_by, limit, domain}
                  {type: 'list', title, model, order: ['amount_total desc'], limit, domain,
                      columns: ['name', {name, string, width, format: '#,##0',
                      highlight: 'negative'|'past'}, ...]}
                  {type: 'text', text, heading: true|false}

                Every chart, pivot and list opens with a heading that links to its
                records in Odoo (link: false turns that off) and may carry a note: a
                short explanation shown under the heading.

                A measure is 'field', 'field:avg' or '__count'; pivot and chart measures
                and group_by need stored fields. Every global filter is wired to a
                matching field on each widget's model; a widget may carry
                filters: {'<filter label>': '<field chain>'} to choose the field, or
                filters: false to ignore the filters.
            """
            result = await self._handle_write_dashboard_tool(
                spec, name, group, dashboard_id, publish, dry_run, overwrite
            )
            return DashboardWriteResult(**result)

    # -- handlers -----------------------------------------------------------

    async def _handle_list_dashboards_tool(self, group: Optional[str] = None) -> Dict[str, Any]:
        """Handle list_dashboards tool request."""
        try:
            with perf_logger.track_operation("tool_list_dashboards"):
                await self._require(DASHBOARD_MODEL, "read")
                groups = await asyncio.to_thread(
                    self.connection.search_read, GROUP_MODEL, [], ["id", "name"]
                )
                domain: List[Any] = [["dashboard_group_id.name", "=", group]] if group else []
                dashboards = await asyncio.to_thread(
                    self.connection.search_read,
                    DASHBOARD_MODEL,
                    domain,
                    ["id", "name", "dashboard_group_id", "sequence", "is_published"],
                )
                return {
                    "groups": sorted(
                        ({"id": g["id"], "name": g["name"]} for g in groups),
                        key=lambda g: g["name"],
                    ),
                    "dashboards": [
                        {
                            "id": d["id"],
                            "name": d["name"],
                            "group": _m2o_name(d.get("dashboard_group_id")),
                            "sequence": d.get("sequence"),
                            "is_published": d.get("is_published"),
                        }
                        for d in dashboards
                    ],
                    "total": len(dashboards),
                }
        except Exception as e:
            raise self._as_validation_error(e, "list dashboards") from e

    async def _handle_get_dashboard_tool(
        self, dashboard_id: int, raw: bool = False
    ) -> Dict[str, Any]:
        """Handle get_dashboard tool request."""
        try:
            with perf_logger.track_operation("tool_get_dashboard"):
                await self._require(DASHBOARD_MODEL, "read")
                record = await self._read_dashboard(dashboard_id)
                document = _parse_document(record.get("spreadsheet_data"))
                edits = await self._count_editor_revisions(dashboard_id)
                result = {
                    "id": dashboard_id,
                    "name": record.get("name"),
                    "group": _m2o_name(record.get("dashboard_group_id")),
                    "is_published": record.get("is_published"),
                    "summary": summarize_document(document),
                    "url": self._dashboard_url(dashboard_id),
                    "spec": document.get(SPEC_KEY),
                    "edited_in_odoo": bool(edits),
                }
                if raw:
                    result["document"] = document
                return result
        except Exception as e:
            raise self._as_validation_error(e, "read dashboard") from e

    async def _handle_write_dashboard_tool(
        self,
        spec: Any,
        name: Optional[str] = None,
        group: Optional[str] = None,
        dashboard_id: Optional[int] = None,
        publish: bool = True,
        dry_run: bool = False,
        overwrite: bool = False,
    ) -> Dict[str, Any]:
        """Handle write_dashboard tool request."""
        try:
            with perf_logger.track_operation("tool_write_dashboard"):
                if isinstance(spec, str):
                    try:
                        spec = json.loads(spec)
                    except json.JSONDecodeError as exc:
                        raise ValidationError(f"spec is not valid JSON: {exc}") from exc
                if not isinstance(spec, dict):
                    raise ValidationError("spec must be an object")
                if not dry_run:
                    operation = "write" if dashboard_id else "create"
                    await self._require(DASHBOARD_MODEL, operation)
                    major = await asyncio.to_thread(self.connection.get_major_version)
                    if major is not None and major < MIN_WRITE_VERSION:
                        raise ValidationError(
                            f"write_dashboard needs Odoo {MIN_WRITE_VERSION} or later; "
                            f"this is Odoo {major}"
                        )
                    if not dashboard_id:
                        if not name:
                            raise ValidationError("A new dashboard needs a name")
                        if not group:
                            raise ValidationError(
                                "A new dashboard needs a group (section), e.g. 'Finance'"
                            )

                models = collect_models(spec)
                fields_by_model: Dict[str, Dict[str, Any]] = {}
                for model in models:
                    await self._require(model, "read")
                    try:
                        fields_by_model[model] = await asyncio.to_thread(
                            self.connection.fields_get, model, FIELD_ATTRIBUTES
                        )
                    except (OdooValidationFault, OdooRequestFault) as exc:
                        raise ValidationError(f"Cannot read the fields of {model}: {exc}") from exc

                document = build_dashboard(spec, fields_by_model)
                payload = json.dumps(document)
                summary = summarize_document(document)
                summary["models"] = models

                if dashboard_id:
                    await self._check_replaceable(dashboard_id, overwrite)

                if dry_run:
                    return {
                        "success": True,
                        "dashboard_id": dashboard_id,
                        "url": None,
                        "created": False,
                        "summary": summary,
                        "message": (
                            "Dry run: the dashboard was built and checked but not saved "
                            f"({len(payload):,} bytes, {summary['widgets']} widgets)."
                        ),
                    }

                values: Dict[str, Any] = {"spreadsheet_data": payload, "is_published": publish}
                if name:
                    values["name"] = name
                if group:
                    values["dashboard_group_id"] = await self._resolve_group(group)
                if dashboard_id:
                    await asyncio.to_thread(
                        self.connection.write, DASHBOARD_MODEL, [dashboard_id], values
                    )
                    created = False
                else:
                    dashboard_id = await asyncio.to_thread(
                        self.connection.create, DASHBOARD_MODEL, values
                    )
                    created = True

                verb = "Created" if created else "Replaced the content of"
                return {
                    "success": True,
                    "dashboard_id": dashboard_id,
                    "url": self._dashboard_url(dashboard_id),
                    "created": created,
                    "summary": summary,
                    "message": f"{verb} dashboard {dashboard_id} ({summary['widgets']} widgets)",
                }
        except DashboardSpecError as e:
            raise ValidationError(f"Invalid dashboard spec: {e}") from e
        except Exception as e:
            raise self._as_validation_error(e, "write dashboard") from e

    # -- helpers ------------------------------------------------------------

    async def _require(self, model: str, operation: str) -> None:
        await asyncio.to_thread(self.access_controller.validate_model_access, model, operation)
        if not self.connection.is_authenticated:
            raise ValidationError("Not authenticated with Odoo")

    async def _read_dashboard(self, dashboard_id: int) -> Dict[str, Any]:
        records = await asyncio.to_thread(
            self.connection.read,
            DASHBOARD_MODEL,
            [dashboard_id],
            ["name", "dashboard_group_id", "is_published", "spreadsheet_data"],
        )
        if not records:
            raise NotFoundError(f"No dashboard with ID {dashboard_id}")
        return records[0]

    async def _count_editor_revisions(self, dashboard_id: int) -> Optional[int]:
        """Active editor revisions of a dashboard; None if they cannot be read.

        Writing ``spreadsheet_data`` archives the revisions, so an active one
        means someone changed the dashboard in the editor since.
        """
        try:
            return await asyncio.to_thread(
                self.connection.search_count,
                REVISION_MODEL,
                [["res_model", "=", DASHBOARD_MODEL], ["res_id", "=", dashboard_id]],
            )
        except Exception as exc:  # not every user may read revisions
            logger.warning(f"Could not count the revisions of dashboard {dashboard_id}: {exc}")
            return None

    async def _check_replaceable(self, dashboard_id: int, overwrite: bool) -> None:
        """Refuse to replace work that the spec does not hold, unless told to."""
        record = await self._read_dashboard(dashboard_id)
        if overwrite:
            return
        if SPEC_KEY not in _parse_document(record.get("spreadsheet_data")):
            raise ValidationError(
                f"Dashboard {dashboard_id} was not built by write_dashboard, so the spec "
                "cannot reproduce what it shows. Pass overwrite=true to replace it anyway."
            )
        edits = await self._count_editor_revisions(dashboard_id)
        if edits is None:
            raise ValidationError(
                f"Could not check whether dashboard {dashboard_id} was changed in Odoo's "
                "editor (spreadsheet.revision is not readable). Pass overwrite=true to "
                "replace it anyway."
            )
        if edits:
            raise ValidationError(
                f"Dashboard {dashboard_id} was changed in Odoo's editor since it was last "
                f"written ({edits} revisions); replacing it would discard those changes. "
                "Pass overwrite=true to replace it anyway."
            )

    async def _resolve_group(self, group: Any) -> int:
        """Return the ID of a dashboard section, creating it when it is new."""
        if isinstance(group, int) or (isinstance(group, str) and group.isdigit()):
            return int(group)
        found = await asyncio.to_thread(self.connection.search, GROUP_MODEL, [["name", "=", group]])
        if found:
            return found[0]
        await self._require(GROUP_MODEL, "create")
        return await asyncio.to_thread(self.connection.create, GROUP_MODEL, {"name": group})

    def _dashboard_url(self, dashboard_id: Optional[int]) -> Optional[str]:
        if not dashboard_id:
            return None
        base_url = (self.config.url or "").rstrip("/")
        return f"{base_url}/odoo/dashboards?dashboard_id={dashboard_id}"

    @staticmethod
    def _as_validation_error(error: Exception, action: str) -> ValidationError:
        if isinstance(error, ValidationError):
            return error
        if isinstance(error, NotFoundError):
            return ValidationError(str(error))
        if isinstance(error, AccessControlUnavailableError):
            return ValidationError(f"Could not verify access (connection error): {error}")
        if isinstance(error, AccessControlError):
            return ValidationError(access_denied_message(error))
        if isinstance(error, (OdooValidationFault, OdooRequestFault)):
            return ValidationError(str(error))
        if isinstance(error, OdooConnectionError):
            return ValidationError(f"Connection error: {error}")
        logger.error(f"Error in {action}: {error}")
        return ValidationError(f"Failed to {action}: {ErrorSanitizer.sanitize_message(str(error))}")


def register_dashboard_tools(
    app: MCPServer,
    connection: OdooConnection,
    access_controller: AccessController,
    config: OdooConfig,
) -> OdooDashboardHandler:
    """Register the dashboard tools with the MCPServer app."""
    handler = OdooDashboardHandler(app, connection, access_controller, config)
    logger.info("Registered Odoo dashboard tools")
    return handler


def _m2o_name(value: Any) -> Optional[str]:
    return value[1] if isinstance(value, (list, tuple)) and len(value) > 1 else None


def _parse_document(data: Any) -> Dict[str, Any]:
    try:
        document = json.loads(data or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return document if isinstance(document, dict) else {}


def summarize_document(document: Dict[str, Any]) -> Dict[str, Any]:
    """Describe a dashboard document without returning all of it.

    Documents also come from Odoo's editor and its built-in dashboards, so
    list columns are accepted as field names or as ``{name, string}`` objects,
    and a chart without its own title is named after the heading above it.
    """
    figures = [(sheet, f) for sheet in document.get("sheets", []) for f in sheet.get("figures", [])]
    charts = [
        {
            "title": _chart_title(sheet, f),
            "type": f["data"].get("type"),
            "model": (f["data"].get("metaData") or {}).get("resModel"),
            "measure": (f["data"].get("metaData") or {}).get("measure"),
            "group_by": (f["data"].get("metaData") or {}).get("groupBy"),
        }
        for sheet, f in figures
        if f.get("tag") == "chart" and str(f.get("data", {}).get("type", "")).startswith("odoo_")
    ]
    scorecards = [
        _title_text(f["data"])
        for _, f in figures
        if f.get("tag") == "chart" and f.get("data", {}).get("type") == "scorecard"
    ]
    pivots = [
        {
            "name": p.get("name"),
            "model": p.get("model"),
            "rows": [r.get("fieldName") for r in p.get("rows") or []],
            "columns": [c.get("fieldName") for c in p.get("columns") or []],
            "measures": [m.get("fieldName") for m in p.get("measures") or []],
        }
        for p in (document.get("pivots") or {}).values()
        if p.get("rows") or p.get("columns")
    ]
    lists = [
        {
            "name": lst.get("name"),
            "model": lst.get("model"),
            "columns": [
                c.get("name") if isinstance(c, dict) else c for c in lst.get("columns") or []
            ],
        }
        for lst in (document.get("lists") or {}).values()
    ]
    return {
        "version": document.get("version"),
        "widgets": len(charts) + len(scorecards) + len(pivots) + len(lists),
        "scorecards": scorecards,
        "charts": charts,
        "pivots": pivots,
        "lists": lists,
        "filters": [
            {"label": f.get("label"), "type": f.get("type"), "model": f.get("modelName")}
            for f in document.get("globalFilters") or []
        ],
    }


def _title_text(data: Dict[str, Any]) -> str:
    title = data.get("title")
    if isinstance(title, dict):
        return title.get("text") or ""
    return title or ""


def _chart_title(sheet: Dict[str, Any], figure: Dict[str, Any]) -> str:
    """A chart's own title, or the heading in the cells just above it."""
    own = _title_text(figure["data"])
    if own:
        return own
    cells = sheet.get("cells") or {}
    row = int(figure.get("row") or 0)
    texts = []
    # A1 names are 1-based, so A{row} is the row just above the anchor
    for above in range(row, max(row - 3, 0), -1):
        text = cells.get(f"A{above}")
        if isinstance(text, dict):  # older documents store {"content": ...}
            text = text.get("content")
        if isinstance(text, str) and text and not text.startswith("="):
            texts.append(text)
    # A linked heading beats a note between it and the chart
    for text in texts:
        if text.startswith("[") and "](" in text:
            return text[1 : text.index("](")]
    return texts[0] if texts else ""
