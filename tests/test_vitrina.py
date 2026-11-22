"""Pruebas de la vitrina. Solo stdlib: unittest, sin pytest y sin dependencias.

    python3 -m unittest discover -s tests -v

Se monta un almacén falso en un directorio temporal y se levanta el servidor de verdad
en un puerto efímero. Probar el handler por dentro dejaría sin cubrir justo lo que
importa: que la auth y el traversal se comporten sobre HTTP real.
"""

import base64
import json
import shutil
import socket
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from vitrina import almacen, web
from vitrina.indice import Indice, ficha

CRED = "vsl:secreta"


def _resultados(sitio, puntaje=6.6, auditadas=15, descubiertas=47, parcial=True):
    return {
        "version_esquema": "1.0",
        "sitio": sitio,
        "url": "https://" + sitio,
        "cliente": "Cliente " + sitio,
        "fecha_iso": "2026-08-22T17:00:40-04:00",
        "puntaje_global": puntaje,
        "duracion_segundos": 289,
        "cobertura_paginas": {
            "auditadas": auditadas,
            "descubiertas": descubiertas,
            "muestra_parcial": parcial,
        },
        "ejes": {
            "eje1": {"id": "eje1", "nombre": "Performance", "puntaje": 4.7, "hallazgos": [1, 2, 3]},
            "eje6": {"id": "eje6", "nombre": "Accesibilidad", "puntaje": 4.4, "hallazgos": [1]},
        },
    }


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.raiz = self.tmp / "almacen"
        (self.raiz / "proyecto" / "docs" / "evidencia").mkdir(parents=True)

        self.buena = self.raiz / "proyecto" / "docs" / "evidencia" / "sitio.com-2026-08-22"
        self.buena.mkdir()
        (self.buena / "resultados.json").write_text(
            json.dumps(_resultados("sitio.com")), encoding="utf-8"
        )
        (self.buena / "informe.html").write_text("<h1>informe</h1>", encoding="utf-8")
        (self.buena / "informe.pdf").write_bytes(b"%PDF-1.4 falso")
        (self.buena / "AUDITORIA-sitio.com-2026-08-22.md").write_text("# md", encoding="utf-8")
        # Evidencia cruda: existe en toda auditoría real y NO se debe poder servir.
        (self.buena / "eje1-evidencia").mkdir()
        (self.buena / "eje1-evidencia" / "traza.json").write_text("{}", encoding="utf-8")

        self.rota = self.raiz / "proyecto" / "docs" / "evidencia" / "roto.com-2026-08-01"
        self.rota.mkdir()
        (self.rota / "resultados.json").write_text("{esto no es json", encoding="utf-8")

        # Directorio sin resultados.json: no es una auditoría.
        (self.raiz / "proyecto" / "docs" / "evidencia" / "basura").mkdir()

        self.secreto = self.tmp / "secreto.txt"
        self.secreto.write_text("clave-privada", encoding="utf-8")

        self.indice = Indice(almacen.raices_desde(str(self.raiz)))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestIndice(Base):
    def test_encuentra_solo_directorios_con_resultados(self):
        ids = [f["id"] for f in self.indice.fichas()]
        self.assertIn("sitio.com-2026-08-22", ids)
        self.assertIn("roto.com-2026-08-01", ids)
        self.assertNotIn("basura", ids)

    def test_json_corrupto_sale_ilegible_sin_tumbar_el_indice(self):
        f = self.indice.por_id("roto.com-2026-08-01")
        self.assertTrue(f["ilegible"])
        self.assertTrue(f["motivo"])
        # Y la buena sigue apareciendo: una rota no se lleva puesto el índice.
        self.assertFalse(self.indice.por_id("sitio.com-2026-08-22")["ilegible"])

    def test_muestra_parcial_llega_a_la_ficha(self):
        f = self.indice.por_id("sitio.com-2026-08-22")
        self.assertTrue(f["cobertura"]["parcial"])
        self.assertEqual(f["cobertura"]["auditadas"], 15)
        self.assertEqual(f["cobertura"]["descubiertas"], 47)

    def test_la_parcialidad_se_declara_en_el_html(self):
        html = web.render_indice(self.indice.fichas(), [])
        self.assertIn("muestra parcial", html)
        self.assertIn("15 de 47", html)

    def test_no_inventa_puntaje_cuando_falta(self):
        d = self.raiz / "sinpuntaje-2026-01-01"
        d.mkdir()
        datos = _resultados("x.com")
        del datos["puntaje_global"]
        (d / "resultados.json").write_text(json.dumps(datos), encoding="utf-8")
        f = ficha("sinpuntaje-2026-01-01", d)
        self.assertIsNone(f["puntaje_global"])
        self.assertIn("—", web._ficha_html(f))

    def test_estado_por_defecto_listo_y_respeta_estado_json(self):
        self.assertEqual(self.indice.por_id("sitio.com-2026-08-22")["estado"], "listo")
        (self.buena / "estado.json").write_text('{"estado": "corriendo"}', encoding="utf-8")
        self.assertEqual(ficha("x", self.buena)["estado"], "corriendo")

    def test_el_indice_se_rehace_cuando_cambia_el_disco(self):
        self.assertEqual(len(self.indice.fichas()), 2)
        nueva = self.raiz / "nueva.com-2026-08-23"
        nueva.mkdir()
        (nueva / "resultados.json").write_text(
            json.dumps(_resultados("nueva.com")), encoding="utf-8"
        )
        self.assertEqual(len(self.indice.fichas()), 3)


