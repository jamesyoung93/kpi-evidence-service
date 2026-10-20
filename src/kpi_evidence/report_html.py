from __future__ import annotations

import html
import itertools
import math
import re
from collections import defaultdict
from datetime import date
from typing import Any

import pandas as pd

from . import __version__
from .report import (
    FLAG_INFO, REUSED_TOOLTIP, TILE_NAMES, VERDICT_INFO, CheckResult, EvidenceIndex, alternative_sql, alternatives_for,
    as_date, clean, durable_path, flag_for, flags_text, fmt_rel, fmt_value, location_text, relations, status_label,
    status_tooltip,
)

EXTRACTION_HTML_LIMIT = 3000
_COUNTER = itertools.count(1)

_GROUP_CLASS = {"GOOD": "good", "REVIEW": "review", "BLOCKED": "blocked", "INFO": "info"}
_GROUP_ICON = {"GOOD": "✓", "REVIEW": "!", "BLOCKED": "×", "INFO": "i"}
_PLATFORM_COLORS = ("#2a78d6", "#6f7f94", "#2f9bb5")
_REPORT_COLORS = ("#e0662f", "#18a274", "#d4609a", "#c48a00", "#d64545", "#5a9a2e")
_OTHER_COLOR = "#8a94a3"
_ALT_COLOR = "#8a63d2"

_TILES_TOOLTIP = {
    "matches_platform": status_tooltip("VERIFIED"),
    "captured": status_tooltip("REPORT_ONLY"),
    "needs_review": FLAG_INFO["REVIEW"][1],
    "blocked": FLAG_INFO["BLOCKED"][1],
    "other_numbers": status_tooltip("UNMAPPED_CAPTURED"),
}
_TILES_CAPTION = {
    "matches_platform": "reproduced by the platform within tolerance",
    "captured": "report-only KPIs, captured as printed",
    "needs_review": "differences, other definitions, values to confirm",
    "blocked": "platform value could not be computed",
    "other_numbers": "numbers not on the KPI list",
}
_TILES_GROUP = {"matches_platform": "GOOD", "captured": "GOOD", "needs_review": "REVIEW", "blocked": "BLOCKED",
                "other_numbers": "INFO"}

FONTS_URL = ("https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500"
             "&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap")

_TOKENS_LIGHT = """--ke-ink:#1e2430;--ke-ink2:#465061;--ke-muted:#687385;--ke-line:#dde1e6;--ke-surface:#ffffff;
--ke-plane:#f4f6f8;--ke-hover:#e9edf2;--ke-code:#eef1f4;--ke-link:#1d5b79;--ke-accent:#1d5b79;
--ke-grid:#e3e7ec;--ke-axis:#b9c1cc;
--ke-good:#1c6b40;--ke-good-bg:#e4f2e9;--ke-good-bd:#9dcfb0;--ke-review:#7a4f00;--ke-review-bg:#fdf1d8;
--ke-review-bd:#e8c26a;--ke-blocked:#962a2a;--ke-blocked-bg:#fbe7e6;--ke-blocked-bd:#e6a8a4;--ke-info:#38495f;
--ke-info-bg:#e8edf3;--ke-info-bd:#b7c4d5;"""

_TOKENS_DARK = """--ke-ink:#e3e7ec;--ke-ink2:#bcc4cf;--ke-muted:#8f9aa8;--ke-line:#2c333d;--ke-surface:#171b21;
--ke-plane:#11151a;--ke-hover:#222932;--ke-code:#1e242c;--ke-link:#7fbad8;--ke-accent:#7fbad8;
--ke-grid:#262d36;--ke-axis:#47515e;
--ke-good:#7fd3a0;--ke-good-bg:#16301f;--ke-good-bd:#2f6a45;--ke-review:#f2c46b;--ke-review-bg:#33280f;
--ke-review-bd:#7a5c1c;--ke-blocked:#f29b93;--ke-blocked-bg:#3a1b1a;--ke-blocked-bd:#7d3a35;--ke-info:#b3c2d6;
--ke-info-bg:#1d2633;--ke-info-bd:#3d4c60;color-scheme:dark;"""


def theme_css(theme: str = "auto") -> str:
    """Token blocks: light by default."""
    css = ":root{" + _TOKENS_LIGHT + "}"
    if theme == "auto":
        css += ("@media (prefers-color-scheme: dark){:root:not([data-theme=\"light\"]){" + _TOKENS_DARK + "}}"
                ":root[data-theme=\"dark\"]{" + _TOKENS_DARK + "}")
    elif theme == "dark":
        css += ":root{" + _TOKENS_DARK + "}"
    return css


