"""Acceso a los datos públicos del Servei Català de Trànsit (SCT).

Responsabilidades de este módulo:
  - descargar el inventario XML (WFS/GML) de cámaras, con límite de tamaño
    y caché en memoria,
  - convertir ese XML en objetos Python manejables (TransitCatCamera),
  - deduplicar las entradas repetidas que trae el propio feed,
  - validar que las URLs de imagen apuntan de verdad a uno de los cuatro
    proveedores conocidos del feed.
"""

from __future__ import annotations

import asyncio
import json
from html.parser import HTMLParser
import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

import aiohttp
from homeassistant.core import HomeAssistant

from .const import (
    ALLOWED_IMAGE_DOMAINS,
    CAMERA_INVENTORY_URL,
    INVENTORY_CACHE_SECONDS,
    INVENTORY_HEADERS,
    INVENTORY_TIMEOUT_SECONDS,
    INCIDENTS_HEADERS,
    INCIDENTS_URL,
    MAX_INVENTORY_BYTES,
    RASOS_CAMERA_URL,
    RASOS_HEADERS,
    THREECAT_CAMERA_URL,
    THREECAT_HEADERS,
    XML_NAMESPACES,
)

_LOGGER = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"[^a-z0-9]+")


class InventoryTooLargeError(Exception):
    """El XML descargado supera el tamaño máximo permitido."""


class _ThreeCatCameraParser(HTMLParser):
    """Extrae las imágenes actuales del catálogo público de 3Cat."""

    def __init__(self) -> None:
        super().__init__()
        self.cameras: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "img":
            return
        values = dict(attrs)
        src = values.get("src") or values.get("data-src") or ""
        alt = (values.get("alt") or "").strip()
        if alt and src.startswith("https://statics.3cat.cat/meteo/beauties/"):
            self.cameras.append((alt, src))


def _parse_threecat_cameras(html_bytes: bytes) -> list[TransitCatCamera]:
    parser = _ThreeCatCameraParser()
    parser.feed(html_bytes.decode("utf-8", errors="replace"))
    cameras: list[TransitCatCamera] = []
    seen_urls: set[str] = set()
    ski_terms = (
        "esquí",
        "esqui",
        "molina",
        "masella",
        "núria",
        "nuria",
        "boí",
        "boi",
        "baqueira",
        "port aine",
        "port del comte",
        "capdella",
        "vallter",
    )
    for name, image_url in parser.cameras:
        if image_url in seen_urls:
            continue
        seen_urls.add(image_url)
        category = "3Cat · Esquí" if any(term in name.lower() for term in ski_terms) else "3Cat · Webcams"
        cameras.append(
            TransitCatCamera(
                device_id=_build_device_id("3cat", image_url),
                source="3Cat",
                road_name=category,
                municipality=name,
                kilometer_point=None,
                latitude=None,
                longitude=None,
                image_url=image_url,
            )
        )
    return cameras


async def async_fetch_threecat_camera_inventory(
    session: aiohttp.ClientSession,
) -> list[TransitCatCamera]:
    """Descarga las instantáneas públicas del catálogo de cámaras de 3Cat."""
    html_bytes = await async_download_xml(
        session,
        THREECAT_CAMERA_URL,
        headers=THREECAT_HEADERS,
        timeout_seconds=INVENTORY_TIMEOUT_SECONDS,
        max_bytes=MAX_INVENTORY_BYTES,
    )
    return _parse_threecat_cameras(html_bytes)


def _parse_rasos_camera(html_bytes: bytes) -> list[TransitCatCamera]:
    """Extrae la instantánea oficial de Rasos de Peguera."""
    html = html_bytes.decode("utf-8", errors="replace")
    image_url = "https://app.projecte4estacions.com/snapshots/refugirasos.jpg"
    if image_url not in html:
        return []
    return [
        TransitCatCamera(
            device_id="rasos_refugi",
            source="Rasos de Peguera",
            road_name="Rasos de Peguera",
            municipality="Castellar del Riu",
            kilometer_point=None,
            latitude=None,
            longitude=None,
            image_url=image_url,
        )
    ]


