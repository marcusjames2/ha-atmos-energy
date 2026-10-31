"""Bill rates over time, so each day's cost uses the rates of the bill it fell on."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

from .const import (
    CONF_FIXED_COST,
    CONF_GCR_RATE,
    CONF_RATE_SCHEDULE,
    CONF_TAX_PERCENT,
    CONF_URI_SURCHARGE,
    CONF_USAGE_RATE,
)

# Defaults used by calculate_total_cost when a rate was never entered (Mid-Tex)
RATE_DEFAULTS: dict[str, float] = {
    CONF_FIXED_COST: 24.40,
    CONF_USAGE_RATE: 0.78,
    CONF_GCR_RATE: 1.17,
    CONF_URI_SURCHARGE: 0.018431,
    CONF_TAX_PERCENT: 8.0,
}


class RateSchedule:
    """The rates entered in the options, each set starting on a date.

    The rates at the top level of the options have no date and cover all
    history before the first dated set. Each dated set, stored under
    `rate_schedule`, applies from its date until the next one's.
    """

    def __init__(self, base: Mapping[str, float], dated: list[tuple[date, dict[str, float]]]) -> None:
        self._base = {k: float(base.get(k, v)) for k, v in RATE_DEFAULTS.items()}
        self._dated = sorted(dated, key=lambda item: item[0])

    @classmethod
    def from_options(cls, options: Mapping[str, Any]) -> RateSchedule:
        dated = [
            (date.fromisoformat(e["effective_from"]), {k: float(e[k]) for k in RATE_DEFAULTS if k in e})
            for e in options.get(CONF_RATE_SCHEDULE, [])
        ]
        return cls(options, dated)

    def at(self, day: date) -> dict[str, float]:
        """The rates in effect on `day`."""
        rates = self._base
        for start, dated in self._dated:
            if start > day:
                break
            rates = {**self._base, **dated}
        return rates

    def with_rates(self, effective_from: date, rates: Mapping[str, float]) -> RateSchedule:
        """A copy where `rates` apply from `effective_from` until the next later set."""
        kept = [(d, r) for d, r in self._dated if d != effective_from]
        new = {k: float(rates[k]) for k in RATE_DEFAULTS if k in rates}
        return RateSchedule(self._base, [*kept, (effective_from, new)])

    def as_option(self) -> list[dict[str, Any]]:
        """The dated sets, for options[CONF_RATE_SCHEDULE]."""
        return [{"effective_from": d.isoformat(), **r} for d, r in self._dated]

    @property
    def dated(self) -> list[tuple[date, dict[str, float]]]:
        return list(self._dated)
