"""Dated rates: each bill's rates for its own service period, and recalculating history."""
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.atmos_energy.const import DOMAIN
from custom_components.atmos_energy.coordinator import AtmosEnergyDataUpdateCoordinator
from custom_components.atmos_energy.rates import RateSchedule

LA = {"fixed_cost": 31.25, "usage_rate": 0.75187, "uri_surcharge": 0.0, "tax_percent": 4.0}
DAILY = {"username": "atmos_user", "password": "pw", "operation_mode": "daily"}

# Three Atmos Louisiana bills: service period, billing month, CCF, gas cost, current charges
BILLS = [
    (date(2026, 7, 7), date(2026, 8, 5), "Aug 26", 37, 0.46844, 79.44),  # before the $0.18 interim refund
    (date(2026, 8, 6), date(2026, 9, 3), "Sep 26", 35, 0.48771, 77.63),
    (date(2026, 9, 4), date(2026, 10, 5), "Oct 26", 41, 0.50254, 85.99),
]


def schedule_options() -> dict:
    """Undated Louisiana rates, then each bill's gas cost from its service start."""
    return {
        **LA,
        "gcr_rate": 0.5,
        "rate_schedule": [
            {"effective_from": start.isoformat(), **LA, "gcr_rate": gcr}
            for start, _, _, _, gcr, _ in BILLS
        ],
    }


def daily_records(start: date, end: date, month: str, total: float) -> list[dict]:
    days = (end - start).days + 1
    return [
        {"date": (start + timedelta(days=i)).strftime("%m/%d/%Y"), "usage": total / days, "billing_month": month}
        for i in range(days)
    ]


@pytest.fixture
def coordinator(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="atmos_user", data=DAILY, options=schedule_options())
    entry.add_to_hass(hass)
    return AtmosEnergyDataUpdateCoordinator(hass, MagicMock(), entry)


def test_rates_apply_from_their_date_until_the_next():
    schedule = RateSchedule.from_options(schedule_options())
    assert schedule.at(date(2026, 7, 6))["gcr_rate"] == 0.5  # undated set
    assert schedule.at(date(2026, 7, 7))["gcr_rate"] == 0.46844
    assert schedule.at(date(2026, 9, 3))["gcr_rate"] == 0.48771
    assert schedule.at(date(2026, 12, 1))["gcr_rate"] == 0.50254


def test_with_rates_replaces_its_date_and_keeps_later_sets():
    schedule = RateSchedule.from_options(schedule_options())
    corrected = schedule.with_rates(date(2026, 8, 6), {**LA, "gcr_rate": 0.49})
    assert corrected.at(date(2026, 8, 20))["gcr_rate"] == 0.49
    assert corrected.at(date(2026, 9, 20))["gcr_rate"] == 0.50254
    assert len(corrected.dated) == 3


def test_flat_options_from_older_versions_cover_all_history():
    schedule = RateSchedule.from_options({**LA, "gcr_rate": 0.48})
    assert schedule.dated == []
    assert schedule.at(date(2020, 1, 1))["gcr_rate"] == 0.48


@pytest.mark.parametrize("start,end,month,ccf,gcr,charges", BILLS)
async def test_daily_costs_add_up_to_each_bill(hass, coordinator, start, end, month, ccf, gcr, charges):
    records = daily_records(start, end, month, ccf)
    with patch("custom_components.atmos_energy.coordinator.statistics_during_period", return_value={}), \
         patch("custom_components.atmos_energy.coordinator.get_last_statistics", return_value={}), \
         patch("custom_components.atmos_energy.coordinator.async_add_external_statistics") as add:
        await coordinator._async_import_statistics_list(records)

    cost = next(c.args[2] for c in add.call_args_list if c.args[1]["statistic_id"].startswith(f"{DOMAIN}:cost_"))
    assert len(cost) == (end - start).days + 1
    # Bills round each line (Aug: franchise fee $3.0556 printed as $3.05), so allow 2 cents
    assert cost[-1]["sum"] == pytest.approx(charges, abs=0.02)


async def test_saved_rates_recalculate_from_their_date(coordinator):
    coordinator._backfill_done = True
    coordinator.client.get_billing_periods = AsyncMock(
        return_value=["October,2026", "September,2026", "August,2026", "July,2026"]
    )
    coordinator.client.get_daily_history = AsyncMock(return_value=[])
    options = {**coordinator.config_entry.options, "recalculate_from": "2026-08-06"}
    coordinator.hass.config_entries.async_update_entry(coordinator.config_entry, options=options)

    assert coordinator.pending_recalculation() == date(2026, 8, 6)
    await coordinator._async_add_past_periods([])
    # Every period billed from August on, oldest first; July's bill predates the change
    coordinator.client.get_daily_history.assert_awaited_once_with(
        ["August,2026", "September,2026", "October,2026"]
    )


async def test_recalculation_is_marked_done_only_after_import(hass, coordinator):
    options = {**coordinator.config_entry.options, "recalculate_from": "2026-09-04"}
    hass.config_entries.async_update_entry(coordinator.config_entry, options=options)
    coordinator._backfill_done = True
    coordinator.client.get_account_data = AsyncMock(return_value={"usage": 1.0, "history": []})
    coordinator.client.get_billing_periods = AsyncMock(return_value=["October,2026"])
    coordinator.client.get_daily_history = AsyncMock(
        return_value=daily_records(date(2026, 9, 4), date(2026, 10, 5), "Oct 26", 41)
    )
    with patch("custom_components.atmos_energy.coordinator.statistics_during_period", return_value={}), \
         patch("custom_components.atmos_energy.coordinator.get_last_statistics", return_value={}), \
         patch("custom_components.atmos_energy.coordinator.async_add_external_statistics"), \
         patch.object(coordinator, "_async_save_history", AsyncMock()):
        await coordinator._async_update_data()

    assert coordinator.pending_recalculation() is None


async def test_latest_bill_period_start(coordinator):
    for record in daily_records(date(2026, 8, 6), date(2026, 9, 3), "Sep 26", 35) + daily_records(
        date(2026, 9, 4), date(2026, 10, 5), "Oct 26", 41
    ):
        coordinator._history[record["date"]] = record
    assert coordinator.latest_bill_period_start() == date(2026, 9, 4)


async def test_options_date_step_defaults_to_latest_bill_and_keeps_pending_earliest(hass, flow_client, no_setup):
    flow_client.holder["accounts"] = []
    entry = MockConfigEntry(domain=DOMAIN, unique_id="atmos_user", data=DAILY, options=schedule_options())
    entry.add_to_hass(hass)
    coordinator = MagicMock()
    coordinator.latest_bill_period_start.return_value = date(2026, 9, 4)
    coordinator.pending_recalculation.return_value = date(2026, 8, 6)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], DAILY)
    assert result["step_id"] == "rates_date"
    (field,) = result["data_schema"].schema
    assert field.default() == "2026-09-04"

    result = await hass.config_entries.options.async_configure(result["flow_id"], {"effective_from": "2026-09-04"})
    defaults = {key.schema: key.default() for key in result["data_schema"].schema}
    assert defaults["gcr_rate"] == 0.50254  # what was already in effect on 9/4

    result = await hass.config_entries.options.async_configure(result["flow_id"], {**defaults, "gcr_rate": 0.51})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert RateSchedule.from_options(entry.options).at(date(2026, 9, 10))["gcr_rate"] == 0.51
    assert entry.options["recalculate_from"] == "2026-08-06"
