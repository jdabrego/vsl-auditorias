"""Dos sitios gemelos: uno con defectos puestos a propósito y otro limpio.

POR QUÉ GEMELOS Y NO UNO SOLO
─────────────────────────────
Probar que el motor ENCUENTRA un defecto no demuestra nada por sí solo: un medidor que
reportara problemas siempre pasaría esa prueba. Y probar que no encuentra nada en un sitio
bueno tampoco: un medidor que no mirara nada también la pasaría.

Lo que sí es falsable es el PAR. El mismo indicador tiene que salir mal en el sucio y bien
en el limpio. Un medidor que grita siempre falla la mitad limpia; uno que no mira falla la
sucia. Solo pasa el que de verdad mide.

Las dos páginas son idénticas salvo por los defectos. Si se agrega algo a una, va a la otra.

EL LADO LIMPIO TIENE QUE ESTAR LIMPIO DE VERDAD
───────────────────────────────────────────────
La primera versión de este archivo daba dos falsos positivos y en los dos el equivocado era
el fixture, no el motor:

  · el canonical decía `http://127.0.0.1/` mientras el sitio se servía en `:8610`, así que
    apuntaba a otra URL y el medidor hizo bien en marcarlo mal
  · Open Graph llevaba 4 etiquetas y el medidor exige las 6 que declara su método

Por eso la página se arma POR PETICIÓN con el Host real: un fixture con la URL escrita a
mano no puede ser canónico de sí mismo. Y la imagen se sirve de verdad, porque el medidor
PIDE la og:image en vez de creerle a la etiqueta.
"""

import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from calibracion.corpus import _Base  # noqa: E402

# Rutas que los dos sirven con 200. El enlace roto del sucio apunta FUERA de esta lista.
RUTAS = ("/", "/servicios/", "/contacto/")

# PNG de 1x1 transparente. Existe para que la og:image y el <img> resuelvan de verdad:
# el eje 4 pide la imagen en vez de creerle a la etiqueta.
LOGO = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)

_NAV = "".join('<a href="%s">%s</a> ' % (r, r.strip("/") or "inicio") for r in RUTAS)

# Un párrafo con cuerpo real: varios medidores necesitan texto para poder decir algo, y
# una página de tres palabras hace que salgan NO_MEDIDO por falta de contenido, no por el
# defecto que se quiere comprobar.
_CUERPO = (
    "<p>Hacemos muebles de madera a medida para casas y oficinas. "
    "Diseñamos cada pieza con el cliente y entregamos en todo el país. "
    "Escribinos y te pasamos un presupuesto sin compromiso.</p>"
)


def sucia(base, ruta):
    """Página CON defectos. Cada uno está anotado con el indicador que debe encontrarlo."""
    return (
        "<!doctype html><html><head><meta charset=utf-8>"      # <html> sin lang
        "<title>Inicio</title>"
        # · sin <meta name=description>   -> eje4.meta_description
        # · sin <link rel=canonical>      -> eje4.canonical
        # · sin <meta name=viewport>      -> eje2.viewport_meta
        # · sin Open Graph                -> eje4.open_graph
        # · sin lang en <html>            -> eje4.idioma_html / eje6.idioma_documento
        "</head><body>"
        "<h1>Muebles a medida</h1>"
        "<h1>Segundo encabezado principal</h1>"                # dos H1 -> eje4.h1_por_pagina
        "<nav>{nav}<a href='/no-existe-en-ningun-lado/'>catálogo</a></nav>"   # enlace roto
        "<img src='/logo.png'>"                                # sin alt -> eje4/eje6.alt_imagenes
        "{cuerpo}"
        "</body></html>"
    ).format(nav=_NAV, cuerpo=_CUERPO)


def limpia(base, ruta):
    """La misma página con los defectos corregidos. `base` es el origen REAL de la corrida.

    El canonical y og:url se arman con el Host de la petición a propósito: escritos a mano
    apuntarían a otro puerto y el sitio dejaría de ser canónico de sí mismo — que es justo
    el falso positivo que dio la primera versión de este archivo.
    """
    aqui = base + ruta
    return (
        "<!doctype html><html lang=es><head><meta charset=utf-8>"
        "<title>Muebles de madera a medida</title>"
        "<meta name=description content='Muebles de madera hechos a medida "
        "para casas y oficinas, con entrega en todo el país.'>"
        "<meta name=viewport content='width=device-width, initial-scale=1'>"
        "<link rel=canonical href='{aqui}'>"
        "<meta property='og:title' content='Muebles de madera a medida'>"
        "<meta property='og:description' content='Muebles de madera hechos a medida.'>"
        "<meta property='og:url' content='{aqui}'>"
        "<meta property='og:type' content='website'>"
        "<meta property='og:site_name' content='Muebles a medida'>"
        "<meta property='og:image' content='{base}/logo.png'>"
        "</head><body>"
        "<main>"
        "<h1>Muebles de madera a medida</h1>"
        "<nav>{nav}</nav>"
        "<img src='/logo.png' alt='Mesa de roble terminada en el taller'>"
        "{cuerpo}"
        "</main>"
        "</body></html>"
    ).format(aqui=aqui, base=base, nav=_NAV, cuerpo=_CUERPO)


class _Gemela(_Base):
    """Sirve la misma página en las tres rutas, el logo de verdad, y 404 en lo demás."""

    rutas = RUTAS
    arma = staticmethod(lambda base, ruta: "")

    def responder(self):
        if self.path == "/logo.png":
            return self._pagina(200, cuerpo=LOGO, tipo="image/png")
        if self.path in self.rutas:
            base = "http://" + self.headers.get("Host", "127.0.0.1")
            return self._pagina(200, cuerpo=self.arma(base, self.path))
        self._pagina(404, cuerpo="<h1>no existe</h1>")


class SitioSucio(_Gemela):
    titulo = "sitio con defectos"
    arma = staticmethod(sucia)


class SitioLimpio(_Gemela):
    titulo = "sitio limpio"
    arma = staticmethod(limpia)


GEMELOS = (("sucio", SitioSucio), ("limpio", SitioLimpio))
