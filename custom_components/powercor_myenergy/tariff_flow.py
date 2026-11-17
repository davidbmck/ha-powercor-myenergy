"""Preset-led tariff options; persist dated prices in the HA config entry."""

from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
import re
from zoneinfo import ZoneInfo

import voluptuous as vol
from homeassistant.config_entries import OptionsFlow
from homeassistant.helpers import selector

from .tariff import ALL_DAYS, CLOCKS, DAYS, MODELS, prepare_plan, preset, price, validate_plan

TEXT = selector.TextSelector()
BOOL = selector.BooleanSelector()
DATE = selector.DateSelector()
TIME = selector.TimeSelector()


def select(values, key):
    return selector.SelectSelector(selector.SelectSelectorConfig(options=values, translation_key=key))


class TariffOptionsFlow(OptionsFlow):
    def __init__(self):
        self._settings = None
        self._plan = None
        self._edit = None
        self._customise = False
        self._extras = False

    async def async_step_init(self, user_input=None):
        self._settings = deepcopy(self.config_entry.options.get("tariff", {}))
        if self._settings and self._settings["currency"] != self.hass.config.currency:
            return self.async_abort(reason="currency_changed")
        old = (self._settings.get("plans") or [{}])[-1]
        tomorrow = (datetime.now(ZoneInfo(old.get("timezone", "Australia/Melbourne"))).date() + timedelta(days=1)).isoformat()
        default_day = max(tomorrow, (date.fromisoformat(old["effective_date"]) + timedelta(days=1)).isoformat()) if old.get("effective_date") else tomorrow
        errors = {}
        if user_input is not None:
            if user_input.get("operation") == "edit":
                return await self.async_step_edit_plan()
            try:
                for key, value in user_input.items():
                    if key.endswith("price") and value != "":
                        price(value)
                price(user_input["import_price"])
                exported = user_input.get("export_price") or None
                if exported is not None:
                    price(exported)
                if old.get("export_price") is not None and exported is None:
                    errors["export_price"] = "cannot_disable"
                effective = user_input.get("effective_date")
                if old.get("effective_date") and effective <= old["effective_date"]:
                    errors["effective_date"] = "date_order"
                if not errors:
                    self._plan = {**deepcopy(old), "import_price": user_input["import_price"],
                                  "export_price": exported, "effective_date": effective,
                                  "timezone": old.get("timezone", "Australia/Melbourne"),
                                  "model": old.get("model", "flat"), "bands": deepcopy(old.get("bands", [])),
                                  "export_model": "block" if old.get("export_model") == "block" else "flat" if exported is not None else "none"}
                    for band in self._plan["bands"]:
                        key = band["name"] + "_price"
                        if key in user_input:
                            band["price"] = user_input[key]
                    for direction in ("import", "export"):
                        if block := self._plan.get(direction + "_block"):
                            block["after_price"] = user_input[direction + "_after_price"]
                    if "controlled_load_price" in user_input:
                        self._plan["controlled_load_price"] = user_input["controlled_load_price"]
                    if user_input["advanced"]:
                        return await self.async_step_add_plan()
                    return self._save()
            except ValueError:
                errors["base"] = "invalid_price"
        values = user_input or old
        schema = {
            vol.Required("import_price", default=values.get("import_price", "")): TEXT,
            vol.Optional("export_price", default=values.get("export_price") or ""): TEXT,
            vol.Required("advanced", default=values.get("advanced", False)): BOOL,
        }
        for band in old.get("bands", []):
            if band["name"] != "free":
                key = band["name"] + "_price"
                schema[vol.Required(key, default=values.get(key, band["price"]))] = TEXT
        for direction in ("import", "export"):
            if block := old.get(direction + "_block"):
                key = direction + "_after_price"
                schema[vol.Required(key, default=values.get(key, block["after_price"]))] = TEXT
        if old.get("controlled_load_price") is not None:
            schema[vol.Required("controlled_load_price", default=values.get("controlled_load_price", old["controlled_load_price"]))] = TEXT
        if self._settings:
            schema[vol.Required("effective_date", default=values.get("effective_date") if user_input else default_day)] = DATE
            schema[vol.Required("operation", default="new")] = select(["new", "edit"], "operation")
        return self.async_show_form(step_id="init", data_schema=vol.Schema(schema), errors=errors,
                                    description_placeholders={"currency": self.hass.config.currency})

    async def async_step_edit_plan(self, user_input=None):
        if user_input is not None:
            self._edit = int(user_input["plan"])
            self._plan = deepcopy(self._settings["plans"][self._edit])
            return await self.async_step_add_plan()
        choices = [{"value": str(index), "label": (plan["effective_date"] or "Initial historical prices")}
                   for index, plan in enumerate(self._settings["plans"])]
        return self.async_show_form(step_id="edit_plan", data_schema=vol.Schema({
            vol.Required("plan"): selector.SelectSelector(selector.SelectSelectorConfig(options=choices)),
        }))

    async def async_step_add_plan(self, user_input=None):
        old = self._plan or (self._settings.get("plans") or [{}])[-1]
        errors = {}
        if user_input is not None:
            model = user_input["model"]
            self._plan = {**deepcopy(old), "model": model, "export_model": user_input["export_model"],
                          "timezone": CLOCKS[user_input["timezone"]], "effective_date": old.get("effective_date")}
            if model != old.get("model"):
                self._plan["bands"] = preset(model)
                self._plan.pop("import_block", None)
            self._customise = user_input["customise_times"]
            self._extras = user_input["extras"]
            if (old.get("export_model", "none") != "none" and user_input["export_model"] == "none"):
                errors["export_model"] = "cannot_disable"
            if not errors:
                return await self.async_step_rates()
        schema = {
            vol.Required("model", default=old.get("model", "flat")): select(list(MODELS), "model"),
            vol.Required("export_model", default=old.get("export_model", "none")): select(["none", "flat", "block"], "export_model"),
            vol.Required("timezone", default="aest" if old.get("timezone") == "Etc/GMT-10" else "victorian"): select(list(CLOCKS), "timezone"),
            vol.Required("customise_times", default=False): BOOL,
            vol.Required("extras", default=bool(old.get("controlled_load_price") is not None or any(band["name"] == "extra_overnight" for band in old.get("bands", [])))): BOOL,
        }
        return self.async_show_form(step_id="add_plan", data_schema=vol.Schema(schema), errors=errors)

    async def async_step_rates(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                for key, value in user_input.items():
                    if key.endswith("price") or key.endswith("kwh"):
                        price(value)
                for key in ("import_price", "export_price"):
                    if key in user_input:
                        self._plan[key] = user_input[key]
                for band in self._plan["bands"]:
                    if band["name"] + "_price" in user_input:
                        band["price"] = user_input[band["name"] + "_price"]
                for direction in ("import", "export"):
                    block_key = direction + "_block"
                    if (direction == "import" and self._plan["model"] == "block" or
                            direction == "export" and self._plan["export_model"] == "block"):
                        self._plan[block_key] = {"kwh": user_input[direction + "_block_kwh"],
                                                "after_price": user_input[direction + "_after_price"],
                                                "reset": user_input[direction + "_reset"],
                                                "anchor": user_input.get(direction + "_anchor", "2026-01-01")}
                for key in ("import_block", "export_block"):
                    if self._plan.get(key) and price(self._plan[key]["kwh"]) <= 0:
                        raise ValueError("invalid_block")
                if self._plan["export_model"] == "none":
                    self._plan["export_price"] = None
                if self._customise and self._plan["bands"]:
                    return await self.async_step_times()
                return await self._async_after_times()
            except ValueError:
                errors["base"] = "invalid_price"
        schema = {}
        for key in (["import_price"] if self._edit is not None else []) + (["export_price"] if self._plan["export_model"] != "none" and (self._edit is not None or self._plan.get("export_price") is None) else []):
            schema[vol.Required(key, default=self._plan.get(key) or "")] = TEXT
        for band in self._plan["bands"]:
            if band["name"] in ("free", "overnight", "extra_overnight"):
                continue
            key = band["name"] + "_price"
            schema[vol.Required(key, default=band.get("price", ""))] = TEXT
        if self._plan["model"] == "overnight":
            overnight = next(band for band in self._plan["bands"] if band["name"] == "overnight")
            schema[vol.Required("overnight_price", default=overnight.get("price", ""))] = TEXT
        for direction in ("import", "export"):
            if ((direction == "import" and self._plan["model"] == "block") or
                    (direction == "export" and self._plan["export_model"] == "block")):
                block = self._plan.get(direction + "_block", {})
                schema[vol.Required(direction + "_block_kwh", default=block.get("kwh", ""))] = TEXT
                schema[vol.Required(direction + "_after_price", default=block.get("after_price", ""))] = TEXT
                schema[vol.Required(direction + "_reset", default=block.get("reset", "daily"))] = select(["daily", "monthly", "quarterly"], "reset")
                schema[vol.Required(direction + "_anchor", default=block.get("anchor", "2026-01-01"))] = DATE
        if not schema and user_input is None:
            if self._customise and self._plan["bands"]:
                return await self.async_step_times()
            return await self._async_after_times()
        return self.async_show_form(step_id="rates", data_schema=vol.Schema(schema), errors=errors,
                                    description_placeholders={"currency": self.hass.config.currency})

    async def async_step_times(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                for band in self._plan["bands"]:
                    if band["name"] == "extra_overnight":
                        continue
                    name = band["name"]
                    band.update(start=user_input[name + "_start"], end=user_input[name + "_end"],
                                days=[DAYS.index(day) for day in user_input[name + "_days"]])
                validate_plan(self._plan)
                return await self._async_after_times()
            except ValueError:
                errors["base"] = "invalid_time"
        schema = {}
        for band in self._plan["bands"]:
            if band["name"] == "extra_overnight":
                continue
            name = band["name"]
            schema[vol.Required(name + "_start", default=band["start"])] = TIME
            schema[vol.Required(name + "_end", default=band["end"])] = TIME
            schema[vol.Required(name + "_days", default=[DAYS[day] for day in band["days"]])] = selector.SelectSelector(
                selector.SelectSelectorConfig(options=DAYS, multiple=True, translation_key="days"))
        return self.async_show_form(step_id="times", data_schema=vol.Schema(schema), errors=errors)

    async def _async_after_times(self):
        if self._extras:
            return await self.async_step_extras()
        previous = (self._settings.get("plans") or [{}])[self._edit if self._edit is not None else -1]
        if previous.get("controlled_load_price") is not None:
            self._extras = True
            return await self.async_step_extras()
        self._plan.pop("controlled_load_price", None)
        self._plan.pop("controlled_load_register", None)
        self._plan["bands"] = [band for band in self._plan["bands"] if band["name"] != "extra_overnight"]
        return await self._async_finish()

    async def async_step_extras(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                controlled = user_input.get("controlled_load_price") or None
                previous = (self._settings.get("plans") or [{}])[self._edit if self._edit is not None else -1]
                if previous.get("controlled_load_price") is not None and controlled is None:
                    raise ValueError("Keep previously configured controlled-load pricing")
                self._plan["controlled_load_price"] = controlled
                register = user_input.get("controlled_load_register", "").strip().upper()
                if controlled is not None and not re.fullmatch(r"E[2-9]", register):
                    raise ValueError("Confirm the separately identified controlled-load register")
                self._plan["controlled_load_register"] = register if controlled is not None else None
                self._plan["bands"] = [band for band in self._plan["bands"] if band["name"] != "extra_overnight"]
                extra_price = user_input.get("extra_overnight_price") or None
                if extra_price is not None:
                    if self._plan["model"] == "block":
                        raise ValueError("Block import prices cannot also have time bands")
                    self._plan["bands"].insert(0, {"name": "extra_overnight", "price": extra_price,
                                                  "start": user_input["extra_overnight_start"],
                                                  "end": user_input["extra_overnight_end"], "days": ALL_DAYS})
                validate_plan(self._plan)
                return await self._async_finish()
            except ValueError:
                errors["base"] = "invalid_extras"
        extra = next((band for band in self._plan["bands"] if band["name"] == "extra_overnight"), {})
        return self.async_show_form(step_id="extras", data_schema=vol.Schema({
            vol.Optional("controlled_load_price", default=self._plan.get("controlled_load_price") or ""): TEXT,
            vol.Optional("controlled_load_register", default=self._plan.get("controlled_load_register") or ""): TEXT,
            vol.Optional("extra_overnight_price", default=extra.get("price", "")): TEXT,
            vol.Required("extra_overnight_start", default=extra.get("start", "00:00:00")): TIME,
            vol.Required("extra_overnight_end", default=extra.get("end", "06:00:00")): TIME,
        }), errors=errors, description_placeholders={"currency": self.hass.config.currency})

    async def _async_finish(self):
        if self._edit is not None:
            self._plan = prepare_plan(self._plan)
            return await self.async_step_confirm()
        return self._save()

    async def async_step_confirm(self, user_input=None):
        if user_input is not None:
            return self._save()
        return self.async_show_form(step_id="confirm", data_schema=vol.Schema({}),
                                    description_placeholders={"date": self._plan.get("effective_date") or "all history before the next saved plan"})

    def _save(self):
        # Store a complete version, not mutable shared defaults. HA persists options
        # across reauthentication and restart; no rolling consumption counter exists.
        plan = prepare_plan(self._plan)
        if self._edit is None:
            if day := plan["effective_date"]:
                plan["effective_from"] = datetime.combine(date.fromisoformat(day), time.min,
                                                         ZoneInfo(plan["timezone"])).astimezone(timezone.utc).isoformat()
            else:
                plan["effective_from"] = None
        if not self._settings:
            self._settings = {"currency": self.hass.config.currency, "plans": []}
        if self._edit is None:
            self._settings["plans"].append(plan)
        else:
            # Keep the original boundaries on edits: only this saved period changes.
            original = self._settings["plans"][self._edit]
            plan["effective_from"] = original["effective_from"]
            plan["effective_date"] = original["effective_date"]
            self._settings["plans"][self._edit] = plan
        return self.async_create_entry(title="", data={**self.config_entry.options, "tariff": self._settings})
