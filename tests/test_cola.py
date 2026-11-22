"""Pruebas del motor (fase 2): validación de URL, cola serial y quién puede lanzar.

    python3 -m unittest discover -s tests -t .

No se corre `auditar.sh` de verdad en ninguna prueba: tarda cinco minutos y sale a
internet. Se sustituye por un comando de juguete donde hace falta.
"""

import json
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

from vitrina import cola as mod_cola
from vitrina import web

# Tope de la cuota que usan las pruebas de HTTP. Pequeño a propósito: agotarlo tiene que
# ser barato.
TOPE_CUOTA = 3
from vitrina.cola import Cola, escribir_estado, leer_estado, normalizar_url


class TestNormalizarURL(unittest.TestCase):
    def test_acepta_lo_razonable(self):
        casos = [
            ("tienda.example", "https://tienda.example", "tienda.example"),
            ("https://tienda.example", "https://tienda.example", "tienda.example"),
            ("http://sub.dominio.co.uk/x?y=1", "http://sub.dominio.co.uk/x?y=1",
             "sub.dominio.co.uk"),
            ("www.otra.example", "https://www.otra.example", "otra.example"),
        ]
        for crudo, url, sitio in casos:
            self.assertEqual(normalizar_url(crudo), (url, sitio), crudo)

    def test_rechaza_lo_peligroso(self):
        # El sitio se usa para armar una ruta en disco, asi que un host con '/' o '..'
        # escribiria fuera de la casa. Que no haya shell no cubre eso.
        malos = [
            "", "   ", "ftp://x.com", "file:///etc/passwd", "javascript:alert(1)",
            "../../etc/passwd", "http://", "https:// espacio.com",
            "http://usuario:clave@sitio.com", "sinpunto", "http://-guion.com",
            "x" * 400,
        ]
        for m in malos:
            with self.assertRaises(ValueError, msg=m):
                normalizar_url(m)

    def test_no_se_cuela_metacaracter_de_shell(self):
        for m in ["sitio.com; rm -rf /", "sitio.com`whoami`", "sitio.com$(id)", "a|b.com"]:
            with self.assertRaises(ValueError, msg=m):
                normalizar_url(m)


