"""Five-field cron expressions used by vlambda schedules."""

from __future__ import annotations

import datetime as _dt
from zoneinfo import ZoneInfo

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_DAYS = {
    "sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6,
}


class CronError(ValueError):
    pass


def _replace_names(value: str, names: dict[str, int]) -> str:
    out = value.lower()
    for name, number in names.items():
        out = out.replace(name, str(number))
    return out


def _parse_field(value: str, low: int, high: int, names=None) -> set[int]:
    value = value.strip()
    if not value:
        raise CronError("empty cron field")
    if names:
        value = _replace_names(value, names)
    result: set[int] = set()
    for part in value.split(","):
        part = part.strip()
        if not part:
            raise CronError("empty cron list item")
        step = 1
        has_step = "/" in part
        if has_step:
            part, raw_step = part.split("/", 1)
            try:
                step = int(raw_step)
            except ValueError as error:
                raise CronError(f"invalid cron step: {raw_step}") from error
            if step <= 0:
                raise CronError("cron step must be positive")
        if part == "*":
            start, end = low, high
        elif "-" in part:
            raw_start, raw_end = part.split("-", 1)
            try:
                start, end = int(raw_start), int(raw_end)
            except ValueError as error:
                raise CronError(f"invalid cron range: {part}") from error
        else:
            try:
                start = int(part)
            except ValueError as error:
                raise CronError(f"invalid cron value: {part}") from error
            end = high if has_step else start
        if start < low or end > high or start > end:
            raise CronError(f"cron value out of range [{low}, {high}]: {part}")
        result.update(range(start, end + 1, step))
    if not result:
        raise CronError("cron field has no values")
    return result


class Cron:
    def __init__(self, expression: str):
        fields = str(expression or "").split()
        if len(fields) != 5:
            raise CronError("cron must have five fields: minute hour day month weekday")
        self.expression = " ".join(fields)
        self.minutes = _parse_field(fields[0], 0, 59)
        self.hours = _parse_field(fields[1], 0, 23)
        self.days = _parse_field(fields[2], 1, 31)
        self.months = _parse_field(fields[3], 1, 12, _MONTHS)
        self.weekdays = _parse_field(fields[4], 0, 7, _DAYS)
        if 7 in self.weekdays:
            self.weekdays.discard(7)
            self.weekdays.add(0)
        self.dom_restricted = fields[2].strip() != "*"
        self.dow_restricted = fields[4].strip() != "*"

    def _day_matches(self, day: _dt.datetime) -> bool:
        dom_ok = day.day in self.days
        dow_ok = ((day.weekday() + 1) % 7) in self.weekdays
        if self.dom_restricted and self.dow_restricted:
            return dom_ok or dow_ok
        return dom_ok and dow_ok

    def matches(self, value: _dt.datetime) -> bool:
        return (
            value.minute in self.minutes
            and value.hour in self.hours
            and value.month in self.months
            and self._day_matches(value)
        )

    def next_after(self, after: _dt.datetime, tz: ZoneInfo) -> _dt.datetime:
        if after.tzinfo is None:
            after = after.replace(tzinfo=tz)
        current = after.astimezone(tz).replace(second=0, microsecond=0)
        current += _dt.timedelta(minutes=1)
        # Five years of minutes is a hard bound for impossible expressions.
        for _ in range(5 * 366 * 24 * 60):
            if current.month not in self.months:
                if current.month == 12:
                    current = current.replace(
                        year=current.year + 1, month=1, day=1,
                        hour=0, minute=0, second=0, microsecond=0,
                    )
                else:
                    current = current.replace(
                        month=current.month + 1, day=1,
                        hour=0, minute=0, second=0, microsecond=0,
                    )
                continue
            if not self._day_matches(current):
                current = (current + _dt.timedelta(days=1)).replace(
                    hour=0, minute=0, second=0, microsecond=0,
                )
                continue
            if current.hour not in self.hours:
                current = (current + _dt.timedelta(hours=1)).replace(
                    minute=0, second=0, microsecond=0,
                )
                continue
            if current.minute not in self.minutes:
                current += _dt.timedelta(minutes=1)
                continue
            return current
        raise CronError("cron expression has no occurrence in the next five years")

    def next_timestamp(self, tz_name: str, after_ts: float) -> float:
        try:
            tz = ZoneInfo(tz_name)
        except Exception as error:
            raise CronError(f"invalid timezone: {tz_name}") from error
        after = _dt.datetime.fromtimestamp(after_ts, tz=tz)
        return self.next_after(after, tz).timestamp()
