from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

GRAIN_MONTHS = {"month": 1, "quarter": 3, "r3m": 3, "r6m": 6, "r12m": 12}

_MONTH_NUMBERS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_WORD_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "six": 6, "nine": 9, "twelve": 12,
                 "first": 1, "second": 2, "third": 3, "fourth": 4, "1st": 1, "2nd": 2, "3rd": 3, "4th": 4}


@dataclass(frozen=True)
class PeriodParse:
    """One recognized period."""

    start: date
    end: date
    grain: str
    label: str
    confidence: float
    kind: str
    span: tuple[int, int] = (0, 0)

    def contains(self, other: "PeriodParse") -> bool:
        """True when ``other`` lies inside this period (a month inside its quarter, YTD, ...)."""
        return self.start <= other.start and other.end <= self.end

    def same_period(self, other: "PeriodParse") -> bool:
        return (self.start, self.end, self.grain) == (other.start, other.end, other.grain)


def month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def add_months(d: date, n: int) -> date:
    """First day of the month ``n`` months after the month of ``d``."""
    index = d.year * 12 + (d.month - 1) + n
    return date(index // 12, index % 12 + 1, 1)


def snap_month_end(d: date) -> date:
    return month_end(d.year, d.month)


def fiscal_year_start(fiscal_year: int, fiscal_year_start_month: int = 1) -> date:
    """First day of fiscal year ``fiscal_year`` (named by the calendar year it ends in)."""
    if fiscal_year_start_month == 1:
        return date(fiscal_year, 1, 1)
    return date(fiscal_year - 1, fiscal_year_start_month, 1)


def ytd_start(end: date, fiscal_year_start_month: int = 1) -> date:
    year = end.year if end.month >= fiscal_year_start_month else end.year - 1
    return date(year, fiscal_year_start_month, 1)


def period_bounds(end: date, grain: str, *, fiscal_year_start_month: int = 1) -> tuple[date, date]:
    """(start, end) of the ``grain`` window that ends in the month of ``end``."""
    end = snap_month_end(end)
    if grain == "ytd":
        return ytd_start(end, fiscal_year_start_month), end
    if grain == "qtd":
        return date(end.year, end.month - (end.month - 1) % 3, 1), end
    months = GRAIN_MONTHS.get(grain)
    if months is None:
        m = re.fullmatch(r"r(\d{1,2})([mw])", grain or "")
        months = 1 if not m else (int(m[1]) if m[2] == "m" else max(1, round(int(m[1]) / 4.345)))
    return add_months(end, -(months - 1)), end


def grain_from_bounds(start: date | None, end: date | None, *, fiscal_year_start_month: int = 1) -> str | None:
    """Best grain for a (start, end) window: 1 month -> month, a calendar-aligned 3 -> quarter."""
    if start is None or end is None:
        return None
    months = (end.year * 12 + end.month) - (start.year * 12 + start.month) + 1
    if months < 1:
        return None
    if months == 1:
        return "month"
    if months == 3:
        return "quarter" if start.month in (1, 4, 7, 10) else "r3m"
    if months in (6, 12):
        return f"r{months}m"
    if start == ytd_start(snap_month_end(end), fiscal_year_start_month):
        return "ytd"
    return f"r{months}m"


def excel_serial_to_date(serial: float, date_system: str = "1900") -> date:
    """Convert an Excel date serial (1900 or 1904 system) to a date."""
    days = int(float(serial))
    if days < 0:
        raise ValueError(f"negative Excel serial: {serial!r}")
    if str(date_system) == "1904":
        return (datetime(1904, 1, 1) + timedelta(days=days)).date()
    if days == 60:
        raise ValueError("Excel serial 60 is the fictitious 1900-02-29")
    if days < 61:
        return (datetime(1899, 12, 31) + timedelta(days=days)).date()
    return (datetime(1899, 12, 30) + timedelta(days=days)).date()


def _year(text: str) -> int:
    y = int(text)
    if len(text) == 2:
        return 2000 + y if y < 70 else 1900 + y
    return y


def _month_number(name: str) -> int:
    return _MONTH_NUMBERS[name.lower().rstrip(".")[:3]]


_MON = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
        r"|sep(?:t(?:ember)?|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?")