_CSS = """
.ke{font-family:"IBM Plex Sans",system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
font-size:14px;line-height:1.5;color:var(--ke-ink);background:var(--ke-plane);max-width:1200px;margin:0 auto;
padding-inline:16px;padding-block:20px 28px;box-sizing:border-box}
.ke *{box-sizing:border-box}
.ke h1{font-size:24px;line-height:1.2;margin:0 0 6px;font-weight:700;letter-spacing:-.01em;text-wrap:balance}
.ke h2{font-size:18px;line-height:1.3;margin:30px 0 8px;padding-top:16px;border-top:1px solid var(--ke-line);
font-weight:600;text-wrap:balance}
.ke h3{font-size:15px;margin:18px 0 6px;font-weight:600}
.ke p{margin:4px 0 8px;max-width:78ch}
.ke .eyebrow{font-size:11px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:var(--ke-accent)}
.ke .lede{color:var(--ke-ink2);margin:0 0 14px}
.ke .muted{color:var(--ke-muted)}
.ke .small{font-size:12px}
.ke dl.facts{display:grid;grid-template-columns:max-content 1fr;gap:4px 18px;margin:10px 0 18px;padding:12px 14px;
background:var(--ke-surface);border:1px solid var(--ke-line);border-radius:6px}
.ke dl.facts dt{color:var(--ke-muted)}
.ke dl.facts dd{margin:0;overflow-wrap:anywhere}
.ke .tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin:12px 0 8px}
.ke .tile{border:1px solid var(--ke-line);border-top:3px solid var(--c);border-radius:6px;padding:10px 12px;
background:var(--ke-surface)}
.ke .tile.good{--c:var(--ke-good)}.ke .tile.review{--c:var(--ke-review)}.ke .tile.blocked{--c:var(--ke-blocked)}
.ke .tile.info{--c:var(--ke-info)}
.ke .tile .l{font-weight:600}
.ke .tile .v{font-size:30px;font-weight:600;line-height:1.15;font-variant-numeric:tabular-nums;color:var(--c)}
.ke .tile .d{color:var(--ke-muted);font-size:12px}
.ke .tw{overflow-x:auto;margin:8px 0 14px;border:1px solid var(--ke-line);border-radius:6px;max-width:100%;
background:var(--ke-surface)}
.ke table{border-collapse:collapse;width:100%;font-size:13px}
.ke th,.ke td{padding:6px 8px;border-bottom:1px solid var(--ke-line);text-align:left;vertical-align:top}
.ke th{background:var(--ke-plane);font-weight:600;white-space:nowrap;font-size:12px;color:var(--ke-ink2)}
.ke table.sortable th{cursor:pointer;user-select:none}
.ke table.sortable th:hover,.ke table.sortable th:focus-visible{background:var(--ke-hover)}
.ke th:focus-visible,.ke summary:focus-visible,.ke a:focus-visible{outline:2px solid var(--ke-accent);outline-offset:1px}
.ke th[aria-sort=ascending]::after{content:" \\25B2";font-size:9px;color:var(--ke-muted)}
.ke th[aria-sort=descending]::after{content:" \\25BC";font-size:9px;color:var(--ke-muted)}
.ke td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.ke td.wrap{min-width:160px;max-width:380px}
.ke td.file{min-width:150px;max-width:230px}
.ke td.loc{min-width:170px}
.ke td.nw{white-space:nowrap}
.ke td.detail{min-width:280px;max-width:440px}
.ke tr.ev td{background:var(--ke-plane);padding:0 8px 2px;border-bottom:1px solid var(--ke-line)}
.ke .chip{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;font-weight:600;
white-space:nowrap;border:1px solid}
.ke .chip.good{background:var(--ke-good-bg);color:var(--ke-good);border-color:var(--ke-good-bd)}
.ke .chip.review{background:var(--ke-review-bg);color:var(--ke-review);border-color:var(--ke-review-bd)}
.ke .chip.blocked{background:var(--ke-blocked-bg);color:var(--ke-blocked);border-color:var(--ke-blocked-bd)}
.ke .chip.info{background:var(--ke-info-bg);color:var(--ke-info);border-color:var(--ke-info-bd)}
.ke .chip .ic{display:inline-block;width:1em;text-align:center;margin-right:3px}
.ke details>summary{cursor:pointer}
.ke details.drawer>summary{color:var(--ke-ink2);font-size:12px;padding:3px 0}
.ke details.section>summary{font-weight:600;font-size:15px;margin:18px 0 6px}
.ke .ev-body{display:grid;grid-template-columns:150px minmax(0,1fr);gap:5px 14px;padding:6px 2px 10px}
.ke .ev-body>.k{color:var(--ke-muted)}
.ke .ev-body>div{overflow-wrap:anywhere}
.ke pre{background:var(--ke-code);border:1px solid var(--ke-line);border-radius:6px;padding:8px;overflow-x:auto;
font-size:12px;white-space:pre;margin:0;max-width:100%}
.ke code{font-family:"IBM Plex Mono",ui-monospace,SFMono-Regular,Consolas,Menlo,monospace}
.ke figure{margin:8px 0 6px;padding:10px 12px;background:var(--ke-surface);border:1px solid var(--ke-line);
border-radius:6px;max-width:720px}
.ke figure svg{display:block;width:100%;height:auto}
.ke svg .grid{stroke:var(--ke-grid)}
.ke svg .axis{stroke:var(--ke-axis)}
.ke svg .tick{fill:var(--ke-muted);font-family:"IBM Plex Sans",system-ui,sans-serif}
.ke svg .hollow{fill:var(--ke-surface)}
.ke svg .halo{stroke:var(--ke-surface)}
.ke figcaption{color:var(--ke-muted);font-size:12px}
.ke .legend{display:flex;flex-wrap:wrap;gap:4px 16px;font-size:12px;color:var(--ke-ink2);margin:6px 0 2px}
.ke .sw{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:5px;vertical-align:-1px}
.ke .sw.ring{background:var(--ke-surface);border:2px solid var(--c)}
.ke .sw.dot{background:var(--c)}
.ke .sw.diamond{background:var(--ke-surface);border:2px solid var(--c);border-radius:1px;transform:rotate(45deg);
width:9px;height:9px}
.ke table.alts td,.ke table.alts th{padding:3px 6px;font-size:12px}
.ke .kpi-head{display:flex;flex-wrap:wrap;align-items:baseline;gap:6px 10px}
.ke a{color:var(--ke-link)}
.ke footer{margin-top:28px;padding-top:10px;border-top:1px solid var(--ke-line);color:var(--ke-muted);font-size:12px}
@media (max-width:640px){.ke{padding-block:14px 20px}.ke dl.facts{grid-template-columns:1fr}
.ke .ev-body{grid-template-columns:1fr}.ke .tile .v{font-size:24px}}
@media (prefers-reduced-motion:reduce){.ke *{transition:none!important;animation:none!important}}
"""

_JS = """
(function(){
  var root = document.getElementById(%s);
  if (!root) { return; }
  function key(row, i) {
    var c = row.cells[i]; if (!c) { return ''; }
    var v = c.getAttribute('data-v'); return v !== null ? v : c.textContent.trim();
  }
  function sortTable(table, i, th) {
    var body = table.tBodies[0]; if (!body) { return; }
    var dir = th.getAttribute('aria-sort') === 'ascending' ? -1 : 1;
    Array.prototype.forEach.call(th.parentNode.cells, function(c){ c.removeAttribute('aria-sort'); });
    th.setAttribute('aria-sort', dir === 1 ? 'ascending' : 'descending');
    var rows = Array.prototype.slice.call(body.rows), groups = [];
    for (var r = 0; r < rows.length; r++) {
      var g = [rows[r]];
      while (r + 1 < rows.length && rows[r + 1].classList.contains('ev')) { g.push(rows[++r]); }
      groups.push(g);
    }
    groups.sort(function(a, b){
      var x = key(a[0], i), y = key(b[0], i);
      if (x === '' && y !== '') { return 1; }
      if (y === '' && x !== '') { return -1; }
      var nx = Number(x), ny = Number(y);
      if (x !== '' && y !== '' && !isNaN(nx) && !isNaN(ny)) { return (nx - ny) * dir; }
      return x.localeCompare(y, undefined, {numeric: true}) * dir;
    });
    groups.forEach(function(g){ g.forEach(function(row){ body.appendChild(row); }); });
  }
  root.querySelectorAll('table.sortable').forEach(function(table){
    if (!table.tHead) { return; }
    Array.prototype.forEach.call(table.tHead.rows[0].cells, function(th, i){
      th.tabIndex = 0;
      th.addEventListener('click', function(){ sortTable(table, i, th); });
      th.addEventListener('keydown', function(e){
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); sortTable(table, i, th); }
      });
    });
  });
})();
"""


def esc(value: Any) -> str:
    """HTML-escape any value (missing values become an empty string)."""
    value = clean(value)
    return "" if value is None else html.escape(str(value), quote=True)


