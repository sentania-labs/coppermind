"""The building blocks every Admin page is made of.

One page shape everywhere: a heading with one line saying what the page is for,
a status card saying what state the thing is in, rows of what is set now with
one button each, and a confirmation for anything destructive. These helpers
return HTML; text that came from a person or a file goes through `e` first.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from html import escape
from typing import Any
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo


def e(value: Any) -> str:
    return escape(str(value), quote=True)


def plural(count: int, one: str, many: str | None = None) -> str:
    return f"{count:,} {one if count == 1 else (many or one + 's')}"


def size(count: float) -> str:
    """A size in the units a person uses: KB, MB, GB."""
    megabytes = count / 1_000_000
    if megabytes >= 1000:
        return f"{megabytes / 1000:.1f} GB"
    if megabytes >= 1:
        return f"{round(megabytes)} MB"
    return f"{max(1, round(count / 1000))} KB"


def url(path: str, **params: Any) -> str:
    """A link inside Admin, with only the parameters that are set."""
    kept = {key: value for key, value in params.items() if value not in (None, "", False)}
    return path + (("&" if "?" in path else "?") + urlencode(kept) if kept else "")


def segment(value: str) -> str:
    return quote(value, safe="")


# -- time, in the owner's own time zone and words --------------------------------------------


def _local(stamp: str | datetime, zone: ZoneInfo) -> datetime | None:
    try:
        moment = datetime.fromisoformat(stamp) if isinstance(stamp, str) else stamp
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(zone)


def clock(moment: datetime) -> str:
    return moment.strftime("%I:%M %p").lstrip("0").lower()


def day(moment: datetime, today: datetime) -> str:
    if moment.date() == today.date():
        return "Today"
    if moment.date() == (today - timedelta(days=1)).date():
        return "Yesterday"
    text = moment.strftime("%b ") + str(moment.day)
    return text if moment.year == today.year else f"{text}, {moment.year}"


def when(stamp: str | datetime | None, zone: ZoneInfo, *, time: bool = True) -> str:
    """ "Today, 2:30 pm", "Yesterday, 2:50 pm" or "Nov 10, 9:01 am"."""
    if not stamp:
        return "an unknown time"
    moment = _local(stamp, zone)
    if moment is None:
        return "an unknown time"
    today = datetime.now(zone)
    return day(moment, today) + (", " + clock(moment) if time else "")


def date_only(stamp: str | datetime | None, zone: ZoneInfo) -> str:
    return when(stamp, zone, time=False)


def month(stamp: str | datetime | None, zone: ZoneInfo) -> str:
    """ "Nov 2026": the month something falls in, for a span too long to give days for."""
    moment = _local(stamp, zone) if stamp else None
    return moment.strftime("%b %Y") if moment else "an unknown month"


def ago(stamp: str | datetime | None, zone: ZoneInfo) -> str:
    """ "a moment ago", "5 minutes ago", "2 hours ago", then the day."""
    if not stamp:
        return "at an unknown time"
    moment = _local(stamp, zone)
    if moment is None:
        return "at an unknown time"
    seconds = (datetime.now(zone) - moment).total_seconds()
    if seconds < 90:
        return "a moment ago"
    if seconds < 3600:
        return f"{round(seconds / 60)} minutes ago"
    if seconds < 86400:
        hours = round(seconds / 3600)
        return "1 hour ago" if hours == 1 else f"{hours} hours ago"
    text = when(moment, zone, time=False)
    return text.lower() if text in {"Today", "Yesterday"} else text


def since(stamp: str | datetime | None, zone: ZoneInfo) -> str:
    """A moment for "has not answered since": a time today, or a day and a time."""
    moment = _local(stamp, zone) if stamp else None
    if moment is None:
        return "a moment ago"
    today = datetime.now(zone)
    if (today - moment).total_seconds() < 90:
        return "a moment ago"
    return clock(moment) if moment.date() == today.date() else when(moment, zone)


# -- pieces ----------------------------------------------------------------------------------


def hidden(fields: Mapping[str, Any] | None) -> str:
    return "".join(
        f'<input type="hidden" name="{e(name)}" value="{e(value)}">'
        for name, value in (fields or {}).items()
        if value is not None
    )


def btn(
    label: str,
    action: str,
    kind: str = "",
    fields: Mapping[str, Any] | None = None,
    *,
    busy: str = "",
    disabled: bool = False,
) -> str:
    """A button that does something: one small form that posts to `action`."""
    style = f" lat-btn--{kind}" if kind else ""
    working = f' data-busy="{e(busy)}"' if busy else ""
    off = " disabled" if disabled else ""
    return (
        f'<form class="act" method="post" action="{e(action)}">{hidden(fields)}'
        f'<button class="lat-btn{style}"{working}{off}>{label}</button></form>'
    )


def submit(
    label: str,
    kind: str = "primary",
    *,
    busy: str = "",
    name: str = "",
    value: str = "",
    formaction: str = "",
) -> str:
    """The button that sends the form it sits in."""
    style = f" lat-btn--{kind}" if kind else ""
    extra = f' name="{e(name)}" value="{e(value)}"' if name else ""
    extra += f' data-busy="{e(busy)}"' if busy else ""
    extra += f' formaction="{e(formaction)}"' if formaction else ""
    return f'<button class="lat-btn{style}"{extra}>{label}</button>'


def go(label: str, to: str, kind: str = "") -> str:
    """A link that looks like a button: it opens a page and changes nothing."""
    style = f" lat-btn--{kind}" if kind else ""
    return f'<a class="lat-btn{style}" href="{e(to)}">{label}</a>'


def a(label: str, to: str) -> str:
    return f'<a href="{e(to)}">{label}</a>'


def banner(kind: str, inner: str, *, fresh: bool = False) -> str:
    role = "alert" if kind == "bad" else "status"
    mark = " data-fresh" if fresh else ""
    return f'<div class="lat-banner lat-banner--{kind}" role="{role}"{mark}>{inner}</div>'


def badge(kind: str, text: str) -> str:
    style = f" lat-badge--{kind}" if kind else ""
    return f'<span class="lat-badge{style}">{text}</span>'


def dot(kind: str) -> str:
    return f'<span class="dot dot--{kind}"></span>'


def card(inner: str, title: str = "") -> str:
    heading = f"<h2>{title}</h2>" if title else ""
    return f'<section class="lat-card">{heading}{inner}</section>'


def row(label: str, value: str, actions: str = "") -> str:
    return (
        f'<div class="row"><div class="row-l">{label}</div><div class="row-v">{value}</div>'
        f'<div class="row-a">{actions}</div></div>'
    )


def rows(*items: str, one: bool = False) -> str:
    return f'<div class="rows{" rows--one" if one else ""}">{"".join(items)}</div>'


def item(title: str, detail: str, actions: str = "") -> str:
    return (
        f'<div class="item"><div class="item-b"><div class="item-t">{title}</div>'
        f'<div class="item-d">{detail}</div></div><div class="item-a">{actions}</div></div>'
    )


def head(title: str, lede: str, back: tuple[str, str] | None = None) -> str:
    """The top of a page. `back` is where the person came from, and what it is called."""
    link = f'<a class="back" href="{e(back[0])}">&lsaquo; Back to {back[1]}</a>' if back else ""
    return f'{link}<h1>{title}</h1><p class="lede">{lede}</p>'


def status(kind: str, title: str, sub: str, actions: str = "") -> str:
    """The one line that says what state the thing is in, with the one action beside it."""
    return (
        f'<section class="lat-card"><div class="status">{dot(kind)}<div>'
        f'<div class="status-t">{title}</div><div class="muted">{sub}</div></div>'
        f'<div class="status-a">{actions}</div></div></section>'
    )


def stepper(labels: Sequence[str], current: int) -> str:
    out = ""
    for index, label in enumerate(labels, start=1):
        state = "done" if index < current else "cur" if index == current else ""
        mark = "&#10003;" if index < current else str(index)
        out += f'<li class="{state}"><span class="n">{mark}</span>{label}</li>'
    return f'<ol class="steps" aria-label="Steps">{out}</ol>'


def field_error(message: str) -> str:
    return f'<div class="lat-hint lat-hint--bad" role="alert">{message}</div>' if message else ""


def text_input(
    name: str,
    label: str,
    value: str = "",
    *,
    kind: str = "text",
    hint: str = "",
    error: str = "",
    placeholder: str = "",
    live: bool = False,
    autofocus: bool = False,
    autocomplete: str = "",
    readonly: bool = False,
) -> str:
    extra = f' placeholder="{e(placeholder)}"' if placeholder else ""
    extra += " data-live" if live else ""
    extra += " autofocus" if autofocus else ""
    extra += " readonly" if readonly else ""
    extra += f' autocomplete="{autocomplete}"' if autocomplete else ""
    extra += ' aria-invalid="true"' if error else ""
    return (
        f'<div class="field"><label class="lat-label" for="{e(name)}">{label}</label>'
        f'<input class="lat-input" id="{e(name)}" name="{e(name)}" type="{kind}" '
        f'value="{e(value)}"{extra}>'
        + (f'<div class="lat-hint">{hint}</div>' if hint else "")
        + field_error(error)
        + "</div>"
    )


def text_area(
    name: str,
    label: str,
    value: str = "",
    *,
    rows: int = 10,
    hint: str = "",
    error: str = "",
    maxlength: int = 0,
) -> str:
    """A box for a few paragraphs of the person's own words."""
    extra = f' maxlength="{maxlength}"' if maxlength else ""
    extra += ' aria-invalid="true"' if error else ""
    return (
        f'<div class="field field--wide"><label class="lat-label" for="{e(name)}">{label}'
        f'</label><textarea class="lat-textarea" id="{e(name)}" name="{e(name)}" '
        f'rows="{rows}"{extra}>{e(value)}</textarea>'
        + (f'<div class="lat-hint">{hint}</div>' if hint else "")
        + field_error(error)
        + "</div>"
    )


