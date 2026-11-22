"""campana — corre la campaña completa.

No orquesta nada por su cuenta: encola en la vitrina, que ya es serial (obligatorio, el
eje 1 debe correr solo), ya sobrevive reinicios y ya deja todo indexado. Acá solo se
espera, se aplica el veredicto y se escribe el informe.

    python3 -m calibracion.campana [--solo-hostiles] [--una-corrida]

Compatible con Python 3.9.
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

import subprocess

from vitrina import almacen

from . import oraculos, veredicto
from .corpus import HOSTILES, Servidores, reales

from vitrina.cola import SKILL  # noqa: E402  (misma ubicación del motor que la cola)
HOSTILES_CASA = Path.home() / "VSL-Auditorias-hostiles"

VITRINA = "http://127.0.0.1:8096"
# Las MISMAS dos casas que servidor.py, leídas del mismo entorno. La campaña POSTea con
# X-Vitrina, así que para la vitrina no viene de internet y sus auditorías caen en la
# casa PRIVADA: mirar sólo la pública dejaba a la campaña esperando un directorio que
# estaba naciendo en otro lado, y terminaba en "se encoló X pero no apareció".
CASA = Path(os.environ.get("CASA", "~/VSL-Auditorias-privado")).expanduser()
CASA_PUBLICA = Path(os.environ.get("CASA_PUBLICA", "~/VSL-Auditorias")).expanduser()
CASAS = [CASA, CASA_PUBLICA]
SALIDA = Path(__file__).resolve().parent.parent / "docs" / "campanas"
ESPERA_MAX = 60 * 20          # 20 min por auditoría: de sobra para las de 6


def registrar(msg):
    print("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg), flush=True)


def _pedir(camino, datos=None):
    url = VITRINA + camino
    cuerpo = urllib.parse.urlencode(datos).encode() if datos else None
    req = urllib.request.Request(url, data=cuerpo)
    req.add_header("X-Vitrina", "1")      # prueba de origen: ver web.mismo_origen()
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, r.read().decode("utf-8", "replace")


def _dirs():
    return {d.name: d for casa in CASAS for d in casa.glob("*/")}


def ruta_de(ident):
    return _dirs().get(ident)


def encolar(url, cliente=None):
    antes = set(_dirs())
    _pedir("/auditar", {"url": url, "cliente": cliente or ""})
    for _ in range(40):
        nuevas = set(_dirs()) - antes
        if nuevas:
            return sorted(nuevas)[-1]
        time.sleep(0.25)
    raise RuntimeError("se encoló %s pero no apareció el directorio" % url)


def esperar(ident):
    """Espera a que el trabajo llegue a un estado terminal. Devuelve (estado, segundos)."""
    t0 = time.time()
    while time.time() - t0 < ESPERA_MAX:
        d = ruta_de(ident)
        e = almacen.leer_json(d, "estado.json") if d else None
        if not isinstance(e, dict):
            time.sleep(3)
            continue
        if e.get("estado") in ("listo", "fallo", "interrumpido", "cancelado"):
            return e.get("estado"), round(time.time() - t0)
        time.sleep(5)
    return "timeout", round(time.time() - t0)


def correr_hostil(nombre, url, vuelta):
    """Los hostiles NO van por la cola.

    Todos viven en 127.0.0.1, así que la convención de nombres del motor les daría a los
    siete el mismo id y en el informe serían indistinguibles. Como lo que prueban es el
    MOTOR y no la cola —a la que ya la dogfoodean las 16 corridas de sitios reales—, se
    lanzan directo con --salida a un directorio con nombre propio.
    """
    HOSTILES_CASA.mkdir(parents=True, exist_ok=True)
    d = HOSTILES_CASA / ("%s-v%d" % (nombre, vuelta))
    if d.exists():
        import shutil
        shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    log = d / "corrida.log"
    with open(log, "w", encoding="utf-8") as fh:
        try:
            p = subprocess.run(
                ["bash", str(SKILL / "scripts" / "auditar.sh"), url, "--salida", str(d)],
                stdout=fh, stderr=subprocess.STDOUT, timeout=ESPERA_MAX,
                cwd=str(d), start_new_session=True)
            codigo = p.returncode
        except subprocess.TimeoutExpired:
            return d, "timeout"
    if codigo != 0:
        return d, "fallo"
    if not (d / "resultados.json").is_file():
        return d, "sin_resultados"
    subprocess.run(["node", str(SKILL / "scripts" / "informe.mjs"),
                    str(d / "resultados.json"), "--salida", str(d), "--sin-pdf"],
                   capture_output=True, timeout=300)
    return d, "listo"


def _host(url):
    return urllib.parse.urlparse(url).netloc.split(":")[0]


def evaluar(ident, url, es_hostil):
    return evaluar_dir(ruta_de(ident) or (CASA / ident), url, es_hostil)


def evaluar_dir(d, url, es_hostil):
    """Aplica las tres capas a un trabajo terminado."""
    d = Path(d)
    r = {"id": d.name, "url": url, "hostil": es_hostil}
    r["invariantes"] = veredicto.revisar(d)

    # Capa 3: los oráculos. Sobre los hostiles no aplican (son localhost sin TLS ni
    # sitemap público) salvo el de cabeceras, que es justo el que prueba el caso
    # de cabeceras-solo-en-404.
    r["oraculos"] = {}
    try:
        r["oraculos"]["cabeceras"] = oraculos.contrastar_cabeceras(d)
    except Exception as err:
        r["oraculos"]["cabeceras"] = {"estado": "reventó", "detalle": repr(err),
                                     "discrepancias": []}
    if not es_hostil:
        for nombre, fn, arg in (("tls", oraculos.contrastar_tls, _host(url)),
                                ("descubrimiento", oraculos.contrastar_descubrimiento, url)):
            try:
                r["oraculos"][nombre] = fn(d, arg)
            except Exception as err:
                r["oraculos"][nombre] = {"estado": "reventó", "detalle": repr(err),
                                         "discrepancias": []}
    return r


def problemas_de(r):
    """Todo lo que cuenta como defecto en un resultado ya evaluado."""
    p = []
    if r.get("estado_final") not in ("listo",):
        p.append("terminó en estado %s" % r.get("estado_final"))
    inv = r.get("invariantes") or {}
    for f in inv.get("fallos") or []:
        p.append("invariante %s: %s" % (f["invariante"], f["detalle"]))
    if not r["hostil"] and inv.get("pct_no_medido", 0) > 20:
        p.append("%.1f%% de indicadores sin medir (umbral 20%%)" % inv["pct_no_medido"])
    for nombre, o in (r.get("oraculos") or {}).items():
        if o.get("estado") == "reventó":
            p.append("el oráculo %s reventó: %s" % (nombre, o.get("detalle")))
        for dsc in o.get("discrepancias") or []:
            p.append("oráculo %s: %s (motor=%s)"
                     % (nombre, dsc.get("indicador"), dsc.get("motor")))
    return p


def main(argv):
    solo_hostiles = "--solo-hostiles" in argv
    corridas = 1 if "--una-corrida" in argv else 2
    SALIDA.mkdir(parents=True, exist_ok=True)

    with Servidores() as srv:
        objetivos = []
        if not solo_hostiles:
            objetivos += [("https://%s/" % s, False, motivo) for s, motivo in reales()]
        mapa = dict((n, d) for n, _c, d in [(h[0], h[1], h[2]) for h in HOSTILES])
        objetivos += [(u, True, mapa.get(n, n)) for n, u in srv.urls()]

        registrar("campaña: %d objetivos × %d corrida(s)" % (len(objetivos), corridas))
        resultados = []
        for vuelta in range(1, corridas + 1):
            for url, hostil, motivo in objetivos:
                if hostil:
                    nombre = dict((u, n) for n, u in srv.urls())[url]
                    d, estado = correr_hostil(nombre, url, vuelta)
                    registrar("v%d %-46s %-12s (directo)"
                      % (vuelta, nombre, estado))
                    r = evaluar_dir(d, url, True)
                    r.update(vuelta=vuelta, estado_final=estado, motivo=motivo, id=d.name)
                    r["problemas"] = problemas_de(r)
                    resultados.append(r)
                    continue
                try:
                    ident = encolar(url, "calibración")
                except Exception as err:
                    registrar("NO SE PUDO ENCOLAR %s: %r" % (url, err))
                    resultados.append({"id": None, "url": url, "hostil": hostil,
                                       "vuelta": vuelta, "estado_final": "no_encolado",
                                       "invariantes": {}, "oraculos": {},
                                       "problemas": ["no se pudo encolar: %r" % err]})
                    continue
                estado, seg = esperar(ident)
                registrar("v%d %-46s %-12s %4ds" % (vuelta, url[:46], estado, seg))
                r = evaluar(ident, url, hostil)
                r.update(vuelta=vuelta, estado_final=estado, segundos=seg, motivo=motivo)
                r["problemas"] = problemas_de(r)
                resultados.append(r)

    crudo = SALIDA / ("campana-%s.json" % datetime.now().strftime("%Y-%m-%d"))
    crudo.write_text(json.dumps(resultados, ensure_ascii=False, indent=1), encoding="utf-8")
    registrar("crudo -> %s" % crudo)

    con = sum(1 for r in resultados if r.get("problemas"))
    registrar("terminado: %d objetivo-corridas, %d con problemas" % (len(resultados), con))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
