"""Pruebas de la campaña de calibración.

La clase importante es TestInvariantesDetectan: una invariante que nunca falla es
decoración. Cada prueba rompe un dato a propósito y exige que se cace.
"""

import copy
import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from calibracion import veredicto as V
from calibracion.corpus import HOSTILES, Servidores

BASE = {
    "sitio": "x.com",
    "puntaje_global": 6.6,
    "ponderaciones": {"eje1": 0.5, "eje2": 0.5},
    "cobertura_paginas": {"auditadas": 15, "descubiertas": 47, "muestra_parcial": True},
    "ejes": {
        "eje1": {
            "puntaje": 4.6,
            "indicadores": {
                "eje1.uno": {"valor": 3, "estado": "mal", "metodo": "m", "desde": "d",
                             "detalle": "medido en 15 de 15 páginas"},
            },
            "hallazgos": [{"titulo": "algo", "esfuerzo_horas": "NO_MEDIDO"}],
        },
        "eje2": {"puntaje": 8.6, "indicadores": {}, "hallazgos": []},
    },
}


class TestInvariantesDetectan(unittest.TestCase):
    """Cada invariante, contra un dato roto a propósito."""

    def _roto(self, mutar):
        d = copy.deepcopy(BASE)
        mutar(d)
        return d

    def test_puntaje_global_fuera_de_rango(self):
        d = self._roto(lambda x: x.update(puntaje_global=42))
        self.assertTrue(V.inv_puntajes_en_rango(d))

    def test_puntaje_de_eje_negativo(self):
        d = self._roto(lambda x: x["ejes"]["eje1"].update(puntaje=-3))
        self.assertTrue(V.inv_puntajes_en_rango(d))

    def test_global_que_no_cuadra_con_los_ejes(self):
        d = self._roto(lambda x: x.update(puntaje_global=9.9))
        self.assertTrue(V.inv_global_es_la_suma_ponderada(d))

    def test_sin_ponderaciones_publicadas(self):
        d = self._roto(lambda x: x.pop("ponderaciones"))
        self.assertTrue(V.inv_global_es_la_suma_ponderada(d))

    def test_global_correcto_no_da_falso_positivo(self):
        self.assertEqual(V.inv_global_es_la_suma_ponderada(copy.deepcopy(BASE)), [])

    def test_no_medido_sin_motivo(self):
        d = self._roto(lambda x: x["ejes"]["eje1"]["indicadores"]["eje1.uno"].update(
            valor="NO_MEDIDO", estado="no_medido", motivo_no_medido=""))
        self.assertTrue(V.inv_no_medido_con_motivo(d))

    def test_no_medido_con_motivo_pasa(self):
        d = self._roto(lambda x: x["ejes"]["eje1"]["indicadores"]["eje1.uno"].update(
            valor="NO_MEDIDO", estado="no_medido", motivo_no_medido="el servidor no lo publica"))
        self.assertEqual(V.inv_no_medido_con_motivo(d), [])

    def test_indicador_sin_procedencia(self):
        for campo in ("metodo", "desde"):
            d = self._roto(lambda x,
                c=campo: x["ejes"]["eje1"]["indicadores"]["eje1.uno"].update({c: ""}))
            self.assertTrue(V.inv_todo_numero_con_procedencia(d), campo)

    def test_esfuerzo_con_numero(self):
        # La guardia del 22-ago, verificada sobre el producto terminado y no dentro del motor.
        d = self._roto(lambda x: x["ejes"]["eje1"]["hallazgos"][0].update(esfuerzo_horas=6))
        self.assertTrue(V.inv_esfuerzo_nunca_es_numero(d))

    def test_conteo_por_encima_de_la_muestra(self):
        # Es la forma automática de cazar el «15 de 15 descubiertas» que se arregló el 22-ago.
        d = self._roto(lambda x: x["ejes"]["eje1"]["indicadores"]["eje1.uno"].update(
            detalle="medido en 15 de 99 páginas"))
        self.assertTrue(V.inv_conteos_no_superan_la_muestra(d))

    def test_conteo_contra_las_descubiertas_es_legitimo(self):
        d = self._roto(lambda x: x["ejes"]["eje1"]["indicadores"]["eje1.uno"].update(
            detalle="se auditaron 15 de 47 páginas"))
        self.assertEqual(V.inv_conteos_no_superan_la_muestra(d), [])

    def test_parcialidad_no_declarada(self):
        self.assertTrue(V.inv_parcialidad_declarada(copy.deepcopy(BASE), "informe sin la palabra"))

    def test_parcialidad_declarada_pasa(self):
        self.assertEqual(
            V.inv_parcialidad_declarada(copy.deepcopy(BASE), "MUESTRA PARCIAL: 15 de 47"), [])

    def test_una_invariante_que_revienta_no_tumba_al_resto(self):
        import tempfile, shutil
        tmp = Path(tempfile.mkdtemp())
        try:
            (tmp / "resultados.json").write_text(json.dumps({"ejes": "esto no es un dict"}))
            r = V.revisar(tmp)
            self.assertEqual(r["estado"], "revisada")   # no lanzó
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestCasosHostiles(unittest.TestCase):
    """El andamio también se prueba: si el doble miente, la campaña entera miente."""

    @classmethod
    def setUpClass(cls):
        cls.srv = Servidores(puerto_base=8400).__enter__()
        time.sleep(0.4)

    @classmethod
    def tearDownClass(cls):
        cls.srv.__exit__()

    def _pedir(self, url):
        try:
            r = urllib.request.urlopen(url, timeout=10)
            return r.status, {k.lower(): v for k, v in r.headers.items()}
        except urllib.error.HTTPError as e:
            return e.code, {k.lower(): v for k, v in e.headers.items()}

    def _url(self, nombre):
        return dict(self.srv.urls())[nombre]

    def test_cabeceras_solo_en_404_reproduce_la_trampa(self):
        """Es el caso más valioso del corpus: si no reproduce, no prueba nada."""
        base = self._url("cabeceras-404")
        c200, h200 = self._pedir(base)
        c404, h404 = self._pedir(base + "no-existe-xyz/")
        self.assertEqual(c200, 200)
        self.assertEqual(c404, 404)
        self.assertNotIn("strict-transport-security", h200, "el 200 NO debe traer cabeceras")
        self.assertIn("strict-transport-security", h404, "el 404 SÍ debe traerlas")

    def test_caso_500(self):
        self.assertEqual(self._pedir(self._url("http500") + "rota/")[0], 500)
        self.assertEqual(self._pedir(self._url("http500"))[0], 200)

    def test_caso_redirecciones_llega_al_final(self):
        r = urllib.request.urlopen(self._url("redirecciones") + "a/", timeout=10)
        self.assertTrue(r.url.endswith("/final/"), r.url)

    def test_caso_sin_sitemap(self):
        self.assertEqual(self._pedir(self._url("sin-sitemap") + "sitemap.xml")[0], 404)
        self.assertEqual(self._pedir(self._url("sin-sitemap") + "robots.txt")[0], 404)

    def test_caso_charset_declara_latin1(self):
        _c, h = self._pedir(self._url("charset"))
        self.assertIn("iso-8859-1", h.get("content-type", "").lower())

    def test_los_demas_responden(self):
        for nombre in ("html-invalido", "lento"):
            self.assertEqual(self._pedir(self._url(nombre))[0], 200, nombre)

    def test_todos_los_hostiles_estan_levantados(self):
        self.assertEqual(len(self.srv.urls()), len(HOSTILES))



