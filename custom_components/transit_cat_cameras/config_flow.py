"""Config flow de 'Càmeres de trànsit (SCT)'.

Con unas ~250 cámaras en el feed, no tiene sentido mostrar una lista plana.
El flujo se divide en dos pasos que van acotando el conjunto:

  1) elegir la carretera (p.ej. "C-58", "AP-7"...)
  2) elegir, con casillas, las cámaras concretas de esa carretera

A diferencia de dgt_traffic_cameras_ha (provincia -> carretera), el feed
del SCT no trae provincia, así que la carretera es directamente el primer
nivel de selección.

Cada ConfigEntry representa "una tanda de cámaras añadidas" para una
carretera. Para añadir cámaras de otra carretera más adelante, se puede
volver a ejecutar la integración desde Ajustes > Dispositivos y servicios,
o usar el flujo de opciones para añadir más a una entrada ya existente.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .api import (
    InventoryTooLargeError,
    TransitCatCamera,
    async_fetch_camera_inventory,
    async_fetch_incidents,
    async_fetch_rasos_camera_inventory,
    async_fetch_threecat_camera_inventory,
)
from .const import CONF_CAMERAS, CONF_INCIDENT_ROADS, DOMAIN

_LOGGER = logging.getLogger(__name__)

_FALLBACK_INCIDENT_ROADS = (
    "A-2", "AP-7", "B-10", "B-20", "B-23", "B-30", "C-16", "C-17",
    "C-25", "C-31", "C-32", "C-33", "C-35", "C-58", "C-60",
    "N-340", "N-II", "T-11",
)


def _deduplicate_cameras(cameras: list[TransitCatCamera]) -> list[TransitCatCamera]:
    """Deduplica ubicaciones externas y conserva todas las cámaras SCT."""
    result: list[TransitCatCamera] = []
    seen: set[tuple[str, str, str]] = set()
    for camera in cameras:
        if camera.source in ("3Cat", "Rasos de Peguera"):
            key = (
                camera.source,
                camera.road_name or "",
                " ".join((camera.municipality or "").casefold().split()),
            )
            if key in seen:
                continue
            seen.add(key)
        result.append(camera)
    return result


async def _get_inventory_or_error(hass) -> tuple[list[TransitCatCamera] | None, str | None]:
    """Descarga el inventario y traduce cualquier fallo a una clave de error."""
    session = async_get_clientsession(hass)
    try:
        # Never mutate the shared inventory cache when adding external feeds.
        cameras = list(await async_fetch_camera_inventory(hass, session))
        try:
            threecat_cameras = await async_fetch_threecat_camera_inventory(session)
            cameras.extend(threecat_cameras)
            _LOGGER.debug("Catálogo 3Cat añadido: %d cámaras", len(threecat_cameras))
        except Exception:  # noqa: BLE001 - 3Cat no debe impedir las cámaras SCT
            _LOGGER.warning(
                "No se pudo añadir el catálogo de cámaras de 3Cat", exc_info=True
            )
        try:
            rasos_cameras = await async_fetch_rasos_camera_inventory(session)
            cameras.extend(rasos_cameras)
            _LOGGER.debug("Cámaras Rasos añadidas: %d", len(rasos_cameras))
        except Exception:  # noqa: BLE001 - Rasos no debe impedir otras cámaras
            _LOGGER.warning("No se pudo añadir la cámara de Rasos", exc_info=True)
    except TimeoutError:
        return None, "timeout"
    except InventoryTooLargeError:
        _LOGGER.error("El inventario del SCT superó el tamaño máximo permitido")
        return None, "inventory_too_large"
    except Exception:  # noqa: BLE001 - cualquier fallo de red/parseo cuenta como error genérico
        _LOGGER.exception("Fallo al descargar/parsear el inventario de cámaras del SCT")
        return None, "cannot_connect"

    cameras = _deduplicate_cameras(cameras)
    if not cameras:
        return None, "no_cameras_found"

    return cameras, None


def _camera_source(camera: TransitCatCamera) -> str:
    if camera.road_name == "Estaciones de esquí":
        return "ski"
    if camera.source == "3Cat":
        return "webcams"
    return "sct"


def _source_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required("source"): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(value="sct", label="SCT · Carreteras"),
                        SelectOptionDict(value="webcams", label="3Cat · Webcams"),
                        SelectOptionDict(value="ski", label="Estaciones de esquí"),
                    ],
                    mode=SelectSelectorMode.DROPDOWN,
                )
            )
        }
    )


def _road_schema(items: list[TransitCatCamera], source: str) -> vol.Schema:
    roads = sorted(
        {i.road_name for i in items if i.road_name and _camera_source(i) == source}
    )
    return vol.Schema(
        {
            vol.Required("road"): SelectSelector(
                SelectSelectorConfig(
                    options=[SelectOptionDict(value=r, label=r) for r in roads],
                    mode=SelectSelectorMode.DROPDOWN,
                )
            )
        }
    )


def _cameras_schema(candidates: list[TransitCatCamera]) -> vol.Schema:
    options = [
        SelectOptionDict(
            value=c.device_id,
            label=f"{c.display_name} (km {c.kilometer_point})"
            if c.kilometer_point
            else c.display_name,
        )
        for c in candidates
    ]
    return vol.Schema(
        {
            vol.Required("camera_ids"): SelectSelector(
                SelectSelectorConfig(
                    options=options,
                    multiple=True,
                    mode=SelectSelectorMode.LIST,
                )
            )
        }
    )


def _incident_roads_schema(roads: list[str]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required("roads"): SelectSelector(
                SelectSelectorConfig(
                    options=[SelectOptionDict(value=road, label=road) for road in roads],
                    multiple=True,
                    mode=SelectSelectorMode.LIST,
                )
            )
        }
    )


def _remove_cameras_schema(cameras: list[dict[str, Any]]) -> vol.Schema:
    options = [
        SelectOptionDict(value=c["device_id"], label=c.get("name") or c["device_id"])
        for c in cameras
    ]
    return vol.Schema(
        {
            vol.Required("camera_ids"): SelectSelector(
                SelectSelectorConfig(
                    options=options,
                    multiple=True,
                    mode=SelectSelectorMode.LIST,
                )
            )
        }
    )


def _camera_to_dict(camera: TransitCatCamera) -> dict[str, Any]:
    """Serializa una TransitCatCamera a dict plano para ConfigEntry.data."""
    return {
        "device_id": camera.device_id,
        "name": camera.display_name,
        "road_name": camera.road_name,
        "municipality": camera.municipality,
        "kilometer_point": camera.kilometer_point,
        "source": camera.source,
        "latitude": camera.latitude,
        "longitude": camera.longitude,
        "image_url": camera.image_url,
    }


def _km_sort_key(km: str | None) -> float:
    try:
        return float(km) if km is not None else float("inf")
    except ValueError:
        return float("inf")


class TransitCatCamerasConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Maneja la creación inicial de la integración."""

    VERSION = 1

    def __init__(self) -> None:
        self._all_cameras: list[TransitCatCamera] = []
        self._camera_source: str | None = None
        self._road: str | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Descarga el inventario y pide la carretera."""
        if not self._all_cameras:
            cameras, error = await _get_inventory_or_error(self.hass)
            if error:
                return self.async_show_form(
                    step_id="user", data_schema=vol.Schema({}), errors={"base": error}
                )
            self._all_cameras = cameras

        if user_input:
            self._camera_source = user_input["source"]
            return await self.async_step_road()

        return self.async_show_form(step_id="user", data_schema=_source_schema())

    async def async_step_road(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if user_input:
            self._road = user_input["road"]
            return await self.async_step_cameras()
        return self.async_show_form(
            step_id="road",
            data_schema=_road_schema(self._all_cameras, self._camera_source or "sct"),
        )

    async def async_step_cameras(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Elegir, con casillas, las cámaras concretas de la carretera."""
        candidates = [c for c in self._all_cameras if c.road_name == self._road]
        candidates.sort(
            key=lambda c: (_km_sort_key(c.kilometer_point), c.municipality or "")
        )

        if user_input is not None:
            selected_ids = set(user_input["camera_ids"])
            selected = [
                _camera_to_dict(c) for c in candidates if c.device_id in selected_ids
            ]
            if not selected:
                return self.async_show_form(
                    step_id="cameras",
                    data_schema=_cameras_schema(candidates),
                    errors={"base": "no_cameras_selected"},
                )

            # Un unique_id por carretera evita crear dos entradas
            # duplicadas pidiendo las mismas fotos al servidor del SCT.
            await self.async_set_unique_id(self._road)
            self._abort_if_unique_id_configured()

            title = f"SCT · {self._road}"
            return self.async_create_entry(title=title, data={CONF_CAMERAS: selected})

        return self.async_show_form(
            step_id="cameras", data_schema=_cameras_schema(candidates)
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        return TransitCatCamerasOptionsFlow(config_entry)


class TransitCatCamerasOptionsFlow(config_entries.OptionsFlow):
    """Permite añadir o quitar cámaras de una entrada ya existente."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        # NO asignar self.config_entry: en HA >= 2024.11 es de solo lectura
        # y el framework ya lo rellena.
        self._all_cameras: list[TransitCatCamera] = []
        self._road: str | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=["add_cameras", "add_incident_panels", "remove_cameras"],
        )

    async def async_step_add_incident_panels(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Selecciona las carreteras que tendrán sensores tipo panel."""
        if user_input is not None:
            selected_roads = set(user_input["roads"])
            existing_roads = set(
                self.config_entry.data.get(CONF_INCIDENT_ROADS, [])
            )
            self.hass.config_entries.async_update_entry(
                self.config_entry,
                data={
                    **self.config_entry.data,
                    CONF_INCIDENT_ROADS: sorted(existing_roads | selected_roads),
                },
            )
            return self.async_create_entry(title="", data={})

        session = async_get_clientsession(self.hass)
        try:
            incidents = await async_fetch_incidents(session)
        except Exception:  # noqa: BLE001 - mostrar un error útil en el flujo
            _LOGGER.warning(
                "No se pudieron cargar las incidencias; se usará el inventario de carreteras",
                exc_info=True,
            )
            try:
                cameras = await async_fetch_camera_inventory(self.hass, session)
                roads = sorted({camera.road_name for camera in cameras if camera.road_name})
            except Exception:  # noqa: BLE001
                _LOGGER.warning("Se usará la lista de carreteras de reserva", exc_info=True)
                roads = list(_FALLBACK_INCIDENT_ROADS)
        else:
            roads = sorted({incident.road for incident in incidents})

        if not roads:
            roads = list(_FALLBACK_INCIDENT_ROADS)
        return self.async_show_form(
            step_id="add_incident_panels",
            data_schema=_incident_roads_schema(roads),
        )

    async def async_step_add_cameras(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if user_input:
            self._camera_source = user_input["source"]
            return await self.async_step_road()
        return self.async_show_form(
            step_id="add_cameras", data_schema=_source_schema()
        )

    async def async_step_road(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if not self._all_cameras:
            cameras, error = await _get_inventory_or_error(self.hass)
            if error:
                return self.async_show_form(
                    step_id="road", data_schema=vol.Schema({}), errors={"base": error}
                )
            self._all_cameras = cameras

        if user_input:
            self._road = user_input["road"]
            return await self.async_step_cameras()

        return self.async_show_form(
            step_id="road",
            data_schema=_road_schema(self._all_cameras, self._camera_source or "sct"),
        )

    async def async_step_cameras(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        candidates = [c for c in self._all_cameras if c.road_name == self._road]
        candidates.sort(
            key=lambda c: (_km_sort_key(c.kilometer_point), c.municipality or "")
        )

        if user_input is not None:
            selected_ids = set(user_input["camera_ids"])
            new_cameras = [
                _camera_to_dict(c) for c in candidates if c.device_id in selected_ids
            ]
            existing = list(self.config_entry.data.get(CONF_CAMERAS, []))
            existing_ids = {c["device_id"] for c in existing}
            realmente_nuevas = [
                c for c in new_cameras if c["device_id"] not in existing_ids
            ]

            if not realmente_nuevas:
                return self.async_create_entry(title="", data={})

            merged = existing + realmente_nuevas
            self.hass.config_entries.async_update_entry(
                self.config_entry, data={**self.config_entry.data, CONF_CAMERAS: merged}
            )
            return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="cameras", data_schema=_cameras_schema(candidates)
        )

    async def async_step_remove_cameras(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Quita de esta entrada las cámaras que el usuario marque."""
        existing = list(self.config_entry.data.get(CONF_CAMERAS, []))

        if user_input is not None:
            selected_ids = set(user_input["camera_ids"])
            if not selected_ids:
                return self.async_show_form(
                    step_id="remove_cameras",
                    data_schema=_remove_cameras_schema(existing),
                    errors={"base": "no_cameras_selected"},
                )

            remaining = [c for c in existing if c["device_id"] not in selected_ids]
            if not remaining:
                return self.async_show_form(
                    step_id="remove_cameras",
                    data_schema=_remove_cameras_schema(existing),
                    errors={"base": "cannot_remove_all_cameras"},
                )

            registry = er.async_get(self.hass)
            for camera_data in existing:
                device_id = camera_data.get("device_id")
                if device_id not in selected_ids:
                    continue
                unique_id = f"{self.config_entry.entry_id}_{device_id}"
                entity_id = registry.async_get_entity_id("camera", DOMAIN, unique_id)
                if entity_id:
                    registry.async_remove(entity_id)

            self.hass.config_entries.async_update_entry(
                self.config_entry,
                data={**self.config_entry.data, CONF_CAMERAS: remaining},
            )
            return self.async_create_entry(title="", data={})

        if not existing:
            return self.async_abort(reason="no_cameras_to_remove")

        return self.async_show_form(
            step_id="remove_cameras", data_schema=_remove_cameras_schema(existing)
        )
