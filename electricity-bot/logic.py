"""Period math, usage computation, and text rendering of a user's meter tree.

Every function here is scoped to a single Telegram user_id.
"""
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass

import db

DEFAULT_PERIOD_DAYS = 7


def parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass
class Period:
    start: datetime
    end: datetime
    index: int
    length_days: int


def _get_or_init_period_settings(user_id: int) -> tuple[datetime, int]:
    anchor_raw = db.get_setting(user_id, "period_anchor")
    days_raw = db.get_setting(user_id, "period_days")
    if anchor_raw is None or days_raw is None:
        anchor = datetime.now(timezone.utc)
        days = DEFAULT_PERIOD_DAYS
        db.set_setting(user_id, "period_anchor", anchor.isoformat())
        db.set_setting(user_id, "period_days", str(days))
        return anchor, days
    return parse_iso(anchor_raw), int(days_raw)


def current_period(user_id: int, at: datetime | None = None) -> Period:
    at = at or datetime.now(timezone.utc)
    anchor, length_days = _get_or_init_period_settings(user_id)
    length = timedelta(days=length_days)
    if at < anchor:
        return Period(start=anchor, end=anchor + length, index=0, length_days=length_days)
    elapsed = at - anchor
    index = elapsed // length
    start = anchor + index * length
    end = start + length
    return Period(start=start, end=end, index=int(index), length_days=length_days)


def period_by_index(user_id: int, index: int) -> Period:
    anchor, length_days = _get_or_init_period_settings(user_id)
    length = timedelta(days=length_days)
    start = anchor + index * length
    end = start + length
    return Period(start=start, end=end, index=index, length_days=length_days)


def set_period(user_id: int, length_days: int, anchor: datetime | None = None):
    db.set_setting(user_id, "period_days", str(length_days))
    db.set_setting(user_id, "period_anchor", (anchor or datetime.now(timezone.utc)).isoformat())


@dataclass
class MeterUsage:
    meter: object
    baseline: float | None
    latest: float | None
    usage: float | None  # None if not enough data
    baseline_is_exact: bool  # False if we had to fall back to first-in-period reading


def compute_usage(meter_id: int, user_id: int, period: Period) -> MeterUsage:
    meter = db.get_meter(meter_id, user_id)
    start_iso = period.start.isoformat()
    end_iso = period.end.isoformat()

    baseline_row = db.get_last_reading_at_or_before(meter_id, user_id, start_iso)
    baseline_is_exact = True
    if baseline_row is None:
        baseline_row = db.get_first_reading_at_or_after(meter_id, user_id, start_iso)
        baseline_is_exact = False

    now_iso = datetime.now(timezone.utc).isoformat()
    cap_iso = min(end_iso, now_iso)
    latest_row = db.get_last_reading_at_or_before(meter_id, user_id, cap_iso)

    if baseline_row is None or latest_row is None:
        return MeterUsage(meter, None, None, None, baseline_is_exact)

    baseline = baseline_row["value"]
    latest = latest_row["value"]
    if latest_row["id"] == baseline_row["id"]:
        # only one reading available in range
        return MeterUsage(meter, baseline, latest, None, baseline_is_exact)

    usage = latest - baseline
    return MeterUsage(meter, baseline, latest, usage, baseline_is_exact)


def build_tree(user_id: int, parent_id: int | None = None):
    """Returns nested list of (meter_row, [children...]) sorted by name."""
    children = db.get_children(user_id, parent_id)
    return [(m, build_tree(user_id, m["id"])) for m in children]


def flatten_tree(tree, depth: int = 0):
    """Yield (meter_row, depth) in preorder."""
    for meter, children in tree:
        yield meter, depth
        yield from flatten_tree(children, depth + 1)


def format_meter_path(meter_id: int, user_id: int) -> str:
    parts = []
    m = db.get_meter(meter_id, user_id)
    while m is not None:
        parts.append(m["name"])
        m = db.get_meter(m["parent_id"], user_id) if m["parent_id"] else None
    return " > ".join(reversed(parts))


def fmt_num(x: float) -> str:
    return f"{x:,.2f}"


def render_tree_text(user_id: int) -> str:
    tree = build_tree(user_id, None)
    if not tree:
        return "No meters set up yet. Use /addmeter to create your first one."
    lines = ["🔌 *Meter structure*", ""]
    _render_branch(tree, user_id, "", lines)
    return "\n".join(lines)


def _render_branch(tree, user_id: int, prefix: str, lines: list[str]):
    for i, (meter, children) in enumerate(tree):
        last = i == len(tree) - 1
        connector = "└─ " if last else "├─ "
        last_reading = db.get_last_reading(meter["id"], user_id)
        suffix = f" _(last: {fmt_num(last_reading['value'])})_" if last_reading else " _(no readings yet)_"
        lines.append(f"{prefix}{connector}{meter['name']} `#{meter['id']}`{suffix}")
        extension = "    " if last else "│   "
        _render_branch(children, user_id, prefix + extension, lines)


def render_usage_report(user_id: int, period: Period | None = None) -> str:
    period = period or current_period(user_id)
    tree = build_tree(user_id, None)
    if not tree:
        return "No meters set up yet. Use /addmeter to create your first one."

    now = datetime.now(timezone.utc)
    status = "ongoing" if period.end > now else "closed"
    header = (
        f"📊 *Usage report* — every {period.length_days}d period\n"
        f"{period.start:%Y-%m-%d %H:%M} → {period.end:%Y-%m-%d %H:%M} UTC ({status})\n"
    )
    lines = [header]
    _render_usage_branch(tree, user_id, "", period, lines)
    return "\n".join(lines)


def _render_usage_branch(tree, user_id: int, prefix: str, period: Period, lines: list[str]):
    for i, (meter, children) in enumerate(tree):
        last = i == len(tree) - 1
        connector = "└─ " if last else "├─ "
        mu = compute_usage(meter["id"], user_id, period)
        if mu.usage is not None:
            approx = "" if mu.baseline_is_exact else "~"
            usage_str = f"{approx}{fmt_num(mu.usage)} kWh"
        elif mu.latest is not None:
            usage_str = "no baseline yet"
        else:
            usage_str = "no data"
        lines.append(f"{prefix}{connector}*{meter['name']}* — {usage_str}")

        extension = "    " if last else "│   "

        # unaccounted = this meter's usage minus sum of its direct children's usage
        if children and mu.usage is not None:
            child_usages = [compute_usage(c["id"], user_id, period) for c, _ in children]
            if all(cu.usage is not None for cu in child_usages):
                unaccounted = mu.usage - sum(cu.usage for cu in child_usages)
                child_prefix = prefix + extension
                lines.append(f"{child_prefix}· _(unaccounted: {fmt_num(unaccounted)} kWh)_")

        _render_usage_branch(children, user_id, prefix + extension, period, lines)