_Y4 = r"(?:19|20)\d{2}"
_AP = r"['’‘`]"
_L = r"(?<![A-Za-z0-9])"
_R = r"(?![A-Za-z0-9])"
_FY = r"(?P<fy>FY|CY)"
_FLAGS = re.IGNORECASE

_WINDOW = re.compile(
    _L + r"(?:(?P<fytd>FYTD|fiscal[\s-]+year[\s-]+to[\s-]+date)|(?P<ytd>YTD|year[\s-]+to[\s-]+date)"
    r"|(?P<qtd>QTD|quarter[\s-]+to[\s-]+date)|(?P<mtd>MTD|month[\s-]+to[\s-]+date)"
    r"|(?P<mat>MAT|TTM|LTM)"
    r"|[RLT](?P<n1>\d{1,2})M"
    r"|(?:rolling|last|trailing|past)[\s-]+(?P<n2>\d{1,2}|three|six|twelve)[\s-]*(?:months?|mos?\.?|m)"
    r"|(?P<n3>\d{1,2})[\s-]*(?:months?|mos?)[\s-]+(?:rolling|trailing|moving)"
    r"|[RL](?P<w1>\d{1,2})W"
    r"|(?:rolling|last|trailing|past)[\s-]+(?P<w2>\d{1,2})[\s-]*(?:weeks?|wks?))" + _R,
    _FLAGS)

_THROUGH = re.compile(
    _L + r"(?:(?:data\s+)?(?:through|thru)|(?:data\s+)?as\s+of|(?:month|period|quarter|year)s?[\s-]+end(?:ing|ed)"
    r"|ending|ended|up\s+to)" + _R, _FLAGS)

_WEEK_ENDING = re.compile(_L + r"(?:week|wk)s?[\s-]+end(?:ing|ed)" + _R, _FLAGS)

_PREFIX_GAP = re.compile(r"[\s:,\-–—(]*(?:(?:ending|ended|through|thru|to|as\s+of|for)[\s:,\-–—]*)?", _FLAGS)
_SUFFIX_GAP = re.compile(r"[\s,\-–—(]*")
_THROUGH_GAP = re.compile(r"[\s:,\-–—(]*")


def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(_L + pattern + _R, _FLAGS)