async def async_fetch_rasos_camera_inventory(
    session: aiohttp.ClientSession,
) -> list[TransitCatCamera]:
    """Descarga la cámara pública de Rasos de Peguera."""
    html_bytes = await async_download_xml(
        session,
        RASOS_CAMERA_URL,
        headers=RASOS_HEADERS,
        timeout_seconds=INVENTORY_TIMEOUT_SECONDS,
        max_bytes=MAX_INVENTORY_BYTES,
    )
    return _parse_rasos_camera(html_bytes)


@dataclass
class TransitCatCamera:
    """Una cámara tal y como la describe el feed del SCT."""

    device_id: str  # slug estable, p.ej. "sct_nc87"
    source: str  # <cite:font>: "SCT", "Terrassa", "IMI" o "Andorra"
    road_name: str | None  # <cite:carretera>
    municipality: str | None  # <cite:municipi>
    kilometer_point: str | None  # <cite:pk>, si está presente
    latitude: float | None
    longitude: float | None
    image_url: str = ""

    @property
    def display_name(self) -> str:
        """Nombre legible para el selector y para la entidad."""
        parts = [p for p in (self.road_name, self.municipality) if p]
        if parts:
            return " — ".join(parts)
        return f"Càmera {self.device_id}"


@dataclass
class TransitCatIncident:
    """Incidència viària publicada por el SCT."""

    incident_id: str
    road: str
    municipality: str | None
    kilometer_start: float | None
    kilometer_end: float | None
    direction: str | None
    level: str | None
    cause: str | None
    description: str | None
    destination: str | None
    timestamp: int | None

    @property
    def display_name(self) -> str:
        parts = [self.road]
        if self.kilometer_start is not None:
            parts.append(f"km {self.kilometer_start:g}")
        if self.direction:
            parts.append(self.direction)
        return " · ".join(parts)

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.incident_id,
            "carretera": self.road,
            "municipio": self.municipality,
            "km_inicio": self.kilometer_start,
            "km_fin": self.kilometer_end,
            "sentido": self.direction,
            "nivel": self.level,
            "causa": self.cause,
            "descripcion": self.description,
            "destino": self.destination,
            "timestamp": self.timestamp,
        }


