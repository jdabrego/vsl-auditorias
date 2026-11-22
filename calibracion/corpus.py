"""corpus — qué sitios entran en la campaña y cómo se levantan los casos hostiles.

Los 8 reales son de Daniel o de sus clientes. Los 7 hostiles se sirven en localhost: no
se sale a molestar sitios ajenos, y además así son reproducibles y deterministas, que es
justo lo que un caso de prueba tiene que ser.

Compatible con Python 3.9.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# --- los sitios reales -----------------------------------------------------
# NO se listan aquí: son sitios de clientes, y una lista de clientes no va en el código.
# Viven en corpus.local.json, que está fuera del control de versiones. Ver el .example.

RUTA_LOCAL = Path(__file__).resolve().parent / "corpus.local.json"


def reales():
    """Los sitios reales del corpus, leídos de la configuración local.

    Devuelve [] si no hay archivo: la campaña puede correr solo con los casos hostiles,
    que sí son parte del repositorio porque no revelan nada de nadie.
    """
    try:
        datos = json.loads(RUTA_LOCAL.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    salida = []
    for x in datos.get("reales") or []:
        if isinstance(x, dict) and x.get("sitio"):
            salida.append((x["sitio"], x.get("nota") or ""))
    return salida


PAGINA = (
    "<!doctype html><html lang=es><head><meta charset=utf-8>"
    "<title>{titulo}</title><meta name=description content='{titulo} de prueba'>"
    "</head><body><h1>{titulo}</h1><nav>{enlaces}</nav>"
    "<p>Caso de calibración servido en localhost.</p></body></html>"
)


def _enlaces(rutas):
    return "".join('<a href="%s">%s</a> ' % (r, r.strip("/") or "inicio") for r in rutas)


class _Base(BaseHTTPRequestHandler):
    """Handler base: silencia el log y da una página válida por defecto."""

    rutas = ("/", "/uno/", "/dos/")
    titulo = "caso"

    def log_message(self, *a):
        pass

    def _pagina(self, codigo=200, cabeceras=None, cuerpo=None, tipo="text/html; charset=utf-8"):
        if cuerpo is None:
            cuerpo = PAGINA.format(titulo=self.titulo, enlaces=_enlaces(self.rutas))
        if isinstance(cuerpo, str):
            cuerpo = cuerpo.encode("utf-8", "replace")
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(cuerpo)))
        for k, v in (cabeceras or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(cuerpo)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if self.path == "/robots.txt":
            return self._pagina(cuerpo=self.robots(), tipo="text/plain; charset=utf-8")
        if self.path == "/sitemap.xml":
            return self._pagina(cuerpo=self.sitemap(), tipo="application/xml; charset=utf-8")
        self.responder()

    def robots(self):
        host = self.headers.get("Host", "")
        return "User-agent: *\nAllow: /\nSitemap: http://%s/sitemap.xml\n" % host

    def sitemap(self):
        host = self.headers.get("Host", "")
        urls = "".join("<url><loc>http://%s%s</loc></url>" % (host, r) for r in self.rutas)
        return ('<?xml version="1.0" encoding="UTF-8"?>'
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">%s</urlset>' % urls)

    def responder(self):
        if self.path in self.rutas:
            return self._pagina()
        self._pagina(404, cuerpo="<h1>no existe</h1>")


# --- los 7 hostiles ---------------------------------------------------------

CABECERAS_SEG = {
    "Strict-Transport-Security": "max-age=31536000",
    "X-Frame-Options": "SAMEORIGIN",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


class CasoCabecerasSoloEn404(_Base):
    """El caso más valioso: cabeceras en el 404 y NINGUNA en el 200.

    Reproduce un error real, cometido por una agencia y repetido dos veces por nosotros: con
    caché de página, un 404 ejecuta PHP y emite las cabeceras, mientras la página real
    sale del caché como archivo estático y no emite ninguna. Medir en el 404 da verde
    justo en las URLs que ningún visitante pide.

    Espera: la regla dura 1 debe ANULAR la medición del 404 y reportar el 200 como
    desprotegido.
    """

    titulo = "caso cabeceras solo en 404"

    def responder(self):
        if self.path in self.rutas:
            return self._pagina(200)                       # sin cabeceras: sale del "cache"
        self._pagina(404, CABECERAS_SEG, "<h1>no existe</h1>")   # el 404 sí las emite


class Caso500(_Base):
    """Una página de la muestra devuelve 500 permanente.

    Espera: se reporta como hallazgo del sitio y NO tumba la auditoría ni contagia la
    severidad de los demás hallazgos (eso se arregló el 22-ago).
    """

    titulo = "caso 500"
    rutas = ("/", "/uno/", "/rota/")

    def responder(self):
        if self.path == "/rota/":
            return self._pagina(500, cuerpo="<h1>error interno</h1>")
        if self.path in self.rutas:
            return self._pagina()
        self._pagina(404, cuerpo="<h1>no existe</h1>")


class CasoRedirecciones(_Base):
    """Cadena /a/ -> /b/ -> /c/ -> documento final.

    Espera: se resuelve al documento final y NO se cuenta tres veces la misma página.
    """

    titulo = "caso redirecciones"
    rutas = ("/", "/a/", "/final/")

    def responder(self):
        saltos = {"/a/": "/b/", "/b/": "/c/", "/c/": "/final/"}
        if self.path in saltos:
            self.send_response(301)
            self.send_header("Location", saltos[self.path])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path in ("/", "/final/"):
            return self._pagina()
        self._pagina(404, cuerpo="<h1>no existe</h1>")


class CasoHTMLInvalido(_Base):
    """HTML roto: etiquetas sin cerrar, anidamiento imposible, atributos sin comillas.

    Espera: no revienta el parseo ni el informe.
    """

    titulo = "caso html invalido"

    def responder(self):
        if self.path in self.rutas:
            return self._pagina(cuerpo=(
                "<!doctype html><html><head><title>roto<body><p><div><h1>hola"
                "<a href=/uno/ >uno<a href=/dos/>dos<ul><li>x<table><tr><p>y"
                "<img src=nada.png alt=><span><b></i></span>"
            ))
        self._pagina(404, cuerpo="<h1>no existe</h1>")


class CasoCharset(_Base):
    """Latin-1 declarado, con acentos y eñes.

    Espera: el texto no sale corrupto en el informe (ni mojibake ni caracteres perdidos).
    """

    titulo = "caso charset"

    def responder(self):
        if self.path in self.rutas:
            cuerpo = (
                "<!doctype html><html lang=es><head>"
                "<meta charset=iso-8859-1><title>Ni\xf1o, a\xf1o, cami\xf3n</title></head>"
                "<body><h1>Ni\xf1o, a\xf1o, cami\xf3n</h1>"
                "<p>Se\xf1al de calibraci\xf3n con e\xf1es y tildes.</p></body></html>"
            ).encode("iso-8859-1")
            return self._pagina(cuerpo=cuerpo, tipo="text/html; charset=iso-8859-1")
        self._pagina(404, cuerpo="<h1>no existe</h1>")


class CasoLento(_Base):
    """Cada respuesta tarda 3 s.

    Espera: la auditoría termina igual, respetando sus propios topes, sin colgarse.
    """

    titulo = "caso lento"
    demora = 3.0

    def responder(self):
        import time
        time.sleep(self.demora)
        if self.path in self.rutas:
            return self._pagina()
        self._pagina(404, cuerpo="<h1>no existe</h1>")


class CasoSinSitemap(_Base):
    """Ni robots.txt ni sitemap.xml: solo enlaces en el menú.

    Espera: cae al descubrimiento por menú y lo declara como tal.
    """

    titulo = "caso sin sitemap"

    def do_GET(self):
        if self.path in ("/robots.txt", "/sitemap.xml"):
            return self._pagina(404, cuerpo="no hay")
        self.responder()


HOSTILES = [
    ("cabeceras-404", CasoCabecerasSoloEn404,
     "Cabeceras de seguridad SOLO en el 404, ninguna en el 200"),
    ("http500", Caso500, "Una página con 500 permanente"),
    ("redirecciones", CasoRedirecciones, "Cadena de 3 redirecciones"),
    ("html-invalido", CasoHTMLInvalido, "HTML sin cerrar y mal anidado"),
    ("charset", CasoCharset, "Latin-1 con eñes y tildes"),
    ("lento", CasoLento, "3 s por respuesta"),
    ("sin-sitemap", CasoSinSitemap, "Sin robots.txt ni sitemap.xml"),
]


class Servidores:
    """Levanta los casos hostiles, cada uno en su puerto. Context manager."""

    def __init__(self, puerto_base=8300):
        self.puerto_base = puerto_base
        self.vivos = []

    def __enter__(self):
        for i, (nombre, clase, _desc) in enumerate(HOSTILES):
            srv = ThreadingHTTPServer(("127.0.0.1", self.puerto_base + i), clase)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            self.vivos.append((nombre, srv, self.puerto_base + i))
        return self

    def __exit__(self, *a):
        for _n, srv, _p in self.vivos:
            srv.shutdown()
            srv.server_close()
        self.vivos = []

    def urls(self):
        return [(n, "http://127.0.0.1:%d/" % p) for n, _s, p in self.vivos]
