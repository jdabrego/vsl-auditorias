"""indice — convierte cada directorio de auditoria en una ficha para el listado.

Regla que se hereda de la skill site-audit: aqui NO se calcula, NO se estima y NO se
completa nada. Cada campo de la ficha sale tal cual de resultados.json. Lo unico que
este modulo "cuenta" es el largo de listas que ya vienen escritas.

Y la consecuencia incomoda de esa regla: si una auditoria midio 15 de 47 paginas, la
ficha lo dice. Mostrar "6.6/10" a secas seria mentir igual que mentia el informe que se
arreglo el 22-ago-2026.
"""

import json
import os
from pathlib import Path

from . import almacen

EJES = ("eje1", "eje2", "eje3", "eje4", "eje5", "eje6")

ARTEFACTOS = (
    ("html", "informe.html"),
    ("pdf", "informe.pdf"),
    ("json", "resultados.json"),
)


def ficha(ident, directorio):
    """Arma la ficha de una auditoria. Nunca lanza: una auditoria rota sale marcada."""
    base = {
        "id": ident,
        "directorio": str(directorio),
        "ilegible": False,
        "motivo": None,
        "estado": _estado(directorio),
        "artefactos": _artefactos(directorio),
        "markdown": _markdown(directorio),
    }
    ruta = Path(directorio) / almacen.MARCA
    if not ruta.is_file():
        # Trabajo encolado o corriendo: todavia no midio nada. No es un error, y marcarlo
        # "ilegible" seria mentir sobre lo que pasa.
        est = _estado_completo(directorio)
        base.update({
            "sitio": est.get("sitio") or ident,
            "url": est.get("url"),
            "cliente": est.get("cliente"),
            "fecha_iso": est.get("encolado_en"),
            "puntaje_global": None,
            "ejes": [],
            "cobertura": None,
            "hallazgos": 0,
            "paso": est.get("paso"),
            "error": est.get("error"),
            "en_curso": base["estado"] in ("en_cola", "corriendo"),
        })
        return base
    try:
        with open(ruta, "r", encoding="utf-8") as fh:
            datos = json.load(fh)
    except (OSError, ValueError) as err:
        # No se esconde: una auditoria ilegible que desaparece del listado es una
        # auditoria que nadie arregla.
        base["ilegible"] = True
        base["motivo"] = "{}: {}".format(type(err).__name__, err)
        base["sitio"] = ident
        return base

    if not isinstance(datos, dict):
        base["ilegible"] = True
        base["motivo"] = "resultados.json no es un objeto"
        base["sitio"] = ident
        return base

    base.update(
        {
            "sitio": datos.get("sitio") or ident,
            "url": datos.get("url"),
            "cliente": datos.get("cliente"),
            "fecha_iso": datos.get("fecha_iso"),
            "puntaje_global": datos.get("puntaje_global"),
            "duracion_segundos": datos.get("duracion_segundos"),
            "version_esquema": datos.get("version_esquema"),
            "ejes": _ejes(datos),
            "cobertura": _cobertura(datos),
            "hallazgos": _hallazgos(datos),
        }
    )
    base["delta"] = _delta(directorio)
    est = _estado_completo(directorio)
    base["paso"] = est.get("paso")
    base["error"] = est.get("error")
    base["en_curso"] = base["estado"] in ("en_cola", "corriendo")
    return base


def _ejes(datos):
    salida = []
    ejes = datos.get("ejes") or {}
    for eid in EJES:
        e = ejes.get(eid)
        if not isinstance(e, dict):
            continue
        salida.append(
            {
                "id": eid,
                "nombre": e.get("nombre") or eid,
                "puntaje": e.get("puntaje"),
                "hallazgos": len(e.get("hallazgos") or []),
                "error": e.get("error"),
            }
        )
    return salida


def _cobertura(datos):
    c = datos.get("cobertura_paginas")
    if not isinstance(c, dict):
        return None
    return {
        "auditadas": c.get("auditadas"),
        "descubiertas": c.get("descubiertas"),
        "parcial": bool(c.get("muestra_parcial")),
    }


def _hallazgos(datos):
    total = 0
    for e in (datos.get("ejes") or {}).values():
        if isinstance(e, dict):
            total += len(e.get("hallazgos") or [])
    return total


def _delta(directorio):
    """El cambio contra la corrida anterior del mismo sitio, si lo hay.

    Lo escribe la cola llamando a comparar.mjs al terminar. Acá NO se calcula nada: se
    lee lo que ya se comparó, igual que con todo lo demás de este módulo.
    """
    d = almacen.leer_json(directorio, "comparacion.json")
    if not isinstance(d, dict):
        return None
    g = d.get("global") or {}
    r = d.get("resumen") or {}
    return {
        "contra": d.get("contra"),
        "antes": g.get("antes"),
        "ahora": g.get("despues"),
        "diferencia": g.get("diferencia"),
        "sentido": g.get("sentido"),
        "mejoran": r.get("mejoran"),
        "empeoran": r.get("empeoran"),
        "mismo_conjunto": (d.get("paginas") or {}).get("mismo_conjunto"),
    }