def options(choices: Iterable[tuple[str, str]], current: str) -> str:
    return "".join(
        f'<option value="{e(value)}"{" selected" if value == current else ""}>{e(label)}</option>'
        for value, label in choices
    )


def select(
    name: str,
    label: str,
    choices: Iterable[tuple[str, str]],
    current: str,
    *,
    hint: str = "",
    auto: bool = False,
) -> str:
    return (
        f'<div class="field"><label class="lat-label" for="{e(name)}">{label}</label>'
        f'<select class="lat-select" id="{e(name)}" name="{e(name)}"{" data-auto" if auto else ""}>'
        f"{options(choices, current)}</select>"
        + (f'<div class="lat-hint">{hint}</div>' if hint else "")
        + "</div>"
    )


def quick_select(
    action: str,
    name: str,
    label: str,
    choices: Iterable[tuple[str, str]],
    current: str,
    fields: Mapping[str, Any] | None = None,
) -> str:
    """A choice that is saved the moment it is changed, with a Save button when scripts are off."""
    return (
        f'<form class="act" method="post" action="{e(action)}">{hidden(fields)}'
        f'<select class="lat-select inline" name="{e(name)}" aria-label="{e(label)}" data-auto>'
        f"{options(choices, current)}</select>"
        '<noscript> <button class="lat-btn">Save</button></noscript></form>'
    )


