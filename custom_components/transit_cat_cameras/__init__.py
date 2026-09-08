"""Integración 'Càmeres de trànsit (Servei Català de Trànsit)' para Home Assistant.

Descarga el feed abierto del SCT (gencat.cat, formato WFS/GML) y permite
elegir, con un selector guiado (carretera -> selección), qué cámaras
añadir a Home Assistant como entidades camera.*.

Existe porque el feed equivalente de la DGT (ver dgt_traffic_cameras_ha)
excluye explícitamente Cataluña; esta integración cubre justo ese hueco
usando la fuente propia de la Generalitat.

Limitaciones conocidas:
- Solo cámaras (no hay paneles de mensaje variable: no se ha localizado un
  feed público equivalente del SCT para eso).
- Las cámaras son instantáneas fijas, no vídeo en directo.
- El feed mezcla cámaras de fuera de Catalunya (Andorra) porque el propio
  SCT las incluye en su mapa de tráfico; puedes simplemente no seleccionar
  esas cámaras al configurar la integración.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .api import clear_inventory_cache
from .const import CONF_CAMERAS, CONF_INCIDENT_ROADS, DOMAIN

_LOGGER = logging.getLogger(__name__)

_HUELLAS = "huellas_entradas"

PLATFORMS = ["camera", "sensor", "button"]


def _huella_entry(entry: ConfigEntry) -> tuple[str, ...]:
    """Resume la lista de cámaras guardada como una tupla ordenada.

    Sirve para no recargar la integración cuando Home Assistant nos avisa
    de un cambio que no afecta realmente a las entidades (p.ej. el propio
    flujo de opciones dispara dos avisos seguidos al añadir cámaras).
    """
    dispositivos = entry.data.get(CONF_CAMERAS, [])
    incident_roads = entry.data.get(CONF_INCIDENT_ROADS, [])
    return tuple(
        sorted(d.get("device_id", "") for d in dispositivos)
        + [f"incident:{road}" for road in incident_roads]
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Configura una entrada ya creada."""
    hass.data.setdefault(DOMAIN, {}).setdefault(_HUELLAS, {})[entry.entry_id] = (
        _huella_entry(entry)
    )

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Descarga una entrada (elimina sus entidades)."""
    descargada = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if descargada:
        domain_data = hass.data.get(DOMAIN, {})
        domain_data.get("camera_entities", {}).pop(entry.entry_id, None)
        huellas = domain_data.get(_HUELLAS, {})
        huellas.pop(entry.entry_id, None)

        if not huellas:
            clear_inventory_cache()
            hass.data.pop(DOMAIN, None)

    return descargada


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Recarga la entrada solo si la lista de cámaras ha cambiado de verdad."""
    huellas = hass.data.setdefault(DOMAIN, {}).setdefault(_HUELLAS, {})
    anterior = huellas.get(entry.entry_id)
    actual = _huella_entry(entry)

    if anterior == actual:
        _LOGGER.debug(
            "'%s': la configuración no ha cambiado; se omite la recarga",
            entry.title,
        )
        return

    _LOGGER.debug("'%s': la configuración ha cambiado; recargando", entry.title)
    huellas[entry.entry_id] = actual
    await hass.config_entries.async_reload(entry.entry_id)