def _attr_num(value: Any) -> str:
    value = clean(value)
    if value is None:
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return esc(value)
    return repr(number) if math.isfinite(number) else ""


def chip(text: str, group: str, tooltip: str = "") -> str:
    """A status chip: icon + plain label, colored by flag group, tooltip in the title attribute."""
    cls = _GROUP_CLASS.get(group, "info")
    title = f' title="{esc(tooltip)}"' if tooltip else ""
    return f'<span class="chip {cls}"{title}><span class="ic" aria-hidden="true">{_GROUP_ICON.get(group, "i")}</span>{esc(text)}</span>'


def status_chip(row: dict[str, Any]) -> str:
    status = row.get("status")
    return chip(row.get("status_label") or status or "", flag_for(status), status_tooltip(status))


def verdict_chip(verdict: str) -> str:
    group, tooltip = VERDICT_INFO.get(verdict, ("INFO", ""))
    return chip(verdict, group, tooltip)


def _th(label: str, tooltip: str = "") -> str:
    title = f' title="{esc(tooltip)}"' if tooltip else ""
    return f'<th scope="col"{title}>{esc(label)}</th>'


def _td(value: Any, *, num: Any = None, cls: str = "", raw: bool = False) -> str:
    attrs = ""
    if num is not None:
        attrs += f' data-v="{_attr_num(num)}"'
    if cls:
        attrs += f' class="{cls}"'
    return f"<td{attrs}>{value if raw else esc(value)}</td>"


def _file_td(name: Any) -> str:
    text = esc(name)
    for sep in ("_", "-"):
        text = text.replace(sep, sep + "<wbr>")
    return f'<td class="file">{text.replace(".", "<wbr>.")}</td>'


def _date_text(value: Any) -> str:
    d = as_date(value)
    return d.isoformat() if d else ""


def period_text(row: dict[str, Any]) -> str:
    """Period as printed, else start-end dates."""
    label = clean(row.get("period_label"))
    if label:
        return str(label)
    start, end = _date_text(row.get("period_start")), _date_text(row.get("period_end"))
    return f"{start} to {end}" if start and end else end


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-") or "x"


def _records(df: pd.DataFrame) -> list[dict[str, Any]]:
    return [{k: clean(v) for k, v in r.items()} for r in df.to_dict(orient="records")] if len(df) else []


def _table(head: list[str], rows: list[str], *, sortable: bool = True, caption: str = "") -> str:
    cls = ' class="sortable"' if sortable else ""
    cap = f"<caption class=\"muted small\">{esc(caption)}</caption>" if caption else ""
    return (f'<div class="tw"><table{cls}>{cap}<thead><tr>{"".join(head)}</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


_AI_MODES = {
    "off": "off (no model calls)",
    "local": "local (built-in matching rules, no model calls)",
    "ai_query": "ai_query (Databricks AI Functions)",
    "gateway": "gateway (model serving endpoint)",
}


def ai_text(summary: dict[str, Any]) -> str:
    """The AI mode of the run in plain words, with any failed model calls."""
    mode = str(summary.get("ai_mode") or "")
    text = _AI_MODES.get(mode, mode)
    failed = int(summary.get("ai_failures") or 0)
    if failed:
        text += f"; model calls failed for {failed:,} value(s), which were mapped without the model"
    notes = int(summary.get("ai_issues") or 0)
    if notes:
        text += f"; {notes:,} AI message(s) under Issues"
    return text


def _header(result: CheckResult, title: str) -> str:
    s = result.summary or {}
    reports = s.get("reports") or []
    timings = s.get("timings") or {}
    total = timings.get("total_seconds") or timings.get("total") or timings.get("seconds")
    run_bits = [str(s.get("run_id") or "")] if s.get("run_id") else []
    if s.get("generated_at"):
        run_bits.append(f"generated {str(s['generated_at']).replace('T', ' ')} UTC")
    if total is not None:
        try:
            run_bits.append(f"{float(total):.1f} s")
        except (TypeError, ValueError):
            pass
    if s.get("kpi_source", "registry") == "registry":
        kpi_text = "Registry definitions (no KPI table)"
    else:
        rows = f"{s['hint_rows']} rows, " if s.get("hint_rows") is not None else ""
        kpi_text = f"{s.get('kpi_source')}: {rows}{s.get('hint_kpis', 0)} KPI definition(s) from the table"
        if s.get("hint_messages"):
            kpi_text += f"; {s['hint_messages']} message(s) under Issues"
    shown = ", ".join(reports[:6]) + (f" and {len(reports) - 6} more" if len(reports) > 6 else "")
    reused = int(s.get("reused") or 0)
    facts = [
        ("Reports", f"{len(reports)} file(s): {shown}" if reports else "none"),
        ("Platform", s.get("platform") or "not stated"),
        ("KPI definitions", kpi_text),
    ]
    if s.get("state_saved") or reused:
        facts.append(("Saved results", f"{reused:,} of {len(result.comparisons):,} comparison(s) reused from "
                                       f"earlier runs (no platform SQL ran for them)"))
    if s.get("ai_mode"):
        facts.append(("AI", ai_text(s)))
    facts.append(("Run", " · ".join(run_bits) or "not stated"))
    if s.get("focus"):
        facts.insert(3, ("Focus", ", ".join(s["focus"]) + " (other KPIs were extracted and mapped, not compared)"))
    if s.get("brand"):
        facts.insert(1, ("Brand", ", ".join(s["brand"])))
    items = "".join(f"<dt>{esc(k)}</dt><dd>{esc(v)}</dd>" for k, v in facts)
    return (f"<div class=\"eyebrow\">KPI evidence check</div>"
            f"<h1>{esc(title)}</h1><p class=\"lede\">Every number found in the reports, the KPI it maps to, "
            f"the platform value computed by the SQL shown, and a flag. Nothing was published.</p>"
            f'<dl class="facts">{items}</dl>')


def _tiles(result: CheckResult) -> str:
    tiles = (result.summary or {}).get("tiles") or {}
    out = []
    for key, name in TILE_NAMES:
        group = _TILES_GROUP[key]
        out.append(f'<div class="tile {_GROUP_CLASS[group]}" role="listitem" title="{esc(_TILES_TOOLTIP[key])}">'
                   f'<div class="l">{esc(name)}</div><div class="v">{int(tiles.get(key, 0) or 0):,}</div>'
                   f'<div class="d">{esc(_TILES_CAPTION[key])}</div></div>')
    return f'<div class="tiles" role="list">{"".join(out)}</div>'


def _kpi_anchor(kpi_id: Any) -> str:
    return f"kpi-{_slug(kpi_id)}"


def _period_range(r: dict[str, Any]) -> str:
    first, last = _date_text(r.get("first_period")), _date_text(r.get("last_period"))
    if not first:
        return ""
    return esc(first) if first == last else f"{esc(first)}<br>{esc(last)}"


def _verdict_table(result: CheckResult) -> str:
    rows = _records(result.kpis)
    if not rows:
        return "<h2>KPI verdicts</h2><p class=\"muted\">No KPI definitions were used.</p>"
    head = [_th("KPI", "Plain KPI name; click to jump to its section."),
            _th("Source", "registry = governed definition; hint = KPI table row being validated."),
            _th("Verdict", "Whether the definition reproduces the report values."),
            _th("Detail"), _th("Periods compared", "Distinct period and scope combinations compared with the platform."),
            _th("Validated", "Periods where every report value matches the platform under the primary definition."),
            _th("Rows", "Comparison rows for this KPI."), _th("Needs review"), _th("Blocked"),
            _th("Periods", "First and last period end with a report value."),
            _th("Tables", "Platform tables or views the SQL reads.")]
    body = []
    for r in rows:
        name = esc(r["kpi_label"])
        if (r.get("comparisons") or 0) > 0:
            name = f'<a href="#{_kpi_anchor(r["kpi_id"])}">{name}</a>'
        body.append("<tr>" + _td(f'{name}<div class="muted small">{esc(r["kpi_id"])}</div>', raw=True)
                    + _td(r["definition_source"])
                    + _td(verdict_chip(r["verdict"]), raw=True)
                    + _td(r["verdict_detail"] + (f" Error: {r['error']}" if r.get("error") and r["verdict"] != "not computable"
                                                 else ""), cls="detail")
                    + _td(r.get("periods_compared") or 0, num=r.get("periods_compared"), cls="num")
                    + _td(r.get("periods_validated") or 0, num=r.get("periods_validated"), cls="num")
                    + _td(r.get("comparisons") or 0, num=r.get("comparisons"), cls="num")
                    + _td(r.get("review") or 0, num=r.get("review"), cls="num")
                    + _td(r.get("blocked") or 0, num=r.get("blocked"), cls="num")
                    + _td(_period_range(r), num=None, cls="nw", raw=True)
                    + _td(r.get("platform_tables") or "", cls="small") + "</tr>")
    return ("<h2 id=\"verdicts\">KPI verdicts</h2><p class=\"muted small\">One row per KPI definition. "
            "Click a column to sort; hover a verdict for its meaning.</p>" + _table(head, body))


def nice_ticks(lo: float, hi: float, count: int = 4) -> list[float]:
    """Round tick values covering [lo, hi] (about ``count`` intervals)."""
    if not (math.isfinite(lo) and math.isfinite(hi)):
        return [0.0, 1.0]
    if hi < lo:
        lo, hi = hi, lo
    if hi == lo:
        pad = abs(hi) * 0.1 or 1.0
        lo, hi = lo - pad, hi + pad
    raw = (hi - lo) / max(count, 1)
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw - 1e-12)
    start = math.floor(lo / step + 1e-9) * step
    ticks, value = [], start
    while value <= hi + step * 0.5 and len(ticks) < 12:
        ticks.append(round(value, 12))
        value += step
    if ticks[-1] < hi:
        ticks.append(round(ticks[-1] + step, 12))
    return ticks