class TestCola(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cola = Cola(self.tmp / "casa", registrar=lambda m: None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_encolar_crea_directorio_y_estado(self):
        ident = self.cola.encolar("tienda.example", "Cliente Ejemplo")
        d = self.cola.base / ident
        self.assertTrue(d.is_dir())
        e = leer_estado(d)
        self.assertEqual(e["estado"], "en_cola")
        self.assertEqual(e["url"], "https://tienda.example")
        self.assertEqual(e["cliente"], "Cliente Ejemplo")

    def test_dos_del_mismo_sitio_el_mismo_dia_no_se_pisan(self):
        a = self.cola.encolar("sitio.com")
        b = self.cola.encolar("sitio.com")
        self.assertNotEqual(a, b)
        self.assertTrue((self.cola.base / a).is_dir())
        self.assertTrue((self.cola.base / b).is_dir())

    def test_url_invalida_no_deja_basura(self):
        with self.assertRaises(ValueError):
            self.cola.encolar("no-es-una-url")
        self.assertEqual(list(self.cola.base.glob("*")), [])

    def test_sanear_marca_interrumpido_lo_que_quedo_colgado(self):
        ident = self.cola.encolar("sitio.com")
        d = self.cola.base / ident
        escribir_estado(d, estado="corriendo")
        # Simula el reinicio del M4: el obrero murio, el estado.json no.
        Cola(self.cola.base, registrar=lambda m: None).sanear()
        self.assertEqual(leer_estado(d)["estado"], "interrumpido")

    def test_escribir_estado_es_atomico(self):
        ident = self.cola.encolar("sitio.com")
        d = self.cola.base / ident
        fallos = []

        def escritor():
            for i in range(60):
                escribir_estado(d, paso="paso %d" % i)

        def lector():
            for _ in range(200):
                if leer_estado(d) is None:  # un JSON a medio escribir devolveria None
                    fallos.append("lectura de un estado.json incompleto")

        hilos = [threading.Thread(target=escritor), threading.Thread(target=lector)]
        [h.start() for h in hilos]
        [h.join() for h in hilos]
        self.assertEqual(fallos, [])

    def test_la_fila_es_serial(self):
        # Es la razon de ser de la cola: dos auditorias a la vez no son mas lentas,
        # son falsas, porque el eje 1 mide la maquina en vez del sitio.
        for i in range(3):
            self.cola.encolar("sitio%d.com" % i)
        s = self.cola.situacion()
        self.assertIsNone(s["corriendo"])
        self.assertEqual(len(s["en_cola"]), 3)


class TestQuienPuedeLanzar(unittest.TestCase):
    def test_trafico_del_tunel_se_detecta_por_cabecera(self):
        # No se puede mirar la IP: cloudflared se conecta a 127.0.0.1 y todo el trafico
        # del tunel llega como local.
        self.assertTrue(web.desde_internet({"CF-Ray": "8abc123"}))
        self.assertTrue(web.desde_internet({"CF-Connecting-IP": "203.0.113.7"}))
        self.assertTrue(web.desde_internet({"CF-IPCountry": "SV"}))

    def test_trafico_local_no(self):
        self.assertFalse(web.desde_internet({}))
        self.assertFalse(web.desde_internet({"Host": "127.0.0.1:8096"}))
        self.assertFalse(web.desde_internet({"User-Agent": "curl/8"}))

    def test_el_formulario_se_dibuja_para_todos(self):
        """Cambió el 24-ago: antes se escondía a quien llegaba por internet. Ahora se
        dibuja para todos, y sólo desaparece cuando la cuota de la hora se agotó."""
        con = web.render_indice([], [], puede_lanzar=True)
        sin = web.render_indice([], [], puede_lanzar=False, motivo="cuota")
        self.assertIn('action="/auditar"', con)
        self.assertNotIn('action="/auditar"', sin)
        self.assertIn("tope de auditor", sin)

    def test_el_motivo_no_se_inventa(self):
        """Con el motor caído la página decía "se alcanzó el tope de auditorías por
        hora": mandaba a esperar por un tope que no existe y escondía que el motor
        estaba muerto. El motivo lo pasa el handler, la vista no lo deduce."""
        sin_motor = web.render_indice([], [], puede_lanzar=False, motivo="sin_motor")
        self.assertNotIn("tope de auditor", sin_motor)
        self.assertIn("no está disponible", sin_motor)

    def test_cancelar_no_depende_de_la_cuota(self):
        """Cancelar no gasta cuota, así que el botón no puede desaparecer con ella:
        /cancelar seguiría aceptando el pedido que la página ya no ofrece."""
        f = [{"id": "x-2026-08-25", "sitio": "x.com", "en_curso": True,
              "estado": "corriendo", "paso": "midiendo"}]
        html_ = web.render_indice(f, [], puede_lanzar=False, motivo="cuota",
                                  puede_cancelar=True)
        self.assertIn('action="/cancelar"', html_)


class TestCuota(unittest.TestCase):
    def test_deja_pasar_hasta_el_tope_y_despues_no(self):
        c = web.Cuota(tope=3)
        self.assertEqual([c.permite()[0] for _ in range(5)],
                         [True, True, True, False, False])
        self.assertEqual(c.restantes(), 0)

    def test_los_cupos_vuelven_al_pasar_la_ventana(self):
        c = web.Cuota(tope=2, ventana=0.3)
        self.assertTrue(c.permite()[0])
        self.assertTrue(c.permite()[0])
        self.assertFalse(c.permite()[0])
        time.sleep(0.4)
        self.assertTrue(c.permite()[0], "la ventana no liberó los cupos")

    def test_es_segura_entre_hilos(self):
        """Sin cerrojo, dos peticiones simultáneas podrían pasarse del tope."""
        c = web.Cuota(tope=20)
        concedidos = []
        cerrojo = threading.Lock()

        def tirar():
            for _ in range(20):
                ok, _q = c.permite()
                if ok:
                    with cerrojo:
                        concedidos.append(1)

        hilos = [threading.Thread(target=tirar) for _ in range(6)]
        [h.start() for h in hilos]
        [h.join() for h in hilos]
        self.assertEqual(len(concedidos), 20, "se concedieron cupos de más")



class _BasePost(unittest.TestCase):
    """Andamio compartido. Sin metodos de prueba: si los tuviera, cada subclase los
    volveria a correr y una prueba de CSRF terminaria ejecutando las del caso feliz."""

    def setUp(self):
        import http.server
        self.tmp = Path(tempfile.mkdtemp())
        self.cola = Cola(self.tmp / "casa", registrar=lambda m: None)
        from vitrina import almacen
        from vitrina.indice import Indice
        (self.tmp / "casa").mkdir(parents=True, exist_ok=True)
        # La casa de la cola es PRIVADA, igual que en producción (ver servidor.py): el
        # andamio tiene que reproducir el cableado real o las pruebas de privacidad
        # pasan por no tener nada privado que filtrar.
        ind = Indice(almacen.raices_desde(str(self.tmp)),
                     raices_privadas=[self.cola.base.resolve()])
        self.cuota = web.Cuota(tope=TOPE_CUOTA)
        h = web.constructor(ind, "", registrar=lambda m: None, cola=self.cola,
                            cuota=self.cuota)
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), h)
        self.puerto = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _post(self, cuerpo, cabeceras=None):
        import socket
        cab = dict(cabeceras or {})
        # Un solo Host, y el de verdad: mandar dos hace que el parser tome el primero y
        # la comprobacion de mismo-origen falle por culpa del andamio, no del servidor.
        cab.setdefault("Host", "127.0.0.1:%d" % self.puerto)
        extra = "".join("%s: %s\r\n" % kv for kv in cab.items())
        s = socket.create_connection(("127.0.0.1", self.puerto), 10)
        s.sendall((
            "POST /auditar HTTP/1.1\r\n"
            "Content-Type: application/x-www-form-urlencoded\r\n"
            "Content-Length: %d\r\n%sConnection: close\r\n\r\n%s"
            % (len(cuerpo), extra, cuerpo)
        ).encode())
        d = b""
        while True:
            t = s.recv(4096)
            if not t:
                break
            d += t
        s.close()
        return d.split(b"\r\n")[0].decode(), d

    def _post_legitimo(self, cuerpo, extra=None):
        """Como lo manda el formulario de la propia vitrina: con su Origin."""
        cab = {"Origin": "http://127.0.0.1:%d" % self.puerto,
               "Host": "127.0.0.1:%d" % self.puerto}
        cab.update(extra or {})
        return self._post(cuerpo, cab)


