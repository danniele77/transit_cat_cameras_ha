"""Constantes de la integración 'Càmeres de trànsit (Servei Català de Trànsit)'.

Modelada sobre la arquitectura de jonathanathe/dgt_traffic_cameras_ha
(caché del inventario, backoff ante fallos, validación de que la respuesta
es de verdad una imagen, allowlist de dominios contra SSRF), adaptada a la
fuente de datos del SCT en vez de a la DGT.

Diferencia clave respecto a esa integración: el feed de la DGT excluye
explícitamente Cataluña y País Vasco (gestión de tráfico transferida). El
Servei Català de Trànsit (SCT), organismo de la Generalitat de Catalunya,
publica su propio feed abierto que sí cubre las carreteras catalanas — y de
paso trae también algunas cámaras del Ajuntament de Terrassa, de l'IMI de
Barcelona y del Govern d'Andorra (vies frontereres), todas mezcladas.
"""

from __future__ import annotations

DOMAIN = "transit_cat_cameras"

# ---------------------------------------------------------------------------
# Fuente de datos
# ---------------------------------------------------------------------------

# Feed obert del Servei Català de Trànsit: inventario de cámaras en formato
# WFS/GML (no DATEX II, a diferencia de la DGT). Sin API key.
#
# Verificado manualmente (fetch real) el 2026-09-07: cada
# <gml:featureMember><cite:cameres> trae carretera, municipio, punto
# kilométrico (no siempre presente), coordenadas y una URL de imagen fija
# en <cite:link>. El feed mezcla cuatro fuentes distintas en <cite:font>:
# "SCT" (gencat.cat), "Terrassa" (Ajuntament de Terrassa), "IMI" (Institut
# Municipal d'Informàtica de Barcelona) y "Andorra" (Govern d'Andorra, vies
# frontereres). El feed contiene también entradas duplicadas exactas (mismo
# <cite:link>) que hay que deduplicar al parsear.
#
# Ficha del dataset: https://analisi.transparenciacatalunya.cat/d/3tzz-6b9y
CAMERA_INVENTORY_URL = "http://www.gencat.cat/transit/opendata/cameres.xml"
THREECAT_CAMERA_URL = "https://www.3cat.cat/3catinfo/el-temps/cameres/"
INCIDENTS_URL = "https://cit.transit.gencat.cat/cit/rest/incidents"

# Espacios de nombres XML del feed WFS/GML del SCT.
XML_NAMESPACES = {
    "wfs": "http://www.opengis.net/wfs",
    "gml": "http://www.opengis.net/gml",
    "cite": "http://www.opengeospatial.net/cite",
}

# Dominios desde los que aceptamos descargar imágenes de cámara.
#
# POR QUÉ ESTO EXISTE (seguridad): la URL de cada imagen no la elegimos
# nosotros, viene dentro del XML que descargamos de internet. Si ese feed
# estuviera comprometido o mal configurado, podría contener URLs apuntando
# a cualquier sitio, incluidos equipos de TU RED INTERNA (ataque SSRF).
# Comprobando el dominio antes de pedir nada, Home Assistant solo hablará
# con los cuatro proveedores que de verdad aparecen en el feed real.
#
# A diferencia del feed de la DGT (todo HTTPS), varias URLs de este feed
# son HTTP plano (p.ej. las de mct.gencat.cat y bcn.cat). Por eso
# is_allowed_image_url() en api.py acepta ambos esquemas, pero SIEMPRE
# exige que el dominio esté en esta lista.
ALLOWED_IMAGE_DOMAINS = (
    "gencat.cat",
    "bcn.cat",
    "terrassa.cat",
    "mobilitat.ad",
    "statics.3cat.cat",
)

# Clave usada dentro de config_entry.data para guardar la lista de cámaras
# que el usuario ha seleccionado.
CONF_CAMERAS = "cameras"

# ---------------------------------------------------------------------------
# Control de frecuencia de peticiones
# ---------------------------------------------------------------------------

