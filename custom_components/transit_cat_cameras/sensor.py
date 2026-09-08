"""Sensores de incidencias viarias publicadas por el SCT."""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)
from homeassistant.components.sensor import SensorEntity

from .api import TransitCatIncident, async_fetch_incidents
from .const import CONF_INCIDENT_ROADS, DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities,
) -> None:
    """Crea un resumen general y un sensor por carretera."""
    session = async_get_clientsession(hass)
    async def update_incidents() -> list[TransitCatIncident]:
        try:
            return await async_fetch_incidents(session)
        except Exception:  # noqa: BLE001 - los sensores deben seguir disponibles
            _LOGGER.warning("No se pudieron actualizar las incidencias del SCT", exc_info=True)
            return []

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name="Incidencias viarias SCT",
        update_method=update_incidents,
        update_interval=timedelta(minutes=5),
    )
    # The summary is always created. Per-road entities are opt-in so an
    # existing entry does not suddenly create hundreds of entities.
    roads = sorted(set(entry.data.get(CONF_INCIDENT_ROADS, [])))
    async_add_entities(
        [TransitCatIncidentSummarySensor(coordinator, entry)]
        + [TransitCatRoadSensor(coordinator, entry, road) for road in roads]
    )
    # Register entities before the network call. A temporary outage must not
    # hide the sensors from Home Assistant.
    # Do not fail platform setup when the remote endpoint is temporarily
    # unavailable. The entities stay visible and will update on the interval.
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception:  # noqa: BLE001
        _LOGGER.warning("No se pudo hacer la primera actualización de incidencias", exc_info=True)


class _IncidentSensor(CoordinatorEntity, SensorEntity):
    _attr_icon = "mdi:traffic-cone"
    _attr_native_unit_of_measurement = "incidencias"

    def __init__(self, coordinator, entry: ConfigEntry, unique_suffix: str) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_incidents_{unique_suffix}"

    @property
    def _incidents(self) -> list[TransitCatIncident]:
        return self.coordinator.data or []

    @staticmethod
    def _details(incidents: list[TransitCatIncident]) -> list[dict[str, object]]:
        return [incident.as_dict() for incident in incidents]


class TransitCatIncidentSummarySensor(_IncidentSensor):
    _attr_name = "Incidencias viarias"

    @property
    def native_value(self) -> int:
        return len(self._incidents)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        by_road: dict[str, int] = defaultdict(int)
        for incident in self._incidents:
            by_road[incident.road] += 1
        recent = sorted(
            self._incidents,
            key=lambda incident: incident.timestamp or 0,
            reverse=True,
        )[:20]
        return {
            "carreteras": dict(sorted(by_road.items())),
            "incidencias_recientes": self._details(recent),
            "fuente": "https://cit.transit.gencat.cat/cit/AppJava/views/incidents.xhtml",
        }


class TransitCatRoadSensor(_IncidentSensor):
    def __init__(self, coordinator, entry: ConfigEntry, road: str) -> None:
        super().__init__(coordinator, entry, road.lower().replace(" ", "_"))
        self._road = road
        self._attr_name = f"Incidencias {road}"

    @property
    def _road_incidents(self) -> list[TransitCatIncident]:
        return [incident for incident in self._incidents if incident.road == self._road]

    @property
    def native_value(self) -> int:
        return len(self._road_incidents)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {
            "carretera": self._road,
            "incidencias": self._details(self._road_incidents),
            "fuente": "https://cit.transit.gencat.cat/cit/AppJava/views/incidents.xhtml",
        }