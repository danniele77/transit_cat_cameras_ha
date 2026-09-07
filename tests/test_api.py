"""Tests del parseo de XML en api.py, contra un fixture del feed real del SCT."""

from __future__ import annotations

import unittest

from ._load import fixture_bytes, load

api = load("api")


class TestParseCameraInventory(unittest.TestCase):
    """_parse_camera_inventory contra tests/fixtures/cameres_sample.xml.

    Fixture construido a partir de una descarga real (2026-09-07) de
    http://www.gencat.cat/transit/opendata/cameres.xml, con las cuatro
    variantes de <cite:font> presentes en el feed real (SCT, Terrassa,
    Andorra, IMI) y una entrada duplicada (mismo <cite:link>) tal y como
    aparece en el feed de verdad.
    """

    def setUp(self) -> None:
        xml_bytes = fixture_bytes("cameres_sample.xml")
        self.cameras = api._parse_camera_inventory(xml_bytes)
        self.by_id = {c.device_id: c for c in self.cameras}

    def test_deduplica_y_parsea_cuatro_camaras(self) -> None:
        # El fixture trae 5 <gml:featureMember>, pero dos comparten
        # exactamente el mismo <cite:link> (duplicado real del feed).
        self.assertEqual(len(self.cameras), 4)

    def test_camara_sct_render_service(self) -> None:
        cam = self.by_id["sct_nc87"]
        self.assertEqual(cam.source, "SCT")
        self.assertEqual(cam.road_name, "C-58")
        self.assertEqual(cam.municipality, "Nus Trinitat")
        self.assertEqual(cam.kilometer_point, "0.50")
        self.assertAlmostEqual(cam.latitude, 41.45989301)
        self.assertAlmostEqual(cam.longitude, 2.1849528)
        self.assertEqual(
            cam.image_url,
            "http://mct.gencat.cat/mct2bo/RenderService?sctidcam=nc87.gif",
        )

    def test_camara_terrassa_limpia_espacios_y_saltos_de_linea(self) -> None:
        cam = self.by_id["terrassa_cam01"]
        self.assertEqual(cam.source, "Terrassa")
        # El feed real trae un \r suelto dentro de <cite:link>; debe quedar
        # limpio tras el parseo.
        self.assertEqual(
            cam.image_url, "https://emap.terrassa.cat/it_terrassa/cam01.jpeg?a=1"
        )

    def test_camara_andorra_se_parsea_igual_que_las_demas(self) -> None:
        cam = self.by_id["andorra_stacoloma"]
        self.assertEqual(cam.source, "Andorra")
        self.assertEqual(cam.municipality, "Andorra")

    def test_camara_imi_barcelona(self) -> None:
        matches = [c for c in self.cameras if c.device_id.startswith("imi_")]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].municipality, "Barcelona")

    def test_display_name_no_rompe_sin_pk(self) -> None:
        for cam in self.cameras:
            self.assertTrue(cam.display_name)


class TestParseThreeCatCameras(unittest.TestCase):
    def test_parsea_webcams_y_clasifica_esqui(self) -> None:
        html = b'''
        <img alt="La Molina 1.700" src="https://statics.3cat.cat/meteo/beauties/v1/img/2/6/20714962.jpg">
        <img alt="Barcelona, Port Olimpic" src="https://statics.3cat.cat/meteo/beauties/v1/img/2/4/20714942.jpg">
        '''
        cameras = api._parse_threecat_cameras(html)
        self.assertEqual(len(cameras), 2)
        self.assertEqual(cameras[0].road_name, "3Cat · Esquí")
        self.assertEqual(cameras[1].road_name, "3Cat · Webcams")


class TestIsAllowedImageUrl(unittest.TestCase):
    def test_genera_jpg_como_fallback_para_sct(self) -> None:
        self.assertEqual(
            api.image_url_variants(
                "http://mct.gencat.cat/mct2bo/RenderService?sctidcam=nc87.gif"
            ),
            (
                "http://mct.gencat.cat/mct2bo/RenderService?sctidcam=nc87.jpg",
                "http://mct.gencat.cat/mct2bo/RenderService?sctidcam=nc87.gif",
            ),
        )

    def test_no_cambia_urls_de_otros_proveedores(self) -> None:
        url = "https://emap.terrassa.cat/it_terrassa/cam01.jpeg?a=1"
        self.assertEqual(api.image_url_variants(url), (url,))

    def test_acepta_los_cuatro_dominios_del_feed_con_http_o_https(self) -> None:
        urls = [
            "http://mct.gencat.cat/mct2bo/RenderService?sctidcam=nc87.gif",
            "https://emap.terrassa.cat/it_terrassa/cam01.jpeg",
            "http://www.bcn.cat/transit/imatges/PlAntonioLopez.gif",
            "https://app.mobilitat.ad/gifs/stacoloma.gif",
        ]
        for url in urls:
            self.assertTrue(api.is_allowed_image_url(url), url)

    def test_rechaza_dominios_fuera_de_la_allowlist(self) -> None:
        self.assertFalse(
            api.is_allowed_image_url("https://evil.example.com/gencat.cat/foo.gif")
        )
        self.assertFalse(
            api.is_allowed_image_url("https://notgencat.cat/foo.gif")
        )

    def test_rechaza_esquemas_no_http(self) -> None:
        self.assertFalse(api.is_allowed_image_url("ftp://gencat.cat/foo.gif"))
        self.assertFalse(api.is_allowed_image_url("file:///etc/passwd"))

    def test_acepta_imagen_estatica_de_3cat(self) -> None:
        self.assertTrue(
            api.is_allowed_image_url(
                "https://statics.3cat.cat/meteo/beauties/v1/img/2/6/20714962.jpg"
            )
        )


if __name__ == "__main__":
    unittest.main()
