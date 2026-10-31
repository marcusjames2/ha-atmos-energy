"""Atmos Energy Louisiana gas cost (Purchased Gas Adjustment) rates by bill month.

Atmos publishes a "Louisiana PGA Rates - <Month> <Year>" PDF each month, listed
on the Louisiana tariffs page, with the residential (RS) gas cost per CCF for the
last 25 months. A bill charges the rate for its billing month: the October 2026
bill (9/4-10/5 service) used "Oct-26".
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from io import BytesIO
import json
import logging
import re
from typing import Any
from urllib.parse import quote

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://www.atmosenergy.com"
TARIFFS_PAGE_DATA = f"{BASE_URL}/page-data/utility-operationsrates/tariffs-louisiana/page-data.json"
FETCH_INTERVAL = timedelta(days=1)
RETRIES = 3
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json,application/pdf,*/*",
}

# "Oct-26 $0.50254"
_RATE_LINE = re.compile(r"\b([A-Z][a-z]{2})-(\d{2})\s+\$?\s*(\d+\.\d{3,})")


def find_pga_document(tariffs_page_data: dict[str, Any]) -> str | None:
    """Path of the current "Louisiana PGA Rates" document, e.g. /document/louisiana-pga-rates-october-2026."""
    for attributes in _media_attributes(tariffs_page_data):
        name = str(attributes.get("data-media-name", ""))
        if name.lower().startswith("louisiana pga rates"):
            return attributes.get("data-media-url")
    return None


def _media_attributes(node: Any):
    """Every document link's attributes in the page data.

    The page body is JSON stored as a string (field_enhanced_body), so strings
    that look like JSON are decoded and searched too.
    """
    if isinstance(node, dict):
        if "data-media-url" in node:
            yield node
        for value in node.values():
            yield from _media_attributes(value)
    elif isinstance(node, list):
        for value in node:
            yield from _media_attributes(value)
    elif isinstance(node, str) and node[:1] in "[{" and "data-media-url" in node:
        try:
            yield from _media_attributes(json.loads(node))
        except ValueError:
            return


def parse_pga_text(text: str) -> dict[str, float]:
    """Rates by billing month as the daily download labels them, e.g. {"Oct 26": 0.50254}."""
    return {f"{mon} {yy}": float(rate) for mon, yy, rate in _RATE_LINE.findall(text)}


class LouisianaPGAFetcher:
    """Keeps every PGA rate seen, refreshed from Atmos at most once a day."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.rates: dict[str, float] = {}
        self._last_fetch: datetime | None = None
        self._store = Store(hass, 1, "atmos_energy_la_pga")

    async def async_load(self) -> None:
        stored = await self._store.async_load()
        if stored:
            self.rates = stored.get("rates", {})
            last = stored.get("last_fetch")
            self._last_fetch = datetime.fromisoformat(last) if last else None

    def rate_for(self, billing_month: str | None) -> float | None:
        """The rate for a billing month like "Oct 26"; the newest one for None (an unbilled period)."""
        if billing_month:
            return self.rates.get(billing_month)
        latest = self.latest_month
        return self.rates[latest] if latest else None

    @property
    def latest_month(self) -> str | None:
        if not self.rates:
            return None
        return max(self.rates, key=lambda m: datetime.strptime(m, "%b %y"))

    async def async_refresh(self) -> None:
        """Fetch the latest table if a day has passed; keeps the cached rates on failure."""
        now = datetime.now()
        if self.rates and self._last_fetch and now - self._last_fetch < FETCH_INTERVAL:
            return
        try:
            fetched = await self._fetch()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
            _LOGGER.warning("Could not fetch Atmos Louisiana PGA rates: %s", err)
            return
        if not fetched:
            _LOGGER.warning("No rates found in the Atmos Louisiana PGA document")
            return
        self.rates.update(fetched)
        self._last_fetch = now
        await self._store.async_save({"rates": self.rates, "last_fetch": now.isoformat()})
        _LOGGER.debug("Louisiana PGA rates through %s: %s", self.latest_month, self.rates.get(self.latest_month))

    async def _fetch(self) -> dict[str, float]:
        session = async_get_clientsession(self.hass)
        timeout = aiohttp.ClientTimeout(total=30)

        async def get(url: str) -> bytes:
            # atmosenergy.com intermittently resets connections mid-body
            for attempt in range(RETRIES):
                try:
                    async with session.get(url, headers=HEADERS, timeout=timeout) as resp:
                        resp.raise_for_status()
                        return await resp.read()
                except (aiohttp.ClientPayloadError, aiohttp.ClientConnectionError, asyncio.TimeoutError):
                    if attempt == RETRIES - 1:
                        raise
                    await asyncio.sleep(2 ** attempt)
            raise AssertionError("unreachable")

        document = find_pga_document(json.loads(await get(TARIFFS_PAGE_DATA)))
        if not document:
            raise ValueError("No Louisiana PGA document listed on the tariffs page")
        page = json.loads(await get(f"{BASE_URL}/page-data{document.rstrip('/')}/page-data.json"))
        pdf_path = page.get("result", {}).get("pageContext", {}).get("redirect")
        if not pdf_path:
            raise ValueError(f"No PDF link for {document}")
        content = await get(f"{BASE_URL}{quote(pdf_path)}")
        if not content.startswith(b"%PDF"):
            raise ValueError(f"{pdf_path} is not a PDF")
        return await self.hass.async_add_executor_job(_pdf_rates, content)


def _pdf_rates(content: bytes) -> dict[str, float]:
    import pdfplumber

    with pdfplumber.open(BytesIO(content)) as pdf:
        return parse_pga_text("\n".join(page.extract_text() or "" for page in pdf.pages))