class TestOraculoSitemap(unittest.TestCase):
    """La trampa del <image:loc>, que el 24-ago casi me hace denunciar un bug inexistente.

    Los sitemaps de WooCommerce meten las fotos de cada producto como <image:loc>. Un
    parser que recorra el árbol entero buscando etiquetas 'loc' cuenta 131 "páginas"
    donde hay 47, y el oráculo acusa al motor de perderse los productos.
    """

    XML = (
        '<?xml version="1.0"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
        'xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">'
        '<url><loc>https://x.com/producto/uno/</loc>'
        '<image:image><image:loc>https://x.com/foto1.jpg</image:loc></image:image>'
        '<image:image><image:loc>https://x.com/foto2.jpg</image:loc></image:image></url>'
        '<url><loc>https://x.com/producto/dos/</loc>'
        '<image:image><image:loc>https://x.com/foto3.jpg</image:loc></image:image></url>'
        '</urlset>'
    )

    def test_no_cuenta_las_imagenes_como_paginas(self):
        from calibracion.oraculos import _xml_seguro, NS_SITEMAP
        raiz = _xml_seguro(self.XML)
        self.assertIsNotNone(raiz)
        paginas = set()
        for padre in raiz:
            if padre.tag.split("}")[-1] != "url":
                continue
            for hijo in padre:
                if hijo.tag == "{%s}loc" % NS_SITEMAP and (hijo.text or "").strip():
                    paginas.add(hijo.text.strip())
        self.assertEqual(len(paginas), 2, "contó imágenes como páginas")

    def test_el_recorrido_ingenuo_habria_contado_5(self):
        """Deja constancia de por qué el patrón ingenuo no sirve."""
        from calibracion.oraculos import _xml_seguro
        raiz = _xml_seguro(self.XML)
        ingenuo = [e for e in raiz.iter() if e.tag.endswith("}loc") and (e.text or "").strip()]
        self.assertEqual(len(ingenuo), 5, "2 páginas + 3 imágenes")