# Intervalo mínimo entre descargas reales de una imagen para una misma
# cámara. A diferencia de la integración de la DGT (10 minutos, valor
# elegido tras observar su servidor), no hemos podido verificar cada
# cuánto se regenera de verdad cada imagen del SCT — algunas son GIF
# servidos por un "RenderService" que probablemente se actualiza con
# bastante frecuencia. Se elige un valor intermedio, prudente por defecto;
# si compruebas que tu instalación tolera un refresco más rápido sin
# problemas, puedes bajarlo editando esta constante.
MIN_SECONDS_BETWEEN_IMAGE_FETCH = 120
FRAME_INTERVAL_SECONDS = float(MIN_SECONDS_BETWEEN_IMAGE_FETCH)

# --- Backoff: qué hacer cuando el servidor de origen nos falla -------------
#
# Tras cada fallo consecutivo se espera cada vez más antes de volver a
# intentarlo: 60s, 120s, 240s, 480s... hasta un tope de 1 hora. Evita que
# un servidor caído (o que nos esté limitando) provoque una tormenta de
# reintentos por nuestra parte.
BACKOFF_INITIAL_SECONDS = 60
BACKOFF_MAX_SECONDS = 3600
BACKOFF_MULTIPLIER = 2

HTTP_TIMEOUT_SECONDS = 15
# El feed es mucho más ligero que el DATEX II de la DGT (unos cientos de
# cámaras en vez de miles), pero se deja el mismo margen conservador.
INVENTORY_TIMEOUT_SECONDS = 30

# --- Límites de tamaño (protección de memoria) ------------------------------
MAX_IMAGE_BYTES = 15 * 1024 * 1024  # 15 MB: muy por encima de una foto normal
MAX_INVENTORY_BYTES = 20 * 1024 * 1024  # 20 MB: margen amplio para el XML

# Cuánto tiempo reutilizamos el inventario ya descargado en lugar de volver
# a bajarlo. La lista de cámaras cambia muy poco.
INVENTORY_CACHE_SECONDS = 900  # 15 minutos

# ---------------------------------------------------------------------------
# Cabeceras HTTP
# ---------------------------------------------------------------------------

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

IMAGE_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Referer": "https://transit.gencat.cat/",
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
}

INVENTORY_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Referer": "https://transit.gencat.cat/",
    "Accept": "application/xml,text/xml;q=0.9,*/*;q=0.8",
}

THREECAT_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
}

INCIDENTS_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept": "application/json,text/json;q=0.9,*/*;q=0.8",
}

# --- Cómo comprobamos que lo recibido es de verdad una imagen --------------
#
# Igual que en dgt_traffic_cameras_ha: rechazamos de entrada los tipos que
# sabemos seguro que NO son una imagen, y para todo lo demás miramos los
# primeros bytes del fichero (su "firma"), que no se puede falsear con una
# cabecera mal puesta.
REJECTED_CONTENT_TYPE_PREFIXES = (
    "text/",
    "application/json",
    "application/xml",
    "application/xhtml",
)

IMAGE_MAGIC_BYTES = (
    b"\xff\xd8\xff",  # JPEG
    b"\x89PNG\r\n\x1a\n",  # PNG
    b"GIF87a",  # GIF — el formato más habitual en este feed
    b"GIF89a",  # GIF
    b"BM",  # BMP
)
IMAGE_MAGIC_OFFSET_CHECKS = (
    (0, b"RIFF", 8, b"WEBP"),
    (4, b"ftyp", 8, b"avif"),
)

# NOTA sobre la imagen de "no disponible": la integración de referencia de
# la DGT reconoce (por hash exacto y por hash perceptual) la imagen fija
# que la DGT sirve cuando una cámara está averiada, para no guardarla en
# caché como si fuera una foto real. No se ha podido verificar si el SCT
# tiene un equivalente (un único fichero fijo de "cámara no disponible")
# para ninguna de las cuatro fuentes de este feed, así que esa comprobación
# se omite aquí a propósito en vez de inventar unos hashes sin contrastar.
# Si detectas ese patrón en tu instalación, es el sitio natural para
# añadirlo.
