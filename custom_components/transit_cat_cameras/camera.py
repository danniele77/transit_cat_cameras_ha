"""Plataforma 'camera' de la integración.

Cada cámara elegida en el config_flow se convierte en una entidad Camera.
No hay vídeo: el SCT publica fotos fijas (la mayoría GIF) que se renuevan
periódicamente en su propio servidor.

Protección del servidor de origen (mismo esquema que dgt_traffic_cameras_ha):

  1. Caché normal: una foto nueva como mucho cada MIN_SECONDS_BETWEEN_IMAGE_FETCH
     segundos por cámara, sin importar cada cuánto refresque el panel.

  2. Caché condicional (ETag / Last-Modified): cuando toca refrescar, se le
     pregunta al servidor "¿ha cambiado desde la última vez?" antes de
     descargar la foto entera.

  3. Backoff ante fallos: si el servidor falla, se espera cada vez más
     antes de reintentar (60s, 120s, 240s... hasta 1 hora), para no
     convertir una caída en una tormenta de reintentos.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import time

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import image_url_variants, is_allowed_image_url
from .const import (
    BACKOFF_INITIAL_SECONDS,
    BACKOFF_MAX_SECONDS,
    BACKOFF_MULTIPLIER,
    CONF_CAMERAS,
    DOMAIN,
    FRAME_INTERVAL_SECONDS,
    HTTP_TIMEOUT_SECONDS,
    IMAGE_HEADERS,
    IMAGE_MAGIC_BYTES,
    IMAGE_MAGIC_OFFSET_CHECKS,
    MAX_IMAGE_BYTES,
    MIN_SECONDS_BETWEEN_IMAGE_FETCH,
    REJECTED_CONTENT_TYPE_PREFIXES,
)

_LOGGER = logging.getLogger(__name__)

_SIN_CAMBIOS = object()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Crea una entidad Camera por cada cámara guardada en la ConfigEntry."""
    cameras_data = entry.data.get(CONF_CAMERAS, [])

    vistas: set[str] = set()
    entities: list[TransitCatTrafficCamera] = []
    for camera_data in cameras_data:
        device_id = camera_data.get("device_id")
        if not device_id or device_id in vistas:
            continue
        vistas.add(device_id)
        entities.append(TransitCatTrafficCamera(entry, camera_data))

    async_add_entities(entities)
    hass.data.setdefault(DOMAIN, {}).setdefault("camera_entities", {})[
        entry.entry_id
    ] = entities


