"""Config flow for Atmos Energy integration."""
from datetime import date
import logging
from typing import Any
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.const import CONF_USERNAME, CONF_PASSWORD
from homeassistant.helpers import selector

from .const import (
    DOMAIN, 
    CONF_FIXED_COST, 
    CONF_USAGE_RATE, 
    CONF_TAX_PERCENT, 
    CONF_WEATHER_ENTITY,
    CONF_OPERATION_MODE,
    CONF_WEATHER_STATION,
    CONF_GCR_RATE,
    CONF_URI_SURCHARGE,
    CONF_AUTO_FETCH_GCR,
    CONF_ACCOUNT_NUMBER,
    CONF_SERVICE_ADDRESS,
    CONF_RATE_SCHEDULE,
    CONF_RECALCULATE_FROM,
    MODE_MONTHLY,
    MODE_DAILY,
    MODE_DAILY_ADVANCED
)
from .api import AtmosEnergyApiClient
from .rates import RATE_DEFAULTS, RateSchedule
from .exceptions import AuthenticationError, APIError
from .wna.ndd_data import WEATHER_STATION_CONFIG

_LOGGER = logging.getLogger(__name__)


def _account_selector(accounts: list[dict[str, str]]) -> selector.SelectSelector:
    """Dropdown of accounts, labelled by service address."""
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[
                selector.SelectOptionDict(
                    value=a["account_number"],
                    label=f"{a['address']} ({a['account_number']})" if a["address"] else a["account_number"],
                )
                for a in accounts
            ],
            mode=selector.SelectSelectorMode.LIST,
        )
    )


def _cost_schema(options: dict[str, Any], advanced: bool) -> vol.Schema:
    """Rate fields, pre-filled from `options`.

    Defaults are Mid-Tex rates. GCR auto-fetch reads Mid-Tex filings, so it's
    offered only in Daily Advanced (Mid-Tex) mode; elsewhere the entered gas
    cost rate is used.
    """
    rate = vol.All(vol.Coerce(float), vol.Range(min=0))
    fields = {
        vol.Required(CONF_FIXED_COST, default=options.get(CONF_FIXED_COST, 24.40)): rate,
        vol.Required(CONF_USAGE_RATE, default=options.get(CONF_USAGE_RATE, 1.03839)): rate,
    }
    if advanced:
        fields[vol.Required(CONF_AUTO_FETCH_GCR, default=options.get(CONF_AUTO_FETCH_GCR, True))] = bool
    fields.update({
        vol.Required(CONF_GCR_RATE, default=options.get(CONF_GCR_RATE, 1.16939)): rate,
        vol.Required(CONF_URI_SURCHARGE, default=options.get(CONF_URI_SURCHARGE, 0.01843)): rate,
        vol.Required(CONF_TAX_PERCENT, default=options.get(CONF_TAX_PERCENT, 8.08)): vol.All(
            vol.Coerce(float), vol.Range(min=0, max=100)
        ),
    })
    return vol.Schema(fields)


def _describe_schedule(schedule: RateSchedule) -> str:
    """One line per dated set of rates, for the date step's description."""
    lines = [
        f"- from {d.isoformat()}: gas cost {r.get(CONF_GCR_RATE, '?')}/CCF"
        for d, r in schedule.dated
    ]
    return "\n".join(lines) or "- none yet (your original rates apply to all history)"


def _configured_account_numbers(hass, exclude_entry_id: str | None = None) -> set[str]:
    """Account numbers already used by other Atmos Energy entries."""
    return {
        entry.data[CONF_ACCOUNT_NUMBER]
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.data.get(CONF_ACCOUNT_NUMBER) and entry.entry_id != exclude_entry_id
    }


class AtmosEnergyConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Atmos Energy."""

    VERSION = 1

    def __init__(self):
        """Initialize."""
        self._user_data = {}
        self._options = {}
        self._accounts: list[dict[str, str]] = []

    async def async_step_user(self, user_input=None):
        """Handle the initial step (Credentials)."""
        errors = {}

        if user_input is not None:
            username = user_input[CONF_USERNAME]
            password = user_input[CONF_PASSWORD]

            client = AtmosEnergyApiClient(username, password, source="setup")

            try:
                accounts = await client.get_accounts()
                if len(accounts) <= 1:
                    # Single-account login: validate the full session as before
                    await client.login()
            except AuthenticationError:
                errors["base"] = "invalid_auth"
            except (APIError, Exception) as err:
                _LOGGER.exception("Unexpected error during authentication: %s", err)
                errors["base"] = "cannot_connect"
            finally:
                await client.close()

            if not errors:
                self._user_data = user_input
                if len(accounts) > 1:
                    self._accounts = accounts
                    return await self.async_step_account()

                await self.async_set_unique_id(username)
                self._abort_if_unique_id_configured()
                return await self._async_step_after_account()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): str,
                    vol.Required(CONF_OPERATION_MODE, default=MODE_MONTHLY): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[MODE_MONTHLY, MODE_DAILY, MODE_DAILY_ADVANCED],
                            translation_key="operation_mode",
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_account(self, user_input=None):
        """Pick one account (service address) when the login has several."""
        configured = _configured_account_numbers(self.hass)
        available = [a for a in self._accounts if a["account_number"] not in configured]
        if not available:
            return self.async_abort(reason="already_configured")

        if user_input is not None:
            number = user_input[CONF_ACCOUNT_NUMBER]
            await self.async_set_unique_id(number)
            self._abort_if_unique_id_configured()
            address = next((a["address"] for a in available if a["account_number"] == number), "")
            self._user_data[CONF_ACCOUNT_NUMBER] = number
            self._user_data[CONF_SERVICE_ADDRESS] = address
            return await self._async_step_after_account()

        return self.async_show_form(
            step_id="account",
            data_schema=vol.Schema({vol.Required(CONF_ACCOUNT_NUMBER): _account_selector(available)}),
        )

    async def _async_step_after_account(self):
        """Branch based on mode."""
        mode = self._user_data.get(CONF_OPERATION_MODE, MODE_DAILY_ADVANCED)
        if mode == MODE_DAILY_ADVANCED:
            return await self.async_step_weather_station()
        if mode == MODE_DAILY:
            # Daily costs are computed from these rates, so ask for them in every region
            return await self.async_step_cost()

        return self.async_create_entry(
            title=self._entry_title(),
            data=self._user_data,
            options={}
        )

    def _entry_title(self) -> str:
        """Username, plus the street address for multi-account logins."""
        address = self._user_data.get(CONF_SERVICE_ADDRESS)
        if address:
            return f"{self._user_data[CONF_USERNAME]} ({address.split(',')[0]})"
        return self._user_data[CONF_USERNAME]

    async def async_step_weather_station(self, user_input=None):
        """Step 2: Select weather station."""
        if user_input is not None:
            self._options.update(user_input)
            return await self.async_step_cost()

        stations = {id: info["name"] for id, info in WEATHER_STATION_CONFIG.items()}
        return self.async_show_form(
            step_id="weather_station",
            data_schema=vol.Schema({
                vol.Required(CONF_WEATHER_STATION, default="austin"): vol.In(stations),
            }),
        )

    async def async_step_cost(self, user_input=None):
        """Step 3: Handle choosing cost parameters."""
        advanced = self._user_data.get(CONF_OPERATION_MODE) == MODE_DAILY_ADVANCED
        if user_input is not None:
            self._options.update(user_input)
            if advanced:
                return await self.async_step_weather_forecast()
            return self.async_create_entry(
                title=self._entry_title(),
                data=self._user_data,
                options=self._options
            )

        return self.async_show_form(
            step_id="cost",
            data_schema=_cost_schema({}, advanced),
        )

    async def async_step_weather_forecast(self, user_input=None):
        """Step 4: Select weather entity."""
        if user_input is not None:
            self._options.update(user_input)
            return self.async_create_entry(
                title=self._entry_title(),
                data=self._user_data,
                options=self._options
            )

        return self.async_show_form(
            step_id="weather_forecast",
            data_schema=vol.Schema({
                vol.Optional(CONF_WEATHER_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="weather")
                ),
            }),
        )

    async def async_step_reauth(self, user_input=None):
        """Handle reauth flow."""
        errors = {}
        if user_input is not None:
            username = self._get_reauth_entry().data[CONF_USERNAME]
            password = user_input[CONF_PASSWORD]
            client = AtmosEnergyApiClient(username, password, source="reauth")
            try:
                # Credentials check only; account selection is not a reauth concern
                await client.get_accounts()
                self.hass.config_entries.async_update_entry(
                    self._get_reauth_entry(),
                    data={**self._get_reauth_entry().data, CONF_PASSWORD: password}
                )
                await self.hass.config_entries.async_reload(self._get_reauth_entry().entry_id)
                return self.async_abort(reason="reauth_successful")
            except AuthenticationError:
                errors["base"] = "invalid_auth"
            except Exception:
                errors["base"] = "unknown"
            finally:
                await client.close()
        
        return self.async_show_form(
            step_id="reauth",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            errors=errors,
            description_placeholders={"username": self._get_reauth_entry().data[CONF_USERNAME]},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return AtmosEnergyOptionsFlowHandler(config_entry)


class AtmosEnergyOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options (reconfiguration)."""

    def __init__(self, config_entry):
        """Initialize options flow."""
        self._config_entry = config_entry
        self._options = dict(config_entry.options)
        self._data = dict(config_entry.data)
        self._accounts: list[dict[str, str]] = []
        self._effective_from: date | None = None

    async def async_step_init(self, user_input=None):
        """Step 1: Credentials and Toggle."""
        errors = {}
        if user_input is not None:
            # Update local state
            self._data.update({
                CONF_USERNAME: user_input[CONF_USERNAME],
                CONF_PASSWORD: user_input[CONF_PASSWORD],
                CONF_OPERATION_MODE: user_input[CONF_OPERATION_MODE],
            })

            # Validate credentials
            client = AtmosEnergyApiClient(self._data[CONF_USERNAME], self._data[CONF_PASSWORD], source="options")
            accounts = []
            try:
                accounts = await client.get_accounts()
                if len(accounts) <= 1:
                    await client.login()
            except AuthenticationError:
                errors["base"] = "invalid_auth"
            except Exception:
                errors["base"] = "cannot_connect"
            finally:
                await client.close()

            current = self._data.get(CONF_ACCOUNT_NUMBER)
            if not errors and current and accounts and current not in [a["account_number"] for a in accounts]:
                errors["base"] = "account_not_found"

            if not errors:
                # Sync data back to the config entry if credentials changed
                self.hass.config_entries.async_update_entry(self._config_entry, data=self._data)

                # Entries made before multi-account support have no account; let them pick one.
                # An entry that already has one keeps it: switching would mix two meters'
                # history and statistics under one entry.
                if len(accounts) > 1 and not current:
                    self._accounts = accounts
                    return await self.async_step_account()

                return await self._async_step_after_account()

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required(CONF_USERNAME, default=self._data.get(CONF_USERNAME)): str,
                vol.Required(CONF_PASSWORD, default=self._data.get(CONF_PASSWORD)): str,
                vol.Required(
                    CONF_OPERATION_MODE, 
                    default=self._data.get(CONF_OPERATION_MODE, MODE_MONTHLY)
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[MODE_MONTHLY, MODE_DAILY, MODE_DAILY_ADVANCED],
                        translation_key="operation_mode",
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
            }),
            errors=errors,
        )

    async def async_step_account(self, user_input=None):
        """Pick the account for an entry created before multi-account support."""
        configured = _configured_account_numbers(self.hass, exclude_entry_id=self._config_entry.entry_id)
        available = [a for a in self._accounts if a["account_number"] not in configured]
        if not available:
            return self.async_abort(reason="already_configured")

        if user_input is not None:
            number = user_input[CONF_ACCOUNT_NUMBER]
            self._data[CONF_ACCOUNT_NUMBER] = number
            self._data[CONF_SERVICE_ADDRESS] = next(
                (a["address"] for a in available if a["account_number"] == number), ""
            )
            self.hass.config_entries.async_update_entry(self._config_entry, data=self._data)
            return await self._async_step_after_account()

        return self.async_show_form(
            step_id="account",
            data_schema=vol.Schema({vol.Required(CONF_ACCOUNT_NUMBER): _account_selector(available)}),
        )

    async def _async_step_after_account(self):
        """Branch based on mode."""
        if self._data[CONF_OPERATION_MODE] == MODE_DAILY_ADVANCED:
            return await self.async_step_weather_station()
        if self._data[CONF_OPERATION_MODE] == MODE_DAILY:
            return await self.async_step_rates_date()

        # Keep the stored options; saving {} here would drop the rates
        return self.async_create_entry(title="", data=self._options)

    async def async_step_weather_station(self, user_input=None):
        """Step 2: Region."""
        if user_input is not None:
            self._options.update(user_input)
            return await self.async_step_rates_date()

        stations = {id: info["name"] for id, info in WEATHER_STATION_CONFIG.items()}
        return self.async_show_form(
            step_id="weather_station",
            data_schema=vol.Schema({
                vol.Required(
                    CONF_WEATHER_STATION,  
                    default=self._options.get(CONF_WEATHER_STATION, "austin")
                ): vol.In(stations),
            }),
        )

    def _coordinator(self):
        return self.hass.data.get(DOMAIN, {}).get(self._config_entry.entry_id)

    async def async_step_rates_date(self, user_input=None):
        """Step 3: The date the rates take effect."""
        if user_input is not None:
            self._effective_from = date.fromisoformat(user_input["effective_from"])
            return await self.async_step_cost()

        coordinator = self._coordinator()
        default = (coordinator.latest_bill_period_start() if coordinator else None) or date.today()
        return self.async_show_form(
            step_id="rates_date",
            data_schema=vol.Schema({
                vol.Required("effective_from", default=default.isoformat()): selector.DateSelector(),
            }),
            description_placeholders={"rates": _describe_schedule(RateSchedule.from_options(self._options))},
        )

    async def async_step_cost(self, user_input=None):
        """Step 4: Rates, from the effective date until the next later set."""
        advanced = self._data[CONF_OPERATION_MODE] == MODE_DAILY_ADVANCED
        schedule = RateSchedule.from_options(self._options)
        if user_input is not None:
            rates = {k: user_input.pop(k) for k in RATE_DEFAULTS if k in user_input}
            self._options.update(user_input)  # e.g. auto_fetch_gcr
            self._options[CONF_RATE_SCHEDULE] = schedule.with_rates(self._effective_from, rates).as_option()
            recalculate_from = self._effective_from
            coordinator = self._coordinator()
            pending = coordinator.pending_recalculation() if coordinator else None
            if pending:
                recalculate_from = min(recalculate_from, pending)
            self._options[CONF_RECALCULATE_FROM] = recalculate_from.isoformat()
            if advanced:
                return await self.async_step_weather_forecast()
            return self.async_create_entry(title="", data=self._options)

        current = {**self._options, **schedule.at(self._effective_from)}
        return self.async_show_form(
            step_id="cost",
            data_schema=_cost_schema(current, advanced),
            description_placeholders={"effective_from": self._effective_from.isoformat()},
        )

    async def async_step_weather_forecast(self, user_input=None):
        """Step 4: Weather Entity."""
        if user_input is not None:
            self._options.update(user_input)
            # Finish and save all options
            return self.async_create_entry(title="", data=self._options)

        return self.async_show_form(
            step_id="weather_forecast",
            data_schema=vol.Schema({
                vol.Optional(
                    CONF_WEATHER_ENTITY,
                    description={"suggested_value": self._options.get(CONF_WEATHER_ENTITY)}
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="weather")
                ),
            }),
        )
