"""veredicto — invariantes que se comprueban sobre resultados.json sin verdad externa.

Ninguna de estas necesita saber cómo es el sitio de verdad: son afirmaciones que el
informe hace sobre sí mismo y que tienen que ser coherentes. Una invariante rota es un
defecto seguro, no una sospecha.

Compatible con Python 3.9.
"""

import json
import re
from pathlib import Path

NO_MEDIDO = "NO_MEDIDO"


def _fallo(inv, detalle):
    return {"invariante": inv, "detalle": detalle}


def _ejes(doc):
    ejes = doc.get("ejes")
    if not isinstance(ejes, dict):
        return {}
    return {k: v for k, v in ejes.items() if isinstance(v, dict)}


def _indicadores(doc):
    """Indicadores aplanados. Tolera basura: un resultados.json corrupto es justo lo que
    esto tiene que poder revisar sin morirse."""
    salida = {}
    ejes = doc.get("ejes")
    if not isinstance(ejes, dict):
        return salida
    for eid, e in ejes.items():
        if not isinstance(e, dict):
            continue
        ind = e.get("indicadores")
        if not isinstance(ind, dict):
            continue
        for k, v in ind.items():
            if isinstance(v, dict):
                salida[k] = (eid, v)
    return salida


# --- las invariantes --------------------------------------------------------

def inv_puntajes_en_rango(doc):
    fallos = []
    g = doc.get("puntaje_global")
    if not isinstance(g, (int, float)) or not (0 <= g <= 10):
        fallos.append(_fallo("puntajes_en_rango", "puntaje_global = %r" % (g,)))
    for eid, e in _ejes(doc).items():
        p = e.get("puntaje")
        if p is None or p == NO_MEDIDO:
            # Un eje ENTERO puede estar legítimamente sin medir: pasa con los sitios de
            # localhost, donde PageSpeed no puede llegar y el eje 1 se niega a puntuar.
            # La primera versión de esta invariante lo marcaba como defecto y produjo 14
            # falsos positivos en la campaña del 24-ago.
            continue
        if not isinstance(p, (int, float)) or not (0 <= p <= 10):
            fallos.append(_fallo("puntajes_en_rango", "%s.puntaje = %r" % (eid, p)))
    return fallos


def inv_global_es_la_suma_ponderada(doc):
    """El global tiene que salir de los ejes y sus ponderaciones publicadas.

    Si no cuadra, o el global se calculó de otra forma o las ponderaciones que se
    publican no son las que se usaron. Cualquiera de las dos es grave: el número más
    visible del informe estaría sin respaldo.
    """
    pond = doc.get("ponderaciones") or {}
    if not pond:
        return [_fallo("global_es_suma_ponderada", "el informe no publica ponderaciones")]
    suma = peso = 0.0
    for eid, w in pond.items():
        p = (_ejes(doc).get(eid) or {}).get("puntaje")
        if isinstance(p, (int, float)):
            suma += p * w
            peso += w
    if peso == 0:
        return [_fallo("global_es_suma_ponderada", "ningún eje con puntaje")]
    esperado = suma / peso
    real = doc.get("puntaje_global")
    if not isinstance(real, (int, float)) or abs(esperado - real) > 0.06:
        return [_fallo("global_es_suma_ponderada",
                       "publicado %r pero los ejes ponderados dan %.3f" % (real, esperado))]
    return []


def inv_no_medido_con_motivo(doc):
    fallos = []
    for k, (eid, v) in _indicadores(doc).items():
        if v.get("valor") == NO_MEDIDO or v.get("estado") == "no_medido":
            if not (v.get("motivo_no_medido") or "").strip():
                fallos.append(_fallo("no_medido_con_motivo", "%s sin motivo_no_medido" % k))
    return fallos


def inv_todo_numero_con_procedencia(doc):
    fallos = []
    for k, (eid, v) in _indicadores(doc).items():
        if v.get("valor") == NO_MEDIDO:
            continue
        if not (v.get("metodo") or "").strip():
            fallos.append(_fallo("procedencia", "%s sin metodo" % k))
        if not (v.get("desde") or "").strip():
            fallos.append(_fallo("procedencia", "%s sin desde" % k))
    return fallos