_ANCHORS: list[tuple[str, re.Pattern[str], int]] = [
    ("iso_date", _rx(rf"(?P<y>{_Y4})(?P<sep>[-/.])(?P<m>0?[1-9]|1[0-2])(?P=sep)(?P<d>0?[1-9]|[12]\d|3[01])"
                     r"(?:[T ]\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?Z?)?"), 0),
    ("num_date", _rx(rf"(?P<a>\d{{1,2}})(?P<sep>[/.-])(?P<b>\d{{1,2}})(?P=sep)(?P<y>{_Y4}|\d{{2}})"), 1),
    ("mdy", _rx(rf"(?P<mon>{_MON})\s+(?P<d>\d{{1,2}})(?:st|nd|rd|th)?,?\s+(?P<y>{_Y4})"), 0),
    ("dmy", _rx(rf"(?P<d>\d{{1,2}})(?:st|nd|rd|th)?[\s-]+(?P<mon>{_MON})[\s,-]+(?P<y>{_Y4})"), 0),
    ("mon_y4", _rx(rf"(?P<mon>{_MON})[\s,.\-/]*{_AP}?(?P<y>{_Y4})"), 2),
    ("mon_yy", _rx(rf"(?P<mon>{_MON})(?:\s*[-/]\s*{_AP}?|\s*{_AP})(?P<yy>\d{{2}})"), 3),
    ("ym", _rx(rf"(?P<y>{_Y4})[-/](?P<m>0[1-9]|1[0-2])"), 2),
    ("my", _rx(rf"(?P<m>0?[1-9]|1[0-2])[-/](?P<y>{_Y4})"), 2),
    ("q_y4", _rx(rf"Q(?P<q>[1-4])[\s\-/]*(?:{_FY}\s*{_AP}?\s*)?(?P<y>{_Y4})"), 1),
    ("q_yy", _rx(rf"Q(?P<q>[1-4])(?:\s*[-/]\s*{_AP}?|\s*{_AP}|\s*{_FY}\s*{_AP}?)(?P<yy>\d{{2}})"), 1),
    ("nq_y", _rx(rf"(?P<q>[1-4])Q\s*[-/]?\s*(?:{_FY}\s*)?{_AP}?\s*(?P<y>{_Y4}|\d{{2}})"), 1),
    ("y_q", _rx(rf"(?:{_FY}\s*{_AP}?\s*)?(?P<y>{_Y4}|\d{{2}})\s*[-/\s]?\s*Q(?P<q>[1-4])"), 2),
    ("q_word", _rx(rf"(?P<qw>first|second|third|fourth|1st|2nd|3rd|4th)\s+quarter(?:\s+of)?,?\s+"
                   rf"(?:(?P<fy>FY|CY|fiscal(?:\s+year)?)\s*)?(?P<y>{_Y4})"), 1),
    ("h_y4", _rx(rf"H(?P<h>[12])[\s\-/]*(?:{_FY}\s*{_AP}?\s*)?(?P<y>{_Y4})"), 1),
    ("h_yy", _rx(rf"H(?P<h>[12])(?:\s*[-/]\s*{_AP}?|\s*{_AP}|\s*{_FY}\s*{_AP}?)(?P<yy>\d{{2}})"), 1),
    ("nh_y", _rx(rf"(?P<h>[12])H\s*[-/]?\s*(?:{_FY}\s*)?{_AP}?\s*(?P<y>{_Y4}|\d{{2}})"), 1),
    ("y_h", _rx(rf"(?:{_FY}\s*{_AP}?\s*)?(?P<y>{_Y4}|\d{{2}})\s*[-/\s]?\s*H(?P<h>[12])"), 2),
    ("h_word", _rx(rf"(?P<hw>first|second|1st|2nd)\s+half(?:\s+of)?,?\s+(?:(?P<fy>FY|CY)\s*)?(?P<y>{_Y4})"), 1),
    ("fy", _rx(rf"{_FY}\s*{_AP}?\s*(?P<y>{_Y4}|\d{{2}})(?:\s*[/-]\s*(?P<y2>{_Y4}|\d{{2}}))?"), 3),
    ("fiscal_word", _rx(rf"fiscal\s+(?:year\s+)?(?P<y>{_Y4})"), 3),
]

_BARE_MONTH = re.compile(rf"\s*(?P<mon>{_MON})\s*", _FLAGS)
_BARE_QUARTER = re.compile(r"(?:Q(?P<q1>[1-4])|(?P<q2>[1-4])Q|H(?P<h1>[12])|(?P<h2>[12])H)", _FLAGS)
_SERIAL = re.compile(r"\s*(?P<n>\d{4,7})(?:\.0+)?\s*")


@dataclass
class _Anchor:
    start: date
    end: date
    grain: str
    kind: str
    confidence: float
    span: tuple[int, int]
    priority: int


def _quarter(q: int, year: int, fiscal: bool, fiscal_start: int) -> tuple[date, date]:
    first = add_months(fiscal_year_start(year, fiscal_start), 3 * (q - 1)) if fiscal else date(year, 3 * q - 2, 1)
    return first, snap_month_end(add_months(first, 2))


def _half(h: int, year: int, fiscal: bool, fiscal_start: int) -> tuple[date, date]:
    first = add_months(fiscal_year_start(year, fiscal_start), 6 * (h - 1)) if fiscal else date(year, 6 * h - 5, 1)
    return first, snap_month_end(add_months(first, 5))


