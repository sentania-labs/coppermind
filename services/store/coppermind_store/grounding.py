"""Hold a finished write-up to what was said in the recording.

A model that writes a note sometimes adds a figure, a date or a name nobody said. This reads the
note in code, never by asking a model again, and puts "(not in the recording)" straight after
each number, amount, date or proper name it cannot find in the transcript. It only ever adds that
mark: the note's own words are never removed or changed, so a fact that was real but spelled
differently is at worst flagged, never lost.

What counts as found is deliberately generous, because a wrong mark on a real fact is worse than a
missed invention:

* A number is its value, however it was said: "$250k", "250,000" and "two hundred fifty thousand"
  are one amount, and "25%" is "twenty five percent". Single digits are not checked, because a note
  counts things itself ("two sites"), and so are clock times.
* A date the model worked out from "next Friday" or "end of next year" is found when the recording
  speaks of relative time at all and the date is near the recording's own.
* A name is found when any of its words is heard in the recording, or comes from the speakers, the
  matched invite's title and attendees, or the owner's glossary, in any letter case.
* A word that starts a sentence is not a name by itself, so only what follows it is checked.

Everything is a set lookup over one pass of each text, so a long transcript costs no more than its
length.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

MARK = "(not in the recording)"


def _words(text: str) -> list[str]:
    return text.split()


_ONES = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen"
_ONES += " fifteen sixteen seventeen eighteen nineteen"
_ORDINALS = "first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth"
_ORDINALS += " thirteenth fourteenth fifteenth sixteenth seventeenth eighteenth nineteenth"
_UNITS = {word: number for number, word in enumerate(_words(_ONES))}
_UNITS |= {word: number for number, word in enumerate(_words(_ORDINALS), 1)}
_TENS = {
    word: 20 + 10 * index
    for index, word in enumerate(_words("twenty thirty forty fifty sixty seventy eighty ninety"))
} | {"twentieth": 20, "thirtieth": 30}
_SCALES = {"thousand": 10**3, "grand": 10**3, "million": 10**6, "billion": 10**9}
_NUMBER_WORDS = {*_UNITS, *_TENS, *_SCALES, "hundred"}
_ALPHA = re.compile(r"[A-Za-z]+")
_JOIN = re.compile(r"[ \t-]+")

_MONTHS = {
    name: number
    for number, names in enumerate(
        [
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sept", "sep"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        ],
        1,
    )
    for name in names
}
_FULL = "January|February|March|April|May|June|July|August|September|October|November|December"
_ANY = _FULL + "|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec"
_YEAR = r"(?:19|20)\d{2}"
_DATES = [
    # November 27, 2026 / Nov 27 / March 15th
    re.compile(
        rf"\b(?P<m>{_ANY})\.?\s+(?P<d>\d{{1,2}})(?:st|nd|rd|th)?(?!\d)"
        rf"(?:,?\s+(?P<y>{_YEAR})(?!\d))?"
    ),
    # March 2027
    re.compile(rf"\b(?P<m>{_ANY})\.?,?\s+(?P<y>{_YEAR})(?!\d)"),
    # 27 November 2026
    re.compile(
        rf"\b(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?(?P<m>{_ANY})\b"
        rf"(?:,?\s+(?P<y>{_YEAR})(?!\d))?"
    ),
    # 2026-11-27
    re.compile(r"\b(?P<y>(?:19|20)\d{2})-(?P<mn>\d{2})-(?P<d>\d{2})\b"),
    # 11/27/2026
    re.compile(r"\b(?P<mn>\d{1,2})/(?P<d>\d{1,2})/(?P<y>(?:19|20)?\d{2})\b"),
    # 11/27, which is as often a fraction or a ratio, so it is set aside and never marked
    re.compile(r"(?<![\w/.])(?P<mn>\d{1,2})/(?P<d>\d{1,2})(?![\w/])"),
    # a month on its own: "in March"
    re.compile(rf"\b(?P<m>{_FULL})\b"),
]
_NO_YEAR_NUMERIC = _DATES[5]
# "May" is also a verb, so on its own it is a month only after a word that takes one.
_BEFORE_MAY = re.compile(
    r"\b(?:in|by|until|since|early|late|mid|of|next|last|this|before|after)\s$"
)

# Words in the recording that make a date the model worked out ("by Friday", "end of next
# year", "Q3") a derived fact rather than an invented one, and how far from the recording's own
# day such a date can be.
_CUE_DAYS = {
    **dict.fromkeys(
        _words("today tomorrow tonight yesterday weekend monday tuesday wednesday thursday friday"),
        21,
    ),
    **dict.fromkeys(_words("saturday sunday"), 21),
    **dict.fromkeys(_words("week weeks"), 70),
    **dict.fromkeys(_words("month months"), 140),
    **dict.fromkeys(_words("quarter quarters q1 q2 q3 q4"), 400),
    **dict.fromkeys(_words("year years"), 800),
}
_WEEKDAYS = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}
# Capitalised words that are not names.
_NOT_NAMES = _WEEKDAYS | {name for name in _MONTHS if len(name) > 3}
_NOT_NAMES |= {"i", "may", "mar", "markdown"}
_COMMON_ACRONYMS = set(
    _words(
        "API CEO CTO CIO CFO COO CISO SLA ROI KPI FAQ PDF URL VPN SQL USD EUR GBP OKR POC"
        " TBD FYI ASAP EOD"
    )
)

_TOKEN = re.compile(r"[^\W_]+(?:['’][^\W_]+)*")
_NAME_TOKEN = re.compile(r"[^\W\d_][\w'’]*")
_FIGURE = re.compile(
    r"(?<![\w.,:])(?P<cur>[$€£]\s?)?"
    r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d{1,9})?|\d{1,18}(?:\.\d{1,9})?)(?!\d|,\d|\.\d|:\d)"
    r"(?:(?P<sfx>[kKmMbB]|bn|BN|mm|MM)(?![A-Za-z0-9])|\s(?P<word>thousand|million|billion|grand)\b)?"
    r"(?P<pct>\s?%|\s(?:percent|per\s+cent)\b)?"
)
_HEARD_FIGURE = re.compile(
    r"(?<![\w.])(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d{1,9})?|\d{1,18}(?:\.\d{1,9})?)(?!\d)"
    r"(?:\s?(?P<sfx>[kKmMbB]|bn|BN|mm|MM)(?![A-Za-z0-9])|\s(?P<word>thousand|million|billion|grand)\b)?"
)
_MULTIPLIER = {
    "k": 10**3,
    "m": 10**6,
    "mm": 10**6,
    "b": 10**9,
    "bn": 10**9,
    **_SCALES,
}
# What follows a number when it is a clock time, an ordinal or part of an identifier.
_NOT_A_FIGURE = re.compile(r"(?:[ap]\.?m\b|(?:st|nd|rd|th)\b|[A-Za-z]+\d)", re.I)
_LIST_MARKER = re.compile(r"^\s*(?:[-*+>]\s+|\d{1,3}[.)]\s+)*")
_STAMP = re.compile(r"\[\d{1,2}:\d{2}(?::\d{2})?\]")
_MASKED = [
    re.compile(r"https?://\S+"),
    re.compile(r"`[^`\n]*`"),
    re.compile(r"\[\[[^\]\n]*\]\]"),
    re.compile(r"\]\([^)\n]*\)"),
    _STAMP,
]
_LEAD_LABEL = re.compile(r"^\s*(?:[-*+>]\s+|\d{1,3}[.)]\s+)*\*\*[^*\n]{1,60}\*\*:?")
_IDENTIFIER = re.compile(r"\b[A-Za-z]{2,}-(?P<num>\d{2,18})\b")
_HEADING = re.compile(r"^\s{0,3}#{1,6}(?:\s|$)")


def _norm(word: str) -> str:
    """A word as it is compared: no accents, no letter case, no possessive."""
    plain = "".join(
        c
        for c in unicodedata.normalize("NFKD", word.replace("’", "'"))
        if not unicodedata.combining(c)
    ).casefold()
    return plain[:-2] if plain.endswith("'s") else plain


def _decimal(text: str) -> Decimal | None:
    try:
        return Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None


def spoken(text: str) -> list[tuple[Decimal, int, int]]:
    """Numbers written as words ("two hundred fifty thousand", "twenty-five", "two point five
    million", "two and a half") with where each starts and ends."""
    tokens = [(m.group().lower(), m.start(), m.end()) for m in _ALPHA.finditer(text)]
    found: list[tuple[Decimal, int, int]] = []
    count = len(tokens)

    def joined(k: int) -> bool:
        return _JOIN.fullmatch(text, tokens[k - 1][2], tokens[k][1]) is not None

    i = 0
    while i < count:
        if tokens[i][0] not in _NUMBER_WORDS:
            i += 1
            continue
        total, current, last = Decimal(0), Decimal(0), ""
        j, end = i, i
        while j < count and (j == i or joined(j)):
            word = tokens[j][0]
            if word in _UNITS:
                if last == "unit" or (last == "tens" and _UNITS[word] >= 10):
                    break
                current += _UNITS[word]
                last = "unit"
            elif word in _TENS:
                if last in ("unit", "tens"):
                    break
                current += _TENS[word]
                last = "tens"
            elif word == "hundred":
                if last not in ("", "unit"):
                    break
                current = (current or Decimal(1)) * 100
                last = "hundred"
            elif word in _SCALES:
                total += (current or Decimal(1)) * _SCALES[word]
                current = Decimal(0)
                last = "scale"
            elif word == "and" and last:
                after = j + 1
                if (
                    after + 1 < count
                    and tokens[after][0] == "a"
                    and tokens[after + 1][0] == "half"
                    and joined(after)
                    and joined(after + 1)
                ):
                    current += Decimal("0.5")
                    j = end = after + 2
                    if j < count and joined(j) and tokens[j][0] in _SCALES:
                        total += current * _SCALES[tokens[j][0]]
                        current = Decimal(0)
                        j = end = j + 1
                    break
                if (
                    last in ("hundred", "scale")
                    and after < count
                    and joined(after)
                    and tokens[after][0] in {*_UNITS, *_TENS}
                ):
                    j += 1
                    continue
                break
            elif word == "point" and last:
                digits = ""
                k = j + 1
                while k < count and joined(k) and _UNITS.get(tokens[k][0], 10) < 10:
                    digits += str(_UNITS[tokens[k][0]])
                    k += 1
                if digits:
                    current += Decimal("0." + digits)
                    j = end = k
                    if j < count and joined(j) and tokens[j][0] in _SCALES:
                        total += current * _SCALES[tokens[j][0]]
                        current = Decimal(0)
                        j = end = j + 1
                break
            else:
                break
            j += 1
            end = j
        if end == i:
            i += 1
            continue
        found.append((total + current, tokens[i][1], tokens[end - 1][2]))
        i = end
    return found


@dataclass
class Heard:
    """What the recording and the people behind it said, as sets to look things up in."""

    words: set[str]
    values: set[Decimal]
    months: set[int]
    horizon: int


def listen(transcript: str, known: Iterable[str] = ()) -> Heard:
    words: set[str] = set()
    values: set[Decimal] = set()
    months: set[int] = set()
    horizon = 0
    for text in (_STAMP.sub(" ", transcript), *known):
        previous = ""
        for match in _TOKEN.finditer(text):
            word = _norm(match.group())
            words.add(word)
            if previous:
                words.add(previous + word)
            previous = word
            if word in _MONTHS:
                months.add(_MONTHS[word])
            horizon = max(horizon, _CUE_DAYS.get(word, 0))
        for found in _HEARD_FIGURE.finditer(text):
            base = _decimal(found["num"])
            if base is None:
                continue
            values.add(base)
            scale = _MULTIPLIER.get((found["sfx"] or found["word"] or "").lower())
            if scale:
                values.add(base * scale)
        values.update(value for value, _, _ in spoken(text))
    return Heard(words, values, months, horizon)


def _resolve(month: int, day: int | None, year: int | None, near: date | None) -> date | None:
    """The calendar date a mention stands for; one without a year is the one nearest `near`."""
    try:
        if year is not None:
            return date(year, month, day or 1)
        if near is None:
            return None
        options = [date(near.year + step, month, day or 1) for step in (-1, 0, 1)]
    except ValueError:
        return None
    return min(options, key=lambda option: abs(option - near))


def _date_supported(
    heard: Heard, month: int, day: int | None, year: int | None, recorded_on: date | None
) -> bool:
    far = year is not None and recorded_on is not None and abs(year - recorded_on.year) > 2
    if far and Decimal(year) not in heard.values:  # type: ignore[arg-type]
        return False
    resolved = _resolve(month, day, year, recorded_on)
    if day is not None and resolved is None and year is not None:
        return False
    if resolved is not None and resolved == recorded_on:
        return True
    said_month = month in heard.months
    if day is None and said_month:
        return True
    if day is not None and said_month and Decimal(day) in heard.values:
        return True
    if not heard.horizon:
        return False
    if recorded_on is None or resolved is None:
        return recorded_on is None
    return abs(resolved - recorded_on) <= timedelta(days=heard.horizon)


def _mask(line: str, spans: Iterable[tuple[int, int]]) -> str:
    """The line with each span blanked, in one pass however many spans there are."""
    pieces: list[str] = []
    cursor = 0
    for start, end in sorted(spans):
        start = max(start, cursor)
        if end > start:
            pieces.append(line[cursor:start])
            pieces.append(" " * (end - start))
            cursor = end
    pieces.append(line[cursor:])
    return "".join(pieces)


def _dates(
    line: str, heard: Heard, recorded_on: date | None
) -> tuple[list[tuple[int, str]], list[tuple[int, int]]]:
    marks: list[tuple[int, str]] = []
    taken: list[tuple[int, int]] = []
    working = line
    for pattern in _DATES:
        for match in pattern.finditer(working):
            groups = match.groupdict()
            before = working[max(0, match.start() - 12) : match.start()]
            if (
                groups.get("m") == "May"
                and pattern is _DATES[-1]
                and not _BEFORE_MAY.search(before)
            ):
                continue
            if "mn" in groups and groups["mn"] is not None:
                month = int(groups["mn"])
            else:
                month = _MONTHS[groups["m"].casefold()]
            day = int(groups["d"]) if groups.get("d") else None
            year = int(groups["y"]) if groups.get("y") else None
            if year is not None and year < 100:
                year += 2000
            taken.append((match.start(), match.end()))
            if pattern is _NO_YEAR_NUMERIC:
                continue
            if not 1 <= month <= 12 or (day is not None and not 1 <= day <= 31):
                continue
            if not _date_supported(heard, month, day, year, recorded_on):
                marks.append((match.end(), match.group().strip()))
        working = _mask(working, [(m.start(), m.end()) for m in pattern.finditer(working)])
    return marks, taken


def _figures(line: str, heard: Heard, recorded_on: date | None) -> list[tuple[int, str]]:
    marks: list[tuple[int, str]] = []
    digits = [m.span() for m in _FIGURE.finditer(line)]
    for match in _FIGURE.finditer(line):
        number, suffix = match["num"], match["sfx"]
        if _NOT_A_FIGURE.match(line, match.end()) and not match["pct"]:
            continue
        base = _decimal(number)
        if base is None:
            continue
        scale = None
        end = match.end()
        if match["word"]:
            scale = _MULTIPLIER[match["word"]]
        elif suffix:
            if suffix.lower() in ("k",) or match["cur"] or suffix.isupper():
                scale = _MULTIPLIER[suffix.lower()]
            else:
                end -= len(suffix)
        value = base * scale if scale else base
        plain = not (match["cur"] or match["pct"] or scale or "," in number or "." in number)
        if plain and value < 10:
            continue
        near_year = recorded_on is None or abs(int(value) - recorded_on.year) <= 2
        if plain and 1900 <= value <= 2100 and "." not in number and near_year:
            continue
        if value not in heard.values:
            marks.append((end, line[match.start() : end].strip()))
    for value, start, end in spoken(_mask(line, digits)):
        if value >= 100 and value not in heard.values:
            marks.append((end, line[start:end]))
    return marks


def _sentence_start(line: str, start: int, lead: int) -> bool:
    before = line[max(lead, start - 40) : start].rstrip(" \t*_\"“”‘’'([")
    if not before or before[-1] in ".!?:":
        return True
    return _STAMP.search(before) is not None and before.endswith("]")


def _names(line: str, heard: Heard, lead: int) -> list[tuple[int, str]]:
    marks: list[tuple[int, str]] = []
    tokens = [m for m in _NAME_TOKEN.finditer(line) if m.start() >= lead]
    run: list[re.Match[str]] = []

    def close() -> None:
        if not run:
            return
        first = run[0]
        kept = list(run)
        if _sentence_start(line, first.start(), lead):
            kept = kept[1:]
        kept = [t for t in kept if _is_name_word(t.group())]
        if kept and not any(_known(heard, t.group()) for t in kept):
            marks.append((kept[-1].end(), line[kept[0].start() : kept[-1].end()]))
        run.clear()

    for token in tokens:
        word = token.group()
        looks = word[0].isupper() or (word[0].islower() and any(c.isupper() for c in word[1:]))
        if looks and run and _JOIN.fullmatch(line, run[-1].end(), token.start()) is None:
            close()
        if looks:
            run.append(token)
        else:
            close()
    close()
    return marks


def _is_name_word(word: str) -> bool:
    plain = _norm(word)
    if plain in _NOT_NAMES or re.fullmatch(r"i'[a-z]+", plain):
        return False
    letters = [c for c in word if c.isalpha()]
    return not (word.isupper() and (len(letters) <= 2 or word in _COMMON_ACRONYMS))


def _known(heard: Heard, word: str) -> bool:
    return _norm(word) in heard.words


def ground(
    note: str,
    transcript: str,
    *,
    known: Iterable[str] = (),
    recorded_on: date | None = None,
) -> tuple[str, list[str]]:
    """The note with "(not in the recording)" after each figure, date or name that was not said,
    and what was marked. Nothing else in the note changes."""
    heard = listen(transcript, known)
    pieces: list[str] = []
    unsupported: list[str] = []
    for raw in note.splitlines(keepends=True):
        line = raw.rstrip("\r\n")
        found: list[tuple[int, str]] = []
        if line.strip() and not _HEADING.match(line):
            spans = [m.span() for pattern in _MASKED for m in pattern.finditer(line)]
            label = _LEAD_LABEL.match(line)
            if label:
                spans.append(label.span())
            marker = _LIST_MARKER.match(line)
            lead = marker.end() if marker else 0
            if lead:
                spans.append((0, lead))
            found = []
            protected = _mask(line, spans)
            for ident in _IDENTIFIER.finditer(protected):
                spans.append(ident.span())
                if Decimal(ident["num"]) not in heard.values:
                    found.append((ident.end(), ident.group()))
            scan = _mask(line, spans)
            marks, taken = _dates(scan, heard, recorded_on)
            scan = _mask(scan, taken)
            found += marks + _figures(scan, heard, recorded_on)
            scan = _mask(scan, [(m.start(), m.end()) for m in _FIGURE.finditer(scan)])
            found += _names(scan, heard, lead)
        inserts: dict[int, str] = {}
        for end, what in sorted(found):
            if end not in inserts and not line[end:].lstrip().startswith(MARK):
                inserts[end] = what
        cursor = 0
        built: list[str] = []
        for end in sorted(inserts):
            built.append(line[cursor:end] + " " + MARK)
            cursor = end
            unsupported.append(inserts[end])
        built.append(line[cursor:] + raw[len(line) :])
        pieces.append("".join(built))
    return "".join(pieces), unsupported