class TestPostHTTP(_BasePost):
    """El endpoint de escritura, sobre HTTP real."""

    def test_local_puede_encolar(self):
        linea, _ = self._post_legitimo("url=tienda.example")
        self.assertIn("303", linea)
        self.assertEqual(len(list(self.cola.base.glob("*/"))), 1)

    def test_desde_internet_encola_hasta_agotar_la_cuota(self):
        """Lanzar desde internet está permitido —Daniel quiere que se pruebe sin pedir
        credenciales— pero con tope por hora, para que no se vuelva un servicio de
        escaneo gratuito montado sobre su IP."""
        de_internet = {"CF-Ray": "8abc", "CF-Connecting-IP": "203.0.113.7"}
        for i in range(TOPE_CUOTA):
            linea, _ = self._post_legitimo("url=sitio%d.example" % i, de_internet)
            self.assertIn("303", linea, "el intento %d debía pasar" % (i + 1))
        self.assertEqual(len(list(self.cola.base.glob("*/"))), TOPE_CUOTA)

        linea, cuerpo = self._post_legitimo("url=uno-mas.example", de_internet)
        self.assertIn("429", linea, "no frenó al pasarse de la cuota")
        self.assertIn(b"tope de auditor", cuerpo)
        self.assertEqual(len(list(self.cola.base.glob("*/"))), TOPE_CUOTA,
                         "encoló por encima de la cuota")

    def test_desde_el_m4_la_cuota_no_aplica(self):
        for i in range(TOPE_CUOTA + 2):
            linea, _ = self._post_legitimo("url=local%d.example" % i)
            self.assertIn("303", linea, "la cuota no debe aplicar al M4")

    def test_url_invalida_no_revienta_el_servidor(self):
        linea, _ = self._post_legitimo("url=no-es-url")
        self.assertIn("303", linea)   # vuelve al índice con el aviso
        self.assertEqual(list(self.cola.base.glob("*/")), [])

    def test_cuerpo_gigante_se_rechaza(self):
        linea, _ = self._post_legitimo("url=" + "a" * 9000)
        self.assertIn("303", linea)
        self.assertEqual(list(self.cola.base.glob("*/")), [])