def _estado_completo(directorio):
    datos = almacen.leer_json(directorio, almacen.MARCA_TRABAJO)
    return datos if isinstance(datos, dict) else {}


def _estado(directorio):
    """Costura para la fase 2 (motor): si hay estado.json, se respeta.

    Mientras la fase 2 no exista, ningun directorio tiene estado.json y todo sale
    'listo'. Cuando exista, una auditoria corriendo aparecera como tal sin tocar nada
    de este modulo.
    """
    estado = _estado_completo(directorio).get("estado")
    return estado if isinstance(estado, str) and estado else "listo"


def _artefactos(directorio):
    d = Path(directorio)
    return {clave: (d / nombre).is_file() for clave, nombre in ARTEFACTOS}


def _markdown(directorio):
    try:
        for nombre in sorted(os.listdir(directorio)):
            if almacen.es_servible(nombre) and nombre.startswith(almacen.PREFIJO_MARKDOWN):
                return nombre
    except OSError:
        pass
    return None


def _clave_orden(f):
    """Mas nueva primero. Las sin fecha caen al final en vez de romper el orden."""
    return (f.get("fecha_iso") or "", f.get("id") or "")


class Indice:
    """El indice completo, recalculado cuando el disco cambia.

    No hay base de datos ni cache que se pueda quedar vieja de forma invisible: se
    guarda la huella (ruta + mtime de cada resultados.json) y si cambia, se rehace.
    Escanear unas decenas de directorios es instantaneo; una cache que miente no.
    """

    def __init__(self, raices, hondura=4, raices_privadas=()):
        """
        raices           auditorías que ve cualquiera, incluida internet.
        raices_privadas  auditorías que SOLO se ven desde el M4 o la red privada.

        Existe porque la vitrina se abrió para demostrarla, y una demo no tiene por qué
        exponer los puntajes de seguridad de los clientes. Marcar la ficha no alcanza:
        quien sepa el id podría pedir /a/<id>/ igual, así que la marca se comprueba
        también al servir cada archivo.
        """
        # Una raíz privada se agrega al escaneo sólo si no cuelga ya de otra: si no,
        # los mismos directorios se caminan dos veces y salen como colisión de id.
        self.raices = list(raices) + [
            r for r in raices_privadas
            if not any(almacen.dentro_de(x, r) for x in raices)
        ]
        self.privadas = list(raices_privadas)
        self.hondura = hondura
        self._huella = None
        self._fichas = []
        self._colisiones = []

    def _es_privada(self, directorio):
        """La contención la decide almacen.dentro_de, igual que el traversal.

        Comparar strings aquí sería sensible a la barra final, a los // colapsados y a
        las mayúsculas de APFS — justo donde equivocarse significa publicar en vez de
        fallar.
        """
        try:
            real = Path(directorio).resolve()
        except OSError:
            return True          # ante la duda, privada: nunca se filtra por accidente
        return any(almacen.dentro_de(raiz, real) for raiz in self.privadas)

    def fichas(self, incluir_privadas=True):
        pares, colisiones = almacen.descubrir(self.raices, self.hondura)
        huella = self._huella_de(pares)
        if huella != self._huella:
            todas = []
            for i, d in pares:
                f = ficha(i, d)
                f["privada"] = self._es_privada(d)
                todas.append(f)
            self._fichas = sorted(todas, key=_clave_orden, reverse=True)
            self._colisiones = colisiones
            self._huella = huella
        if incluir_privadas:
            return self._fichas
        return [f for f in self._fichas if not f.get("privada")]

    def colisiones(self):
        # Sólo se escanea si todavía no se escaneó nunca. Antes llamaba a fichas()
        # incondicionalmente, y como el handler pide fichas() y colisiones() en la misma
        # expresión, cada petición al índice caminaba los directorios DOS veces: la
        # mitad del tiempo de servir la portada era el mismo escaneo repetido.
        if self._huella is None:
            self.fichas()
        return self._colisiones

    def por_id(self, ident, incluir_privadas=True):
        """Busca por id. Con incluir_privadas=False una auditoría privada NO existe.

        Es lo que impide que alguien que conozca el id la pida directo por /a/<id>/.
        Filtrar sólo el listado sería seguridad de escaparate.
        """
        for f in self.fichas(incluir_privadas=incluir_privadas):
            if f["id"] == ident:
                return f
        return None

    @staticmethod
    def _huella_de(pares):
        """Huella = mtime de resultados.json Y de estado.json.

        Con solo resultados.json, un trabajo en curso tendria huella constante (el archivo
        todavia no existe) y la cache no se invalidaria nunca: la pagina se quedaria
        congelada en "esperando turno" durante los cinco minutos que dura la medicion.
        """
        marcas = []
        for ident, directorio in pares:
            tiempos = []
            for nombre in (almacen.MARCA, almacen.MARCA_TRABAJO):
                try:
                    tiempos.append((Path(directorio) / nombre).stat().st_mtime_ns)
                except OSError:
                    tiempos.append(0)
            marcas.append((ident, str(directorio), tuple(tiempos)))
        return tuple(marcas)