class TestOraculoXMLSeguro(unittest.TestCase):
    def test_rechaza_bombas_y_entidades(self):
        from calibracion.oraculos import _xml_seguro, TOPE_XML
        bomba = ('<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "aa"><!ENTITY b "&a;&a;">]>'
                 '<urlset><url><loc>&b;</loc></url></urlset>')
        xxe = ('<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
               '<urlset><url><loc>&x;</loc></url></urlset>')
        self.assertIsNone(_xml_seguro(bomba))
        self.assertIsNone(_xml_seguro(xxe))
        self.assertIsNone(_xml_seguro("<a/>" + "x" * (TOPE_XML + 1)))

    def test_acepta_un_sitemap_normal(self):
        from calibracion.oraculos import _xml_seguro
        bueno = ('<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                 '<url><loc>https://x.com/</loc></url></urlset>')
        self.assertIsNotNone(_xml_seguro(bueno))


class TestOraculoTLS(unittest.TestCase):
    def test_no_lee_la_linea_enganosa_de_libressl(self):
        """LibreSSL imprime 'New, TLSv1/SSLv3' hasta en conexiones TLS 1.3.

        Leer esa línea daría 'protocolo obsoleto' en sitios sanos: un falso positivo
        camino de un informe de cliente.
        """
        import re
        salida = ("New, TLSv1/SSLv3, Cipher is AEAD-CHACHA20-POLY1305-SHA256\n"
                  "SSL-Session:\n    Protocol  : TLSv1.3\n    Cipher    : x\n")
        encontrado = None
        for linea in salida.splitlines():
            m = re.match(r"\s*Protocol\s*:\s*(\S+)", linea)
            if m:
                encontrado = m.group(1)
                break
        self.assertEqual(encontrado, "TLSv1.3")


if __name__ == "__main__":
    unittest.main()


class TestEstilo(unittest.TestCase):
    """Guardián de estilo. Sin dependencias: el proyecto no tiene linter y no lo va a
    tener, así que el límite se sostiene con una prueba."""

    LIMITE = 100

    def test_ninguna_linea_pasa_de_100_columnas(self):
        raiz = Path(__file__).resolve().parent.parent
        largas = []
        for f in sorted(raiz.rglob("*.py")):
            if any(p in f.parts for p in ("__pycache__", ".git")):
                continue
            for n, linea in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                if len(linea) > self.LIMITE:
                    largas.append("%s:%d (%d columnas)" % (f.relative_to(raiz), n, len(linea)))
        self.assertEqual(largas, [], "líneas demasiado largas:\n  " + "\n  ".join(largas))