def inv_esfuerzo_nunca_es_numero(doc):
    """La guardia del 22-ago, comprobada desde fuera del motor.

    Dentro, hallazgo() tira excepción. Acá se verifica el producto terminado: si algún
    día alguien añade un hallazgo por otra vía, esto lo caza igual.
    """
    fallos = []
    for eid, e in _ejes(doc).items():
        for h in (e.get("hallazgos") or []):
            if h.get("esfuerzo_horas") != NO_MEDIDO:
                fallos.append(_fallo("esfuerzo_nunca_es_numero",
                                     "%s: %r en «%s»" % (eid, h.get("esfuerzo_horas"),
                                                         str(h.get("titulo"))[:50])))
    return fallos


def inv_conteos_no_superan_la_muestra(doc):
    """Ningún «N de M páginas» puede tener M mayor que la muestra auditada.

    Es la forma automática de cazar la mentira que se arregló el 22-ago, cuando eje4
    afirmaba «15 de 15 descubiertas» habiendo 47.
    """
    cob = doc.get("cobertura_paginas") or {}
    auditadas = cob.get("auditadas")
    if not isinstance(auditadas, int):
        return [_fallo("conteos_vs_muestra", "cobertura_paginas.auditadas no es un entero")]
    fallos = []
    patron = re.compile(r"(\d+)\s+de\s+(\d+)\s+p[aá]gina", re.I)
    for k, (eid, v) in _indicadores(doc).items():
        for texto in (v.get("detalle"), str(v.get("valor"))):
            for n, m in patron.findall(texto or ""):
                if int(m) > auditadas and int(m) != (cob.get("descubiertas") or -1):
                    fallos.append(_fallo(
                        "conteos_vs_muestra",
                        "%s dice «%s de %s páginas» pero se auditaron %d"
                        % (k, n, m, auditadas)))
    return fallos


def inv_parcialidad_declarada(doc, informe_md=None):
    """Si la muestra es parcial, el informe tiene que decirlo con esa palabra."""
    cob = doc.get("cobertura_paginas") or {}
    if not cob.get("muestra_parcial"):
        return []
    if informe_md is None:
        return []
    if "parcial" not in informe_md.lower():
        return [_fallo("parcialidad_declarada",
                       "muestra_parcial=true pero la palabra «parcial» no aparece en el informe")]
    return []


INVARIANTES = (
    inv_puntajes_en_rango,
    inv_global_es_la_suma_ponderada,
    inv_no_medido_con_motivo,
    inv_todo_numero_con_procedencia,
    inv_esfuerzo_nunca_es_numero,
    inv_conteos_no_superan_la_muestra,
)


def revisar(directorio):
    """Aplica todas las invariantes a una auditoría en disco."""
    d = Path(directorio)
    ruta = d / "resultados.json"
    if not ruta.is_file():
        return {"id": d.name, "estado": "sin_resultados", "fallos": []}
    try:
        doc = json.loads(ruta.read_text(encoding="utf-8"))
    except ValueError as err:
        return {"id": d.name, "estado": "ilegible", "fallos": [_fallo("json", str(err))]}

    md = None
    for f in sorted(d.glob("AUDITORIA-*.md")):
        md = f.read_text(encoding="utf-8", errors="replace")
        break

    fallos = []
    for fn in INVARIANTES:
        try:
            fallos.extend(fn(doc))
        except Exception as err:                      # una invariante rota no tumba el resto
            fallos.append(_fallo(fn.__name__, "la invariante reventó: %r" % err))
    try:
        fallos.extend(inv_parcialidad_declarada(doc, md))
    except Exception as err:
        fallos.append(_fallo("inv_parcialidad_declarada", "la invariante reventó: %r" % err))

    ind = _indicadores(doc)
    sin_medir = sum(1 for _k, (_e, v) in ind.items() if v.get("estado") == "no_medido")
    return {
        "id": d.name,
        "estado": "revisada",
        "sitio": doc.get("sitio"),
        "indicadores": len(ind),
        "no_medidos": sin_medir,
        "pct_no_medido": round(100.0 * sin_medir / len(ind), 1) if ind else 0.0,
        "fallos": fallos,
    }