def axis_label(value: float, unit: str | None, step: float) -> str:
    """Compact, unit-aware tick label: 5.7K / $1.2M / 9.1%."""
    u = (unit or "").lower()
    if u in ("fraction", "percent", "%"):
        decimals = max(0, min(2, -math.floor(math.log10(step * 100)) if step > 0 else 0))
        return f"{value * 100:.{decimals}f}%"
    prefix = "$" if u == "usd" else ""
    absval = max(abs(value), step)
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if absval >= div:
            decimals = max(0, min(2, -math.floor(math.log10(step / div)) if step > 0 else 0))
            return f"{prefix}{value / div:,.{decimals}f}{suffix}"
    decimals = max(0, min(3, -math.floor(math.log10(step)) if step > 0 else 0))
    return f"{prefix}{value:,.{decimals}f}"


def period_tick(end: date, grain: str | None) -> str:
    g = (grain or "month").lower()
    if g == "quarter":
        return f"Q{(end.month - 1) // 3 + 1} {end:%y}"
    if g == "month":
        return f"{end:%b %y}"
    return f"{g.upper()} {end:%b %y}"


def _series_key(rows: list[dict[str, Any]]) -> tuple[str, str] | None:
    periods: dict[tuple[str, str], set] = defaultdict(set)
    has_platform: set = set()
    for r in rows:
        end = as_date(r.get("period_end"))
        if end is None:
            continue
        key = (str(r.get("period_grain") or ""), str(r.get("scope") or "total"))
        periods[key].add(end)
        if clean(r.get("platform_value")) is not None or clean(r.get("alternative_value")) is not None:
            has_platform.add(key)
    if not has_platform:
        return None
    return max(has_platform, key=lambda k: (len(periods[k]), k[1] == "total", k[0] == "month", k))


def _platform_name(definition: str, n_definitions: int) -> str:
    if n_definitions <= 1:
        return "Platform"
    if "[" not in definition:
        return "Platform (primary definition)"
    shown = [p.split(" (")[0] for p in definition.split(" - ", 1)[-1].split("; ") if "[" in p]
    return "Platform (" + ", ".join(shown) + ")"


def _scope_tick(scope: str) -> str:
    if scope == "total":
        return "Total"
    return "; ".join(part.split("=", 1)[-1] for part in scope.split("; "))


