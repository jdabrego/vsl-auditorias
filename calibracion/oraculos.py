"""oraculos — verificadores INDEPENDIENTES del motor.

La distinción que sostiene todo esto: un oráculo solo vale si no comparte camino con lo
que verifica.

- `curl` no pasa por Chrome ni por CDP  -> independiente de verdad para el eje 5.
- `openssl s_client` no pasa por SSL Labs -> independiente para el TLS.
- Un sitemap parseado acá no pasa por descubrir-urls.sh -> independiente.

Lo que NO está aquí, a propósito: contrastar el eje 1 contra el JSON de Lighthouse. Eso
validaría la extracción, no la medición, y llamarlo "validación cruzada" sería exactamente
el pecado que esta herramienta existe para no cometer. Se declara y se deja fuera.

Compatible con Python 3.9.
"""

import json
import re
import subprocess
from pathlib import Path
from xml.etree import ElementTree

CABECERAS = (
    "strict-transport-security",
    "x-frame-options",
    "x-content-type-options",
    "referrer-policy",
    "content-security-policy",
    "permissions-policy",
)

TIEMPO = 25


def _correr(cmd, entrada=None):
    """subprocess con lista y con tope de tiempo SIEMPRE (regla de la casa)."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=TIEMPO,
                           input=entrada)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout tras %ss" % TIEMPO
    except OSError as err:
        return 127, "", str(err)


# --- oráculo 1: cabeceras con curl ------------------------------------------

def cabeceras_con_curl(url):
    """Devuelve (status, {cabecera: valor}) leído por curl, sin pasar por Chrome."""
    codigo, salida, _err = _correr(
        ["curl", "-sS", "-o", "/dev/null", "-D", "-", "--max-time", "20",
         "-A", "vsl-calibracion/1.0", url])
    if codigo != 0:
        return None, {}
    # Con redirecciones curl imprime varios bloques; interesa el ÚLTIMO (el documento final).
    bloques = [b for b in re.split(r"\r?\n\r?\n", salida) if b.strip().startswith("HTTP/")]
    if not bloques:
        return None, {}
    ultimo = bloques[-1].splitlines()
    status = None
    m = re.match(r"HTTP/[\d.]+\s+(\d{3})", ultimo[0])
    if m:
        status = int(m.group(1))
    cab = {}
    for linea in ultimo[1:]:
        if ":" in linea:
            k, v = linea.split(":", 1)
            cab[k.strip().lower()] = v.strip()
    return status, cab


def contrastar_cabeceras(directorio):
    """Compara lo que dijo el eje 5 con lo que ve curl sobre las MISMAS URLs.

    Solo se miran URLs que curl vea con 200: la regla dura 1 del motor dice que un no-200
    no cuenta, así que exigirle coherencia sobre un 404 sería comparar contra algo que el
    motor decidió (bien) no medir.
    """
    d = Path(directorio)
    res = d / "resultados.json"
    if not res.is_file():
        return {"estado": "sin_resultados", "discrepancias": []}
    doc = json.loads(res.read_text(encoding="utf-8"))

    paginas = []
    for nombre in ("paginas.json", "descubrimiento.json"):
        f = d / nombre
        if f.is_file():
            try:
                datos = json.loads(f.read_text(encoding="utf-8"))
            except ValueError:
                continue
            if isinstance(datos, dict):
                datos = datos.get("urls") or datos.get("paginas") or []
            for x in datos:
                u = x.get("url") if isinstance(x, dict) else x
                if isinstance(u, str):
                    paginas.append(u)
            if paginas:
                break
    paginas = paginas[:8]          # muestra: 8 páginas alcanzan para detectar desacuerdo
    if not paginas:
        return {"estado": "sin_paginas", "discrepancias": []}

    ind = {}
    for e in (doc.get("ejes") or {}).values():
        ind.update(e.get("indicadores") or {})

    visto = {c: 0 for c in CABECERAS}
    con_200 = 0
    for u in paginas:
        status, cab = cabeceras_con_curl(u)
        if status != 200:
            continue
        con_200 += 1
        for c in CABECERAS:
            if c in cab:
                visto[c] += 1

    if con_200 == 0:
        return {"estado": "ninguna_200", "discrepancias": []}

    mapa = {
        "strict-transport-security": "eje5.hsts",
        "x-frame-options": "eje5.x_frame_options",
        "x-content-type-options": "eje5.x_content_type_options",
        "referrer-policy": "eje5.referrer_policy",
        "content-security-policy": "eje5.csp",
        "permissions-policy": "eje5.permissions_policy",
    }
    discrepancias = []
    for cab, clave in mapa.items():
        v = ind.get(clave)
        if not isinstance(v, dict):
            continue
        valor = v.get("valor")
        if not isinstance(valor, dict):
            continue
        motor_dice = bool(valor.get("presente"))
        if motor_dice is False and "presente_en" in valor:
            motor_dice = bool(valor.get("presente_en"))
        curl_dice = visto[cab] > 0
        if motor_dice != curl_dice:
            discrepancias.append({
                "indicador": clave,
                "motor": "presente" if motor_dice else "ausente",
                "curl": "presente en %d/%d páginas 200" % (visto[cab], con_200),
            })
    return {"estado": "contrastado", "paginas_200": con_200, "discrepancias": discrepancias}


# --- oráculo 2: TLS con openssl ---------------------------------------------

def tls_con_openssl(host, puerto=443):
    """Protocolo TLS negociado, leído de openssl.

    Dos trampas de macOS, las dos comprobadas el 24-ago:

    1. El `openssl` del sistema es **LibreSSL 3.3.6**, no OpenSSL: no acepta `-brief`.
    2. LibreSSL imprime siempre `New, TLSv1/SSLv3, Cipher is ...` aunque la conexión sea
       TLS 1.3. Leer ESA línea daría "protocolo obsoleto" en sitios perfectamente sanos —
       un falso positivo camino de un informe de cliente. El dato bueno está en la línea
       `Protocol  : TLSv1.3` del bloque de sesión.
    """
    codigo, salida, _err = _correr(
        ["openssl", "s_client", "-connect", "%s:%d" % (host, puerto),
         "-servername", host], entrada="")
    if codigo not in (0, 1):
        return {"ok": False, "motivo": "openssl salió con %s" % codigo}
    protocolo = None
    for linea in salida.splitlines():
        m = re.match(r"\s*Protocol\s*:\s*(\S+)", linea)     # NO la línea "New, TLSv1/SSLv3"
        if m:
            protocolo = m.group(1)
            break
    if not protocolo:
        return {"ok": False, "motivo": "no se encontró la línea «Protocol :» en la salida"}
    return {"ok": True, "protocolo": protocolo}


def contrastar_tls(directorio, host):
    """¿Coincide el motor con openssl en si el TLS es moderno?"""
    o = tls_con_openssl(host)
    if not o.get("ok"):
        return {"estado": "sin_oraculo", "detalle": o.get("motivo"), "discrepancias": []}
    viejo = o["protocolo"] in ("TLSv1", "TLSv1.1", "SSLv3")
    d = Path(directorio) / "resultados.json"
    if not d.is_file():
        return {"estado": "sin_resultados", "discrepancias": []}
    doc = json.loads(d.read_text(encoding="utf-8"))
    ind = {}
    for e in (doc.get("ejes") or {}).values():
        ind.update(e.get("indicadores") or {})
    claves = [k for k in ind if "tls" in k.lower() or "protocolo" in k.lower()]
    disc = []
    for k in claves:
        estado = ind[k].get("estado")
        if viejo and estado == "bien":
            disc.append({"indicador": k, "motor": "bien",
                         "openssl": "negocia %s, que es obsoleto" % o["protocolo"]})
    return {"estado": "contrastado", "protocolo": o["protocolo"], "discrepancias": disc}


# --- oráculo 3: el sitemap, parseado aquí -----------------------------------

NS_SITEMAP = "http://www.sitemaps.org/schemas/sitemap/0.9"
TOPE_XML = 5 * 1024 * 1024      # 5 MB: un sitemap legítimo no pesa más


def _xml_seguro(texto):
    """Parsea XML de un tercero sin regalarle la máquina.

    El sitemap lo sirve el sitio auditado, que puede estar comprometido. La stdlib no
    resuelve entidades externas (XXE), pero SÍ expande entidades internas, así que una
    bomba tipo «billion laughs» hincharía la memoria hasta tumbar el proceso. Sin salir
    de la stdlib, la defensa es negarse a parsear lo que declare entidades y acotar el
    tamaño; defusedxml haría lo mismo a cambio de una dependencia que este proyecto no
    tiene.
    """
    if len(texto) > TOPE_XML:
        return None
    cabeza = texto[:4096].upper()
    if "<!DOCTYPE" in cabeza or "<!ENTITY" in cabeza:
        return None
    try:
        return ElementTree.fromstring(texto)
    except ElementTree.ParseError:
        return None


def sitemap_propio(url_base):
    """Cuenta URLs del sitemap sin usar descubrir-urls.sh. Sigue índices de sitemaps."""
    base = url_base.rstrip("/")
    vistos, urls, pendientes = set(), set(), [base + "/sitemap.xml"]
    while pendientes and len(vistos) < 12:
        s = pendientes.pop()
        if s in vistos:
            continue
        vistos.add(s)
        codigo, salida, _e = _correr(
            ["curl", "-sS", "--max-time", "20", "-L", "--max-filesize", str(TOPE_XML), s])
        if codigo != 0 or not salida.strip().startswith("<"):
            continue
        raiz = _xml_seguro(salida)
        if raiz is None:
            continue
        # SOLO los <loc> que cuelgan de <url> o <sitemap> en el espacio de nombres de
        # sitemaps. Con `raiz.iter()` a secas se colaban los <image:loc> de WooCommerce
        # —las fotos de cada producto— y el oráculo contaba 131 "páginas" donde había 47.
        # Verificado el 24-ago: casi denuncio un defecto del motor que no existía.
        for padre in raiz:
            etiqueta = padre.tag.split("}")[-1]
            if etiqueta not in ("url", "sitemap"):
                continue
            for hijo in padre:
                if hijo.tag != "{%s}loc" % NS_SITEMAP and hijo.tag != "loc":
                    continue
                if not (hijo.text or "").strip():
                    continue
                loc = hijo.text.strip()
                if etiqueta == "sitemap" or loc.endswith(".xml"):
                    pendientes.append(loc)
                else:
                    urls.add(loc.rstrip("/"))
    return urls


def contrastar_descubrimiento(directorio, url_base):
    """¿El motor descubrió al menos lo que dice el sitemap?

    No se exige igualdad: el motor colapsa duplicados por documento final y aplica un tope,
    y eso es correcto. Lo que sería un defecto es que declare MENOS descubiertas que las
    que el sitemap publica, porque entonces el denominador de la cobertura estaría mal.
    """
    d = Path(directorio) / "resultados.json"
    if not d.is_file():
        return {"estado": "sin_resultados", "discrepancias": []}
    doc = json.loads(d.read_text(encoding="utf-8"))
    descubiertas = (doc.get("cobertura_paginas") or {}).get("descubiertas")
    delsitemap = sitemap_propio(url_base)
    if not delsitemap:
        return {"estado": "sin_sitemap", "discrepancias": []}
    disc = []
    if isinstance(descubiertas, int) and descubiertas < len(delsitemap) * 0.5:
        disc.append({
            "indicador": "cobertura_paginas.descubiertas",
            "motor": "%s descubiertas" % descubiertas,
            "sitemap": "%d URLs en el sitemap leído aparte" % len(delsitemap),
        })
    return {"estado": "contrastado", "sitemap_urls": len(delsitemap),
            "motor_descubiertas": descubiertas, "discrepancias": disc}