class TestCSRF(_BasePost):
    """CSRF: la web que visita Daniel no puede lanzar auditorías en su M4.

    `desde_internet()` no cubre esto: el POST lo manda SU navegador, así que llega sin
    cabeceras de Cloudflare y parece local. Sin clave, esto era el agujero.
    """

    def test_formulario_de_otra_web_es_rechazado(self):
        linea, cuerpo = self._post(
            "url=victima.com",
            {"Origin": "https://sitio-malicioso.com", "Host": "127.0.0.1:%d" % self.puerto},
        )
        self.assertIn("403", linea)
        self.assertIn(b"no sali", cuerpo)
        self.assertEqual(list(self.cola.base.glob("*/")), [], "encoló un POST de otra web")

    def test_referer_de_otra_web_tambien(self):
        linea, _ = self._post(
            "url=victima.com",
            {"Referer": "https://sitio-malicioso.com/pagina", "Host": "127.0.0.1:%d" % self.puerto},
        )
        self.assertIn("403", linea)
        self.assertEqual(list(self.cola.base.glob("*/")), [])

    def test_sin_origen_demostrable_tampoco(self):
        linea, _ = self._post("url=victima.com")   # ni Origin ni Referer ni X-Vitrina
        self.assertIn("403", linea)
        self.assertEqual(list(self.cola.base.glob("*/")), [])

    def test_el_formulario_propio_si_pasa(self):
        linea, _ = self._post(
            "url=tienda.example",
            {"Origin": "http://127.0.0.1:%d" % self.puerto, "Host": "127.0.0.1:%d" % self.puerto},
        )
        self.assertIn("303", linea)
        self.assertEqual(len(list(self.cola.base.glob("*/"))), 1)

    def test_cabecera_propia_sirve_para_la_terminal(self):
        # Un <form> HTML no puede poner cabeceras, y un fetch() de otro origen que lo
        # intente dispara un preflight que este servidor no contesta.
        linea, _ = self._post("url=tienda.example", {"X-Vitrina": "1"})
        self.assertIn("303", linea)
        self.assertEqual(len(list(self.cola.base.glob("*/"))), 1)