def _build(name: str, m: re.Match[str], priority: int, fiscal_start: int) -> _Anchor | None:
    g = m.groupdict()
    span = m.span()
    try:
        if name in ("iso_date", "num_date", "mdy", "dmy"):
            conf = 0.95
            if name == "iso_date":
                y, mo, d = int(g["y"]), int(g["m"]), int(g["d"])
            elif name == "num_date":
                a, b, y = int(g["a"]), int(g["b"]), _year(g["y"])
                if len(g["y"]) == 2 and g["sep"] != "/":
                    return None
                if a > 12 and b <= 12:
                    mo, d = b, a
                elif a <= 12:
                    mo, d = a, b
                    conf = 0.9 if (b > 12 or a == b) else 0.8
                else:
                    return None
                if len(g["y"]) == 2:
                    conf -= 0.05
            else:
                y, mo, d = int(g["y"]), _month_number(g["mon"]), int(g["d"])
            day = date(y, mo, d)
            return _Anchor(date(y, mo, 1), snap_month_end(day), "month", "date", conf, span, priority)
        if name in ("mon_y4", "mon_yy", "ym", "my"):
            mo = _month_number(g["mon"]) if g.get("mon") else int(g["m"])
            y = _year(g.get("y") or g["yy"])
            conf = 0.9 if name == "mon_yy" else 0.95
            return _Anchor(date(y, mo, 1), month_end(y, mo), "month", "month", conf, span, priority)
        if name.startswith(("q_", "nq_", "y_q")) or name == "q_word":
            ytext = g.get("y") or g.get("yy")
            fy = (g.get("fy") or "").lower()
            if len(ytext) == 2 and not fy and name == "y_q":
                return None
            q = int(g["q"]) if g.get("q") else _WORD_NUMBERS[g["qw"].lower()]
            fiscal = fy.startswith("f")
            start, end = _quarter(q, _year(ytext), fiscal, fiscal_start)
            conf = 0.9 if (fiscal or len(ytext) == 2) else 0.95
            return _Anchor(start, end, "quarter", "quarter", conf, span, priority)
        if name.startswith(("h_", "nh_", "y_h")) or name == "h_word":
            ytext = g.get("y") or g.get("yy")
            fy = (g.get("fy") or "").lower()
            if len(ytext) == 2 and not fy and name == "y_h":
                return None
            h = int(g["h"]) if g.get("h") else _WORD_NUMBERS[g["hw"].lower()]
            fiscal = fy.startswith("f")
            start, end = _half(h, _year(ytext), fiscal, fiscal_start)
            return _Anchor(start, end, "r6m", "half", 0.9, span, priority)
        if name in ("fy", "fiscal_word"):
            fy = (g.get("fy") or "fy").lower()
            year = _year(g["y2"]) if g.get("y2") else _year(g["y"])
            if g.get("y2") and year != _year(g["y"]) + 1:
                return None
            if fy == "cy":
                return _Anchor(date(year, 1, 1), month_end(year, 12), "r12m", "calendar_year", 0.95, span, priority)
            start = fiscal_year_start(year, fiscal_start)
            return _Anchor(start, snap_month_end(add_months(start, 11)), "r12m", "fiscal_year", 0.9, span, priority)
    except (ValueError, KeyError):
        return None
    return None


def _anchors(text: str, fiscal_start: int) -> list[_Anchor]:
    found: list[_Anchor] = []
    for name, rx, priority in _ANCHORS:
        for m in rx.finditer(text):
            a = _build(name, m, priority, fiscal_start)
            if a is not None:
                found.append(a)
    found.sort(key=lambda a: (-(a.span[1] - a.span[0]), a.priority, a.span[0]))
    chosen: list[_Anchor] = []
    for a in found:
        if all(a.span[1] <= c.span[0] or a.span[0] >= c.span[1] for c in chosen):
            chosen.append(a)
    return sorted(chosen, key=lambda a: a.span[0])


def _window_grain_of(m: re.Match[str]) -> str:
    g = m.groupdict()
    if g.get("fytd") or g.get("ytd"):
        return "ytd"
    if g.get("qtd"):
        return "qtd"
    if g.get("mtd"):
        return "month"
    if g.get("mat"):
        return "r12m"
    n = g.get("n1") or g.get("n2") or g.get("n3")
    if n:
        months = _WORD_NUMBERS.get(n.lower()) or int(n)
        return {1: "month", 3: "r3m", 6: "r6m", 12: "r12m"}.get(months, f"r{months}m")
    weeks = g.get("w1") or g.get("w2")
    return f"r{int(weeks)}w"