class TransitCatTrafficCamera(Camera):
    """Una cámara individual de tràfico del SCT."""

    _attr_has_entity_name = True
    _attr_supported_features = CameraEntityFeature(0)  # sin stream
    _attr_icon = "mdi:cctv"

    def __init__(self, entry: ConfigEntry, camera_data: dict) -> None:
        super().__init__()
        self._entry = entry
        self._camera_data = camera_data
        self._image_url: str = camera_data["image_url"]
        self._image_urls = image_url_variants(self._image_url)

        device_id = camera_data["device_id"]
        self._attr_unique_id = f"{entry.entry_id}_{device_id}"
        self._attr_name = camera_data.get("name") or f"Càmera {device_id}"

        self._attr_extra_state_attributes = {
            "carretera": camera_data.get("road_name"),
            "municipi": camera_data.get("municipality"),
            "punt_kilometric": camera_data.get("kilometer_point"),
            "font": camera_data.get("source"),
            "latitude": camera_data.get("latitude"),
            "longitude": camera_data.get("longitude"),
        }

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Servei Català de Trànsit (Generalitat de Catalunya)",
            model="Càmera de trànsit (feed obert)",
            configuration_url="https://analisi.transparenciacatalunya.cat/d/3tzz-6b9y",
        )

        self._cached_image: bytes | None = None
        self._cached_at: float = 0.0
        self._fetch_lock = asyncio.Lock()

        self._etag: str | None = None
        self._last_modified: str | None = None
        self._last_checked_at: datetime | None = None
        self._server_date: str | None = None

        self._fallos_consecutivos = 0
        self._reintentar_a_partir_de: float = 0.0

        self._attr_extra_state_attributes.update(
            {
                "ultima_comprobacion": None,
                "fecha_servidor": None,
                "actualizacion_local": None,
            }
        )

    @property
    def frame_interval(self) -> float:
        return FRAME_INTERVAL_SECONDS

    @property
    def available(self) -> bool:
        """False mientras no tengamos ninguna foto real guardada."""
        return self._cached_image is not None

    def _registrar_fallo(self) -> None:
        self._fallos_consecutivos += 1
        espera = min(
            BACKOFF_INITIAL_SECONDS
            * (BACKOFF_MULTIPLIER ** (self._fallos_consecutivos - 1)),
            BACKOFF_MAX_SECONDS,
        )
        self._reintentar_a_partir_de = time.monotonic() + espera
        _LOGGER.debug(
            "Cámara %s: fallo nº%d, no se reintentará hasta dentro de %ss",
            self._attr_name,
            self._fallos_consecutivos,
            espera,
        )

    def _registrar_exito(self) -> None:
        self._fallos_consecutivos = 0
        self._reintentar_a_partir_de = 0.0

    async def async_force_refresh(self) -> None:
        """Fuerza una comprobación inmediata ignorando caché y backoff."""
        self._cached_at = 0.0
        self._reintentar_a_partir_de = 0.0
        await self.async_camera_image()

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Devuelve la última instantánea, descargándola solo si toca."""
        async with self._fetch_lock:
            ahora = time.monotonic()

            if (
                self._cached_image is not None
                and (ahora - self._cached_at) < MIN_SECONDS_BETWEEN_IMAGE_FETCH
            ):
                return self._cached_image

            if ahora < self._reintentar_a_partir_de:
                return self._cached_image

            if not all(is_allowed_image_url(url) for url in self._image_urls):
                _LOGGER.error(
                    "Cámara %s: la URL guardada (%s) no es de un dominio "
                    "permitido; no se descargará. Vuelve a añadir la cámara.",
                    self._attr_name,
                    self._image_url,
                )
                self._registrar_fallo()
                return self._cached_image

            disponible_antes = self._cached_image is not None

            imagen = await self._async_descargar_imagen()
            if imagen is None:
                return self._cached_image

            self._registrar_exito()
            self._cached_at = ahora
            if imagen is not _SIN_CAMBIOS:
                self._cached_image = imagen

            self._last_checked_at = datetime.now(timezone.utc)
            self._attr_extra_state_attributes["ultima_comprobacion"] = (
                self._last_checked_at.isoformat()
            )
            self.async_write_ha_state()

            # Las entidades "camera" no se sondean solas: si no avisamos
            # aquí, Home Assistant no vuelve a mirar "available" y la
            # entidad se queda congelada en "No disponible" desde el
            # arranque aunque ya tengamos una foto real guardada.
            if not disponible_antes and self._cached_image is not None:
                self.async_write_ha_state()

            return self._cached_image

    async def _async_descargar_imagen(self) -> bytes | None | object:
        """Descarga la foto. Devuelve los bytes, _SIN_CAMBIOS, o None si falla."""
        session = async_get_clientsession(self.hass)

        headers = dict(IMAGE_HEADERS)
        if self._cached_image is not None:
            if self._etag:
                headers["If-None-Match"] = self._etag
            if self._last_modified:
                headers["If-Modified-Since"] = self._last_modified

        try:
            async with asyncio.timeout(HTTP_TIMEOUT_SECONDS):
                for image_url in self._image_urls:
                    try:
                        response = await session.get(image_url, headers=headers)
                    except Exception:
                        if image_url == self._image_urls[-1]:
                            raise
                        continue
                    async with response:
                        if response.status >= 400 and image_url != self._image_urls[-1]:
                            _LOGGER.debug(
                                "Cámara %s: %s devolvió HTTP %s; se probará la "
                                "siguiente variante",
                                self._attr_name,
                                image_url,
                                response.status,
                            )
                            continue
                        if response.status == 304:
                            _LOGGER.debug(
                                "Cámara %s: sin cambios (304)", self._attr_name
                            )
                            return _SIN_CAMBIOS

                        response.raise_for_status()

                        content_type = (response.content_type or "").lower()
                        if content_type.startswith(REJECTED_CONTENT_TYPE_PREFIXES):
                            _LOGGER.warning(
                                "Cámara %s: el servidor devolvió '%s' en lugar de "
                                "una imagen; se descarta la respuesta",
                                self._attr_name,
                                content_type,
                            )
                            self._registrar_fallo()
                            return None

                        declarado = response.content_length
                        if declarado is not None and declarado > MAX_IMAGE_BYTES:
                            _LOGGER.warning(
                                "Cámara %s: imagen demasiado grande (%d bytes), "
                                "se descarta",
                                self._attr_name,
                                declarado,
                            )
                            self._registrar_fallo()
                            return None

                        trozos: list[bytes] = []
                        total = 0
                        async for trozo in response.content.iter_chunked(64 * 1024):
                            total += len(trozo)
                            if total > MAX_IMAGE_BYTES:
                                _LOGGER.warning(
                                    "Cámara %s: la descarga superó %d bytes, se aborta",
                                    self._attr_name,
                                    MAX_IMAGE_BYTES,
                                )
                                self._registrar_fallo()
                                return None
                            trozos.append(trozo)

                        if total == 0:
                            _LOGGER.warning(
                                "Cámara %s: el servidor devolvió una imagen vacía",
                                self._attr_name,
                            )
                            self._registrar_fallo()
                            return None

                        datos = b"".join(trozos)

                        if not _parece_imagen(datos):
                            _LOGGER.warning(
                                "Cámara %s: la respuesta no parece una imagen "
                                "(empieza por %r); se descarta",
                                self._attr_name,
                                datos[:16],
                            )
                            self._registrar_fallo()
                            return None

                        self._etag = response.headers.get("ETag")
                        self._last_modified = response.headers.get("Last-Modified")
                        self._server_date = response.headers.get("Date")
                        self._attr_extra_state_attributes["fecha_servidor"] = (
                            self._server_date
                        )
                        self._attr_extra_state_attributes["actualizacion_local"] = (
                            datetime.now(timezone.utc).isoformat()
                        )

                        return datos
                return None

        except TimeoutError:
            _LOGGER.warning(
                "Cámara %s: el servidor no respondió en %ss",
                self._attr_name,
                HTTP_TIMEOUT_SECONDS,
            )
            self._registrar_fallo()
            return None
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Cámara %s: no se pudo descargar la imagen (%s): %s",
                self._attr_name,
                self._image_urls,
                err,
            )
            self._registrar_fallo()
            return None


def _parece_imagen(datos: bytes) -> bool:
    """Comprueba si unos bytes empiezan por la firma de un formato de imagen."""
    if len(datos) < 12:
        return False

    if datos.startswith(IMAGE_MAGIC_BYTES):
        return True

    for pos1, firma1, pos2, firma2 in IMAGE_MAGIC_OFFSET_CHECKS:
        if (
            datos[pos1 : pos1 + len(firma1)] == firma1
            and datos[pos2 : pos2 + len(firma2)] == firma2
        ):
            return True

    return False