def chart_data(rows: list[dict[str, Any]], index: EvidenceIndex | None) -> dict[str, Any] | None:
    """Points for one KPI chart, or None when no row has a platform value."""
    key = _series_key(rows)
    if key is None:
        return None
    grain, scope = key
    in_key = [r for r in rows if as_date(r.get("period_end")) is not None
              and (str(r.get("period_grain") or ""), str(r.get("scope") or "total")) == key]
    ends = sorted({as_date(r["period_end"]) for r in in_key})
    mode, selected = "period", in_key
    if len(ends) == 1:
        same_period = [r for r in rows if as_date(r.get("period_end")) == ends[0]
                       and str(r.get("period_grain") or "") == grain]
        if len({str(r.get("scope") or "total") for r in same_period}) > 1:
            mode, selected = "scope", same_period

    def x_of(r: dict[str, Any]) -> Any:
        return as_date(r["period_end"]) if mode == "period" else str(r.get("scope") or "total")

    reports: dict[str, list[tuple[Any, float, dict[str, Any]]]] = defaultdict(list)
    platform: dict[str, dict[Any, float]] = defaultdict(dict)
    alternatives: dict[str, dict[Any, float]] = defaultdict(dict)
    artifact_def: dict[str, str] = {}
    for r in selected:
        x = x_of(r)
        if clean(r.get("report_value")) is not None:
            reports[str(r.get("report_file") or "report")].append((x, float(r["report_value"]), r))
        if clean(r.get("platform_value")) is not None:
            platform[str(r.get("definition") or "")][x] = float(r["platform_value"])
            result = index.results.get(str(r.get("result_id"))) if index is not None else None
            if result is not None and result.sql_artifact_id:
                artifact_def[result.sql_artifact_id] = str(r.get("definition") or "")
        if clean(r.get("alternative_value")) is not None:
            alternatives[str(r.get("matched_alternative") or "alternative")][x] = float(r["alternative_value"])
    if mode == "period" and index is not None:
        for pkv in index.series:
            definition = artifact_def.get(pkv.sql_artifact_id)
            end = as_date(pkv.period_end)
            if (definition is None or end is None or clean(pkv.value) is None or str(pkv.period_grain) != grain
                    or not (ends[0] <= end <= ends[-1]) or not math.isfinite(float(pkv.value))):
                continue
            platform[definition].setdefault(end, float(pkv.value))
    if mode == "period":
        xs = sorted({x for s in (*platform.values(), *alternatives.values()) for x in s}
                    | {x for pts in reports.values() for x, _v, _r in pts})
        ticks = {x: period_tick(x, grain) for x in xs}
    else:
        xs = sorted({x_of(r) for r in selected}, key=lambda s: (s != "total", s))
        ticks = {x: _scope_tick(x) for x in xs}
    unit = next((r.get("unit") for r in selected if r.get("unit")), None)
    return {"mode": mode, "grain": grain, "scope": scope, "period": ends[0] if mode == "scope" else None,
            "xs": xs, "ticks": ticks, "reports": dict(reports), "platform": dict(platform),
            "alternatives": dict(alternatives), "unit": unit, "outside": len(rows) - len(selected)}


def _report_color(j: int) -> str:
    return _REPORT_COLORS[j] if j < len(_REPORT_COLORS) else _OTHER_COLOR


def _platform_color(k: int) -> str:
    return _PLATFORM_COLORS[min(k, len(_PLATFORM_COLORS) - 1)]


def chart_svg(label: str, data: dict[str, Any], uid: str) -> str:
    """Inline SVG: platform values as rings (joined by a line over periods), report values as dots."""
    xs = data["xs"]
    width = 360 if len(xs) == 1 else 520 if len(xs) <= 4 else 680
    height = 210 if len(xs) <= 4 else 250
    left, right, top, bottom = 60, 16, 14, 40
    pw, ph = width - left - right, height - top - bottom
    unit = data.get("unit")
    values = [v for s in (*data["platform"].values(), *data["alternatives"].values()) for v in s.values()]
    values += [v for pts in data["reports"].values() for _x, v, _r in pts]
    lo, hi = min(values), max(values)
    span = (hi - lo) or abs(hi) * 0.1 or 1.0
    if str(unit or '').lower() not in ('fraction', 'percent', '%') and abs(hi) >= 1 and span < 2:
        span = 2.0
    ticks = nice_ticks(lo - span * 0.08, hi + span * 0.08)
    step = ticks[1] - ticks[0] if len(ticks) > 1 else 1.0
    y0, y1 = ticks[0], ticks[-1]

    def y(v: float) -> float:
        return top + ph - (v - y0) / ((y1 - y0) or 1.0) * ph

    inner = 30.0
    pos = {x: (left + pw / 2 if len(xs) == 1 else left + inner + i * (pw - 2 * inner) / (len(xs) - 1))
           for i, x in enumerate(xs)}
    tick_text = data["ticks"]
    what = "by period" if data["mode"] == "period" else "by scope"
    out = [f'<svg viewBox="0 0 {width} {height}" style="max-width:{width}px" role="img" '
           f'aria-labelledby="{uid}-t {uid}-d">',
           f'<title id="{uid}-t">{esc(label)}: report values compared with platform values {what}</title>',
           f'<desc id="{uid}-d">{esc(_chart_desc(data))}</desc>']
    for t in ticks:
        yy = y(t)
        out.append(f'<line class="grid" x1="{left}" x2="{width - right}" y1="{yy:.1f}" y2="{yy:.1f}" stroke="#e3e7ec" stroke-width="1"/>')
        out.append(f'<text class="tick" x="{left - 8}" y="{yy + 4:.1f}" text-anchor="end" font-size="11" fill="#687385">'
                   f'{esc(axis_label(t, unit, step))}</text>')
    out.append(f'<line class="axis" x1="{left}" x2="{width - right}" y1="{top + ph}" y2="{top + ph}" stroke="#b9c1cc" stroke-width="1"/>')
    every = max(1, math.ceil(len(xs) / 10))
    for i, x in enumerate(xs):
        if i % every == 0 or i == len(xs) - 1:
            out.append(f'<text class="tick" x="{pos[x]:.1f}" y="{top + ph + 18}" text-anchor="middle" font-size="11" fill="#687385">'
                       f'{esc(tick_text[x])}</text>')
    n_defs = len(data["platform"])
    for k, (definition, series) in enumerate(data["platform"].items()):
        color, name = _platform_color(k), _platform_name(definition, n_defs)
        pts = sorted(series.items(), key=lambda kv: xs.index(kv[0]))
        if data["mode"] == "period" and len(pts) > 1:
            path = " ".join(f"{pos[x]:.1f},{y(v):.1f}" for x, v in pts)
            out.append(f'<polyline points="{path}" fill="none" stroke="{color}" stroke-width="2" '
                       f'stroke-linejoin="round" stroke-linecap="round"/>')
        for x, v in pts:
            out.append(f'<circle class="hollow" cx="{pos[x]:.1f}" cy="{y(v):.1f}" r="6" fill="#ffffff" stroke="{color}" stroke-width="2">'
                       f'<title>{esc(name)}: {esc(fmt_value(v, unit))} ({esc(tick_text[x])})</title></circle>')
    for name, series in data["alternatives"].items():
        for x, v in series.items():
            cx, cy = pos[x], y(v)
            out.append(f'<path d="M{cx:.1f} {cy - 7:.1f} L{cx + 7:.1f} {cy:.1f} L{cx:.1f} {cy + 7:.1f} L{cx - 7:.1f} {cy:.1f} Z" '
                       f'class="hollow" fill="#ffffff" stroke="{_ALT_COLOR}" stroke-width="2"><title>Matched alternative ({esc(name)}): '
                       f'{esc(fmt_value(v, unit))} ({esc(tick_text[x])})</title></path>')
    files = list(data["reports"])
    for j, file_name in enumerate(files):
        offset = (j - (len(files) - 1) / 2) * 4 if len(files) > 1 else 0.0
        for x, v, r in data["reports"][file_name]:
            tip = (f"{file_name}: {r.get('value_as_printed') or fmt_value(v, unit)} as printed "
                   f"({period_text(r)}, {r.get('scope') or 'total'}); {r.get('status_label') or ''}")
            out.append(f'<circle class="halo" cx="{pos[x] + offset:.1f}" cy="{y(v):.1f}" r="4.5" fill="{_report_color(j)}" '
                       f'stroke="#ffffff" stroke-width="2"><title>{esc(tip)}</title></circle>')
    out.append("</svg>")
    return "".join(out)