def window_grain(text: str | None) -> str | None:
    """Grain of the first window token in ``text`` ("YTD" -> ytd, "R3M" -> r3m, "MAT" -> r12m), else None."""
    if not text:
        return None
    m = _WINDOW.search(_prep(text))
    return _window_grain_of(m) if m else None


def _window_kind(grain: str) -> str:
    return grain if grain in ("ytd", "qtd", "month") else "rolling"


def window_bounds(end: date, grain: str, *, fiscal_year_start_month: int = 1) -> tuple[date, date, str]:
    """(start, end, grain) of the window that ends in the month of ``end``."""
    start, end = period_bounds(end, grain, fiscal_year_start_month=fiscal_year_start_month)
    if grain == "qtd":
        months = (end.year * 12 + end.month) - (start.year * 12 + start.month) + 1
        grain = {1: "month", 3: "quarter"}.get(months, "qtd")
    return start, end, grain


def _apply_window(start_end_month: date, grain: str, fiscal_start: int) -> tuple[date, date, str]:
    return window_bounds(start_end_month, grain, fiscal_year_start_month=fiscal_start)


_YEAR_AFTER = re.compile(rf"[\s:,\-–—(]*(?P<y>{_Y4})" + _R)
_YEAR_BEFORE = re.compile(_L + rf"(?P<y>{_Y4})[\s:,\-–—)]*$")
_YEAR_LABEL = re.compile(
    rf"\s*(?:(?:full|calendar)[\s-]+year[\s-]+)?(?P<y>{_Y4})\s*(?:a|act(?:uals?)?|full[\s-]+year|total)?\s*", _FLAGS)


def _adjacent_year(text: str, span: tuple[int, int], free) -> tuple[int, tuple[int, int]] | None:
    m = _YEAR_AFTER.match(text, span[1])
    if m and free(m.span("y")):
        return int(m["y"]), (span[0], m.end("y"))
    m = _YEAR_BEFORE.search(text[:span[0]])
    if m and free(m.span("y")):
        return int(m["y"]), (m.start("y"), span[1])
    return None


def parse_year_label(text: str | None, *, reference: date | None = None) -> PeriodParse | None:
    """A label that is only a completed calendar year ("2025", "2025 Actual"), as an r12m period."""
    if text is None:
        return None
    raw = str(text)
    m = _YEAR_LABEL.fullmatch(_prep(raw))
    if not m:
        return None
    year = int(m["y"])
    if reference is not None and month_end(year, 12) > snap_month_end(reference):
        return None
    return PeriodParse(date(year, 1, 1), month_end(year, 12), "r12m", raw.strip(), 0.8, "calendar_year", (0, len(raw)))


def _prep(text: str) -> str:
    return str(text).replace("_", " ").replace(" ", " ").replace(" ", " ")


def _latest_not_after(month: int, reference: date) -> int:
    return reference.year if month <= reference.month else reference.year - 1


