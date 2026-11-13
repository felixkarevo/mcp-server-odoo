# SPDX-License-Identifier: MPL-2.0
# SPDX-FileCopyrightText: 2025-2026 Pantalytics B.V.
"""Build Odoo spreadsheet-dashboard JSON from a small declarative spec.

An Odoo 19 dashboard (``spreadsheet.dashboard``) stores its whole content as
one o-spreadsheet JSON document in ``spreadsheet_data``. Hand-writing that
document is error prone: data sources, figures, cell formulas and global
filter field matchings all have to agree with each other.

This module takes a spec like::

    {
      "title": "Cash",
      "filters": [{"type": "date", "label": "Period", "default": "last_90_days"}],
      "widgets": [
        {"type": "kpi", "title": "Open", "model": "account.move",
         "measure": "amount_residual", "domain": [["state", "=", "posted"]]},
        {"type": "chart", "chart": "line", "model": "account.invoice.report",
         "measure": "price_subtotal", "group_by": ["invoice_date:month"]},
      ],
    }

and returns the JSON document. Callers pass in the field metadata of every
model the spec mentions so global filters can be wired to the right field.

Format version
--------------
``DATA_VERSION`` is deliberately an *older* o-spreadsheet version than the one
the live instance writes. o-spreadsheet migrates documents forward on load, and
this is the version Odoo itself ships its built-in dashboards in, so it is the
shape with the best-tested migration path. Do not bump it to match the running
instance: that would skip the migrations and pin us to an internal format that
changes every release.
"""

from __future__ import annotations

import difflib
import json
import re
import uuid
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

DATA_VERSION = "18.5.10"

# o-spreadsheet geometry defaults, in pixels.
DEFAULT_ROW_HEIGHT = 23
DEFAULT_COL_WIDTH = 96
DASHBOARD_WIDTH = 1000
KPI_HEIGHT = 109
KPIS_PER_ROW = 4
CHART_HEIGHT = 345
FIGURE_GAP = 15
PAGE_TITLE_ROW_HEIGHT = 44
HEADING_ROW_HEIGHT = 40
TABLE_ROW_HEIGHT = 26

# Rough text metrics for sizing a wrapped note, which o-spreadsheet does not
# grow on its own when the note spans merged cells.
NOTE_CHAR_WIDTH = 6.6
NOTE_LINE_HEIGHT = 17

# Column widths a list falls back to when a column names none, by field type.
TYPE_WIDTHS = {
    "many2one": 220,
    "many2many": 220,
    "char": 150,
    "text": 220,
    "html": 220,
    "selection": 115,
    "date": 100,
    "datetime": 100,
    "float": 95,
    "monetary": 110,
    "integer": 85,
    "boolean": 75,
}
WIDE_FIELD_NAMES = {"name", "display_name"}

# Field types whose list cells get the locale's date format. An explicit cell
# format overrides the one ODOO.LIST returns, which for a datetime includes the
# time.
DATE_TYPES = {"date", "datetime"}

KPI_TONES = {
    "neutral": "#EFF6FF",
    "good": "#EAF5EA",
    "warning": "#FFF4E0",
    "bad": "#FDECEC",
}
NEGATIVE_TEXT_COLOR = "#C0392B"

# List-column highlights: cells below the threshold turn red. "past" compares a
# date with today, evaluated when the dashboard is opened, which is how an
# overdue marker stays current; a domain cannot do that, since it is stored
# with a fixed date.
HIGHLIGHT_THRESHOLDS = {
    "negative": "0",
    "past": "=TODAY()",
}

# Top-level document key holding the spec a dashboard was built from, so it can
# be read back, changed and written again instead of reverse-engineered from
# the document. o-spreadsheet ignores keys it does not know. Edits made in
# Odoo's editor land in separate revisions the spec knows nothing about;
# get_dashboard reports when there are any.
SPEC_KEY = "mcpSpec"

DASHBOARD_SHEET_NAME = "Dashboard"
DATA_SHEET_NAME = "Data"

CHART_TYPES = {
    "bar": "odoo_bar",
    "line": "odoo_line",
    "pie": "odoo_pie",
    "radar": "odoo_radar",
    "waterfall": "odoo_waterfall",
    "pyramid": "odoo_pyramid",
    "scatter": "odoo_scatter",
    "funnel": "odoo_funnel",
    "geo": "odoo_geo",
    "treemap": "odoo_treemap",
    "sunburst": "odoo_sunburst",
}

FILTER_TYPES = {"date", "relation", "text", "numeric", "boolean", "selection"}

WIDGET_TYPES = {"kpi", "chart", "pivot", "list", "text", "row"}

# Date fields Odoo models commonly use as "the" business date, best first.
PREFERRED_DATE_FIELDS = (
    "date",
    "invoice_date",
    "date_order",
    "date_invoice",
    "date_deadline",
    "date_done",
    "date_start",
    "create_date",
)

# o-spreadsheet's own default locale; a spec's `locale` replaces it, e.g.
# {"name": "German / Deutsch", "code": "de_DE", "thousandsSeparator": ".",
#  "decimalSeparator": ",", "dateFormat": "dd.mm.yyyy", "timeFormat": "hh:mm:ss",
#  "formulaArgSeparator": ";", "weekStart": 1}
DEFAULT_LOCALE = {
    "name": "English (US)",
    "code": "en_US",
    "thousandsSeparator": ",",
    "decimalSeparator": ".",
    "dateFormat": "m/d/yyyy",
    "timeFormat": "hh:mm:ss a",
    "formulaArgSeparator": ",",
    "weekStart": 7,
}

