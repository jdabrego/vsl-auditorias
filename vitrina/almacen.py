"""almacen — dónde viven las auditorías y qué ruta se puede servir.

La vitrina NO es dueña de los archivos. `auditar.sh` ya define la convencion VSL
(`~/Projects/<proyecto>/docs/evidencia/<sitio>-YYYY-MM-DD/`), asi que aqui solo se
escanean raices y se considera auditoria a todo directorio que tenga resultados.json.
El sistema de archivos es la fuente de verdad: mover o borrar desde el Finder funciona
sin que nada quede desincronizado, porque no hay base de datos que sincronizar.

Compatible con Python 3.9 (el /usr/bin/python3 del M4).
"""

import json
import os
from pathlib import Path

MARCA = "resultados.json"

# Desde la fase 2 un directorio tambien es una auditoria si solo tiene estado.json: es un
# trabajo encolado o corriendo, que todavia no produjo resultados. Sin esto, lo que acabas
# de encolar no aparece en ningun lado hasta que termina — cinco minutos de pantalla muda.
MARCA_TRABAJO = "estado.json"

# Lo que la vitrina esta dispuesta a servir de dentro de una auditoria. Es una lista
# blanca a proposito: el directorio tiene evidencia cruda (trazas de Lighthouse, HTML
# de paginas del cliente) que no tiene por que salir a la web por descuido.
SERVIBLES = {
    "informe.html": "text/html; charset=utf-8",
    "informe.pdf": "application/pdf",
    "resultados.json": "application/json; charset=utf-8",
    "puntaje-detalle.json": "application/json; charset=utf-8",
    "clasificacion-hallazgos.json": "application/json; charset=utf-8",
    "estado-ejes.json": "application/json; charset=utf-8",
}

# El markdown se llama AUDITORIA-<sitio>-<fecha>.md, asi que no se puede poner en la
# lista blanca por nombre exacto.
SUFIJO_MARKDOWN = ".md"
PREFIJO_MARKDOWN = "AUDITORIA-"


def raices_desde(texto):
    """Convierte "~/a:~/b" en una lista de Path existentes y absolutos.

    Las raices que no existen se descartan en silencio: es normal que una este
    configurada y todavia no tenga nada dentro.
    """
    raices = []
    for trozo in (texto or "").split(os.pathsep):
        trozo = trozo.strip()
        if not trozo:
            continue
        p = Path(trozo).expanduser()
        try:
            p = p.resolve()
        except OSError:
            continue
        if p.is_dir() and p not in raices:
            raices.append(p)
    return raices


def leer_json(directorio, nombre, por_defecto=None):
    """Lee un JSON de dentro de una auditoria, o `por_defecto` si no se puede.

    Una sola respuesta para "el archivo no esta, o esta a medio escribir, o tiene
    basura". Antes cada modulo tenia la suya —cinco copias del mismo try/except— y la
    politica de tolerancia solo se podia cambiar acordandose de las cinco.
    """
    try:
        with open(Path(directorio) / nombre, "r", encoding="utf-8") as fh:
            datos = json.load(fh)
    except (OSError, ValueError):
        return por_defecto
    return datos


def dentro_de(base, ruta):
    """¿`ruta` es `base` o cuelga de ella? Sobre las rutas REALES, no sobre el texto.

    Lo usan las dos comprobaciones que custodian algo: la que frena el traversal al
    servir un archivo, y la que decide si una auditoria es privada. Comparar strings
    aqui seria fragil (barra final, // colapsados, mayusculas en APFS) justo donde
    equivocarse significa filtrar, no fallar.
    """
    try:
        base_real = Path(base).resolve()
        destino = Path(ruta).resolve()
    except OSError:
        return False
    return base_real == destino or base_real in destino.parents


def descubrir(raices, hondura=4):
    """Encuentra auditorias bajo las raices.

    Devuelve una lista de (id, directorio) ordenada por id. El id es el nombre del
    directorio, que la convencion VSL ya hace unico y legible ("ejemplo.com-2026-08-22").

    Ante una colision de id entre dos raices gana la raiz declarada primero, y la
    colision se devuelve aparte para que quien llame la registre: descartar en silencio
    una auditoria que existe es la clase de cosa que se descubre tarde y mal.
    """
    encontradas = {}
    colisiones = []
    for raiz in raices:
        for directorio in _caminar(raiz, hondura):
            if not ((directorio / MARCA).is_file() or (directorio / MARCA_TRABAJO).is_file()):
                continue
            ident = directorio.name
            if ident in encontradas:
                colisiones.append((ident, encontradas[ident], directorio))
                continue
            encontradas[ident] = directorio
    return sorted(encontradas.items()), colisiones


def _caminar(raiz, hondura):
    """Recorre raiz hasta `hondura` niveles sin seguir symlinks ni entrar en ruido."""
    ignorar = {"node_modules", ".git", "__pycache__", ".venv", "venv"}
    pendientes = [(raiz, 0)]
    while pendientes:
        actual, nivel = pendientes.pop()
        yield actual
        if nivel >= hondura:
            continue
        try:
            hijos = list(os.scandir(actual))
        except OSError:
            continue
        for h in hijos:
            # follow_symlinks=False: un symlink a / convertiria el escaneo en un paseo
            # por todo el disco.
            if (h.is_dir(follow_symlinks=False) and h.name not in ignorar
                    and not h.name.startswith(".")):
                pendientes.append((Path(h.path), nivel + 1))


def es_servible(nombre):
    """¿Este archivo se puede entregar por la web?"""
    if nombre in SERVIBLES:
        return True
    return nombre.startswith(PREFIJO_MARKDOWN) and nombre.endswith(SUFIJO_MARKDOWN)


def tipo_de(nombre):
    if nombre in SERVIBLES:
        return SERVIBLES[nombre]
    return "text/markdown; charset=utf-8"


def ruta_segura(base, pedido):
    """Resuelve `pedido` dentro de `base`, o None si se sale.

    Dos comprobaciones, y las dos hacen falta:

    1. Que el nombre este en la lista blanca. Sin esto se sirve evidencia cruda.
    2. Que la ruta REAL (con symlinks ya resueltos) siga dentro de la base real. Con
       resolve() sobre ambas, `../../.ssh/id_rsa` y un symlink que apunte fuera dan
       igual: los dos quedan afuera. Comprobar sobre la ruta sin resolver seria
       decorativo, porque un symlink pasa esa prueba y despues lee donde quiera.
    """
    nombre = (pedido or "").strip("/")
    if not nombre or not es_servible(nombre):
        return None
    # Un pedido con separadores ya es sospechoso: los servibles son todos de un nivel.
    if "/" in nombre or "\\" in nombre or nombre in (".", ".."):
        return None
    try:
        destino = (Path(base).resolve() / nombre).resolve()
    except OSError:
        return None
    if not dentro_de(base, destino):
        return None
    if not destino.is_file():
        return None
    return destino