def choice(group: str, value: str, title: str, sub: str = "", checked: bool = False) -> str:
    return (
        f'<label class="choice"><input type="radio" name="{e(group)}" value="{e(value)}"'
        f"{' checked' if checked else ''}><span><b>{title}</b>"
        + (f"<span>{sub}</span>" if sub else "")
        + "</span></label>"
    )


def check(
    name: str,
    label: str,
    sub: str = "",
    *,
    checked: bool = False,
    disabled: bool = False,
    value: str = "on",
) -> str:
    return (
        f'<label class="check"><input type="checkbox" name="{e(name)}" value="{e(value)}"'
        f"{' checked' if checked else ''}{' disabled' if disabled else ''}><span>{label}"
        + (f"<small>{sub}</small>" if sub else "")
        + "</span></label>"
    )


def seg(base: str, choices: Sequence[tuple[str, str]], current: str, param: str = "tab") -> str:
    """Tabs. Each one is a link, so a tab can be bookmarked and Back works."""
    out = "".join(
        f'<a href="{e(url(base, **{param: key}))}"'
        + (' class="on" aria-current="true"' if key == current else "")
        + f">{label}</a>"
        for key, label in choices
    )
    return f'<div class="seg">{out}</div>'


def tech(text: str) -> str:
    """The raw detail, for when support asks. Closed until someone wants it."""
    return (
        '<details class="support"><summary class="linkish">Details for support</summary>'
        f'<div class="tech">{e(text)}</div></details>'
    )


def reveal(summary: str, inner: str, *, open_: bool = False) -> str:
    return (
        f'<details class="support"{" open" if open_ else ""}>'
        f'<summary class="linkish">{summary}</summary>{inner}</details>'
    )


def empty(title: str, text: str = "", action: str = "") -> str:
    extra = f'<div class="actions actions--mid">{action}</div>' if action else ""
    return f'<div class="empty"><b>{title}</b>{text}{extra}</div>'


def table(heads: Sequence[str], body: Sequence[Sequence[str]]) -> str:
    top = "".join(f"<th>{name}</th>" for name in heads)
    lines = ""
    for cells in body:
        lines += "<tr>"
        for index, cell in enumerate(cells):
            label = f' data-l="{e(heads[index])}"' if index and heads[index] else ""
            lines += f"<td{label}>{cell}</td>"
        lines += "</tr>"
    return (
        f'<table class="lat-table tcards"><thead><tr>{top}</tr></thead>'
        f"<tbody>{lines}</tbody></table>"
    )


def timeline(entries: Sequence[tuple[str, str]]) -> str:
    return (
        '<ul class="timeline">'
        + "".join(f"<li><time>{at}</time><span>{text}</span></li>" for at, text in entries)
        + "</ul>"
    )


def dialog(
    title: str,
    body: str,
    action: str,
    yes: str,
    cancel: str,
    *,
    danger: bool = False,
    fields: Mapping[str, Any] | None = None,
    busy: str = "",
    no: str = "Cancel",
) -> str:
    """A question drawn over the page. Nothing happens until its button is pressed."""
    kind = "danger" if danger else "primary"
    working = f' data-busy="{e(busy)}"' if busy else ""
    return (
        f'<div class="overlay"><div class="dialog" role="dialog" aria-modal="true" '
        f'aria-label="{e(title)}"><form method="post" action="{e(action)}">{hidden(fields)}'
        f'<h2>{title}</h2><div class="muted">{body}</div><div class="actions actions--end">'
        f'<a class="lat-btn" href="{e(cancel)}" data-cancel>{no}</a>'
        f'<button class="lat-btn lat-btn--{kind}"{working}>{yes}</button>'
        "</div></form></div></div>"
    )


def sentence_list(parts: Sequence[str], joiner: str = "and") -> str:
    """ "a", "a and b", "a, b and c"."""
    parts = [part for part in parts if part]
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + f" {joiner} " + parts[-1]
