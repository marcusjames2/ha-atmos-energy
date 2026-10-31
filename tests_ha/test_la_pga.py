"""Atmos Louisiana's published gas cost (PGA) table."""
from datetime import date, datetime, timedelta
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.atmos_energy import la_pga
from custom_components.atmos_energy.const import DOMAIN
from custom_components.atmos_energy.coordinator import AtmosEnergyDataUpdateCoordinator
from custom_components.atmos_energy.la_pga import LouisianaPGAFetcher, find_pga_document, parse_pga_text

DATA = Path(__file__).parent / "data"
PDF = (DATA / "Louisiana PGA Rates - October 2026.pdf").read_bytes()
LA_BASE = {"fixed_cost": 31.25, "usage_rate": 0.75187, "gcr_rate": 0.40, "uri_surcharge": 0.0, "tax_percent": 4.0}
DAILY = {"username": "atmos_user", "password": "pw", "operation_mode": "daily"}


def test_finds_the_pga_document_on_the_tariffs_page():
    page = json.loads((DATA / "tariffs-louisiana-page-data.json").read_text())
    assert find_pga_document(page) == "/document/louisiana-pga-rates-october-2026"


def test_reads_25_months_from_the_published_pdf():
    rates = la_pga._pdf_rates(PDF)
    assert len(rates) == 25
    # The three months on the customer's bills
    assert rates["Oct 26"] == 0.50254
    assert rates["Sep 26"] == 0.48771
    assert rates["Aug 26"] == 0.46844
    assert rates["Oct 24"] == 0.50227


def test_parse_ignores_issue_and_effective_dates():
    text = "Issued: 09/29/2026 Effective: 10/01/2026\nOct-26 $0.50254\nSep-26 $0.48771"
    assert parse_pga_text(text) == {"Oct 26": 0.50254, "Sep 26": 0.48771}


async def test_rate_for_unbilled_period_is_the_newest(hass):
    fetcher = LouisianaPGAFetcher(hass)
    fetcher.rates = {"Sep 26": 0.48771, "Oct 26": 0.50254, "Dec 25": 0.64161}
    assert fetcher.latest_month == "Oct 26"
    assert fetcher.rate_for(None) == 0.50254
    assert fetcher.rate_for("Dec 25") == 0.64161
    assert fetcher.rate_for("Jan 20") is None


async def test_refresh_keeps_older_months_and_waits_a_day(hass):
    fetcher = LouisianaPGAFetcher(hass)
    fetcher.rates = {"Sep 24": 0.49}
    with patch.object(fetcher, "_fetch", AsyncMock(return_value={"Oct 26": 0.50254})) as fetch:
        await fetcher.async_refresh()
        await fetcher.async_refresh()
    fetch.assert_awaited_once()
    assert fetcher.rates == {"Sep 24": 0.49, "Oct 26": 0.50254}


async def test_refresh_failure_keeps_cached_rates(hass):
    fetcher = LouisianaPGAFetcher(hass)
    fetcher.rates = {"Oct 26": 0.50254}
    fetcher._last_fetch = datetime.now() - timedelta(days=2)
    with patch.object(fetcher, "_fetch", AsyncMock(side_effect=ValueError("no PDF"))):
        await fetcher.async_refresh()
    assert fetcher.rates == {"Oct 26": 0.50254}


BILLS = [
    (date(2026, 7, 7), date(2026, 8, 5), "Aug 26", 37, 79.44),
    (date(2026, 8, 6), date(2026, 9, 3), "Sep 26", 35, 77.63),
    (date(2026, 9, 4), date(2026, 10, 5), "Oct 26", 41, 85.99),
]


@pytest.mark.parametrize("start,end,month,ccf,charges", BILLS)
async def test_bills_match_with_only_the_fixed_rates_entered(hass, start, end, month, ccf, charges):
    """With PGA on, the entered gas cost (0.40 here) is never used for these months."""
    entry = MockConfigEntry(domain=DOMAIN, data=DAILY, options={**LA_BASE, "auto_fetch_la_pga": True})
    entry.add_to_hass(hass)
    coordinator = AtmosEnergyDataUpdateCoordinator(hass, MagicMock(), entry)
    coordinator.la_pga.rates = la_pga._pdf_rates(PDF)

    days = (end - start).days + 1
    records = [
        {"date": (start + timedelta(days=i)).strftime("%m/%d/%Y"), "usage": ccf / days, "billing_month": month}
        for i in range(days)
    ]
    with patch("custom_components.atmos_energy.coordinator.statistics_during_period", return_value={}), \
         patch("custom_components.atmos_energy.coordinator.get_last_statistics", return_value={}), \
         patch("custom_components.atmos_energy.coordinator.async_add_external_statistics") as add:
        await coordinator._async_import_statistics_list(records)

    cost = next(c.args[2] for c in add.call_args_list if c.args[1]["statistic_id"].startswith(f"{DOMAIN}:cost_"))
    assert cost[-1]["sum"] == pytest.approx(charges, abs=0.02)


async def test_pga_off_uses_entered_rate(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=DAILY, options=LA_BASE)
    entry.add_to_hass(hass)
    coordinator = AtmosEnergyDataUpdateCoordinator(hass, MagicMock(), entry)
    coordinator.la_pga.rates = {"Oct 26": 0.50254}
    breakdown = coordinator.calculate_total_cost(10.0, include_fixed=False, billing_month="Oct 26")["breakdown"]
    assert breakdown["rates"]["gcr"] == 0.4


async def test_turning_pga_on_recalculates_all_history(hass, flow_client, no_setup):
    flow_client.holder["accounts"] = []
    entry = MockConfigEntry(domain=DOMAIN, unique_id="atmos_user", data=DAILY, options=LA_BASE)
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], DAILY)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"effective_from": "2026-09-04"})
    defaults = {key.schema: key.default() for key in result["data_schema"].schema}
    assert defaults["auto_fetch_la_pga"] is False

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**defaults, "auto_fetch_la_pga": True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["auto_fetch_la_pga"] is True
    assert entry.options["recalculate_from"] == "2000-01-01"


async def test_fetch_retries_a_reset_connection(hass):
    """atmosenergy.com sometimes resets mid-body; the next try usually works."""
    import aiohttp

    page = (DATA / "tariffs-louisiana-page-data.json").read_bytes()
    doc_page = json.dumps({"result": {"pageContext": {"redirect": "/static/abc/Louisiana PGA Rates - October 2026.pdf"}}}).encode()
    bodies = {"tariffs-louisiana": [aiohttp.ClientPayloadError("reset"), page], "pga-rates": [doc_page], ".pdf": [PDF]}
    requested = []

    class Response:
        def __init__(self, body): self.body = body
        async def __aenter__(self):
            if isinstance(self.body, Exception):
                raise self.body
            return self
        async def __aexit__(self, *exc): return False
        def raise_for_status(self): pass
        async def read(self): return self.body

    class Session:
        def get(self, url, **kwargs):
            requested.append(url)
            key = next(k for k in bodies if k in url)
            return Response(bodies[key].pop(0))

    with patch.object(la_pga, "async_get_clientsession", lambda h: Session()), \
         patch.object(la_pga.asyncio, "sleep", AsyncMock()):
        rates = await LouisianaPGAFetcher(hass)._fetch()

    assert rates["Oct 26"] == 0.50254
    assert sum("tariffs-louisiana" in u for u in requested) == 2
    assert requested[-1].endswith("Louisiana%20PGA%20Rates%20-%20October%202026.pdf")
