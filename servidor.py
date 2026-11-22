#!/usr/bin/env python3
"""Vitrina de auditorías VSL — arranque.

    AUTH_FILE=~/.auditorias-auth RAICES=~/VSL-Auditorias:~/Projects \\
        python3 servidor.py 8096

Escucha SOLO en 127.0.0.1: quien llega de afuera es el túnel de Cloudflare, que además
pone el TLS en el borde. Es el mismo trato que motor.voidspore.tech.

Esta vitrina es de solo lectura. No crea, no borra y no modifica ninguna auditoría.
"""

import os
import sys
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from vitrina import almacen, web  # noqa: E402
from vitrina.cola import Cola  # noqa: E402
from vitrina.indice import Indice  # noqa: E402

RAICES_POR_DEFECTO = "~/VSL-Auditorias:~/Projects"
# Donde caen las auditorias que se lanzan DESDE la web. Las de terminal siguen yendo a
# donde auditar.sh decida; esta es solo la casa de las que nacen aca.
# Dos casas: lo que lanza Daniel desde el M4 cae en la privada; lo que lanza un
# visitante de internet, en la pública, que es la que se ve.
CASA = Path(os.environ.get("CASA", "~/VSL-Auditorias-privado")).expanduser()
CASA_PUBLICA = Path(os.environ.get("CASA_PUBLICA", "~/VSL-Auditorias")).expanduser()
PUERTO_POR_DEFECTO = 8096


def registrar(mensaje):
    print("[{}] {}".format(datetime.now().strftime("%H:%M:%S"), mensaje), flush=True)


def credencial_de(ruta_env):
    """Lee usuario:clave de AUTH_FILE. Sin archivo, no hay clave y se avisa fuerte."""
    if not ruta_env:
        return ""
    p = Path(ruta_env).expanduser()
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError:
        registrar("AVISO: no se pudo leer AUTH_FILE ({}). Se sigue SIN clave.".format(p))
        return ""


def main(argv):
    puerto = int(argv[1]) if len(argv) > 1 else PUERTO_POR_DEFECTO
    raices = almacen.raices_desde(os.environ.get("RAICES", RAICES_POR_DEFECTO))
    credencial = credencial_de(os.environ.get("AUTH_FILE", ""))

    if not raices:
        registrar("ERROR: ninguna de las raíces configuradas existe. RAICES="
                  + os.environ.get("RAICES", RAICES_POR_DEFECTO))
        return 1

    cola = Cola(CASA, registrar, max_paginas=os.environ.get("SITE_AUDIT_MAX_PAGINAS"),
                base_publica=CASA_PUBLICA)
    cola.arrancar()

    # Raíces cuyas auditorías SOLO se ven desde el M4 o la red privada.
    #
    # La casa privada de la cola se agrega SIEMPRE, aunque no esté en RAICES_PRIVADAS:
    # dónde cae una auditoría (CASA) y qué se ve (RAICES_PRIVADAS) eran dos variables
    # independientes que coincidían por disciplina del plist. Cambiar una sin la otra
    # publicaba el trabajo de cliente o lo hacía invisible hasta para Daniel, y los dos
    # defectos del código apuntaban en direcciones opuestas. Ahora es por construcción.
    privadas = almacen.raices_desde(os.environ.get("RAICES_PRIVADAS", ""))
    if cola.base not in privadas:
        privadas.append(cola.base.resolve())

    indice = Indice(raices, raices_privadas=privadas)
    fichas = indice.fichas()

    registrar("raíces: " + ", ".join(str(r) for r in raices))
    if privadas:
        registrar("raíces PRIVADAS (no salen a internet): "
                  + ", ".join(str(r) for r in privadas))
        registrar("auditorías ocultas para internet: {}".format(
            sum(1 for f in fichas if f.get("privada"))))
    registrar("auditorías indexadas: {}".format(len(fichas)))
    registrar("motor activo · desde el M4 caen en {} · desde internet en {}".format(
        CASA, CASA_PUBLICA))
    for ident, primera, repetida in indice.colisiones():
        registrar("COLISIÓN de id '{}': se usa {} y se ignora {}".format(ident, primera, repetida))
    if not credencial:
        # A gritos y por stderr, como el panel del motor: un panel abierto que arranca
        # en silencio es un panel que se publica sin que nadie se entere.
        print(
            "AVISO: sin AUTH_FILE la vitrina queda ABIERTA. No la publiques así.",
            file=sys.stderr,
            flush=True,
        )

    # Lanzar desde internet está permitido, con tope por hora. Ver web.Cuota.
    cuota = web.Cuota(tope=int(os.environ.get("CUOTA_POR_HORA", "6")))
    registrar("cuota desde internet: {} auditorías por hora".format(cuota.tope))

    handler = web.constructor(indice, credencial, registrar, cola=cola, cuota=cuota)
    servidor = ThreadingHTTPServer(("127.0.0.1", puerto), handler)
    registrar("escuchando en http://127.0.0.1:{}".format(puerto))
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        registrar("cerrando")
    finally:
        servidor.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
