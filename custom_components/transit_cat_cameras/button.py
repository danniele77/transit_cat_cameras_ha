"""Botones de control de la integración."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.components.button import ButtonEntity
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities) -> None:
    async_add_entities([TransitCatRefreshButton(hass, entry)])


class TransitCatRefreshButton(ButtonEntity):
    """Fuerza una descarga de las instantáneas de la entrada."""

    _attr_has_entity_name = True
    _attr_name = "Actualizar instantáneas"
    _attr_icon = "mdi:refresh"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_refresh_images"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Servei Català de Trànsit (Generalitat de Catalunya)",
            model="Càmeres de trànsit (feed obert)",
        )

    async def async_press(self) -> None:
        entities = self._hass.data.get(DOMAIN, {}).get("camera_entities", {}).get(
            self._entry.entry_id, []
        )
        for entity in entities:
            await entity.async_force_refresh()