def _chart_desc(data: dict[str, Any]) -> str:
    xs = data["xs"]
    n_reports = len(data["reports"])
    if data["mode"] == "scope":
        where = f"{len(xs)} scope(s) for the period ending {data['period'].isoformat()}"
    else:
        span = f"{xs[0].isoformat()} to {xs[-1].isoformat()}" if len(xs) > 1 else xs[0].isoformat()
        where = f"{len(xs)} period(s), {span}, scope {data['scope']}"
    return (f"{where}; {n_reports} report file(s); {len(data['platform'])} platform definition(s); "
            f"{len(data['alternatives'])} matched alternative(s).")


def _legend(data: dict[str, Any]) -> str:
    items = []
    n_defs = len(data["platform"])
    for k, definition in enumerate(data["platform"]):
        items.append(f'<span><span class="sw ring" style="--c:{_platform_color(k)}"></span>'
                     f'{esc(_platform_name(definition, n_defs))}</span>')
    for name in data["alternatives"]:
        items.append(f'<span><span class="sw diamond" style="--c:{_ALT_COLOR}"></span>'
                     f'Matched alternative ({esc(name)})</span>')
    for j, file_name in enumerate(data["reports"]):
        items.append(f'<span><span class="sw dot" style="--c:{_report_color(j)}"></span>{esc(file_name)}</span>')
    return f'<div class="legend">{"".join(items)}</div>'


def chart_figure(label: str, rows: list[dict[str, Any]], index: EvidenceIndex | None, uid: str) -> str:
    """The chart figure for one KPI, or a one-line note when there are no platform values to chart."""
    data = chart_data(rows, index)
    if data is None:
        return '<p class="muted small">No platform values to chart for this KPI; each row below says why.</p>'
    if data["mode"] == "scope":
        note = f"{(data['grain'] or 'period').capitalize()} ending {data['period']:%b %d, %Y}, by scope."
    else:
        scope = "brand total" if data["scope"] == "total" else data["scope"]
        note = f"{(data['grain'] or 'period').capitalize()} values, {scope}."
    if data["outside"]:
        note += f" {data['outside']} other row(s) (other scopes or period types) are in the table."
    marks = []
    if data["platform"]:
        marks.append("rings: platform value under the definition used")
    marks.append("dots: value printed in each report")
    if data["alternatives"]:
        marks.append("diamonds: the declared alternative that matches")
    text = "; ".join(marks)
    caption = text[0].upper() + text[1:] + ". Hover a mark for its value. " + note
    return (f"<figure>{chart_svg(label, data, uid)}{_legend(data)}"
            f"<figcaption>{esc(caption)}</figcaption></figure>")


_ROW_HEAD = (("Report", "Report file the value was read from."), ("Source", "Where in the file: chart, table, text, image."),
             ("Page", "Slide or page number."), ("Object", "Chart or table title, sheet name or text line."),
             ("Label as printed", "The label next to the number in the report."), ("Period", "Period the value refers to."),
             ("Scope", "total = brand total; otherwise region, territory or segment."),
             ("Report value", "The value as printed, read as a number."),
             ("Platform value", "Computed by the SQL in the evidence drawer."),
             ("Difference", "Report relative to platform."), ("Status", "Hover for what the status means."))


def _page_word(index: EvidenceIndex | None, doc_id: Any) -> str:
    doc = index.docs.get(str(doc_id)) if index is not None else None
    fmt = getattr(getattr(doc, "format", None), "value", getattr(doc, "format", None))
    return "Slide" if fmt in ("PPTX", "PPTM") else "Page"


def _alternatives_html(alts: list[dict[str, Any]], unit: str | None) -> str:
    if not alts:
        return '<span class="muted">No other declared definition was tested for this period.</span>'
    rows = []
    for a in alts:
        if a.get("error"):
            outcome = f"could not run: {a['error']}"
        elif a.get("matches_report"):
            outcome = "matches the report"
        elif a.get("matches_report") is False:
            outcome = "differs from the report"
        else:
            outcome = ""
        rows.append(f"<tr><td>{esc(a.get('label'))}</td><td class=\"num\">{esc(fmt_value(a.get('value'), unit))}</td>"
                    f"<td>{esc(outcome)}</td></tr>")
    return ('<table class="alts"><thead><tr><th>Definition tried</th><th>Value</th><th>Outcome</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>')