def find_periods(text: str | None, *, reference: date | None = None,
                 fiscal_year_start_month: int = 1) -> list[PeriodParse]:
    """Every period recognized in ``text``, in reading order."""
    if not text or not str(text).strip():
        return []
    t = _prep(text)
    fs = fiscal_year_start_month
    anchors = _anchors(t, fs)
    occupied = [a.span for a in anchors]

    def free(span: tuple[int, int]) -> bool:
        return all(span[1] <= s or span[0] >= e for s, e in occupied)

    windows = [m for m in _WINDOW.finditer(t) if free(m.span())]
    throughs = [m for m in _THROUGH.finditer(t) if free(m.span())]
    week_endings = [m for m in _WEEK_ENDING.finditer(t) if free(m.span())]
    used_windows: set[int] = set()
    out: list[PeriodParse] = []

    if reference is not None:
        for m in throughs:
            tail = re.match(rf"[\s:,\-–—(]*(?P<mon>{_MON})" + _R, t[m.end():], _FLAGS)
            if tail and free((m.end(), m.end() + tail.end())):
                mo = _month_number(tail["mon"])
                y = _latest_not_after(mo, reference)
                span = (m.end() + tail.start("mon"), m.end() + tail.end())
                anchors.append(_Anchor(date(y, mo, 1), month_end(y, mo), "month", "month", 0.8, span, 5))
                occupied.append(span)
        for m in re.finditer(_L + _BARE_QUARTER.pattern + _R, t, _FLAGS):
            if not free(m.span()):
                continue
            q = m["q1"] or m["q2"]
            if q:
                year = reference.year if 3 * int(q) - 2 <= reference.month else reference.year - 1
                start, end = _quarter(int(q), year, False, fs)
                anchors.append(_Anchor(start, end, "quarter", "quarter", 0.6, m.span(), 5))
            else:
                h = int(m["h1"] or m["h2"])
                year = reference.year if 6 * h - 5 <= reference.month else reference.year - 1
                start, end = _half(h, year, False, fs)
                anchors.append(_Anchor(start, end, "r6m", "half", 0.6, m.span(), 5))
            occupied.append(m.span())
        anchors.sort(key=lambda a: a.span[0])

    for a in anchors:
        start, end, grain, kind, conf = a.start, a.end, a.grain, a.kind, a.confidence
        s0, s1 = a.span
        window = None
        for i, w in enumerate(windows):
            if i in used_windows or w.end() > s0:
                continue
            gap = t[w.end():s0]
            if _PREFIX_GAP.fullmatch(gap):
                window = (i, w)
        if window is None:
            for i, w in enumerate(windows):
                if i not in used_windows and w.start() >= s1 and _SUFFIX_GAP.fullmatch(t[s1:w.start()]):
                    window = (i, w)
                    break
        if window is not None:
            i, w = window
            used_windows.add(i)
            start, end, grain = _apply_window(end, _window_grain_of(w), fs)
            kind = _window_kind(grain)
            s0, s1 = min(s0, w.start()), max(s1, w.end())
        else:
            week = next((m for m in week_endings if m.end() <= s0 and _THROUGH_GAP.fullmatch(t[m.end():s0])), None)
            through = next((m for m in throughs if m.end() <= s0 and _THROUGH_GAP.fullmatch(t[m.end():s0])), None)
            if week is not None:
                kind, grain, conf = "week", "week", min(conf, 0.5)
            elif through is not None and a.grain == "month":
                kind = "through"
                if a.kind == "date" and a.end.day != _day_of(t, a):
                    conf = min(conf, 0.8)
        label = t[a.span[0]:a.span[1]].strip() if kind == "through" else t[s0:s1].strip()
        out.append(PeriodParse(start, end, grain, label, round(conf, 3), kind, (s0, s1)))

    if reference is not None:
        ref_end = snap_month_end(reference)
        for i, w in enumerate(windows):
            if i in used_windows:
                continue
            year = _adjacent_year(t, w.span(), free)
            if year is None:
                start, end, grain = _apply_window(ref_end, _window_grain_of(w), fs)
                out.append(PeriodParse(start, end, grain, w.group(0).strip(), 0.6, _window_kind(grain), w.span()))
                continue
            y, span = year
            start, end, grain = _apply_window(month_end(y, ref_end.month), _window_grain_of(w), fs)
            out.append(PeriodParse(start, end, grain, t[span[0]:span[1]].strip(), 0.8, _window_kind(grain), span))
    return sorted(out, key=lambda p: p.span[0])


def _day_of(text: str, anchor: _Anchor) -> int:
    snippet = text[anchor.span[0]:anchor.span[1]]
    for name in ("iso_date", "num_date", "mdy", "dmy"):
        rx = next(r for n, r, _p in _ANCHORS if n == name)
        m = rx.fullmatch(snippet)
        if not m:
            continue
        g = m.groupdict()
        if name == "num_date":
            a, b = int(g["a"]), int(g["b"])
            return a if a > 12 else b
        return int(g["d"])
    return anchor.end.day


