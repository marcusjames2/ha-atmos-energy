"""Rates in Daily mode, for regions outside Mid-Tex."""
from unittest.mock import MagicMock

import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.atmos_energy.const import DOMAIN
from custom_components.atmos_energy.coordinator import AtmosEnergyDataUpdateCoordinator

# Atmos Louisiana residential bill, 9/4/26-10/5/26: 41 CCF, $85.99
LOUISIANA = {
    "fixed_cost": 31.25,
    "usage_rate": 0.72769 + 0.01656 + 0.00762,  # consumption + two Energy Efficiency riders
    "gcr_rate": 0.50254,
    "uri_surcharge": 0.0,
    "tax_percent": 4.0,  # franchise fee
}
DAILY = {"username": "atmos_user", "password": "pw", "operation_mode": "daily"}


async def test_louisiana_rates_reproduce_the_bill(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=DAILY, options=LOUISIANA)
    entry.add_to_hass(hass)
    coordinator = AtmosEnergyDataUpdateCoordinator(hass, MagicMock(), entry)

    assert coordinator.calculate_total_cost(41.0)["total"] == 85.99


async def test_setup_in_daily_mode_asks_for_rates(hass, flow_client, no_setup):
    flow_client.holder["accounts"] = []
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], DAILY)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "cost"
    # GCR auto-fetch reads Mid-Tex filings, so it isn't offered outside Daily Advanced
    assert "auto_fetch_gcr" not in result["data_schema"].schema

    result = await hass.config_entries.flow.async_configure(result["flow_id"], LOUISIANA)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"] == LOUISIANA


async def test_options_in_daily_mode_edit_rates(hass, flow_client, no_setup):
    flow_client.holder["accounts"] = []
    entry = MockConfigEntry(domain=DOMAIN, unique_id="atmos_user", data=DAILY, options={"gcr_rate": 0.48771})
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], DAILY)
    assert result["step_id"] == "cost"
    defaults = {key.schema: key.default() for key in result["data_schema"].schema}
    assert defaults["gcr_rate"] == 0.48771

    result = await hass.config_entries.options.async_configure(result["flow_id"], LOUISIANA)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == LOUISIANA


async def test_options_in_monthly_mode_keep_stored_rates(hass, flow_client, no_setup):
    flow_client.holder["accounts"] = []
    monthly = {**DAILY, "operation_mode": "monthly"}
    entry = MockConfigEntry(domain=DOMAIN, unique_id="atmos_user", data=monthly, options=LOUISIANA)
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], monthly)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == LOUISIANA