def _number(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _parse_incidents(payload: bytes) -> list[TransitCatIncident]:
    data = json.loads(payload.decode("utf-8"))
    incidents: list[TransitCatIncident] = []
    for item in data.get("incidents", []):
        road = str(item.get("descCarretera") or "").strip()
        if not road:
            continue
        incidents.append(
            TransitCatIncident(
                incident_id=str(item.get("identificador")),
                road=road,
                municipality=item.get("nomMunicipi"),
                kilometer_start=_number(item.get("quilometreInicial")),
                kilometer_end=_number(item.get("quilometreFinal")),
                direction=item.get("descripcioSentit"),
                level=item.get("descripcioNivell"),
                cause=item.get("descripcioCausa"),
                description=item.get("infoAdicional"),
                destination=item.get("nomMunicipiDesti"),
                timestamp=item.get("dataHora"),
            )
        )
    return incidents


async def async_fetch_incidents(
    session: aiohttp.ClientSession,
) -> list[TransitCatIncident]:
    """Descarga las incidencias activas del SCT."""
    params = {
        "retencions": "on",
        "obres": "on",
        "cons": "on",
        "meteo": "on",
        "demarcacio": "",
        "comarca": "",
        "via": "",
        "ordenacioSelect": "data",
        "ordenacioDirSelect": "desc",
        "offset": "0",
        "limit": "1000",
    }
    async with asyncio.timeout(INVENTORY_TIMEOUT_SECONDS):
        async with session.get(
            INCIDENTS_URL, params=params, headers=INCIDENTS_HEADERS
        ) as response:
            response.raise_for_status()
            return _parse_incidents(await response.read())


def is_allowed_image_url(url: str) -> bool:
    """Comprueba que una URL de imagen es de uno de los dominios del feed.

    A diferencia del feed de la DGT (todo HTTPS), este feed mezcla HTTP y
    HTTPS según el proveedor, así que aquí se aceptan ambos esquemas — pero
    SIEMPRE se exige que el dominio esté en ALLOWED_IMAGE_DOMAINS. Sin esta
    comprobación, una URL manipulada en el feed podría hacer que Home
    Assistant lanzara peticiones contra equipos de tu red interna (SSRF).
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False

    if parsed.scheme not in ("http", "https"):
        return False

    host = (parsed.hostname or "").lower()
    if not host:
        return False

    return any(
        host == dominio or host.endswith(f".{dominio}")
        for dominio in ALLOWED_IMAGE_DOMAINS
    )


def image_url_variants(url: str) -> tuple[str, ...]:
    """Devuelve variantes conocidas para una URL de cámara del SCT.

    El inventario XML publica algunas cámaras SCT con extensión ``.gif``,
    mientras que la web oficial las enlaza como ``.jpg``. El servidor puede
    fallar con una de las dos formas, así que se prueba primero la variante
    JPG y después la URL original.
    """
    if "mct.gencat.cat" not in (urlparse(url).hostname or "").lower():
        return (url,)
    if "sctidcam=" not in url.lower() or not url.lower().endswith(".gif"):
        return (url,)
    return (url[:-4] + ".jpg", url)


def _slugify(value: str) -> str:
    value = _SLUG_RE.sub("_", value.lower()).strip("_")
    return value or "cam"


def _build_device_id(source: str, image_url: str) -> str:
    """Deriva un id corto y estable a partir de la fuente y la URL de imagen.

    Las cámaras del SCT (mct.gencat.cat/.../RenderService) llevan su
    identificador real en el parámetro de consulta "sctidcam" (p.ej.
    "nc87.gif"); las de Terrassa, IMI y Andorra lo llevan en el último
    segmento de la ruta (p.ej. "cam01.jpeg", "PlAntonioLopez.gif").
    """
    prefix = _slugify(source) or "sct"
    stem = ""
    try:
        parsed = urlparse(image_url)
        qs = parse_qs(parsed.query)
        if "sctidcam" in qs and qs["sctidcam"]:
            stem = qs["sctidcam"][0]
        else:
            segments = [s for s in parsed.path.split("/") if s]
            stem = segments[-1] if segments else ""
    except ValueError:
        stem = image_url

    stem = re.sub(r"\.(gif|jpg|jpeg|png)$", "", stem, flags=re.IGNORECASE)
    return f"{prefix}_{_slugify(stem)}"


def _text(el: ET.Element | None) -> str | None:
    if el is None or el.text is None:
        return None
    # El feed a veces embebe un \r suelto dentro del valor (visto en
    # <cite:link> de cámaras de Terrassa); se limpia junto al espacio.
    cleaned = re.sub(r"[\r\n\t]+", " ", el.text).strip()
    return cleaned or None


def _parse_camera_inventory(xml_bytes: bytes) -> list[TransitCatCamera]:
    """Convierte el XML WFS/GML del SCT en una lista de TransitCatCamera.

    NOTA: función síncrona a propósito, pensada para ejecutarse en un hilo
    aparte (ver async_fetch_camera_inventory), igual que el parseo del
    inventario DATEX II de la DGT en la integración de referencia.
    """
    root = ET.fromstring(xml_bytes)
    ns = XML_NAMESPACES

    cameras: list[TransitCatCamera] = []
    seen_urls: set[str] = set()
    descartadas_por_url = 0

    for member in root.findall(f"{{{ns['gml']}}}featureMember"):
        cam = member.find(f"{{{ns['cite']}}}cameres")
        if cam is None:
            continue

        image_url = _text(cam.find(f"{{{ns['cite']}}}link"))
        if not image_url:
            continue

        if image_url in seen_urls:
            # El feed real trae entradas duplicadas exactas (mismo enlace
            # de imagen con distinto fid de GML); nos quedamos con la
            # primera.
            continue

        if not is_allowed_image_url(image_url):
            descartadas_por_url += 1
            continue

        source = _text(cam.find(f"{{{ns['cite']}}}font")) or "SCT"
        road_name = _text(cam.find(f"{{{ns['cite']}}}carretera"))
        municipality = _text(cam.find(f"{{{ns['cite']}}}municipi"))
        kilometer_point = _text(cam.find(f"{{{ns['cite']}}}pk"))

        latitude: float | None = None
        longitude: float | None = None
        coords_el = cam.find(
            f"{{{ns['cite']}}}geom/{{{ns['gml']}}}Point/{{{ns['gml']}}}coordinates"
        )
        coords_text = _text(coords_el)
        if coords_text:
            try:
                lon_str, lat_str = coords_text.split(",")
                longitude = float(lon_str)
                latitude = float(lat_str)
            except (ValueError, IndexError):
                pass

        seen_urls.add(image_url)
        cameras.append(
            TransitCatCamera(
                device_id=_build_device_id(source, image_url),
                source=source,
                road_name=road_name,
                municipality=municipality,
                kilometer_point=kilometer_point,
                latitude=latitude,
                longitude=longitude,
                image_url=image_url,
            )
        )

    if descartadas_por_url:
        _LOGGER.warning(
            "Inventario SCT: %d cámaras descartadas por tener una URL de "
            "imagen fuera de los dominios permitidos",
            descartadas_por_url,
        )

    _LOGGER.debug("Inventario SCT: %d cámaras parseadas", len(cameras))
    return cameras


async def async_download_xml(
    session: aiohttp.ClientSession,
    url: str,
    *,
    headers: dict[str, str],
    timeout_seconds: float,
    max_bytes: int,
) -> bytes:
    """Descarga un XML respetando un límite de tamaño (declarado y real)."""
    async with asyncio.timeout(timeout_seconds):
        async with session.get(url, headers=headers) as response:
            response.raise_for_status()

            declarado = response.content_length
            if declarado is not None and declarado > max_bytes:
                raise InventoryTooLargeError(
                    f"{url} declara {declarado} bytes, "
                    f"por encima del límite de {max_bytes}"
                )

            trozos: list[bytes] = []
            total = 0
            async for trozo in response.content.iter_chunked(64 * 1024):
                total += len(trozo)
                if total > max_bytes:
                    raise InventoryTooLargeError(
                        f"{url} superó el límite de {max_bytes} bytes"
                    )
                trozos.append(trozo)

            return b"".join(trozos)


# --- Caché del inventario en memoria ---------------------------------------
_inventory_cache: list[TransitCatCamera] | None = None
_inventory_cached_at: float = 0.0
_inventory_lock = asyncio.Lock()


def clear_inventory_cache() -> None:
    """Vacía el inventario guardado en memoria (al desinstalar la última entrada)."""
    global _inventory_cache, _inventory_cached_at
    _inventory_cache = None
    _inventory_cached_at = 0.0
    _LOGGER.debug("Caché del inventario SCT vaciada")


async def async_fetch_camera_inventory(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    force_refresh: bool = False,
) -> list[TransitCatCamera]:
    """Devuelve el inventario de cámaras, descargándolo solo si hace falta.

    Lanza aiohttp.ClientError, TimeoutError, InventoryTooLargeError o
    ET.ParseError si algo va mal; quien llame debe capturarlo y mostrar un
    error legible al usuario.
    """
    global _inventory_cache, _inventory_cached_at

    async with _inventory_lock:
        ahora = time.monotonic()
        cache_valido = (
            _inventory_cache is not None
            and (ahora - _inventory_cached_at) < INVENTORY_CACHE_SECONDS
        )
        if cache_valido and not force_refresh:
            _LOGGER.debug(
                "Inventario SCT servido desde caché (%d cámaras)",
                len(_inventory_cache),
            )
            return _inventory_cache

        xml_bytes = await async_download_xml(
            session,
            CAMERA_INVENTORY_URL,
            headers=INVENTORY_HEADERS,
            timeout_seconds=INVENTORY_TIMEOUT_SECONDS,
            max_bytes=MAX_INVENTORY_BYTES,
        )

        # El parseo es síncrono; se ejecuta en un hilo aparte para no
        # bloquear el bucle de eventos de Home Assistant.
        cameras = await hass.async_add_executor_job(
            _parse_camera_inventory, xml_bytes
        )

        _inventory_cache = cameras
        _inventory_cached_at = ahora
        return cameras