class TestRutaSegura(Base):
    def test_sirve_lo_permitido(self):
        for nombre in ("informe.html", "informe.pdf", "resultados.json",
                       "AUDITORIA-sitio.com-2026-08-22.md"):
            self.assertIsNotNone(almacen.ruta_segura(self.buena, nombre), nombre)

    def test_bloquea_traversal(self):
        for intento in ("../../../secreto.txt", "..%2f..%2fsecreto.txt", "../secreto.txt",
                        "/etc/passwd", "..", "."):
            self.assertIsNone(almacen.ruta_segura(self.buena, intento), intento)

    def test_bloquea_evidencia_cruda(self):
        # No está en la lista blanca: la evidencia trae HTML del sitio del cliente.
        self.assertIsNone(almacen.ruta_segura(self.buena, "eje1-evidencia/traza.json"))

    def test_bloquea_symlink_que_apunta_afuera(self):
        # Esta es la que hace decorativa a la comprobación ingenua: el nombre está en la
        # lista blanca y no tiene "..", pero el archivo real vive fuera de la auditoría.
        enlace = self.buena / "resultados.json"
        enlace.unlink()
        enlace.symlink_to(self.secreto)
        self.assertIsNone(almacen.ruta_segura(self.buena, "resultados.json"))


class TestHTTP(Base):
    def setUp(self):
        super().setUp()
        handler = web.constructor(self.indice, CRED, registrar=lambda m: None)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.puerto = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def _pedir(self, camino, cred=CRED):
        req = Request("http://127.0.0.1:{}{}".format(self.puerto, camino))
        if cred:
            req.add_header(
                "Authorization",
                "Basic " + base64.b64encode(cred.encode()).decode(),
            )
        return urlopen(req, timeout=10)

    def test_sin_clave_401(self):
        with self.assertRaises(HTTPError) as e:
            self._pedir("/", cred=None)
        self.assertEqual(e.exception.code, 401)

    def test_clave_mala_401(self):
        with self.assertRaises(HTTPError) as e:
            self._pedir("/", cred="vsl:equivocada")
        self.assertEqual(e.exception.code, 401)

    def test_salud_no_pide_clave(self):
        r = self._pedir("/salud", cred=None)
        self.assertEqual(r.status, 200)
        self.assertEqual(r.read().strip(), b"ok")

    def test_indice_con_clave(self):
        r = self._pedir("/")
        cuerpo = r.read().decode("utf-8")
        self.assertEqual(r.status, 200)
        self.assertIn("sitio.com", cuerpo)
        self.assertIn("muestra parcial", cuerpo)

    def test_informe_en_la_raiz_de_la_auditoria(self):
        r = self._pedir("/a/sitio.com-2026-08-22/")
        self.assertEqual(r.status, 200)
        self.assertIn(b"<h1>informe</h1>", r.read())

    def test_pdf_y_json(self):
        self.assertEqual(self._pedir("/a/sitio.com-2026-08-22/informe.pdf").status, 200)
        self.assertEqual(self._pedir("/a/sitio.com-2026-08-22/resultados.json").status, 200)

    def test_api_indice(self):
        r = self._pedir("/api/indice.json")
        datos = json.loads(r.read().decode("utf-8"))
        self.assertEqual(len(datos["auditorias"]), 2)

    def test_traversal_por_http_da_403(self):
        with self.assertRaises(HTTPError) as e:
            self._pedir("/a/sitio.com-2026-08-22/../../../../etc/passwd")
        self.assertIn(e.exception.code, (403, 404))

    def test_traversal_con_socket_crudo_no_filtra(self):
        """Guardiana. El test de arriba pasa aunque el servidor sea inseguro: urlopen
        normaliza los '..' antes de mandar el pedido, así que nunca llegan al handler.
        Esta prueba habla HTTP a mano para que el traversal llegue de verdad."""
        auth = base64.b64encode(CRED.encode()).decode()

        def crudo(camino):
            s = socket.create_connection(("127.0.0.1", self.puerto), 10)
            s.sendall(
                ("GET {} HTTP/1.1\r\nHost: x\r\nAuthorization: Basic {}\r\n"
                 "Connection: close\r\n\r\n").format(camino, auth).encode()
            )
            datos = b""
            while True:
                trozo = s.recv(4096)
                if not trozo:
                    break
                datos += trozo
            s.close()
            return datos

        base = "/a/sitio.com-2026-08-22/"
        for cola in ("../../secreto.txt", "..%2f..%2fsecreto.txt", "....//secreto.txt",
                     "../../../../etc/passwd"):
            r = crudo(base + cola)
            self.assertIn(b"403", r.split(b"\r\n")[0], cola)
            self.assertNotIn(b"clave-privada", r, cola)
        # Y lo legítimo sigue funcionando: una guardia que bloquea todo no sirve.
        self.assertIn(b"200", crudo(base + "informe.pdf").split(b"\r\n")[0])

    def test_auditoria_inexistente_404(self):
        with self.assertRaises(HTTPError) as e:
            self._pedir("/a/no-existe/")
        self.assertEqual(e.exception.code, 404)


if __name__ == "__main__":
    unittest.main()


class TestFecha(unittest.TestCase):
    def test_mes_en_espanol_sin_depender_del_locale(self):
        # launchd arranca con locale C: strftime("%b") daría "Aug".
        self.assertEqual(web._fecha_legible("2026-08-22T17:00:40-04:00"), "22 ago 2026, 17:00")

    def test_fecha_invalida_no_revienta(self):
        self.assertEqual(web._fecha_legible("no-es-fecha"), "no-es-fecha")
        self.assertEqual(web._fecha_legible(None), "sin fecha")
