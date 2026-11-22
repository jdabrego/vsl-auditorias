"""Arnés del MOTOR: que los seis medidores sigan cumpliendo sus reglas duras.

POR QUÉ EXISTE
──────────────
Hasta el 26-ago-2026 el motor tenía 15.169 líneas y CERO pruebas. Todo lo probado era el
andamiaje de alrededor: la cola, la vitrina, el informe. Lo que produce cada cifra de cada
informe no tenía una sola verificación automática.

Ese día se encontraron tres defectos reales —rutas de la máquina en informes ya entregados
a clientes, el nombre de una agencia dentro de las expresiones que lo filtraban, y la
detección de proyecto mandando evidencia al repositorio equivocado— y ninguno lo encontró
una prueba.

QUÉ SE AFIRMA, Y QUÉ NO
───────────────────────
Aquí NO se afirma que un número sea correcto: contra un sitio de localhost, "LCP = 1,2 s"
no significa nada y afirmarlo sería atarse a la velocidad de esta máquina.

Lo que se afirma es que las REGLAS DURAS se cumplen — que es lo único que la herramienta
promete y lo único que la distingue:

  · las cabeceras se miden SOLO sobre respuestas 200
  · lo que no se pudo medir sale NO_MEDIDO con su motivo, nunca como cero
  · el esfuerzo NUNCA es un número
  · un sitio roto no tumba al medidor: sale medido a medias y declarado

Y esa lección de hoy: la prueba pregunta por la CLASE de defecto, no por el caso concreto
en que se vio. El arnés del informe tenía una prueba de rutas que eximía al anexo por
escrito, y por ese hueco pasaron siete rutas absolutas a cada informe entregado.
"""

import json
import os
import subprocess
import sys
import tempfile
import shutil
import unittest
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from calibracion.corpus import HOSTILES            # noqa: E402
from tests.sitios import GEMELOS, SitioSucio        # noqa: E402
from vitrina.cola import SKILL                     # noqa: E402

EJES = {
    "eje1": "eje1-performance.mjs",
    "eje2": "eje2-ux.mjs",
    "eje3": "eje3-tecnologia.mjs",
    "eje4": "eje4-seo.mjs",
    "eje5": "eje5-seguridad.mjs",
    "eje6": "eje6-accesibilidad.mjs",
}

# El PATH de este proceso puede no traer node (launchd arranca con uno mínimo), y sin él
# los seis medidores fallan con "command not found" en vez de con algo legible.
ENTORNO = dict(os.environ, PATH="/opt/homebrew/bin:" + os.environ.get("PATH", ""))

HAY_MOTOR = (SKILL / "scripts" / "eje5-seguridad.mjs").is_file()


# Por qué murió cada medidor que no produjo nada. Sin esto, un fallo de este arnés dice
# "no escribió su JSON" y no da un solo dato para averiguar por qué: hay que reproducirlo
# a mano, y como es intermitente puede no reproducirse. La causa se guarda cuando ocurre.
DIAGNOSTICO = {}


