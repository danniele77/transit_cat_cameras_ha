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
from homeassistant.helpers import entity_registry as er

from .api import clear_inventory_cache, rasos_camera
from .const import CONF_CAMERAS, CONF_INCIDENT_ROADS, DOMAIN

_LOGGER = logging.getLogger(__name__)

_HUELLAS = "huellas_entradas"

PLATFORMS = ["camera", "sensor", "button"]


def _camera_key(camera: dict) -> tuple[str, ...]:
    source = camera.get("source", "")
    if source in ("3Cat", "Rasos de Peguera"):
        return (
            source,
            camera.get("road_name", ""),
            " ".join((camera.get("municipality") or "").casefold().split()),
        )
    return ("device", camera.get("device_id", ""))


def _deduplicate_entry_cameras(cameras: list[dict]) -> tuple[list[dict], set[str]]:
    """Consolida duplicados antiguos guardados antes de la versión 0.5.0."""
    result: list[dict] = []
    seen: set[tuple[str, ...]] = set()
    removed_ids: set[str] = set()
    for camera in cameras:
        key = _camera_key(camera)
        if key in seen:
            if camera.get("device_id"):
                removed_ids.add(camera["device_id"])
            continue
        seen.add(key)
        result.append(camera)
    return result, removed_ids


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
    cameras = list(entry.data.get(CONF_CAMERAS, []))
    normalized_cameras, removed_ids = _deduplicate_entry_cameras(cameras)
    if not any(camera.get("device_id") == "rasos_refugi" for camera in normalized_cameras):
        rasos = rasos_camera()
        normalized_cameras.append(
            {
                "device_id": rasos.device_id,
                "name": rasos.display_name,
                "road_name": rasos.road_name,
                "municipality": rasos.municipality,
                "kilometer_point": rasos.kilometer_point,
                "source": rasos.source,
                "latitude": rasos.latitude,
                "longitude": rasos.longitude,
                "image_url": rasos.image_url,
            }
        )
    if len(normalized_cameras) != len(cameras):
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_CAMERAS: normalized_cameras},
        )
        registry = er.async_get(hass)
        for device_id in removed_ids:
            old_unique_id = f"{entry.entry_id}_{device_id}"
            entity_id = registry.async_get_entity_id("camera", DOMAIN, old_unique_id)
            if entity_id:
                registry.async_remove(entity_id)

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