# The teal Odoo uses on its own dashboards; a spec's `accent` replaces it.
DEFAULT_ACCENT = "#01666b"
TEXT_COLOR = "#434343"
MUTED_TEXT_COLOR = "#6B7280"
RULE_COLOR = "#CCCCCC"
HEADER_FILL = "#F2F2F2"


class DashboardSpecError(ValueError):
    """The spec is not something we can turn into a dashboard."""


def _uid() -> str:
    return str(uuid.uuid4())


def _col_letter(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _cell(col: int, row: int) -> str:
    """0-based (col, row) -> A1 notation."""
    return f"{_col_letter(col)}{row + 1}"


def _px_to_rows(pixels: int) -> int:
    return -(-pixels // DEFAULT_ROW_HEIGHT)  # ceil


def _as_domain(value: Any) -> List[Any]:
    """Accept a domain as a list or as a JSON string."""
    if value in (None, "", False):
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise DashboardSpecError(f"domain is not valid JSON: {value!r}") from exc
    if not isinstance(value, list):
        raise DashboardSpecError(f"domain must be a list, got {type(value).__name__}")
    return value


def _split_granularity(field_spec: str) -> Tuple[str, Optional[str]]:
    """``invoice_date:month`` -> ``("invoice_date", "month")``."""
    if ":" in field_spec:
        name, _, granularity = field_spec.partition(":")
        return name.strip(), granularity.strip() or None
    return field_spec.strip(), None


def _dimension(field_spec: Any) -> Dict[str, Any]:
    """Normalise a row/column dimension into an o-spreadsheet PivotDimension."""
    if isinstance(field_spec, dict):
        if "fieldName" not in field_spec:
            raise DashboardSpecError(f"dimension needs a fieldName: {field_spec!r}")
        return dict(field_spec)
    name, granularity = _split_granularity(str(field_spec))
    dim: Dict[str, Any] = {"fieldName": name}
    if granularity:
        dim["granularity"] = granularity
    return dim


def _measure(measure_spec: Any) -> Dict[str, Any]:
    """Normalise a measure into an o-spreadsheet PivotMeasure.

    Accepts ``"amount_total"``, ``"amount_total:avg"``, ``"__count"`` or a
    ready-made dict.
    """
    if isinstance(measure_spec, dict):
        if "fieldName" not in measure_spec:
            raise DashboardSpecError(f"measure needs a fieldName: {measure_spec!r}")
        out = dict(measure_spec)
        out.setdefault("id", out["fieldName"])
        return out
    name, aggregator = _split_granularity(str(measure_spec))
    if aggregator:
        return {"id": f"{name}:{aggregator}", "fieldName": name, "aggregator": aggregator}
    return {"id": name, "fieldName": name}


def _measure_formula_id(measure_spec: Any) -> str:
    """The string PIVOT.VALUE() needs to address this measure."""
    return _measure(measure_spec)["id"]


class FieldMatcher:
    """Wires global filters to the field each data source should filter on.

    ``fields_by_model`` is ``{model: {field_name: field_metadata}}``, exactly
    what ``fields_get`` returns. Models missing from it simply get no automatic
    matching — an explicit ``filters`` mapping on the widget still works.
    """

    def __init__(
        self,
        filters: Sequence[Dict[str, Any]],
        fields_by_model: Optional[Dict[str, Dict[str, Any]]] = None,
        date_hints: Optional[Dict[str, str]] = None,
    ):
        self.filters = list(filters)
        self.fields_by_model = fields_by_model or {}
        # One date field per model, so a date filter narrows every widget on
        # that model the same way. Without this, a KPI on account.move would
        # filter on `date` while a chart beside it filters on `invoice_date`,
        # and the two would disagree.
        self.date_hints = date_hints or {}

    def models(self) -> List[str]:
        return sorted(self.fields_by_model)

    def match(
        self,
        model: str,
        overrides: Any = None,
        date_hint: Optional[str] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """Build the ``fieldMatching`` map for one data source.

        ``overrides`` maps a filter label (or id) to a field chain, or is
        ``False`` to detach this widget from every filter.
        """
        if overrides is False:
            return {}
        overrides = overrides or {}
        if not isinstance(overrides, dict):
            raise DashboardSpecError("a widget's `filters` must be a mapping or false")

        by_key: Dict[str, str] = {}
        for key, chain in overrides.items():
            by_key[str(key).strip().lower()] = chain

        fields = self.fields_by_model.get(model, {})
        date_hint = date_hint or self.date_hints.get(model)
        matching: Dict[str, Dict[str, Any]] = {}
        for flt in self.filters:
            chain = by_key.get(flt["label"].strip().lower(), by_key.get(flt["id"]))
            if chain is False:
                continue
            if chain is None:
                chain = self._auto_chain(flt, fields, date_hint)
            if not chain:
                continue
            entry: Dict[str, Any] = {"chain": chain, "type": self._chain_type(flt, fields, chain)}
            if flt["type"] == "date":
                entry["offset"] = 0
            matching[flt["id"]] = entry
        return matching

    @staticmethod
    def _chain_type(flt: Dict[str, Any], fields: Dict[str, Any], chain: str) -> str:
        head = chain.split(".")[0]
        meta = fields.get(head)
        if meta and meta.get("type"):
            return meta["type"]
        return {"date": "date", "relation": "many2one"}.get(flt["type"], "char")

    def _auto_chain(
        self,
        flt: Dict[str, Any],
        fields: Dict[str, Any],
        date_hint: Optional[str],
    ) -> Optional[str]:
        if not fields:
            return None
        if flt["type"] == "date":
            if date_hint and date_hint in fields:
                return date_hint
            for name in PREFERRED_DATE_FIELDS:
                meta = fields.get(name)
                if meta and meta.get("type") in ("date", "datetime"):
                    return name
            for name, meta in sorted(fields.items()):
                if meta.get("type") in ("date", "datetime") and meta.get("store", True):
                    return name
            return None
        if flt["type"] == "relation":
            target = flt.get("modelName")
            if not target:
                return None
            candidates = [
                name
                for name, meta in fields.items()
                if meta.get("type") in ("many2one", "many2many") and meta.get("relation") == target
            ]
            if not candidates:
                return None
            # A field named after the target model beats an incidental match
            # such as create_uid when the filter is on res.users.
            preferred = target.split(".")[-1] + "_id"
            for name in (preferred, target.replace(".", "_") + "_id"):
                if name in candidates:
                    return name
            noisy = ("create_uid", "write_uid", "company_id", "currency_id")
            for name in sorted(candidates):
                if name not in noisy:
                    return name
            return sorted(candidates)[0]
        if flt["type"] == "selection":
            name = flt.get("selectionField")
            return name if name and name in fields else None
        return None


class DashboardBuilder:
    """Assembles the o-spreadsheet document one widget at a time.

    Layout mirrors the dashboards Odoo ships: a band of scorecards, then
    sections that each open with a heading on a ruled row. A heading links to
    the Odoo view behind its data, so a click leads from the figure to the
    records. Tables get column widths, cell formats and row heights; charts
    are figures floating over the grid, placed by pixel offset.
    """

    def __init__(
        self,
        title: Optional[str] = None,
        filters: Optional[Sequence[Dict[str, Any]]] = None,
        locale: Optional[Dict[str, Any]] = None,
        fields_by_model: Optional[Dict[str, Dict[str, Any]]] = None,
        date_hints: Optional[Dict[str, str]] = None,
        description: Optional[str] = None,
        accent: Optional[str] = None,
    ):
        self.title = title
        self.locale = locale or dict(DEFAULT_LOCALE)
        self.filters = [_normalise_filter(f) for f in (filters or [])]
        self.fields_by_model = fields_by_model or {}
        self.matcher = FieldMatcher(self.filters, fields_by_model, date_hints)
        self.accent = accent or DEFAULT_ACCENT

        self.dashboard_sheet_id = _uid()
        self.data_sheet_id = _uid()

        self.pivots: Dict[str, Dict[str, Any]] = {}
        self.lists: Dict[str, Dict[str, Any]] = {}
        self.figures: List[Dict[str, Any]] = []
        self.dash_cells: Dict[str, str] = {}
        self.dash_styles: Dict[str, str] = {}
        self.dash_formats: Dict[str, str] = {}
        self.dash_borders: Dict[str, str] = {}
        self.conditional_formats: List[Dict[str, Any]] = []
        self.data_cells: Dict[str, str] = {}

        # Workbook-level registries, addressed by id from the sheets.
        self.styles: Dict[str, Dict[str, Any]] = {}
        self.formats: Dict[str, str] = {}
        self.borders: Dict[str, Dict[str, Any]] = {}

        self.col_widths: Dict[int, int] = {}
        self.row_sizes: Dict[int, int] = {}
        # Rows whose content spans the dashboard width. Their extent depends
        # on the final column widths, so they are resolved in to_dict().
        self._ruled_rows: List[int] = []
        self._notes: List[Tuple[int, str]] = []

        self._next_pivot = 1
        self._next_list = 1
        self._next_data_row = 0
        self._row = 0  # current anchor row on the dashboard sheet
        self._max_col = 6

        if title:
            self._page_title(title)
        if description:
            self._note(description)
            self._advance(1)

    # -- registries -----------------------------------------------------

    def _style(self, **style: Any) -> str:
        return _register(self.styles, style)

    def _format(self, pattern: str) -> str:
        return _register(self.formats, pattern)

    def _border(self, **border: Any) -> str:
        return _register(self.borders, border)

    # -- placement ------------------------------------------------------

    def _advance(self, rows: int) -> None:
        self._row += max(rows, 1)

    def _page_title(self, text: str) -> None:
        ref = _cell(0, self._row)
        self.dash_cells[ref] = text
        self.dash_styles[ref] = self._style(
            textColor=self.accent, bold=True, fontSize=22, verticalAlign="middle"
        )
        self.row_sizes[self._row] = PAGE_TITLE_ROW_HEIGHT
        self._advance(1)

    def _heading(self, text: Optional[str], link: Optional[str] = None) -> None:
        """A section heading on a ruled row, optionally linking to Odoo."""
        if not text:
            return
        ref = _cell(0, self._row)
        self.dash_cells[ref] = _markdown_link(text, link) if link else text
        self.dash_styles[ref] = self._style(
            textColor=self.accent, bold=True, fontSize=16, verticalAlign="middle"
        )
        self.row_sizes[self._row] = HEADING_ROW_HEIGHT
        self._ruled_rows.append(self._row)
        self._advance(1)

    def _note(self, text: Optional[str]) -> None:
        """An explanatory line under a heading, wrapped across the full width."""
        if not text:
            return
        self._notes.append((self._row, str(text)))
        self._advance(1)

    def _section(self, widget: Dict[str, Any], link: Optional[str]) -> None:
        self._heading(widget.get("title"), link if widget.get("link", True) else None)
        self._note(widget.get("note"))

    def _add_figure(
        self, data: Dict[str, Any], width: int, height: int, x: int = 0, y: int = 10
    ) -> str:
        figure_id = _uid()
        data = dict(data)
        data["chartId"] = figure_id
        self.figures.append(
            {
                "id": figure_id,
                "tag": "chart",
                "width": width,
                "height": height,
                "col": 0,
                "row": self._row,
                "offset": {"x": x, "y": y},
                "data": data,
            }
        )
        return figure_id

    def _widen(self, col: int, width: int) -> None:
        self.col_widths[col] = max(self.col_widths.get(col, 0), int(width))

    def _check_field(self, model: str, field_spec: Any, purpose: str) -> None:
        """Refuse a field the model does not have, or cannot group or sum.

        A bad name would otherwise surface only as #ERROR cells once someone
        opens the dashboard. Models whose metadata was not supplied are not
        checked.
        """
        fields = self.fields_by_model.get(model)
        if not fields or field_spec in (None, "__count"):
            return
        name = _split_granularity(str(field_spec))[0].split(".")[0]
        meta = fields.get(name)
        if meta is None:
            close = difflib.get_close_matches(name, list(fields), n=3)
            hint = f"; did you mean {', '.join(close)}?" if close else ""
            raise DashboardSpecError(f"{model} has no field {name!r} ({purpose}){hint}")
        if purpose in ("measure", "group_by") and meta.get("store") is False:
            raise DashboardSpecError(
                f"{model}.{name} is not stored, so it cannot be used as a {purpose}; "
                "filter on it in the domain instead"
            )

    # -- widgets --------------------------------------------------------

    def add_kpis(self, widgets: Sequence[Dict[str, Any]]) -> None:
        """Place scorecards side by side, up to four to a row.

        The cards share the dashboard width. Every KPI is backed by a pivot
        with no rows and no columns, which is how Odoo itself renders a single
        aggregate number.
        """
        per_row = min(len(widgets), KPIS_PER_ROW)
        width = (DASHBOARD_WIDTH - FIGURE_GAP * (per_row - 1)) // per_row
        for start in range(0, len(widgets), KPIS_PER_ROW):
            x = 0
            for widget in widgets[start : start + KPIS_PER_ROW]:
                self._add_kpi(widget, width, x)
                x += width + FIGURE_GAP
            self._advance(_px_to_rows(KPI_HEIGHT + 2 * FIGURE_GAP))

    def _add_kpi(self, widget: Dict[str, Any], width: int, x: int) -> None:
        model = _require(widget, "model")
        measure = widget.get("measure", "__count")
        self._check_field(model, measure, "measure")
        tone = widget.get("tone", "neutral")
        if tone not in KPI_TONES:
            raise DashboardSpecError(f"unknown kpi tone {tone!r}; pick one of {sorted(KPI_TONES)}")
        pivot_id = self._add_pivot_definition(
            model=model,
            name=widget.get("title") or f"KPI - {model}",
            domain=_as_domain(widget.get("domain")),
            context=widget.get("context") or {},
            rows=[],
            columns=[],
            measures=[measure],
            filters=widget.get("filters"),
            date_hint=widget.get("date_field"),
        )
        value_ref = self._add_data_row(
            label=widget.get("title") or model,
            formula=f'=PIVOT.VALUE({pivot_id},"{_measure_formula_id(measure)}")',
            humanize=widget.get("humanize", True),
        )
        self._add_figure(
            {
                "type": "scorecard",
                "title": {"text": widget.get("title") or "", "color": TEXT_COLOR, "bold": True},
                "background": widget.get("background") or KPI_TONES[tone],
                "keyValue": value_ref,
                "baselineMode": "text",
                "baselineColorUp": "#00A04A",
                "baselineColorDown": "#DC6965",
                "baselineDescr": {"text": widget.get("description", "")},
                "humanize": False,
            },
            width=width,
            height=KPI_HEIGHT,
            x=x,
        )

    def add_chart(self, widget: Dict[str, Any]) -> None:
        """A full-width chart under its own section heading."""
        self._section(widget, self._chart_link(widget))
        height = int(widget.get("height", CHART_HEIGHT))
        # The heading already names the chart; a second title inside the
        # figure would only repeat it.
        self._add_figure(
            self._chart_data(widget, show_title=False),
            width=int(widget.get("width", DASHBOARD_WIDTH)),
            height=height,
        )
        self._advance(_px_to_rows(height + 2 * FIGURE_GAP))

    def add_row(self, widget: Dict[str, Any]) -> None:
        """Charts side by side, sharing the dashboard width.

        A row has no per-chart heading cells to hang titles on, so each chart
        keeps its title inside the figure. The row itself may carry a title
        and a note like any other section.
        """
        children = widget.get("widgets") or []
        if not children:
            raise DashboardSpecError("a row needs widgets")
        for child in children:
            if not isinstance(child, dict) or child.get("type", "chart") != "chart":
                raise DashboardSpecError("a row holds charts only")
        self._heading(widget.get("title"))
        self._note(widget.get("note"))

        count = len(children)
        default_width = (DASHBOARD_WIDTH - FIGURE_GAP * (count - 1)) // count
        height = max(int(c.get("height", CHART_HEIGHT)) for c in children)
        x = 0
        for child in children:
            width = int(child.get("width", default_width))
            self._add_figure(
                self._chart_data(child, show_title=True), width=width, height=height, x=x
            )
            x += width + FIGURE_GAP
        self._advance(_px_to_rows(height + 2 * FIGURE_GAP))

    def _chart_data(self, widget: Dict[str, Any], show_title: bool) -> Dict[str, Any]:
        model = _require(widget, "model")
        kind = widget.get("chart", "bar")
        if kind not in CHART_TYPES:
            raise DashboardSpecError(
                f"unknown chart type {kind!r}; pick one of {sorted(CHART_TYPES)}"
            )
        group_by = [str(g) for g in (widget.get("group_by") or [])]
        if not group_by:
            raise DashboardSpecError(f"chart {widget.get('title') or model!r} needs a group_by")
        measure = widget.get("measure", "__count")
        if isinstance(measure, dict):
            raise DashboardSpecError("a chart takes a single measure name, not a dict")
        measure_name, _ = _split_granularity(str(measure))
        plain_group_by = [_split_granularity(g)[0] for g in group_by]
        self._check_field(model, measure_name, "measure")
        for field_spec in group_by:
            self._check_field(model, field_spec, "group_by")

        title: Dict[str, Any] = {"text": (widget.get("title") or "") if show_title else ""}
        if show_title and title["text"]:
            title.update({"color": TEXT_COLOR, "bold": True})
        data = {
            "type": CHART_TYPES[kind],
            "title": title,
            "background": widget.get("background", "#FFFFFF"),
            "legendPosition": widget.get("legend", "top"),
            "metaData": {
                "groupBy": group_by,
                "measure": measure_name,
                "order": widget.get("order"),
                "resModel": model,
                "mode": kind,
            },
            "searchParams": {
                "comparison": None,
                "context": widget.get("context") or {},
                "domain": _as_domain(widget.get("domain")),
                "groupBy": plain_group_by,
                "orderBy": [],
            },
            "dataSets": [{}],
            "stacked": bool(widget.get("stacked", False)),
            "fieldMatching": self.matcher.match(
                model,
                widget.get("filters"),
                date_hint=widget.get("date_field") or _first_date(group_by),
            ),
        }
        if widget.get("show_values"):
            data["showValues"] = True
        return data

    def add_pivot(self, widget: Dict[str, Any]) -> None:
        model = _require(widget, "model")
        rows = [_dimension(r) for r in (widget.get("rows") or [])]
        columns = [_dimension(c) for c in (widget.get("columns") or [])]
        measures = widget.get("measures") or [widget.get("measure", "__count")]
        limit = int(widget.get("limit", 10))
        sort_by = widget.get("sort_by")
        domain = _as_domain(widget.get("domain"))
        for dim in rows + columns:
            self._check_field(model, dim["fieldName"], "group_by")
        for measure in measures:
            self._check_field(model, _measure(measure)["fieldName"], "measure")

        sorted_column = None
        if sort_by:
            sorted_column = {
                "measure": _measure_formula_id(sort_by),
                "order": widget.get("sort_order", "desc"),
                "domain": [],
            }

        pivot_id = self._add_pivot_definition(
            model=model,
            name=widget.get("title") or model,
            domain=domain,
            context=widget.get("context") or {},
            rows=rows,
            columns=columns,
            measures=measures,
            filters=widget.get("filters"),
            date_hint=widget.get("date_field"),
            sorted_column=sorted_column,
        )

        link = _view_link(
            widget.get("title") or model,
            model,
            domain,
            "pivot",
            {
                "pivot_row_groupby": [_dimension_key(r) for r in rows],
                "pivot_column_groupby": [_dimension_key(c) for c in columns],
                "pivot_measures": [_measure(m)["fieldName"] for m in measures],
            },
        )
        self._section(widget, link)
        ref = _cell(0, self._row)
        include_total = "TRUE" if widget.get("include_total", False) else "FALSE"
        self.dash_cells[ref] = f"=PIVOT({pivot_id}, {limit}, {include_total}, FALSE)"
        self.dash_styles[ref] = self._style(textColor=TEXT_COLOR, bold=True, fontSize=11)
        self._widen(0, int(widget.get("width", 240)))
        # Header row plus one row per record, plus breathing room.
        self._advance(limit + 3)
        self._max_col = max(self._max_col, len(columns) + len(measures) + 2)

    def add_list(self, widget: Dict[str, Any]) -> None:
        model = _require(widget, "model")
        columns = widget.get("columns") or []
        if not columns:
            raise DashboardSpecError(f"list {widget.get('title') or model!r} needs columns")
        limit = int(widget.get("limit", 10))
        domain = _as_domain(widget.get("domain"))
        fields = self.fields_by_model.get(model, {})

        order_by = []
        for entry in widget.get("order") or []:
            if isinstance(entry, dict):
                order_by.append(entry)
            elif isinstance(entry, (list, tuple)) and len(entry) == 2:
                order_by.append({"name": entry[0], "asc": str(entry[1]).lower() != "desc"})
            else:
                name, _, direction = str(entry).partition(" ")
                order_by.append({"name": name, "asc": direction.lower() != "desc"})
        for column in columns:
            self._check_field(model, _list_field(column), "column")
        for entry in order_by:
            self._check_field(model, entry["name"], "order")

        list_id = str(self._next_list)
        self._next_list += 1
        self.lists[list_id] = {
            "id": list_id,
            "name": widget.get("title") or model,
            "model": model,
            "columns": [_list_field(c) for c in columns],
            "domain": domain,
            "context": widget.get("context") or {},
            "orderBy": order_by,
            "fieldMatching": self.matcher.match(
                model, widget.get("filters"), date_hint=widget.get("date_field")
            ),
        }

        self._section(widget, _view_link(widget.get("title") or model, model, domain, "list"))
        header_row = self._row
        first_line, last_line = header_row + 1, header_row + limit
        last_col = _col_letter(len(columns) - 1)
        self.dash_borders[f"A{header_row + 1}:{last_col}{header_row + 1}"] = self._border(
            bottom={"style": "thin", "color": RULE_COLOR}
        )
        for row in range(header_row, last_line + 1):
            self.row_sizes[row] = TABLE_ROW_HEIGHT

        for col_index, column in enumerate(columns):
            name = _list_field(column)
            opts = column if isinstance(column, dict) else {}
            label = opts.get("string")
            ftype = (fields.get(name) or {}).get("type")
            numeric = ftype in ("float", "monetary", "integer")
            letter = _col_letter(col_index)
            data_range = f"{letter}{first_line + 1}:{letter}{last_line + 1}"

            ref = _cell(col_index, header_row)
            self.dash_cells[ref] = (
                f'=ODOO.LIST.HEADER({list_id},"{name}","{label}")'
                if label
                else f'=ODOO.LIST.HEADER({list_id},"{name}")'
            )
            header_style = {
                "textColor": TEXT_COLOR,
                "bold": True,
                "fontSize": 11,
                "fillColor": HEADER_FILL,
                "verticalAlign": "middle",
            }
            if numeric:
                header_style["align"] = "right"
            self.dash_styles[ref] = self._style(**header_style)

            for line in range(1, limit + 1):
                self.dash_cells[_cell(col_index, header_row + line)] = (
                    f'=ODOO.LIST({list_id},{line},"{name}")'
                )
            self.dash_styles[data_range] = self._style(
                textColor=self.accent if col_index == 0 else TEXT_COLOR,
                verticalAlign="middle",
            )

            pattern = opts.get("format") or (
                self.locale.get("dateFormat") if ftype in DATE_TYPES else None
            )
            if pattern:
                self.dash_formats[data_range] = self._format(pattern)
            highlight = opts.get("highlight")
            if highlight:
                if highlight not in HIGHLIGHT_THRESHOLDS:
                    raise DashboardSpecError(
                        f"unknown highlight {highlight!r} on column {name!r}; "
                        f"pick one of {sorted(HIGHLIGHT_THRESHOLDS)}"
                    )
                self.conditional_formats.append(
                    {
                        "id": _uid(),
                        "ranges": [data_range],
                        "rule": {
                            "type": "CellIsRule",
                            "operator": "isLessThan",
                            "values": [HIGHLIGHT_THRESHOLDS[highlight]],
                            "style": {"textColor": NEGATIVE_TEXT_COLOR, "bold": True},
                        },
                    }
                )
            self._widen(col_index, opts.get("width") or _default_width(name, ftype))

        self._advance(limit + 3)
        self._max_col = max(self._max_col, len(columns) + 1)

    def add_text(self, widget: Dict[str, Any]) -> None:
        text = widget.get("text") or widget.get("title") or ""
        if widget.get("heading", True):
            self._heading(text)
            self._note(widget.get("note"))
        else:
            self._note(text)
        self._advance(1)

    def _chart_link(self, widget: Dict[str, Any]) -> str:
        model = _require(widget, "model")
        group_by = [str(g) for g in (widget.get("group_by") or [])]
        measure, _ = _split_granularity(str(widget.get("measure", "__count")))
        return _view_link(
            widget.get("title") or model,
            model,
            _as_domain(widget.get("domain")),
            "graph",
            {
                "graph_measure": measure,
                "graph_mode": widget.get("chart", "bar"),
                "graph_groupbys": group_by,
            },
        )

    # -- internals ------------------------------------------------------

    def _add_pivot_definition(
        self,
        model: str,
        name: str,
        domain: List[Any],
        context: Dict[str, Any],
        rows: List[Dict[str, Any]],
        columns: List[Dict[str, Any]],
        measures: Sequence[Any],
        filters: Any,
        date_hint: Optional[str],
        sorted_column: Optional[Dict[str, Any]] = None,
    ) -> str:
        pivot_id = str(self._next_pivot)
        self._next_pivot += 1
        hint = date_hint
        if hint is None:
            hint = _first_date([r.get("fieldName", "") for r in rows + columns])
        self.pivots[pivot_id] = {
            "type": "ODOO",
            "id": pivot_id,
            "formulaId": pivot_id,
            "name": name,
            "model": model,
            "domain": domain,
            "context": context,
            "rows": rows,
            "columns": columns,
            "measures": [_measure(m) for m in measures],
            "sortedColumn": sorted_column,
            "fieldMatching": self.matcher.match(model, filters, date_hint=hint),
        }
        return pivot_id

    def _add_data_row(self, label: str, formula: str, humanize: bool) -> str:
        """Park a KPI formula on the Data sheet and return its display cell."""
        row = self._next_data_row
        self._next_data_row += 1
        self.data_cells[_cell(0, row)] = label
        self.data_cells[_cell(1, row)] = formula
        display_col = 1
        if humanize:
            self.data_cells[_cell(2, row)] = f"=FORMAT.LARGE.NUMBER({_cell(1, row)})"
            display_col = 2
        return f"{DATA_SHEET_NAME}!{_cell(display_col, row)}"

    def _span(self) -> Tuple[int, int]:
        """The last column a full-width row reaches, and that row's pixel width."""
        width, col = 0, 0
        while True:
            width += self.col_widths.get(col, DEFAULT_COL_WIDTH)
            if width >= DASHBOARD_WIDTH - FIGURE_GAP or col >= 25:
                return col, width
            col += 1

    def _resolve_full_width_rows(self) -> List[str]:
        last_col, span_width = self._span()
        end = _col_letter(last_col)
        rule = self._border(bottom={"style": "thin", "color": RULE_COLOR})
        for row in self._ruled_rows:
            self.dash_borders[f"A{row + 1}:{end}{row + 1}"] = rule

        merges = []
        note_style = self._style(
            textColor=MUTED_TEXT_COLOR, fontSize=10, wrapping="wrap", verticalAlign="top"
        )
        chars_per_line = max(int((span_width - 12) / NOTE_CHAR_WIDTH), 20)
        for row, text in self._notes:
            ref = _cell(0, row)
            self.dash_cells[ref] = text
            self.dash_styles[ref] = note_style
            if last_col > 0:
                merges.append(f"{ref}:{end}{row + 1}")
            lines = sum(
                max(1, -(-len(paragraph) // chars_per_line)) for paragraph in text.split("\n")
            )
            self.row_sizes[row] = lines * NOTE_LINE_HEIGHT + 10
        self._max_col = max(self._max_col, last_col + 1)
        return merges

    # -- output ---------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        merges = self._resolve_full_width_rows()
        dashboard_rows = max(self._row + 6, 40)
        return {
            "version": DATA_VERSION,
            "revisionId": "START_REVISION",
            "uniqueFigureIds": True,
            "sheets": [
                {
                    "id": self.dashboard_sheet_id,
                    "name": DASHBOARD_SHEET_NAME,
                    "colNumber": max(self._max_col, 7),
                    "rowNumber": dashboard_rows,
                    "cells": self.dash_cells,
                    "styles": self.dash_styles,
                    "formats": self.dash_formats,
                    "borders": self.dash_borders,
                    "merges": merges,
                    "cols": {str(c): {"size": w} for c, w in sorted(self.col_widths.items())},
                    "rows": {str(r): {"size": s} for r, s in sorted(self.row_sizes.items())},
                    "figures": self.figures,
                    "tables": [],
                    "conditionalFormats": self.conditional_formats,
                    "dataValidationRules": [],
                    "headerGroups": {"ROW": [], "COL": []},
                    "comments": {},
                    "areGridLinesVisible": False,
                    "isVisible": True,
                },
                {
                    "id": self.data_sheet_id,
                    "name": DATA_SHEET_NAME,
                    "colNumber": 6,
                    "rowNumber": max(self._next_data_row + 5, 20),
                    "cells": self.data_cells,
                    "styles": {},
                    "formats": {},
                    "borders": {},
                    "merges": [],
                    "cols": {},
                    "rows": {},
                    "figures": [],
                    "tables": [],
                    "conditionalFormats": [],
                    "dataValidationRules": [],
                    "headerGroups": {"ROW": [], "COL": []},
                    "comments": {},
                    "areGridLinesVisible": True,
                    "isVisible": False,
                },
            ],
            "styles": self.styles,
            "formats": self.formats,
            "borders": self.borders,
            "customTableStyles": {},
            "pivots": self.pivots,
            "pivotNextId": self._next_pivot,
            "lists": self.lists,
            "listNextId": self._next_list,
            "globalFilters": self.filters,
            "chartOdooMenusReferences": {},
            "settings": {"locale": self.locale},
        }


def _register(registry: Dict[str, Any], value: Any) -> str:
    """Return the id of ``value`` in a workbook registry, adding it if new."""
    for key, existing in registry.items():
        if existing == value:
            return key
    key = str(len(registry) + 1)
    registry[key] = value
    return key


def _default_width(name: str, ftype: Optional[str]) -> int:
    if name in WIDE_FIELD_NAMES:
        return 240
    return TYPE_WIDTHS.get(ftype or "", DEFAULT_COL_WIDTH)


def _dimension_key(dimension: Dict[str, Any]) -> str:
    granularity = dimension.get("granularity")
    return f"{dimension['fieldName']}:{granularity}" if granularity else dimension["fieldName"]


VIEW_MODES = {
    "list": [[False, "list"], [False, "form"], [False, "search"]],
    "pivot": [[False, "pivot"], [False, "list"], [False, "form"], [False, "search"]],
    "graph": [[False, "graph"], [False, "list"], [False, "form"], [False, "search"]],
}


def _view_link(
    name: str,
    model: str,
    domain: List[Any],
    view_type: str,
    context: Optional[Dict[str, Any]] = None,
) -> str:
    """The ``odoo://view/`` URL Odoo's own dashboards put behind a heading.

    Clicking the heading opens the records behind the section. The link
    carries the section's own domain, not the dashboard's global filters.
    """
    payload = {
        "viewType": view_type,
        "action": {
            "domain": domain,
            "context": context or {},
            "modelName": model,
            "views": VIEW_MODES[view_type],
        },
        "threshold": 0,
        "name": name,
    }
    return "odoo://view/" + json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def _markdown_link(label: str, url: str) -> str:
    """A cell link. Brackets in the label would end the markdown early."""
    clean = str(label).replace("[", "(").replace("]", ")")
    return f"[{clean}]({url})"


def _list_field(column: Any) -> str:
    """The field name of a list column.

    A list's stored ``columns`` is a flat array of field names. The richer
    ``{name, string}`` form only arrived in a later o-spreadsheet version than
    the one we emit, and the migration that runs on load rejects an object
    here with "Invalid path: [object Object]". A column label therefore lives
    only in the ODOO.LIST.HEADER formula, which has always taken one.
    """
    if isinstance(column, dict):
        if "name" not in column:
            raise DashboardSpecError(f"list column needs a name: {column!r}")
        return str(column["name"])
    return str(column)


def _first_date(field_specs: Iterable[str]) -> Optional[str]:
    """Pick the field a date filter should latch onto, from a group_by list."""
    for spec in field_specs:
        name, granularity = _split_granularity(str(spec))
        if granularity or re.search(r"(^|_)date(_|$)", name):
            return name
    return None


def _require(widget: Dict[str, Any], key: str) -> Any:
    value = widget.get(key)
    if not value:
        raise DashboardSpecError(
            f"widget {widget.get('title') or widget.get('type')!r} needs {key}"
        )
    return value


def _normalise_filter(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Turn a terse filter spec into a full o-spreadsheet GlobalFilter."""
    if not isinstance(spec, dict):
        raise DashboardSpecError(f"a filter must be an object, got {spec!r}")
    kind = spec.get("type", "date")
    if kind not in FILTER_TYPES:
        raise DashboardSpecError(
            f"unknown filter type {kind!r}; pick one of {sorted(FILTER_TYPES)}"
        )
    out: Dict[str, Any] = {
        "id": spec.get("id") or _uid(),
        "type": kind,
        "label": spec.get("label") or kind.capitalize(),
    }
    default = spec.get("default", spec.get("defaultValue"))
    if kind == "relation":
        model = spec.get("model") or spec.get("modelName")
        if not model:
            raise DashboardSpecError(f"relation filter {out['label']!r} needs a model")
        out["modelName"] = model
        out["defaultValueDisplayNames"] = []
        # Only emitted when asked for: the key is newer than the format version
        # we write, and the migration on load has no reason to see it otherwise.
        if spec.get("include_children"):
            out["includeChildren"] = True
    elif kind == "selection":
        out["resModel"] = _require(spec, "model")
        out["selectionField"] = _require(spec, "field")
    if default not in (None, "", False):
        out["defaultValue"] = default
    return out


def collect_date_hints(spec: Dict[str, Any]) -> Dict[str, str]:
    """The date field each model should be filtered on, taken from the spec.

    A widget's own ``date_field`` wins; otherwise the date field it already
    groups by is the one the user clearly cares about. The first widget to
    name one decides for every other widget on the same model.
    """
    hints: Dict[str, str] = {}
    for widget in iter_widgets(spec):
        model = widget.get("model")
        if not model or model in hints:
            continue
        hint = widget.get("date_field") or _first_date(
            [str(g) for g in (widget.get("group_by") or [])]
        )
        if hint:
            hints[model] = hint
    return hints


def collect_models(spec: Dict[str, Any]) -> List[str]:
    """Every Odoo model the spec touches, so the caller can fetch their fields."""
    return sorted({w["model"] for w in iter_widgets(spec) if w.get("model")})


def iter_widgets(spec: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    """Every widget in the spec, with the charts inside a row in place of it."""
    for widget in spec.get("widgets") or []:
        if not isinstance(widget, dict):
            continue
        if widget.get("type") == "row":
            yield from (c for c in widget.get("widgets") or [] if isinstance(c, dict))
        else:
            yield widget


def build_dashboard(
    spec: Dict[str, Any],
    fields_by_model: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Turn a dashboard spec into the o-spreadsheet document Odoo stores.

    Args:
        spec: ``{title, description, accent, locale, filters, widgets}``.
            Consecutive ``kpi`` widgets are laid out side by side as one band
            of scorecards; a ``row`` widget puts its charts side by side.
        fields_by_model: ``fields_get`` output per model, used to wire global
            filters automatically. Optional, but without it a filter only
            reaches a widget that names its field explicitly.

    Returns:
        The document, ready to be JSON-encoded into ``spreadsheet_data``.
    """
    if not isinstance(spec, dict):
        raise DashboardSpecError("the dashboard spec must be an object")
    widgets = spec.get("widgets") or []
    if not isinstance(widgets, list):
        raise DashboardSpecError("`widgets` must be a list")

    builder = DashboardBuilder(
        title=spec.get("title"),
        filters=spec.get("filters") or [],
        locale=spec.get("locale"),
        fields_by_model=fields_by_model,
        date_hints=collect_date_hints(spec),
        description=spec.get("description"),
        accent=spec.get("accent"),
    )

    index = 0
    while index < len(widgets):
        widget = widgets[index]
        if not isinstance(widget, dict):
            raise DashboardSpecError(f"widget #{index} must be an object")
        kind = widget.get("type", "chart")
        if kind not in WIDGET_TYPES:
            raise DashboardSpecError(
                f"unknown widget type {kind!r}; pick one of {sorted(WIDGET_TYPES)}"
            )
        if kind == "kpi":
            band = []
            while index < len(widgets) and widgets[index].get("type") == "kpi":
                band.append(widgets[index])
                index += 1
            builder.add_kpis(band)
            continue
        if kind == "chart":
            builder.add_chart(widget)
        elif kind == "pivot":
            builder.add_pivot(widget)
        elif kind == "list":
            builder.add_list(widget)
        elif kind == "row":
            builder.add_row(widget)
        elif kind == "text":
            builder.add_text(widget)
        index += 1

    document = builder.to_dict()
    document[SPEC_KEY] = json.loads(json.dumps(spec))
    return document