def drawer(row: dict[str, Any], index: EvidenceIndex | None) -> str:
    """The ``<details>`` evidence drawer for one comparison row."""
    unit = row.get("unit")
    page_word = _page_word(index, row.get("doc_id"))
    result = index.results.get(str(row.get("result_id"))) if index is not None else None
    value = index.value_of(result) if (index is not None and result is not None) else None
    location = location_text(value, index.docs.get(str(row.get("doc_id"))) if index is not None else None)
    source = (f"{row.get('report_file') or ''}, {page_word.lower()} {row.get('page') if row.get('page') is not None else '?'}"
              f" · {row.get('source_type') or ''}" + (f" · {row['object']}" if row.get("object") else ""))
    parts = [("Source", f"{esc(source)}" + (f'<div class="muted small">{esc(location)}</div>' if location else ""))]
    printed = row.get("value_as_printed")
    read_as = fmt_value(row.get("report_value"), unit)
    parts.append(("Value as printed",
                  (f"<b>{esc(printed)}</b>" if printed is not None else '<span class="muted">no value read</span>')
                  + (f" read as {esc(read_as)}" + (f" ({esc(unit)})" if unit else "") if read_as else "")
                  + (f'; label "{esc(row.get("label_as_printed"))}"' if row.get("label_as_printed") else "")))
    period_bits = period_text(row)
    if row.get("period_start") is not None and row.get("period_end") is not None:
        period_bits += f" ({_date_text(row['period_start'])} to {_date_text(row['period_end'])}, {row.get('period_grain') or '?'})"
    parts.append(("Period and scope", esc(f"{period_bits}; scope {row.get('scope') or 'total'}")))
    if row.get("kpi_id"):
        parts.append(("KPI", esc(f"{row.get('kpi_label')} ({row.get('kpi_id')}), mapped by "
                                 f"{row.get('mapping_method') or 'n/a'}")))
        parts.append(("Definition", esc(row.get("definition") or "")
                      + f' <span class="muted">({esc(row.get("definition_source"))})</span>'))
    if row.get("platform_value") is not None:
        bits = [fmt_value(row["platform_value"], unit)]
        extra = []
        if row.get("rel_diff") is not None:
            extra.append(f"report {fmt_rel(row['rel_diff'])} vs platform")
        if row.get("tolerance"):
            extra.append(f"tolerance {row['tolerance']}")
        if row.get("match_basis"):
            extra.append(f"match {str(row['match_basis']).lower().replace('_', ' ')}")
        parts.append(("Platform value", esc(bits[0] + (f" ({'; '.join(extra)})" if extra else ""))))
    if row.get("matched_alternative"):
        parts.append(("Matched alternative", esc(f"{row['matched_alternative']} gives "
                                                 f"{fmt_value(row.get('alternative_value'), unit)}")))
    if row.get("sql"):
        tables = row.get("platform_tables") or ""
        parts.append(("SQL executed", f"<pre><code>{esc(row['sql'])}</code></pre>"
                      + (f'<div class="muted small">Reads: {esc(tables)}</div>' if tables else "")))
    alt_sql = alternative_sql(index, result) if result is not None else ""
    if alt_sql:
        alt_tables = "; ".join(relations(alt_sql))
        parts.append(("Alternative SQL", f"<pre><code>{esc(alt_sql)}</code></pre>"
                      + '<div class="muted small">The SQL of the matched alternative, as it ran'
                      + (f"; reads: {esc(alt_tables)}" if alt_tables else "") + "</div>"))
    if result is not None and result.recon_run_id:
        parts.append(("Alternatives tried", _alternatives_html(alternatives_for(index, result), unit)))
    if row.get("reason"):
        parts.append(("Reason", esc(row["reason"])))
    if value is not None and value.flags:
        parts.append(("Extraction notes", esc(flags_text(value.flags))))
    if not row.get("metadata_confirmed", True):
        parts.append(("Metadata", "Report metadata (brand, period) was proposed, not confirmed by a person."))
    parts.append(("Ids", f'<span class="muted small">result {esc(row.get("result_id"))} · observation '
                         f'{esc(row.get("observation_id"))}</span>'))
    body = "".join(f'<div class="k">{esc(k)}</div><div>{v}</div>' for k, v in parts)
    summary = (f"Evidence: {row.get('label_as_printed') or ''} · {period_text(row)} · "
               f"{row.get('report_file') or ''} {page_word.lower()} {row.get('page') if row.get('page') is not None else ''}")
    return f'<details class="drawer"><summary>{esc(summary)}</summary><div class="ev-body">{body}</div></details>'


def rows_table(rows: list[dict[str, Any]], index: EvidenceIndex | None) -> str:
    """Comparison rows with one evidence drawer row under each (sorting keeps the pairs together)."""
    head = [_th(label, tip) for label, tip in _ROW_HEAD]
    body = []
    for r in rows:
        unit = r.get("unit")
        end = as_date(r.get("period_end"))
        body.append(
            "<tr>" + _file_td(r.get("report_file")) + _td(r.get("source_type"))
            + _td(r.get("page"), num=r.get("page"), cls="num")
            + _td(r.get("object"), cls="wrap") + _td(r.get("label_as_printed"))
            + f'<td data-v="{esc(end.isoformat() if end else "")}">{esc(period_text(r))}</td>'
            + _td(r.get("scope")) + _td(fmt_value(r.get("report_value"), unit) or (r.get("value_as_printed") or ""),
                                       num=r.get("report_value"), cls="num")
            + _td(fmt_value(r.get("platform_value"), unit), num=r.get("platform_value"), cls="num")
            + _td(fmt_rel(r.get("rel_diff")), num=r.get("rel_diff"), cls="num")
            + f'<td data-v="{esc(r.get("flag"))} {esc(r.get("status_label"))}">{status_chip(r)}'
            + (" " + chip("reused", "INFO", REUSED_TOOLTIP) if r.get("reused") else "") + "</td></tr>")
        body.append(f'<tr class="ev"><td colspan="{len(head)}">{drawer(r, index)}</td></tr>')
    return _table(head, body)


def _kpi_sections(result: CheckResult, uid: str) -> str:
    comps = _records(result.comparisons)
    by_kpi: dict[str, list[dict[str, Any]]] = defaultdict(list)
    no_kpi = []
    for r in comps:
        (by_kpi[str(r["kpi_id"])] if r.get("kpi_id") else no_kpi).append(r)
    out = []
    index = result._index
    for k, kpi in enumerate(_records(result.kpis)):
        rows = by_kpi.get(str(kpi["kpi_id"]))
        if not rows:
            continue
        source = ("KPI table hint", "INFO", "A draft definition from the KPI table; the verdict validates the hint.") \
            if kpi.get("definition_source") == "hint" else ("Registry", "INFO", "Governed definition from the KPI registry.")
        lines = [f'<section id="{_kpi_anchor(kpi["kpi_id"])}">',
                 f'<h2 class="kpi-head">{esc(kpi["kpi_label"])} <span class="muted small">{esc(kpi["kpi_id"])}</span> '
                 f'{verdict_chip(kpi["verdict"])} {chip(source[0], source[1], source[2])}</h2>',
                 f"<p>{esc(kpi['verdict_detail'])}</p>"]
        facts = [("Definition", kpi.get("definition")), ("Tables", kpi.get("platform_tables"))]
        if kpi.get("definition_source") == "hint":
            facts += [("KPI table row", kpi.get("hint_row")), ("KPI table: table", kpi.get("hint_table")),
                      ("KPI table: value SQL", kpi.get("hint_value_sql")), ("KPI table: notes", kpi.get("hint_notes"))]
        if kpi.get("error"):
            facts.append(("Error", kpi.get("error")))
        lines.append('<dl class="facts small">' + "".join(f"<dt>{esc(a)}</dt><dd>{esc(b)}</dd>" for a, b in facts if b)
                     + "</dl>")
        lines.append(chart_figure(str(kpi["kpi_label"]), rows, index, f"{uid}-c{k}"))
        lines.append(f"<h3>Comparisons ({len(rows)})</h3>")
        lines.append(rows_table(rows, index))
        lines.append("</section>")
        out.append("".join(lines))
    if no_kpi:
        review = sum(1 for r in no_kpi if r.get("flag") == "REVIEW")
        open_attr = " open" if review else ""
        out.append(f'<details class="section"{open_attr}><summary>Numbers with no KPI ({len(no_kpi)}): '
                   f'{review} to check, {len(no_kpi) - review} captured</summary>'
                   f'<p class="muted small">Values the report holds that are not on the KPI list, or that could not '
                   f'be read with confidence.</p>{rows_table(no_kpi, index)}</details>')
    return "".join(out)