class TestCancelar(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cola = Cola(self.tmp / "casa", registrar=lambda m: None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cancelar_uno_que_espera_lo_saca_de_la_fila(self):
        a = self.cola.encolar("uno.com")
        b = self.cola.encolar("dos.com")
        self.assertTrue(self.cola.cancelar(b))
        self.assertEqual(leer_estado(self.cola.base / b)["estado"], "cancelado")
        self.assertEqual(self.cola.situacion()["en_cola"], [a], "sacó al que no era")

    def test_cancelar_algo_que_no_existe_devuelve_falso(self):
        self.assertFalse(self.cola.cancelar("no-existe-2026-01-01"))

    def test_cancelar_no_toca_a_los_demas(self):
        ids = [self.cola.encolar("s%d.com" % i) for i in range(4)]
        self.cola.cancelar(ids[1])
        vivos = self.cola.situacion()["en_cola"]
        self.assertEqual(vivos, [ids[0], ids[2], ids[3]])
        for i in (0, 2, 3):
            self.assertEqual(leer_estado(self.cola.base / ids[i])["estado"], "en_cola")

    def test_una_cancelada_ya_no_se_puede_cancelar_de_nuevo(self):
        a = self.cola.encolar("uno.com")
        self.assertTrue(self.cola.cancelar(a))
        self.assertFalse(self.cola.cancelar(a))


class TestCancelarHTTP(_BasePost):
    def test_cancelar_desde_la_vitrina(self):
        ident = self.cola.encolar("uno.com")
        s = __import__("socket")
        cuerpo = "id=" + ident
        cab = {"Origin": "http://127.0.0.1:%d" % self.puerto,
               "Host": "127.0.0.1:%d" % self.puerto}
        extra = "".join("%s: %s\r\n" % kv for kv in cab.items())
        con = s.create_connection(("127.0.0.1", self.puerto), 10)
        con.sendall(("POST /cancelar HTTP/1.1\r\n"
                     "Content-Type: application/x-www-form-urlencoded\r\n"
                     "Content-Length: %d\r\n%sConnection: close\r\n\r\n%s"
                     % (len(cuerpo), extra, cuerpo)).encode())
        d = b""
        while True:
            t = con.recv(4096)
            if not t:
                break
            d += t
        con.close()
        self.assertIn(b"303", d.split(b"\r\n")[0])
        self.assertEqual(leer_estado(self.cola.base / ident)["estado"], "cancelado")

    def test_cancelar_de_otra_web_es_rechazado(self):
        ident = self.cola.encolar("uno.com")
        s = __import__("socket")
        cuerpo = "id=" + ident
        con = s.create_connection(("127.0.0.1", self.puerto), 10)
        con.sendall(("POST /cancelar HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n"
                     "Origin: https://sitio-malicioso.com\r\n"
                     "Content-Type: application/x-www-form-urlencoded\r\n"
                     "Content-Length: %d\r\nConnection: close\r\n\r\n%s"
                     % (self.puerto, len(cuerpo), cuerpo)).encode())
        d = b""
        while True:
            t = con.recv(4096)
            if not t:
                break
            d += t
        con.close()
        self.assertIn(b"403", d.split(b"\r\n")[0])
        self.assertEqual(leer_estado(self.cola.base / ident)["estado"], "en_cola",
                         "una web ajena canceló un trabajo")



class TestCancelarCorriendo(unittest.TestCase):
    """Cancelar algo que YA está corriendo. Es la prueba que faltaba.

    Las otras cancelan trabajos que solo estaban en la fila, así que todo el camino de
    señales quedaba sin cubrir — y ahí estaba el defecto: `auditar.sh` atrapa el SIGTERM
    sin salir, y sus ejes corren como subshells que sobreviven al SIGKILL de bash Y
    heredan el stdout, así que la tubería nunca da EOF y el obrero se queda bloqueado.

    El doble de abajo reproduce exactamente esa forma: atrapa TERM sin morirse y deja un
    hijo en segundo plano agarrado a la salida.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.falso = self.tmp / "falso-auditar.sh"
        self.falso.write_text(
            "#!/usr/bin/env bash\n"
            "trap 'echo atrapado' TERM INT\n"      # atrapa y NO sale, como auditar.sh
            "( sleep 120 ) &\n"                    # hijo que hereda stdout, como los ejes
            "sleep 120\n",
            encoding="utf-8",
        )
        self.falso.chmod(0o755)
        self.cola = Cola(self.tmp / "casa", registrar=lambda m: None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cancelar_mata_el_arbol_y_libera_al_obrero(self):
        import time as _t
        from vitrina import cola as mod

        original = mod.AUDITAR
        mod.AUDITAR = self.falso
        try:
            self.cola.arrancar()
            ident = self.cola.encolar("sitio.com")
            for _ in range(60):                     # esperar a que arranque de verdad
                if self.cola.situacion()["corriendo"] == ident:
                    break
                _t.sleep(0.1)
            self.assertEqual(self.cola.situacion()["corriendo"], ident, "nunca arrancó")

            inicio = _t.time()
            self.assertTrue(self.cola.cancelar(ident))
            for _ in range(150):                    # el obrero debe soltarse
                if leer_estado(self.cola.base / ident)["estado"] == "cancelado":
                    break
                _t.sleep(0.1)
            tardanza = _t.time() - inicio

            self.assertEqual(leer_estado(self.cola.base / ident)["estado"], "cancelado")
            # Antes del arreglo esto tardaba lo que durase la auditoría entera.
            self.assertLess(tardanza, 15, "el obrero quedó bloqueado en la tubería")
            self.assertIsNone(self.cola.situacion()["corriendo"], "la cola quedó ocupada")
            self.assertNotIn(ident, self.cola._cancelados, "el id se quedó en el conjunto")
        finally:
            mod.AUDITAR = original

    def test_la_fila_sigue_despues_de_una_cancelacion(self):
        import time as _t
        from vitrina import cola as mod

        original = mod.AUDITAR
        mod.AUDITAR = self.falso
        try:
            self.cola.arrancar()
            a = self.cola.encolar("uno.com")
            b = self.cola.encolar("dos.com")
            for _ in range(60):
                if self.cola.situacion()["corriendo"] == a:
                    break
                _t.sleep(0.1)
            self.cola.cancelar(a)
            for _ in range(150):                    # el siguiente debe tomar el relevo
                if self.cola.situacion()["corriendo"] == b:
                    break
                _t.sleep(0.1)
            self.assertEqual(self.cola.situacion()["corriendo"], b,
                             "la cola se atascó: el siguiente nunca arrancó")
            self.cola.cancelar(b)
        finally:
            mod.AUDITAR = original



class TestReferrerPolicy(_BasePost):
    """El bug que rechazaba los formularios de la propia vitrina.

    `Referrer-Policy: no-referrer` suena más seguro, pero hace que el navegador mande
    `Origin: null` en el POST del formulario Y suprima el Referer. Las dos comprobaciones
    de mismo_origen() se quedan sin nada que mirar y la vitrina rechaza sus PROPIOS
    formularios. A Daniel le pasó seis veces entre el 23 y el 24 de agosto.
    """

    def test_no_se_manda_no_referrer(self):
        import socket
        s = socket.create_connection(("127.0.0.1", self.puerto), 10)
        s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        d = b""
        while True:
            t = s.recv(4096)
            if not t:
                break
            d += t
        s.close()
        cabeceras = d.split(b"\r\n\r\n")[0].decode("utf-8", "replace").lower()
        self.assertIn("referrer-policy: same-origin", cabeceras)
        self.assertNotIn("no-referrer", cabeceras,
                         "no-referrer hace que el navegador mande Origin: null "
                         "y se rechace el propio formulario")

    def test_origin_null_sigue_rechazado(self):
        """El arreglo no debe abrir la puerta: 'null' es lo que manda un iframe sandbox."""
        linea, _ = self._post("url=victima.com", {"Origin": "null"})
        self.assertIn("403", linea)
        self.assertEqual(list(self.cola.base.glob("*/")), [])


class _ConDosCasas(unittest.TestCase):
    """Andamio compartido por las pruebas que necesitan casa pública y privada."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cola = Cola(self.tmp / "privada", registrar=lambda m: None,
                         base_publica=self.tmp / "publica")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestCasaPublicaYPrivada(_ConDosCasas):
    """Dónde cae una auditoría decide quién la va a ver.

    El 24-ago-2026 toda auditoría caía en la raíz pública sin importar quién la lanzara,
    así que una auditoría de cliente hecha desde el M4 quedó visible en internet. El
    defecto tiene que ser PRIVADO.
    """

    def test_por_defecto_cae_en_la_privada(self):
        ident = self.cola.encolar("cliente.example")
        self.assertTrue((self.cola.base / ident).is_dir(), "no cayó en la privada")
        self.assertFalse((self.cola.base_publica / ident).exists())

    def test_desde_internet_cae_en_la_publica(self):
        ident = self.cola.encolar("visitante.example", publica=True)
        self.assertTrue((self.cola.base_publica / ident).is_dir())
        self.assertFalse((self.cola.base / ident).exists())

    def test_sin_casa_publica_todo_es_privado(self):
        c = Cola(self.tmp / "sola", registrar=lambda m: None)
        ident = c.encolar("x.example", publica=True)
        self.assertTrue((c.base / ident).is_dir(), "sin casa pública debe quedarse en casa")

    def test_el_saneo_mira_las_dos_casas(self):
        a = self.cola.encolar("uno.example")
        b = self.cola.encolar("dos.example", publica=True)
        escribir_estado(self.cola.base / a, estado="corriendo")
        escribir_estado(self.cola.base_publica / b, estado="corriendo")
        Cola(self.cola.base, registrar=lambda m: None,
             base_publica=self.cola.base_publica).sanear()
        self.assertEqual(leer_estado(self.cola.base / a)["estado"], "interrumpido")
        self.assertEqual(leer_estado(self.cola.base_publica / b)["estado"], "interrumpido",
                         "el saneo se olvidó de la casa pública")


class TestHistorico(_ConDosCasas):
    """Comparar cada corrida con la anterior del mismo sitio.

    Es lo que convierte un diagnóstico suelto ($200 una vez) en un seguimiento ($80 al
    mes): sin el "desde la última vez", cada informe empieza de cero y no hay nada que
    enseñarle al cliente después de arreglar.
    """

    def _auditoria(self, casa, nombre, sitio, puntaje):
        d = casa / nombre
        d.mkdir(parents=True, exist_ok=True)
        (d / "resultados.json").write_text(json.dumps({
            "sitio": sitio, "url": "https://" + sitio, "puntaje_global": puntaje,
            "ponderaciones": {"eje1": 1.0},
            "ejes": {"eje1": {"id": "eje1", "nombre": "Uno", "puntaje": puntaje,
                              "indicadores": {}, "hallazgos": []}},
        }), encoding="utf-8")
        escribir_estado(d, estado="listo", sitio=sitio)
        return d

    def test_encuentra_la_anterior_del_mismo_sitio(self):
        vieja = self._auditoria(self.cola.base, "sitio.example-2026-08-01", "sitio.example", 5.0)
        nueva = self._auditoria(self.cola.base, "sitio.example-2026-08-02", "sitio.example", 7.0)
        hallada = self.cola.anterior_de("sitio.example", excluir=nueva.name)
        self.assertEqual(hallada, vieja)

    def test_no_confunde_sitios_distintos(self):
        self._auditoria(self.cola.base, "otro.example-2026-08-01", "otro.example", 5.0)
        n = self._auditoria(self.cola.base, "sitio.example-2026-08-02", "sitio.example", 7.0)
        self.assertIsNone(self.cola.anterior_de("sitio.example", excluir=n.name),
                          "emparejó con la auditoría de otro sitio")

    def test_mira_las_dos_casas(self):
        """Una auditoría de cliente puede tener antes una corrida pública de prueba."""
        publica = self._auditoria(self.cola.base_publica, "sitio.example-2026-08-01",
                                  "sitio.example", 5.0)
        nueva = self._auditoria(self.cola.base, "sitio.example-2026-08-02",
                                "sitio.example", 7.0)
        self.assertEqual(self.cola.anterior_de("sitio.example", excluir=nueva.name), publica)

    def test_la_primera_vez_no_hay_con_que_comparar(self):
        n = self._auditoria(self.cola.base, "nuevo.example-2026-08-02", "nuevo.example", 7.0)
        self.assertIsNone(self.cola.anterior_de("nuevo.example", excluir=n.name))
        self.assertIsNone(self.cola.comparar_con_anterior(n, "nuevo.example"))
        self.assertFalse((n / "comparacion.json").exists(),
                         "escribió una comparación sin nada contra qué comparar")

    def test_ignora_las_que_no_terminaron(self):
        rota = self.cola.base / "sitio.example-2026-08-01"
        rota.mkdir(parents=True)
        escribir_estado(rota, estado="fallo", sitio="sitio.example")   # sin resultados.json
        n = self._auditoria(self.cola.base, "sitio.example-2026-08-02", "sitio.example", 7.0)
        self.assertIsNone(self.cola.anterior_de("sitio.example", excluir=n.name),
                          "eligió una auditoría que nunca produjo resultados")

    def test_una_publica_no_se_compara_contra_una_privada(self):
        """El comparacion.json vive DENTRO del directorio que se compara.

        Si una demo pública se compara contra la auditoría privada del mismo sitio,
        publica su nombre, su fecha y su puntaje — justo lo que el índice está
        ocultando bien por el otro lado.
        """
        self._auditoria(self.cola.base, "cliente.example-2026-08-01", "cliente.example", 5.0)
        nueva = self._auditoria(self.cola.base_publica, "cliente.example-2026-08-02",
                                "cliente.example", 7.0)
        self.assertIsNone(
            self.cola.anterior_de("cliente.example", excluir=nueva.name, destino=nueva),
            "una auditoría pública se emparejó con una privada")

    def test_ignora_las_canceladas_a_medias(self):
        """Cancelar durante el informe deja resultados.json en disco. Compararse contra
        eso es medir el progreso contra un trabajo que nadie dio por bueno."""
        a_medias = self._auditoria(self.cola.base, "sitio.example-2026-08-01",
                                   "sitio.example", 5.0)
        escribir_estado(a_medias, estado="cancelado")
        n = self._auditoria(self.cola.base, "sitio.example-2026-08-02", "sitio.example", 7.0)
        self.assertIsNone(self.cola.anterior_de("sitio.example", excluir=n.name))

    def test_el_id_es_unico_entre_las_dos_casas(self):
        """El índice deduplica por nombre de directorio: con el mismo nombre en las dos
        casas, una de las dos desaparece del listado y sólo queda una línea en el log."""
        a = self.cola.encolar("mismo.example")
        b = self.cola.encolar("mismo.example", publica=True)
        self.assertNotEqual(a, b, "dos auditorías del mismo día comparten id entre casas")


class TestColaJSONNoFiltra(_BasePost):
    """/api/cola.json nació sin la comprobación de privacidad que ya tenían el listado
    y /a/<id>/, y publicaba los nombres de directorio —o sea el sitio del cliente y la
    fecha— de lo que estuviera encolado o corriendo."""

    def _get(self, camino, cabeceras=None):
        import socket
        cab = dict(cabeceras or {})
        cab.setdefault("Host", "127.0.0.1:%d" % self.puerto)
        extra = "".join("%s: %s\r\n" % kv for kv in cab.items())
        s = socket.create_connection(("127.0.0.1", self.puerto), 10)
        s.sendall(("GET %s HTTP/1.1\r\n%sConnection: close\r\n\r\n"
                   % (camino, extra)).encode())
        d = b""
        while True:
            t = s.recv(4096)
            if not t:
                break
            d += t
        s.close()
        return d.decode("utf-8", "replace")

    def test_internet_no_ve_lo_que_corre_en_privado(self):
        self.cola.encolar("cliente-secreto.example")
        local = self._get("/api/cola.json")
        de_internet = self._get("/api/cola.json", {"CF-Ray": "8abc123"})
        self.assertIn("cliente-secreto.example", local,
                      "desde el M4 sí se tiene que ver")
        self.assertNotIn("cliente-secreto.example", de_internet,
                         "se filtró el sitio del cliente por /api/cola.json")

    def test_internet_no_puede_cancelar_lo_que_no_ve(self):
        """Sin esto, un visitante que adivine el id aborta la auditoría de cliente."""
        ident = self.cola.encolar("cliente-secreto.example")
        self._post("id=" + ident, {
            "Origin": "http://127.0.0.1:%d" % self.puerto, "CF-Ray": "8abc123",
        })
        self.assertNotEqual(leer_estado(self.cola.base / ident).get("estado"), "cancelado",
                            "internet canceló una auditoría privada")


class TestAlcance(_ConDosCasas):
    """Muestra o sitio completo: quien encola elige, y antes no elegia nadie.

    auditar.sh se corta en 15 paginas por un valor fijo en el codigo, asi que un sitio
    de 38 paginas salia declarado MUESTRA PARCIAL sin que nadie lo hubiera decidido.
    """

    def test_muestra_no_impone_tope(self):
        ident = self.cola.encolar("x.example", paginas="muestra")
        self.assertIsNone(leer_estado(self.cola.base / ident)["paginas"],
                          "la muestra debe dejar el defecto de auditar.sh, no fijar un tope")

    def test_completo_pide_el_techo(self):
        ident = self.cola.encolar("x.example", paginas="completo")
        self.assertEqual(leer_estado(self.cola.base / ident)["paginas"],
                         mod_cola.PAGINAS_COMPLETO)

    def test_sin_alcance_se_comporta_como_antes(self):
        ident = self.cola.encolar("x.example")
        self.assertIsNone(leer_estado(self.cola.base / ident)["paginas"])

    def test_un_numero_suelto_vale_si_esta_en_rango(self):
        ident = self.cola.encolar("x.example", paginas="40")
        self.assertEqual(leer_estado(self.cola.base / ident)["paginas"], 40)

    def test_un_tope_absurdo_no_se_acepta(self):
        """La cola es serial: un tope enorme no es un error de tipeo, es una auditoria de
        horas que bloquea todo lo que venga detras."""
        for malo in ("99999", "0", "-3", "todas"):
            with self.assertRaises(ValueError, msg="aceptó %r" % malo):
                self.cola.encolar("x.example", paginas=malo)


class TestSitioDeClienteNoSePublica(_ConDosCasas):
    """Un sitio con trabajo privado no se publica porque lo pida otra persona.

    El 25-ago-2026 alguien escribió el dominio de un cliente en el formulario público de
    audit.voidspore.tech. Como venía de internet, la auditoría cayó en la casa pública y
    quedó visible con sus puntajes de seguridad. El reparto miraba QUIÉN lanzaba, no DE
    QUIÉN era el sitio.
    """

    def _privada_de(self, sitio):
        d = self.cola.base / (sitio + "-2026-08-01")
        d.mkdir(parents=True, exist_ok=True)
        escribir_estado(d, estado="listo", sitio=sitio)
        return d

    def test_desde_internet_no_puede_publicar_un_sitio_de_cliente(self):
        self._privada_de("cliente.example")
        ident = self.cola.encolar("cliente.example", publica=True)
        self.assertTrue((self.cola.base / ident).is_dir(),
                        "una auditoría de cliente salió a la casa pública")
        self.assertFalse((self.cola.base_publica / ident).exists())

    def test_un_sitio_nuevo_desde_internet_si_se_publica(self):
        """La demo tiene que seguir funcionando: lo que no es de cliente, se ve."""
        ident = self.cola.encolar("desconocido.example", publica=True)
        self.assertTrue((self.cola.base_publica / ident).is_dir())

    def test_no_confunde_sitios_parecidos(self):
        self._privada_de("cliente.example")
        ident = self.cola.encolar("otrocliente.example", publica=True)
        self.assertTrue((self.cola.base_publica / ident).is_dir(),
                        "emparejó con un sitio distinto que solo se le parece")


if __name__ == "__main__":
    unittest.main()