def parse_period(text: str | None, *, reference: date | None = None, fiscal_year_start_month: int = 1,
                 excel_serial: bool = False, date_system: str = "1900") -> PeriodParse | None:
    """Parse one period label; None when the text holds no recognizable period."""
    if text is None:
        return None
    raw = str(text)
    if not raw.strip():
        return None
    fs = fiscal_year_start_month
    if excel_serial:
        m = _SERIAL.fullmatch(raw)
        if m:
            try:
                d = excel_serial_to_date(float(m["n"]), date_system)
            except (ValueError, OverflowError):
                return None
            return PeriodParse(date(d.year, d.month, 1), snap_month_end(d), "month", raw.strip(), 0.9, "serial",
                               (0, len(raw)))
    if reference is not None:
        bare = _bare(raw, reference, fs)
        if bare is not None:
            return bare
    found = find_periods(raw, reference=reference, fiscal_year_start_month=fs)
    if not found:
        return None
    through = [p for p in found if p.kind == "through"]
    return through[0] if through else found[0]


def _bare(raw: str, reference: date, fs: int) -> PeriodParse | None:
    t = _prep(raw)
    span = (0, len(raw))
    m = _BARE_MONTH.fullmatch(t)
    if m:
        mo = _month_number(m["mon"])
        y = _latest_not_after(mo, reference)
        return PeriodParse(date(y, mo, 1), month_end(y, mo), "month", raw.strip(), 0.7, "month", span)
    m = re.fullmatch(r"\s*" + _BARE_QUARTER.pattern + r"\s*", t, _FLAGS)
    if m:
        q = m["q1"] or m["q2"]
        if q:
            year = reference.year if 3 * int(q) - 2 <= reference.month else reference.year - 1
            start, end = _quarter(int(q), year, False, fs)
            return PeriodParse(start, end, "quarter", raw.strip(), 0.6, "quarter", span)
        h = int(m["h1"] or m["h2"])
        year = reference.year if 6 * h - 5 <= reference.month else reference.year - 1
        start, end = _half(h, year, False, fs)
        return PeriodParse(start, end, "r6m", raw.strip(), 0.6, "half", span)
    m = _WINDOW.fullmatch(t.strip())
    if m:
        start, end, grain = _apply_window(reference, _window_grain_of(m), fs)
        return PeriodParse(start, end, grain, raw.strip(), 0.6, _window_kind(grain), span)
    m = _BARE_MONTHS_WINDOW.fullmatch(t.strip())
    if m and int(m["n"]) in _BARE_WINDOW_GRAINS:
        grain = _BARE_WINDOW_GRAINS[int(m["n"])]
        end = month_end(reference.year, reference.month)
        start = window_start_for(end, GRAIN_MONTHS[grain])
        return PeriodParse(start, end, grain, raw.strip(), 0.6, "month" if grain == "month" else "rolling", span)
    return None


_BARE_MONTHS_WINDOW = re.compile(r"(?P<n>\d{1,2})\s?M", _FLAGS)
_BARE_WINDOW_GRAINS = {1: "month", 3: "r3m", 6: "r6m", 12: "r12m"}


def window_start_for(end: date, months: int) -> date:
    """First day of the ``months``-month window that ends on ``end``."""
    index = end.year * 12 + end.month - months
    return date(index // 12, index % 12 + 1, 1)


def strip_periods(text: str | None, *, fiscal_year_start_month: int = 1, include_bare: bool = False) -> str:
    """``text`` with every period expression, window token and measurement-end phrase removed."""
    if not text:
        return ""
    t = _prep(text)
    spans = [p.span for p in find_periods(t, fiscal_year_start_month=fiscal_year_start_month)]
    spans += [m.span() for m in _WINDOW.finditer(t)]
    spans += [m.span() for m in _THROUGH.finditer(t)]
    if include_bare:
        spans += [m.span() for m in re.finditer(_L + _BARE_QUARTER.pattern + _R, t, _FLAGS)]
        spans += [m.span() for m in re.finditer(_L + _MON + r"(?![A-Za-z0-9])", t, _FLAGS)]
    out = list(t)
    for s, e in spans:
        for i in range(s, e):
            out[i] = " "
    return re.sub(r"\s+", " ", "".join(out)).strip()