def _correr(eje, url, destino):
    """Corre un medidor y devuelve su ejeN.json, o None si no llegó a escribirlo."""
    guion = SKILL / "scripts" / EJES[eje]
    try:
        p = subprocess.run([str(guion), url, str(destino)] if os.access(guion, os.X_OK)
                           else ["node", str(guion), url, str(destino)],
                           capture_output=True, text=True, timeout=400, env=ENTORNO)
        codigo, err = p.returncode, (p.stderr or "")[-500:]
    except subprocess.SubprocessError as e:
        codigo, err = "excepción", repr(e)
    ruta = Path(destino) / (eje + ".json")
    try:
        with open(ruta, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as e:
        DIAGNOSTICO[eje] = "%s · salida=%s · %r · stderr: %s" % (url, codigo, e, err)
        return None


def _porque(*ejes):
    """El diagnóstico de los medidores que fallaron, para meterlo en el mensaje del fallo."""
    partes = [f"{e}: {DIAGNOSTICO[e]}" for e in (ejes or DIAGNOSTICO) if e in DIAGNOSTICO]
    return ("\n  " + "\n  ".join(partes)) if partes else " (sin diagnóstico guardado)"


def _contra(url, ejes, tmp):
    """Corre los medidores contra una URL. El eje 1 va SOLO; el resto, en paralelo.

    Los cinco livianos no se pisan: cada uno abre su Chrome en su propio puerto CDP.
    El eje 1 sí se saca del grupo, y no por la precisión de sus tiempos —acá no se afirma
    ninguno— sino porque bajo contención Lighthouse a veces no mide NADA y devuelve un eje
    sin fuentes. La primera versión de este arnés lo metía en el pool y falló una vez de
    cada dos corridas: una prueba intermitente es peor que ninguna, porque enseña a
    ignorar el rojo.
    """
    salida = {}
    for e in ejes:
        (tmp / e).mkdir(parents=True, exist_ok=True)
    if "eje1" in ejes:
        salida["eje1"] = _correr("eje1", url, tmp / "eje1")
    livianos = [e for e in ejes if e != "eje1"]
    if livianos:
        with ThreadPoolExecutor(max_workers=len(livianos)) as pool:
            futuros = {e: pool.submit(_correr, e, url, tmp / e) for e in livianos}
            salida.update({e: f.result() for e, f in futuros.items()})
    return salida


@contextmanager
def _servido(clase):
    """Sirve un caso en un puerto EFÍMERO y devuelve su URL.

    Puerto 0 y no uno fijo: con puertos escritos a mano, dos clases de prueba que corren
    seguidas se pisan si la anterior dejó el socket en TIME_WAIT, y el arnés entero falla
    por una razón que no tiene nada que ver con lo que mide. Pasó: doce pruebas rojas en
    una corrida y verdes en la siguiente.
    """
    srv = ThreadingHTTPServer(("127.0.0.1", 0), clase)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield "http://127.0.0.1:%d/" % srv.server_address[1]
    finally:
        srv.shutdown()
        srv.server_close()


def _medir(caso, ejes):
    """Mide contra uno de los casos hostiles, servido en un puerto efímero."""
    clase = dict((n, c) for n, c, _d in HOSTILES)[caso]
    with _servido(clase) as url:
        tmp = Path(tempfile.mkdtemp())
        try:
            return _contra(url, ejes, tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def _indicadores(eje):
    return (eje or {}).get("indicadores") or {}


@unittest.skipUnless(HAY_MOTOR, "la skill site-audit no está en esta máquina")
class TestReglaDel200(unittest.TestCase):
    """LA regla de la casa, contra el caso que la reproduce.

    El servidor emite las cabeceras de seguridad SOLO en el 404 y NINGUNA en el 200 — que
    es lo que hace un WordPress con caché de página: el 404 ejecuta PHP y las emite, la
    página real sale del caché como archivo estático y no emite ninguna.

    Si esta prueba se pone verde midiendo el 404, la herramienta entera está mintiendo en
    la única cosa que dice hacer mejor que las demás.
    """

    @classmethod
    def setUpClass(cls):
        cls.eje5 = _medir("cabeceras-404", ["eje5"])["eje5"]

    def test_el_medidor_produjo_algo(self):
        self.assertIsNotNone(self.eje5, "eje5 no llegó a escribir su JSON")

    def test_no_da_por_presentes_las_cabeceras_del_404(self):
        ind = _indicadores(self.eje5)
        revisados = 0
        for clave in ("eje5.x_frame_options", "eje5.csp", "eje5.referrer_policy"):
            if clave not in ind:
                continue
            revisados += 1
            self.assertNotEqual(
                ind[clave].get("estado"), "bien",
                "%s salió BIEN: se midió sobre el 404, que es el error que esta "
                "herramienta existe para no cometer" % clave)
        self.assertTrue(revisados, "no se revisó ninguna cabecera: cambió el nombre de los "
                                   "indicadores y esta prueba dejó de mirar nada")

    def test_cuenta_solo_las_paginas_que_dieron_200(self):
        """El conteo tiene que ser sobre las 3 páginas válidas, no sobre las 404."""
        ind = _indicadores(self.eje5)
        vistos = 0
        for clave, v in ind.items():
            valor = v.get("valor")
            if isinstance(valor, dict) and "presente_en" in valor and "de" in valor:
                vistos += 1
                self.assertEqual(valor["presente_en"], 0,
                                 "%s vio la cabecera en %s página(s): solo el 404 las emite"
                                 % (clave, valor["presente_en"]))
                self.assertEqual(valor["de"], 3,
                                 "%s midió sobre %s páginas y el sitio sirve 3 con 200"
                                 % (clave, valor["de"]))
        self.assertTrue(vistos, "ningún indicador declaró sobre cuántas páginas midió")


@unittest.skipUnless(HAY_MOTOR, "la skill site-audit no está en esta máquina")
class TestUnSitioRotoNoTumbaElMotor(unittest.TestCase):
    """Un 500 permanente: los seis medidores tienen que sobrevivir y DECLARARLO.

    El modo de fallo que se persigue no es que reviente —eso se ve— sino que devuelva
    ceros silenciosos, que son indistinguibles de una medición que salió bien.
    """

    @classmethod
    def setUpClass(cls):
        cls.ejes = _medir("http500", list(EJES))

    def test_los_seis_escriben_su_json(self):
        faltan = [e for e, v in self.ejes.items() if v is None]
        self.assertFalse(faltan, "no escribieron su JSON contra un sitio en 500: %s%s"
                         % (faltan, _porque(*faltan)))

    def test_nada_se_reporta_como_cero_en_vez_de_no_medido(self):
        """Cero es una afirmación. 'No se pudo medir' es otra cosa completamente distinta."""
        for nombre, eje in self.ejes.items():
            for clave, ind in _indicadores(eje).items():
                if ind.get("estado") != "no_medido":
                    continue
                self.assertEqual(
                    ind.get("valor"), "NO_MEDIDO",
                    "%s · %s dice no_medido pero su valor es %r"
                    % (nombre, clave, ind.get("valor")))

    def test_todo_no_medido_trae_su_motivo(self):
        """Un NO_MEDIDO sin motivo obliga al lector a adivinar, que es lo que se evita.

        El campo es `motivo_no_medido`, y así lo declara esquema.json. La primera versión
        de esta prueba buscó `motivo` a secas y salió roja contra un motor que estaba
        bien: una prueba que pregunta por un campo que no existe no verifica nada, se
        queda roja para siempre y termina ignorada. Por eso el nombre sale del esquema y
        no de la memoria.
        """
        mudos = []
        for nombre, eje in self.ejes.items():
            for clave, ind in _indicadores(eje).items():
                if ind.get("estado") != "no_medido":
                    continue
                if not str(ind.get("motivo_no_medido") or "").strip():
                    mudos.append("%s · %s" % (nombre, clave))
        self.assertFalse(mudos, "NO_MEDIDO sin motivo_no_medido: %s" % mudos[:6])

    def test_el_campo_del_motivo_es_el_que_declara_el_esquema(self):
        """Guardiana de la prueba de arriba: si el contrato renombra el campo, esta falla
        y avisa, en vez de dejar que la otra pase en verde mirando un campo inexistente."""
        with open(SKILL / "esquema.json", "r", encoding="utf-8") as fh:
            props = json.load(fh)["$defs"]["indicador"]["properties"]
        self.assertIn("motivo_no_medido", props,
                      "el esquema ya no declara motivo_no_medido: hay que actualizar la "
                      "prueba de motivos, no borrarla")


@unittest.skipUnless(HAY_MOTOR, "la skill site-audit no está en esta máquina")
class TestInvariantesDeTodosLosEjes(unittest.TestCase):
    """Las reglas que valen para los seis, contra un sitio que responde bien.

    Se corre contra 'sin-sitemap' porque es el hostil más benigno: sirve 200 en todo y
    solo le falta el sitemap. Así lo que falle es una regla rota, no el sitio.
    """

    @classmethod
    def setUpClass(cls):
        cls.ejes = _medir("sin-sitemap", list(EJES))

    def test_el_esfuerzo_nunca_es_un_numero(self):
        """Estimar esfuerzo desde fuera es imposible: no se sabe quién lo va a arreglar,
        con qué acceso ni sobre qué código. hallazgo() lanza si alguien pasa un número;
        esto comprueba que ninguno se coló por otra puerta."""
        malos = []
        for nombre, eje in self.ejes.items():
            for h in ((eje or {}).get("hallazgos") or []):
                esf = h.get("esfuerzo")
                if esf is not None and esf != "NO_MEDIDO":
                    malos.append("%s · %s = %r" % (nombre, h.get("id") or h.get("titulo"), esf))
        self.assertFalse(malos, "esfuerzo con un número dentro: %s" % malos[:5])

    def test_ningun_hallazgo_traduce_a_dinero(self):
        """Nadie midió eso. Es la regla que separa un diagnóstico de un argumento de venta."""
        import re
        patron = re.compile(r"(?:US\$|\$\s?\d|\bUSD\b|\bd[oó]lares\b|\beuros\b)", re.I)
        malos = []
        for nombre, eje in self.ejes.items():
            for h in ((eje or {}).get("hallazgos") or []):
                texto = " ".join(str(h.get(k) or "") for k in ("titulo", "detalle", "por_que"))
                if patron.search(texto):
                    malos.append("%s · %s" % (nombre, str(h.get("titulo"))[:60]))
        self.assertFalse(malos, "un hallazgo habla de dinero: %s" % malos[:5])

    def test_cada_eje_que_midio_algo_declara_con_que(self):
        """Sin la herramienta y su versión, el número no se puede volver a comprobar.

        Se exige solo a los ejes que MIDIERON algo. Un eje que no pudo medir nada no tiene
        con qué haber medido, y exigirle fuentes convertía esta prueba en intermitente:
        bajo carga, Lighthouse a veces devuelve el eje vacío y el rojo no señalaba ningún
        defecto real.
        """
        mudos = []
        for nombre, eje in self.ejes.items():
            midio = any(i.get("estado") not in (None, "no_medido")
                        for i in _indicadores(eje).values())
            if midio and not ((eje or {}).get("fuentes") or []):
                mudos.append(nombre)
        self.assertFalse(mudos, "ejes que midieron sin declarar con qué: %s" % mudos)

    def test_los_indicadores_medidos_traen_su_archivo_de_evidencia(self):
        """Una cifra sin archivo detrás es una cifra que nadie puede auditar."""
        huerfanos = []
        for nombre, eje in self.ejes.items():
            for clave, ind in _indicadores(eje).items():
                if ind.get("estado") in (None, "no_medido", "informativo"):
                    continue
                if not (ind.get("fuente_archivo") or ind.get("herramienta")):
                    huerfanos.append("%s · %s" % (nombre, clave))
        self.assertFalse(huerfanos, "indicadores medidos sin archivo ni herramienta: %s"
                         % huerfanos[:6])

    def test_ninguna_ruta_de_esta_maquina_en_la_salida(self):
        """La CLASE de defecto, no el caso donde se vio.

        El 25-ago-2026 el anexo del informe imprimía rutas absolutas y llegaron a informes
        entregados a clientes. Había una prueba de rutas que eximía al anexo POR ESCRITO,
        así que preguntaba dónde iban las rutas y nunca de qué tipo eran. Esta pregunta por
        el tipo, sobre el JSON entero, sin eximir ninguna sección.
        """
        import re
        casa = re.escape(str(Path.home()))
        malos = []
        for nombre, eje in self.ejes.items():
            crudo = json.dumps(eje or {}, ensure_ascii=False)
            for m in set(re.findall(casa + r"[^\"\\, ]*", crudo)):
                malos.append("%s: %s" % (nombre, m))
        self.assertFalse(malos, "rutas de esta máquina dentro del JSON del eje: %s" % malos[:6])


def _gemelos(ejes):
    """Mide los mismos ejes contra los dos sitios gemelos."""
    salida = {}
    for nombre, clase in GEMELOS:
        with _servido(clase) as url:
            tmp = Path(tempfile.mkdtemp())
            try:
                salida[nombre] = _contra(url, ejes, tmp)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
    return salida


@unittest.skipUnless(HAY_MOTOR, "la skill site-audit no está en esta máquina")
class TestEncuentraLosDefectosYNoLosInventa(unittest.TestCase):
    """Dos sitios idénticos salvo por defectos puestos a propósito.

    Se afirma el PAR, nunca una mitad sola. Que el medidor ENCUENTRE un defecto no prueba
    nada por sí solo —uno que reportara problemas siempre pasaría esa prueba— y que no
    encuentre nada en un sitio bueno tampoco —uno que no mirara nada también pasaría—.
    Solo pasa las dos mitades el que de verdad mide.

    Los defectos y los indicadores que les corresponden están en tests/sitios.py.
    """

    @classmethod
    def setUpClass(cls):
        cls.m = _gemelos(["eje2", "eje4", "eje6"])

    def par(self, eje, indicador):
        """Devuelve (estado en el sucio, estado en el limpio) y comprueba que existan."""
        est = {}
        for lado in ("sucio", "limpio"):
            self.assertIsNotNone(
                (self.m.get(lado) or {}).get(eje),
                "%s no llegó a medir el sitio %s, así que este par no prueba nada:%s"
                % (eje, lado, _porque(eje)))
            ind = _indicadores((self.m.get(lado) or {}).get(eje))
            self.assertIn(indicador, ind,
                          "%s no emitió %s en el sitio %s: o cambió de nombre y esta "
                          "prueba dejó de mirar nada, o el medidor no lo produce"
                          % (eje, indicador, lado))
            est[lado] = ind[indicador].get("estado")
        return est["sucio"], est["limpio"]

    def afirmar_par(self, eje, indicador):
        sucio, limpio = self.par(eje, indicador)
        self.assertIn(sucio, ("mal", "mejorar"),
                      "%s salió '%s' en el sitio CON el defecto: no lo vio" % (indicador, sucio))
        self.assertEqual(limpio, "bien",
                         "%s salió '%s' en el sitio SIN el defecto: lo inventó"
                         % (indicador, limpio))

    # --- eje 4 · SEO -------------------------------------------------------
    def test_dos_h1_en_la_misma_pagina(self):
        self.afirmar_par("eje4", "eje4.h1_por_pagina")

    def test_falta_la_meta_description(self):
        self.afirmar_par("eje4", "eje4.meta_description")

    def test_falta_el_canonical(self):
        self.afirmar_par("eje4", "eje4.canonical")

    def test_falta_open_graph(self):
        self.afirmar_par("eje4", "eje4.open_graph")

    def test_imagen_sin_alt(self):
        self.afirmar_par("eje4", "eje4.alt_imagenes")

    def test_enlace_interno_roto(self):
        self.afirmar_par("eje4", "eje4.enlaces_internos_rotos")

    def test_html_sin_lang(self):
        self.afirmar_par("eje4", "eje4.idioma_html")

    # --- eje 6 · accesibilidad --------------------------------------------
    def test_axe_ve_la_imagen_sin_alt(self):
        self.afirmar_par("eje6", "eje6.alt_imagenes")

    def test_documento_sin_idioma_declarado(self):
        self.afirmar_par("eje6", "eje6.idioma_documento")

    # --- eje 2 · UX --------------------------------------------------------
    def test_falta_el_viewport(self):
        self.afirmar_par("eje2", "eje2.viewport_meta")

    def test_ux_tambien_ve_el_enlace_roto(self):
        self.afirmar_par("eje2", "eje2.enlaces_rotos")


class _ChromeAjeno(BaseHTTPRequestHandler):
    """Se hace pasar por un Chrome ya escuchando en el puerto de depuración.

    No hace falta un Chrome de verdad: lo único que el medidor mira para decidir "acá ya
    hay uno" es la respuesta de /json/version. Un servidor de diez líneas reproduce la
    trampa entera, y sin depender de que haya Chrome instalado.
    """

    def log_message(self, *a):
        pass

    def do_GET(self):
        cuerpo = json.dumps({"Browser": "Chrome/999.0.0.0 (impostor de prueba)",
                             "Protocol-Version": "1.3"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)


@unittest.skipUnless(HAY_MOTOR, "la skill site-audit no está en esta máquina")
class TestNoMidePorUnNavegadorAjeno(unittest.TestCase):
    """Si el puerto de depuración ya está ocupado, el medidor NO mide: se niega.

    EL FALLO QUE ESTO REPRODUCE, visto el 26-ago-2026
    ─────────────────────────────────────────────────
    Quedaron dos Chrome huérfanos de una corrida anterior escuchando en 9232 y 9236. El
    medidor lanzó el suyo, ese no pudo tomar el puerto ocupado, y al preguntar por el
    puerto le contestó el AJENO. Siguió adelante conectado a un navegador que no arrancó.

    El resultado: midiendo un sitio de prueba de localhost que no menciona ningún dominio
    externo, el eje 2 reportó 124 enlaces externos a un dominio ajeno. Estaba leyendo el
    DOM del navegador equivocado — o sea que una auditoría puede describir un sitio
    DISTINTO del que se pidió, y el informe sale perfectamente creíble.

    El guardia que había (`chromesActivos`) solo veía los Chrome de ESE proceso node. Un
    huérfano, o el eje de al lado, le eran invisibles.
    """

    def test_se_niega_si_el_chrome_que_lanzo_murio_y_otro_contesta(self):
        """La rendija que deja el sondeo previo: la carrera entre comprobar y arrancar.

        El sondeo dice "libre", y ANTES de que Chrome termine de arrancar otro toma el
        puerto. El nuestro muere sin conseguirlo, esperarChrome() pregunta y le contesta
        el ajeno — el mismo fallo que el sondeo venía a evitar, por una rendija más fina.

        No hace falta ganar una carrera de verdad para probarlo:
          · CHROME_BIN=/usr/bin/false → el proceso que lanzamos muere al instante
          · el impostor se levanta 300 ms DESPUÉS, o sea con el sondeo ya pasado
          · esperarChrome sondea cada 250 ms, encuentra al impostor, y ahí tiene que
            saltar el segundo guardia

        POR QUÉ EXISTE ESTA PRUEBA: la de arriba cubría el sondeo y NADA MÁS. Quitando el
        guardia de después entero, las 24 pruebas del arnés seguían verdes. La mitad del
        arreglo no estaba probada, y no se veía escribiendo la prueba — solo falsificándola.
        """
        libre = ThreadingHTTPServer(("127.0.0.1", 0), _ChromeAjeno)
        puerto = libre.server_address[1]
        libre.server_close()          # se suelta: el sondeo lo tiene que ver LIBRE

        tarde = {}

        def levantar_impostor():
            try:
                srv = ThreadingHTTPServer(("127.0.0.1", puerto), _ChromeAjeno)
            except OSError:
                return
            tarde["srv"] = srv
            srv.serve_forever()

        hilo = threading.Timer(0.3, levantar_impostor)
        hilo.start()
        try:
            with _servido(SitioSucio) as url:
                tmp = Path(tempfile.mkdtemp())
                try:
                    p = subprocess.run(
                        ["node", str(SKILL / "scripts" / EJES["eje2"]), url, str(tmp)],
                        capture_output=True, text=True, timeout=200,
                        env=dict(ENTORNO, SITE_AUDIT_PUERTO_CDP_2=str(puerto),
                                 CHROME_BIN="/usr/bin/false"))
                    todo = (p.stdout or "") + (p.stderr or "")
                    self.assertIn(
                        "no arrancamos", todo,
                        "midió a través de un navegador que no lanzó, o falló por otra "
                        "razón:\n" + todo[-700:])
                finally:
                    shutil.rmtree(tmp, ignore_errors=True)
        finally:
            hilo.cancel()
            srv = tarde.get("srv")
            if srv:
                srv.shutdown()
                srv.server_close()

    def test_se_niega_a_medir_si_el_puerto_ya_esta_ocupado(self):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _ChromeAjeno)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        puerto = srv.server_address[1]
        try:
            with _servido(SitioSucio) as url:
                tmp = Path(tempfile.mkdtemp())
                try:
                    p = subprocess.run(
                        ["node", str(SKILL / "scripts" / EJES["eje2"]), url, str(tmp)],
                        capture_output=True, text=True, timeout=200,
                        env=dict(ENTORNO, SITE_AUDIT_PUERTO_CDP_2=str(puerto)))
                    todo = (p.stdout or "") + (p.stderr or "")
                    self.assertIn(
                        "otro Chrome", todo,
                        "midió a través de un navegador ajeno en vez de negarse:\n" + todo[-600:])
                    self.assertNotEqual(p.returncode, 0,
                                        "se negó a medir pero salió con código 0")
                finally:
                    shutil.rmtree(tmp, ignore_errors=True)
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