def _documents(result: CheckResult) -> str:
    rows = _records(result.documents)
    if not rows:
        return "<h2>Documents</h2><p class=\"muted\">No documents.</p>"
    head = [_th("Report"), _th("Format"), _th("Brand"), _th("Report type"), _th("Period"), _th("Version"),
            _th("Status"), _th("Metadata", "Confirmed by a person, or proposed with enough confidence."),
            _th("Pages"), _th("Values", "Numbers extracted from the file."), _th("Comparisons"), _th("Note")]
    body = []
    for r in rows:
        meta = chip("confirmed" if r.get("metadata_confirmed") else "not confirmed",
                    "GOOD" if r.get("metadata_confirmed") else "REVIEW", r.get("metadata_basis") or "")
        note = (f"superseded by {r['superseded_by']}" if r.get("superseded_by") else "") or (r.get("error") or "")
        if r.get("needs_confirmation"):
            note = (note + "; " if note else "") + f"needs confirmation: {r['needs_confirmation']}"
        body.append("<tr>" + _file_td(r.get("report_file")) + _td(r.get("format")) + _td(r.get("brand"))
                    + _td(r.get("report_type")) + _td(r.get("period_label") or _date_text(r.get("period_end")))
                    + _td(r.get("version"), num=r.get("version"), cls="num") + _td(r.get("status"))
                    + _td(meta, raw=True) + _td(r.get("pages"), num=r.get("pages"), cls="num")
                    + _td(r.get("extracted_values"), num=r.get("extracted_values"), cls="num")
                    + _td(r.get("comparisons"), num=r.get("comparisons"), cls="num") + _td(note, cls="wrap") + "</tr>")
    return "<h2 id=\"documents\">Documents</h2>" + _table(head, body)


def _issues(result: CheckResult) -> str:
    rows = _records(result.issues)
    if not rows:
        return "<h2 id=\"issues\">Issues</h2><p class=\"muted\">No issues: every document was read and every KPI SQL ran.</p>"
    group = {"error": "BLOCKED", "warning": "REVIEW", "info": "INFO"}
    head = [_th("Severity"), _th("Category"), _th("Report"), _th("KPI"), _th("Item"), _th("Message"),
            _th("What to do"), _th("Count")]
    body = ["<tr>" + _td(chip(r.get("severity") or "", group.get(str(r.get("severity")), "INFO")), raw=True)
            + _td(r.get("category")) + _file_td(r.get("report_file")) + _td(r.get("kpi_id")) + _td(r.get("item"))
            + _td(r.get("message"), cls="wrap") + _td(r.get("remedy"), cls="wrap")
            + _td(r.get("count"), num=r.get("count"), cls="num") + "</tr>" for r in rows]
    return f"<h2 id=\"issues\">Issues ({len(rows)})</h2>" + _table(head, body)


def _inventory(result: CheckResult) -> str:
    df = result.extraction
    total = len(df)
    rows = _records(df.head(EXTRACTION_HTML_LIMIT))
    head = [_th("Report"), _th("Source"), _th("Page"), _th("Object"), _th("Location"), _th("Label"),
            _th("Value as printed"), _th("Confidence", "1.0 = read from structured storage."), _th("Notes"), _th("KPI"),
            _th("Status")]
    body = ["<tr>" + _file_td(r.get("report_file")) + _td(r.get("source_type")) + _td(r.get("page"), num=r.get("page"), cls="num")
            + _td(r.get("object"), cls="wrap") + _td(r.get("location"), cls="small loc") + _td(r.get("label"))
            + _td(r.get("value_as_printed")) + _td(f"{r['confidence']:.2f}" if r.get("confidence") is not None else "",
                                                   num=r.get("confidence"), cls="num")
            + _td(r.get("flags"), cls="wrap small") + _td(r.get("kpi_id"))
            + _td(chip(status_label(r["status"]), flag_for(r["status"]), status_tooltip(r["status"]))
                  if r.get("status") else "", raw=True)
            + "</tr>" for r in rows]
    note = f" Showing the first {EXTRACTION_HTML_LIMIT:,}; the full list is in result.extraction." if total > len(rows) else ""
    return (f'<details class="section"><summary>Extraction inventory ({total:,} values)</summary>'
            f'<p class="muted small">Every number read from the files, with its location.{esc(note)}</p>'
            f"{_table(head, body)}</details>")


def _sql_section(result: CheckResult) -> str:
    rows = _records(result.sql)
    if not rows:
        return ""
    items = []
    for r in rows:
        kind = ("matched alternative" if r.get("alternative") else
                "primary definition" if r.get("governed_default") else "variant")
        meta = " · ".join(x for x in (r.get("scope") and f"scope {r['scope']}", r.get("period_grain"), kind,
                                           f"{r.get('comparisons') or 0} comparison(s)") if x)
        saved = durable_path(r.get("export_path"))
        export = f'<div class="muted small">Saved to {esc(saved)}</div>' if saved else ""
        items.append(f"<h3>{esc(r.get('kpi_label'))} <span class=\"muted small\">{esc(meta)}</span></h3>"
                     f"<p class=\"small\">{esc(r.get('definition'))}</p>"
                     f"<pre><code>{esc(r.get('value_sql'))}</code></pre>{export}")
    note = (" The SQL of a matched alternative is shown as it ran, with the period end filled in."
            if any(r.get("alternative") for r in rows) else "")
    return (f'<details class="section"><summary>SQL artifacts ({len(rows)})</summary>'
            f'<p class="muted small">Deterministic SQL with {{as_of}} left as a placeholder for the period end.{note}</p>'
            f'{"".join(items)}</details>')


def _uid(result: CheckResult) -> str:
    run = re.sub(r"[^A-Za-z0-9]", "", str((result.summary or {}).get("run_id") or ""))[-12:]
    return f"ke-{run or 'report'}-{next(_COUNTER)}"


def render(result: CheckResult, *, title: str | None = None, theme: str = "auto", fragment: bool = False,
           web_fonts: bool = False) -> str:
    """The HTML report for a ``CheckResult`` (self-contained unless ``web_fonts`` links the web fonts)."""
    title = title or "KPI check report"
    uid = _uid(result)
    body = "".join([
        _header(result, title), _tiles(result), _verdict_table(result), _kpi_sections(result, uid),
        _documents(result), _issues(result), _inventory(result), _sql_section(result),
        f"<footer>Built by kpi_evidence {esc(__version__)}. Report values are read from the files; "
        f"platform values come from the SQL shown in each evidence drawer. Nothing was published.</footer>",
    ])
    script = _JS % ('"' + uid + '"')
    fonts = f'<link rel="stylesheet" href="{FONTS_URL}">' if web_fonts else ""
    style = f"<style>{theme_css(theme)}body{{background:var(--ke-plane);margin:0}}{_CSS}</style>"
    content = f'<div class="ke" id="{uid}">{body}</div><script>{script}</script>'
    if fragment:
        return f"<title>{esc(title)}</title>{fonts}{style}{content}"
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>{esc(title)}</title>{fonts}{style}</head><body>{content}</body></html>")
