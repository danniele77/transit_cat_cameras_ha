# Càmeres de trànsit (Servei Català de Trànsit) — integración para Home Assistant

[![Añadir a HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=danniele77&repository=transit_cat_cameras_ha&category=integration)

Integración `custom_components` para Home Assistant que añade las cámaras
de tráfico del **Servei Català de Trànsit (SCT)** como entidades `camera.*`,
con selector por carretera. Es el equivalente para cataluña de
[jonathanathe/dgt_traffic_cameras_ha](https://github.com/jonathanathe/dgt_traffic_cameras_ha),
construida siguiendo la misma arquitectura (config_flow, caché en memoria
del inventario, backoff ante fallos, validación de que la respuesta es de
verdad una imagen, allowlist de dominios contra SSRF) — necesaria porque
el feed de la DGT excluye explícitamente Cataluña (competencia transferida
a la Generalitat).

## Por qué existe

El feed nacional de la DGT (`nap.dgt.es`) no incluye cámaras de Cataluña ni
del País Vasco. El SCT, organismo de la Generalitat, publica su propio feed
abierto (WFS/GML, sin API key) con las cámaras de las carreteras catalanas:

- **Feed usado**: `http://www.gencat.cat/transit/opendata/cameres.xml`
- **Ficha del dataset**: <https://analisi.transparenciacatalunya.cat/d/3tzz-6b9y>

El feed mezcla en realidad **cuatro fuentes** distintas (campo `<cite:font>`):

| Fuente     | Quién la publica                              | Dominio de imagen   |
|------------|-------------------------------------------------|----------------------|
| `SCT`      | Servei Català de Trànsit (Generalitat)         | `mct.gencat.cat`    |
| `Terrassa` | Ajuntament de Terrassa                         | `emap.terrassa.cat` |
| `IMI`      | Institut Municipal d'Informàtica (Barcelona)   | `bcn.cat`            |
| `Andorra`  | Govern d'Andorra (vies frontereres)            | `mobilitat.ad`       |

Las cuatro se pueden añadir; simplemente no selecciones las de Andorra si
solo quieres cobertura catalana.

También se incorporan las imágenes públicas del catálogo de cámaras de
[3Cat](https://www.3cat.cat/3catinfo/el-temps/cameres/). El catálogo incluye
webcams urbanas, costeras y de montaña, entre ellas varias estaciones de
esquí. Aparecen en el selector como **3Cat · Webcams** y **3Cat · Esquí**.
Las imágenes se leen directamente desde el dominio público de estáticos de
3Cat (`statics.3cat.cat`); no se descarga ni se reproduce ningún vídeo.

## Incidencias y carteles de carretera

La página de [incidencias viarias del Servei Català de Trànsit](https://cit.transit.gencat.cat/cit/AppJava/views/incidents.xhtml)
publica un servicio JSON con retenciones, obras, averías, meteorología y
otras afectaciones. La integración lo incorpora como sensores que se
actualizan cada cinco minutos:

- **Incidencias viarias**: total activo y conteo por carretera, con las 20
   incidencias más recientes.
- **Incidencias `<carretera>`**: un sensor por carretera con el detalle de
   sus incidencias, incluyendo km, sentido, municipio, causa, nivel y destino.

Estos sensores permiten crear automatizaciones o tarjetas para una ruta
concreta, por ejemplo `B-10`, `C-32` o `AP-7`. La fuente publica incidencias
estructuradas, no la posición GPS ni el texto de paneles variables; por eso
se muestran como información vial y no como carteles geolocalizados en el
mapa.

## Instalación

### HACS

Pulsa el botón de arriba para abrir esta integración directamente en HACS y
selecciona **Descargar**. Después reinicia Home Assistant y ve a
**Ajustes → Dispositivos y servicios → Añadir integración** para buscar
"Càmeres de trànsit".

Si el botón no está disponible, abre HACS → **Integraciones** → menú de tres
puntos → **Repositorios personalizados**, añade
`danniele77/transit_cat_cameras_ha` como integración y pulsa **Añadir**.

### Instalación manual

1. Copia la carpeta `custom_components/transit_cat_cameras` dentro de la
   carpeta `custom_components` de tu configuración de Home Assistant
   (créala si no existe: `<config>/custom_components/transit_cat_cameras`).
2. Reinicia Home Assistant.
3. Ve a **Ajustes → Dispositivos y servicios → Añadir integración** y busca
   "Càmeres de trànsit" (o "SCT").
4. Elige una carretera del SCT, **3Cat · Webcams** o **3Cat · Esquí** y marca
   las cámaras concretas que quieras añadir.

Puedes repetir el proceso (o usar el botón **Configurar** de la entrada ya
creada → *Añadir cámaras*) para ir añadiendo más carreteras o más cámaras
con el tiempo. Para incorporar las cámaras 3Cat en una instalación existente,
no hace falta eliminar la entrada: abre **Configurar → Añadir cámaras** y
selecciona una de las categorías 3Cat. Cada carretera o categoría crea su
propia entrada, agrupando sus cámaras bajo un mismo dispositivo en Home
Assistant.

## Cómo protege al servidor de origen

Igual que la integración de referencia de la DGT:

1. **Caché normal**: una foto nueva como mucho cada 120 segundos por
   cámara (`MIN_SECONDS_BETWEEN_IMAGE_FETCH` en `const.py`), sin importar
   cada cuánto refresque la tarjeta en el panel.
2. **Caché condicional** (ETag / Last-Modified): antes de descargar la foto
   entera, se pregunta al servidor si ha cambiado.
3. **Backoff ante fallos**: 60s, 120s, 240s... hasta 1 hora, para no
   convertir una caída del servidor en una tormenta de reintentos.
4. **Validación de contenido**: se rechaza cualquier respuesta que no
   empiece por la firma binaria de un formato de imagen real (evita
   guardar en caché una página de error HTML servida con código 200).
5. **Allowlist de dominios**: solo se descargan imágenes de los cuatro
   proveedores SCT y del dominio estático de 3Cat, aunque la URL venga de un
   feed remoto (protección SSRF).

### Diferencia deliberada respecto a la integración de la DGT

- El feed del SCT mezcla HTTP y HTTPS según el proveedor (la DGT usa
  siempre HTTPS), así que aquí se aceptan ambos esquemas — pero el
  dominio se sigue comprobando siempre.
- La DGT tiene una imagen fija de "cámara no disponible" que su
  integración reconoce por hash exacto y por hash perceptual. No se ha
  podido verificar si el SCT tiene un equivalente para ninguna de sus
  cuatro fuentes, así que esa comprobación **no** está incluida — se ha
  preferido omitirla a inventar unos hashes sin contrastar. Si detectas
  ese patrón en tu instalación, `camera.py` es el sitio natural para
  añadirlo.
- No hay plataforma de paneles de mensaje variable (PMV): no se ha
  localizado un feed público del SCT equivalente al de la DGT para eso.

## Tests

```bash
python3 -m unittest discover tests
```

Prueba el parseo del XML (`_parse_camera_inventory`) contra un fixture
construido a partir de una descarga real del feed, cubriendo las cuatro
fuentes (SCT, Terrassa, IMI, Andorra), y prueba el parseo HTML del catálogo
de 3Cat y la clasificación de sus cámaras de esquí. También se valida
`is_allowed_image_url` contra los proveedores reales y contra intentos de
bypass del allowlist. Los tests corren sin tener Home Assistant instalado
(mismo truco de stubs que usa el repo de referencia en `tests/_load.py`).

## Estructura

```
custom_components/transit_cat_cameras/
├── __init__.py        # setup/unload, recarga solo si cambia la lista de cámaras
├── api.py              # descarga + parseo del feed WFS/GML, caché, allowlist
├── camera.py            # entidad Camera: caché, backoff, validación de imagen
├── config_flow.py       # selector carretera -> cámaras, + añadir/quitar
├── const.py              # toda la configuración (URLs, límites, cabeceras...)
├── manifest.json
├── strings.json          # catalán (idioma base)
└── translations/
    ├── ca.json
    └── es.json
tests/
├── _load.py               # carga aislada de módulos sin Home Assistant instalado
├── fixtures/cameres_sample.xml
└── test_api.py
```

## Notas

- Son fotogramas fijos que el propio servidor de origen regenera, no vídeo
  en directo: no esperes fluidez.
- Si una carretera no aparece en el selector, es que ese día no tiene
  ninguna cámara en el feed — la lista de carreteras se calcula a partir
  del propio inventario descargado, no está codificada a mano.
