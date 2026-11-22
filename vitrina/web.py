"""web — auth, ruteo y entrega. La unica pieza que habla HTTP.

TLS lo pone Cloudflare en el borde. Este servidor escucha en 127.0.0.1 y solo el tunel
llega hasta el, igual que el panel de motor.voidspore.tech.
"""

import base64
import html
import json
import threading
import time
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from . import almacen

PLANTILLA = Path(__file__).resolve().parent.parent / "plantillas" / "indice.html"

NOMBRE_CORTO = {
    "eje1": "perf",
    "eje2": "ux",
    "eje3": "tec",
    "eje4": "seo",
    "eje5": "seg",
    "eje6": "a11y",
}

# Cómo se dibuja el veredicto que ya emitió comparar.mjs. '=?' es "cambió el número pero
# nadie puede decir si eso es mejor": se muestra el cambio, sin flecha de color.
SENTIDOS = {
    "MEJORA": ("▲", "d-sube"),
    "EMPEORA": ("▼", "d-baja"),
    "=": ("=", "d-igual"),
}

# Por qué no se puede lanzar. El handler sabe cuál es; la vista no lo adivina.
MOTIVOS = {
    "cuota": "Se alcanzó el tope de auditorías por hora. Volvé a intentar en un rato.",
    "sin_motor": "Lanzar auditorías no está disponible ahora mismo.",
}


def autorizado(cabecera, credencial):
    """LA costura de autorizacion: un solo lugar donde se decide si alguien pasa.

    Sin credencial configurada el servidor queda abierto a proposito, pero servidor.py
    avisa a gritos por stderr. Preferible eso a arrancar en silencio creyendo que hay
    clave.
    """
    if not credencial:
        return True
    if not cabecera or not cabecera.startswith("Basic "):
        return False
    try:
        crudo = base64.b64decode(cabecera[6:]).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    # Comparacion de largo constante: es barata y evita regalar el largo de la clave.
    a = crudo.encode("utf-8")
    b = credencial.encode("utf-8")
    if len(a) != len(b):
        return False
    diferencia = 0
    for x, y in zip(a, b):
        diferencia |= x ^ y
    return diferencia == 0


def desde_internet(cabeceras):
    """¿Esta peticion entro por el tunel de Cloudflare?

    No sirve mirar la IP del cliente: cloudflared se conecta a 127.0.0.1, asi que TODO
    el trafico del tunel llega como local y seria indistinguible del M4. Lo que si
    distingue son las cabeceras que Cloudflare agrega y que un cliente local no tiene.

    Se usa para una sola cosa: LEER queda abierto (decision de Daniel), pero LANZAR una
    auditoria no, porque eso convierte la vitrina en un escaner que cualquiera puede
    apuntar a cualquier sitio desde la IP de Daniel.
    """
    for c in ("CF-Ray", "CF-Connecting-IP", "CF-IPCountry"):
        if cabeceras.get(c):
            return True
    return False


def mismo_origen(cabeceras):
    """¿Este POST lo originó la propia vitrina, y no otra web?

    Hace falta porque `desde_internet()` NO cubre el caso peligroso: si Daniel visita
    cualquier sitio y ese sitio trae un <form> oculto apuntando a 127.0.0.1:8096, es SU
    navegador el que manda el POST. Llega sin cabeceras de Cloudflare, o sea que parece
    local, y sin clave no hay nada mas que lo detenga. Eso es CSRF, y deja a cualquier
    web capaz de poner el M4 a escanear sitios ajenos.

    Dos pruebas, y basta con una:

    1. `Origin` (o `Referer`) coincide con el `Host` al que se pidio. Un formulario desde
       evil.com manda `Origin: https://evil.com` y no coincide.
    2. Una cabecera propia `X-Vitrina`. Un <form> HTML NO puede poner cabeceras, y un
       fetch() de otro origen que lo intente dispara un preflight CORS que este servidor
       no contesta. Sirve para curl y para la terminal.
    """
    host = (cabeceras.get("Host") or "").strip().lower()
    if cabeceras.get("X-Vitrina"):
        return True
    for clave in ("Origin", "Referer"):
        valor = cabeceras.get(clave)
        if not valor:
            continue
        if "://" not in valor:
            return False
        suyo = valor.split("://", 1)[1].split("/", 1)[0].strip().lower()
        return bool(host) and suyo == host
    # Ni Origin ni Referer ni cabecera propia: no se puede probar el origen, no se pasa.
    return False


class Cuota:
    """Cuántas auditorías puede lanzar internet por hora.

    Lanzar quedó ABIERTO por decisión de Daniel: quiere que su jefa pruebe la herramienta
    sin registrarse ni escribir una clave, y con razón — una demo que exige credenciales
    no es una demo.

    El riesgo no desaparece por eso: un botón público que corre un escáner contra la URL
    que le pidan, desde esta IP, sigue siendo eso. Lo que hace el tope es acotarlo. Con
    seis por hora la vitrina es una demo; sin tope es un servicio de escaneo gratuito
    montado sobre la máquina de Daniel.

    En memoria a propósito: si el servicio se reinicia, la cuota se reinicia. Es una demo,
    no un sistema de facturación.
    """

    def __init__(self, tope=6, ventana=3600):
        self.tope = tope
        self.ventana = ventana
        self._sellos = []
        self._cerrojo = threading.Lock()

    def _purgar(self, ahora):
        """Tira los sellos que ya salieron de la ventana. No devuelve nada a propósito:
        un ayudante que además devuelve el estado interno tiene dos contratos."""
        self._sellos = [s for s in self._sellos if ahora - s < self.ventana]

    def permite(self):
        """Consume un cupo si queda. Devuelve (permitido, restantes)."""
        ahora = time.time()
        with self._cerrojo:
            self._purgar(ahora)
            if len(self._sellos) >= self.tope:
                return False, 0
            self._sellos.append(ahora)
            return True, self.tope - len(self._sellos)

    def restantes(self):
        with self._cerrojo:
            self._purgar(time.time())
            return max(0, self.tope - len(self._sellos))

    def devolver(self):
        """Devuelve el último cupo consumido, para el trabajo que no llegó a nacer.

        Una URL mal escrita no debería apagar la demo durante una hora: lo que el tope
        protege es la capacidad del M4 de correr escáneres, no la de recibir POSTs.
        """
        with self._cerrojo:
            if self._sellos:
                self._sellos.pop()


def _clase_puntaje(p):
    if not isinstance(p, (int, float)):
        return "p-nulo"
    if p >= 8:
        return "p-bien"
    if p >= 6:
        return "p-medio"
    return "p-mal"


def _num(p, decimales=1):
    if not isinstance(p, (int, float)):
        return "—"
    return ("{:." + str(decimales) + "f}").format(p)


MESES = ("ene", "feb", "mar", "abr", "may", "jun",
         "jul", "ago", "sep", "oct", "nov", "dic")


def _fecha_legible(iso):
    """Fecha en español sin depender del locale del sistema.

    strftime("%b") devuelve "Aug" bajo el locale C, que es con el que arranca launchd.
    Una tabla de seis palabras es más barata que pelearse con setlocale y no se rompe
    según quién arranque el proceso.
    """
    if not iso:
        return "sin fecha"
    try:
        d = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return str(iso)[:16]
    return "{} {} {}, {:02d}:{:02d}".format(
        d.day, MESES[d.month - 1], d.year, d.hour, d.minute
    )


def _duracion(seg):
    if not isinstance(seg, (int, float)):
        return None
    m, s = divmod(int(seg), 60)
    return "{} min {:02d} s".format(m, s) if m else "{} s".format(s)


def render_indice(fichas, colisiones, puede_lanzar=True, aviso=None, motivo=None,
                  puede_cancelar=True):
    plantilla = PLANTILLA.read_text(encoding="utf-8")
    en_curso = [f for f in fichas if f.get("en_curso")]

    if not fichas:
        contenido = (
            '<div class="vacio"><p>Todavía no hay ninguna auditoría en disco.</p>'
            "<code>site-audit/scripts/auditar.sh https://sitio.com</code></div>"
        )
        resumen = "Ninguna auditoría indexada."
    else:
        fichas_html = "".join(_ficha_html(f, puede_cancelar) for f in fichas)
        contenido = '<ul class="lista">' + fichas_html + "</ul>"
        sitios = len({f.get("sitio") for f in fichas})
        rotas = sum(1 for f in fichas if f.get("ilegible"))
        resumen = "{} auditoría{} · {} sitio{}".format(
            len(fichas), "s" if len(fichas) != 1 else "", sitios, "s" if sitios != 1 else ""
        )
        if rotas:
            resumen += " · {} ilegible{}".format(rotas, "s" if rotas != 1 else "")
        if colisiones:
            resumen += " · {} colisión(es) de id, ver el log".format(len(colisiones))

    if puede_lanzar:
        formulario = (
            '<form class="lanzar" method="post" action="/auditar">'
            '<input name="url" type="text" placeholder="Auditar un sitio nuevo: ejemplo.com" '
            'autocomplete="off" aria-label="Sitio a auditar" required>'
            '<input name="cliente" type="text" placeholder="Cliente (opcional)" '
            'autocomplete="off" aria-label="Nombre del cliente">'
            # El tope de paginas decide si el informe puede decir "cobertura total" o
            # tiene que declarar MUESTRA PARCIAL. Era un 15 fijo en auditar.sh, asi que
            # hasta un sitio de 38 paginas salia parcial sin que nadie lo hubiera elegido.
            '<select name="alcance" aria-label="Alcance de la auditoría">'
            '<option value="muestra">Muestra · 15 páginas</option>'
            '<option value="completo">Sitio completo · más lento</option>'
            '</select>'
            '<button type="submit">Auditar</button></form>'
        )
    else:
        # El motivo lo pasa el handler. Deducirlo aquí del booleano era mentir en el caso
        # que más importa: con el motor caído la página mandaba a esperar por un tope que
        # no existe y escondía que el motor estaba muerto.
        formulario = '<p class="lanzar-no">%s</p>' % html.escape(
            MOTIVOS.get(motivo, MOTIVOS["sin_motor"])
        )

    if aviso:
        formulario += '<p class="aviso">%s</p>' % html.escape(aviso)

    # Mientras haya algo corriendo la pagina se refresca sola: una barra que no avanza es
    # indistinguible de una que se colgo.
    refresco = ('<meta http-equiv="refresh" content="10">' if en_curso else "")

    return (
        plantilla.replace("{{RESUMEN}}", html.escape(resumen))
        .replace("{{CONTENIDO}}", contenido)
        .replace("{{FORMULARIO}}", formulario)
        .replace("{{REFRESCO}}", refresco)
    )


def _ficha_html(f, puede_cancelar=True):
    e = html.escape
    ident = e(f["id"])
    sitio = e(str(f.get("sitio") or f["id"]))
    cliente = f.get("cliente")

    if f.get("en_curso"):
        return (
            '<li class="ficha en-curso" data-busca="{busca}">'
            '<div class="puntaje p-nulo">··</div>'
            '<div><p class="titulo">{sitio}</p>'
            '<div class="meta"><span>{etiqueta}</span><span>{paso}</span></div></div>'
            '<div class="bajadas">{cancelar}</div></li>'
        ).format(
            # Cancelar es un permiso DISTINTO de lanzar: no gasta cuota, porque
            # cancelar algo ya encolado no crea trabajo nuevo. Atarlo a puede_lanzar
            # hacía desaparecer el botón de los trabajos en curso justo cuando la cuota
            # se agotaba, aunque /cancelar los siguiera aceptando.
            cancelar=(
                '<form method="post" action="/cancelar" class="cancelar">'
                '<input type="hidden" name="id" value="%s">'
                '<button type="submit">cancelar</button></form>' % ident
            ) if puede_cancelar else "",
            busca=e((str(f.get("sitio") or "") + " " + f["id"]).lower()),
            sitio=sitio,
            etiqueta="en cola" if f.get("estado") == "en_cola" else "midiendo",
            paso=e(str(f.get("paso") or "…")[:90]),
        )

    if f.get("estado") in ("fallo", "interrumpido", "cancelado"):
        return (
            '<li class="ficha" data-busca="{busca}">'
            '<div class="puntaje p-nulo">—</div>'
            '<div><p class="titulo">{sitio}</p>'
            '<div class="meta"><span>{est}</span></div>'
            '<span class="marca m-ilegible">{motivo}</span></div>'
            '<div></div></li>'
        ).format(
            busca=e((str(f.get("sitio") or "") + " " + f["id"]).lower()),
            sitio=sitio,
            est=e(str(f.get("estado"))),
            motivo=e(str(f.get("error") or "sin detalle")[:140]),
        )

    if f.get("ilegible"):
        return (
            '<li class="ficha" data-busca="{busca}">'
            '<div class="puntaje p-nulo">—</div>'
            '<div><p class="titulo">{sitio}</p>'
            '<div class="meta"><span>{ident}</span></div>'
            '<span class="marca m-ilegible">ilegible — {motivo}</span></div>'
            "<div></div></li>"
        ).format(
            busca=e((str(f.get("sitio") or "") + " " + f["id"]).lower()),
            sitio=sitio,
            ident=ident,
            motivo=e(str(f.get("motivo") or "")[:120]),
        )

    meta = ['<span>{}</span>'.format(e(_fecha_legible(f.get("fecha_iso"))))]
    if cliente:
        meta.append("<span>{}</span>".format(e(str(cliente))))
    cob = f.get("cobertura") or {}
    if cob.get("auditadas") is not None:
        meta.append(
            "<span>{} de {} páginas</span>".format(
                cob.get("auditadas"), cob.get("descubiertas")
            )
        )
    if f.get("hallazgos"):
        meta.append("<span>{} hallazgos</span>".format(f["hallazgos"]))
    dur = _duracion(f.get("duracion_segundos"))
    if dur:
        meta.append("<span>{}</span>".format(e(dur)))

    ejes = "".join(
        '<span class="eje">{} <b>{}</b></span>'.format(
            e(NOMBRE_CORTO.get(x["id"], x["id"])), e(_num(x.get("puntaje")))
        )
        for x in (f.get("ejes") or [])
    )

    # El cambio contra la corrida anterior. Es lo que convierte un diagnostico suelto en
    # un seguimiento: sin esto, cada informe empieza de cero y no hay nada que ensenarle
    # al cliente despues de arreglar.
    d = f.get("delta") or {}
    if isinstance(d.get("diferencia"), (int, float)):
        dif = d["diferencia"]
        # La flecha sale de `sentido`, que ya emitió comparar.mjs, no del signo crudo.
        # Quién decide qué es una mejora es la capa que mide: un indicador que antes
        # estaba NO_MEDIDO y ahora se mide cambia de número sin haber mejorado, y el
        # medidor lo marca '=?' justamente para no llamarlo mejora.
        flecha, clase = SENTIDOS.get(d.get("sentido"), ("·", "d-igual"))
        aviso = "" if d.get("mismo_conjunto") is not False else " (otras páginas)"
        meta.append(
            '<span class="delta {c}">{fl} {sig}{dif} desde la anterior{av}</span>'.format(
                c=clase, fl=flecha, sig="+" if dif > 0 else "",
                dif=_num(dif), av=e(aviso)))

    marcas = ""
    if cob.get("parcial"):
        # La skill se rompio la cabeza para que la parcialidad llegue al informe. Seria
        # absurdo que la vitrina la volviera a esconder.
        marcas += '<span class="marca m-parcial">muestra parcial</span> '
    if f.get("estado") and f["estado"] != "listo":
        marcas += '<span class="marca m-corriendo">{}</span>'.format(e(str(f["estado"])))

    art = f.get("artefactos") or {}
    bajadas = []
    if art.get("html"):
        bajadas.append('<a href="/a/{}/">informe</a>'.format(ident))
    if art.get("pdf"):
        bajadas.append('<a href="/a/{}/informe.pdf">pdf</a>'.format(ident))
    if f.get("markdown"):
        bajadas.append('<a href="/a/{}/{}">md</a>'.format(ident, e(f["markdown"])))
    if art.get("json"):
        bajadas.append('<a href="/a/{}/resultados.json">json</a>'.format(ident))

    enlace = "/a/{}/".format(ident) if art.get("html") else "/a/{}/resultados.json".format(ident)

    return (
        '<li class="ficha" data-busca="{busca}">'
        '<div class="puntaje {clase}">{p}<small>/10</small></div>'
        '<div><p class="titulo"><a href="{enlace}">{sitio}</a></p>'
        '<div class="meta">{meta}</div>'
        '<div class="ejes">{ejes}</div>{marcas}</div>'
        '<div class="bajadas">{bajadas}</div></li>'
    ).format(
        busca=e((str(f.get("sitio") or "") + " " + str(cliente or "") + " " + f["id"]).lower()),
        clase=_clase_puntaje(f.get("puntaje_global")),
        p=e(_num(f.get("puntaje_global"))),
        enlace=enlace,
        sitio=sitio,
        meta="".join(meta),
        ejes=ejes,
        marcas=marcas,
        bajadas="".join(bajadas),
    )


def constructor(indice, credencial, registrar=None, cola=None, cuota=None):
    """Devuelve la clase handler cerrada sobre el indice, la credencial y la cola."""
    cuota = cuota or Cuota()

    class Handler(BaseHTTPRequestHandler):
        server_version = "vitrina-vsl"
        protocol_version = "HTTP/1.1"

        # -- vista ---------------------------------------------------------------
        def _ve_privadas(self):
            """¿Esta petición puede ver las auditorías de cliente?

            Una sola respuesta por petición, en un solo lugar. Estaba escrita literal en
            cada endpoint que devolvía fichas, y el que se agregó después —/api/cola.json—
            nació sin ella y publicaba los nombres de directorio (`cliente.com-2026-08-25`)
            de lo que estuviera encolado o corriendo.
            """
            return not desde_internet(self.headers)

        def _visibles(self):
            """Los ids que esta petición tiene derecho a ver.

            Quién decide qué es privado es el Índice, no cada endpoint: así una superficie
            nueva se filtra preguntando, y no por acordarse de repetir el `not`.
            """
            return {f["id"] for f in indice.fichas(incluir_privadas=self._ve_privadas())}

        # -- plomeria ----------------------------------------------------------
        def log_message(self, formato, *args):
            if registrar:
                registrar("%s - %s" % (self.address_string(), formato % args))

        def _responder(self, codigo, cuerpo, tipo="text/html; charset=utf-8", extra=None):
            if isinstance(cuerpo, str):
                cuerpo = cuerpo.encode("utf-8")
            self.send_response(codigo)
            self.send_header("Content-Type", tipo)
            self.send_header("Content-Length", str(len(cuerpo)))
            self.send_header("X-Content-Type-Options", "nosniff")
            # OJO: "no-referrer" NO sirve acá aunque suene más seguro. Con esa política el
            # navegador manda `Origin: null` en el POST del formulario Y suprime el Referer,
            # así que las dos comprobaciones de mismo_origen() se quedan sin nada que mirar
            # y la vitrina rechaza sus PROPIOS formularios ("Este formulario no salió de la
            # vitrina"). Le pasó a Daniel seis veces entre el 23 y el 24 de agosto.
            # "same-origin" manda el referente completo a la propia vitrina y nada hacia
            # afuera: la defensa contra CSRF queda igual de fuerte y el formulario funciona.
            self.send_header("Referrer-Policy", "same-origin")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(cuerpo)

        def _pedir_clave(self):
            self._responder(
                401,
                "Hace falta clave.",
                "text/plain; charset=utf-8",
                {"WWW-Authenticate": 'Basic realm="Auditorias VSL"'},
            )

        def _archivo(self, ruta, tipo):
            try:
                datos = ruta.read_bytes()
            except OSError:
                return self._responder(404, "No se pudo leer.", "text/plain; charset=utf-8")
            self._responder(200, datos, tipo, {"Cache-Control": "no-cache"})

        # -- ruteo -------------------------------------------------------------
        def do_HEAD(self):
            self.do_GET()

        def do_POST(self):
            camino = self.path.split("?", 1)[0]
            if camino not in ("/auditar", "/cancelar"):
                return self._responder(404, "No existe.", "text/plain; charset=utf-8")
            if not cola:
                return self._responder(503, "El motor no está activo.",
                                       "text/plain; charset=utf-8")
            if not autorizado(self.headers.get("Authorization"), credencial):
                return self._pedir_clave()
            if not mismo_origen(self.headers):
                if registrar:
                    registrar("RECHAZADO POST sin origen propio (posible CSRF) desde %s"
                              % (self.headers.get("Origin")
                                 or self.headers.get("Referer") or "sin origen"))
                self._drenar(int(self.headers.get("Content-Length") or 0))
                return self._responder(
                    403,
                    "Este formulario no salió de la vitrina.",
                    "text/plain; charset=utf-8",
                )

            largo = int(self.headers.get("Content-Length") or 0)
            if largo <= 0 or largo > 4096:
                # Hay que DRENAR el cuerpo antes de contestar y cerrar. Si se responde
                # sin leerlo, el cliente recibe un connection reset en vez del mensaje:
                # el navegador muestra un error de red y el aviso no se ve nunca.
                self._drenar(largo)
                return self._redirigir("el formulario llegó vacío o es demasiado grande")
            crudo = self.rfile.read(largo).decode("utf-8", "replace")
            campos = urllib.parse.parse_qs(crudo)

            if camino == "/cancelar":
                ident = (campos.get("id") or [""])[0]
                # Lo que no se puede ver tampoco se puede cancelar: si no, un visitante
                # que adivine el id aborta la auditoría de cliente que corre en el M4.
                if ident not in self._visibles():
                    return self._redirigir("no se pudo cancelar %s: ya no estaba activa"
                                           % ident)
                if cola.cancelar(ident):
                    return self._redirigir("cancelada %s" % ident)
                return self._redirigir("no se pudo cancelar %s: ya no estaba activa" % ident)

            # La cuota se cobra acá, donde nace el trabajo, y no en la puerta del POST.
            # Cobrándola antes, seis formularios vacíos o seis rechazos por CSRF apagaban
            # la demo una hora entera sin haber lanzado nada — y el log decía "lanzada"
            # de peticiones que tres renglones más abajo terminaban en 403.
            de_internet = desde_internet(self.headers)
            if de_internet:
                permitido, quedan = cuota.permite()
                if not permitido:
                    if registrar:
                        registrar("CUOTA agotada: se rechazó lanzar desde internet")
                    return self._responder(
                        429,
                        "Se alcanzó el tope de auditorías por hora desde internet. "
                        "Volvé a intentar en un rato.",
                        "text/plain; charset=utf-8",
                    )
                if registrar:
                    registrar("lanzada desde internet · quedan %d en la hora" % quedan)

            url = (campos.get("url") or [""])[0]
            cliente = ((campos.get("cliente") or [""])[0] or "").strip() or None
            alcance = (campos.get("alcance") or [""])[0]
            try:
                # Lo que se lanza desde internet es la DEMO y se ve; lo que se lanza
                # desde el M4 es trabajo de cliente y no. Ver Cola.encolar().
                ident = cola.encolar(url, cliente, publica=de_internet, paginas=alcance)
            except ValueError as err:
                if de_internet:
                    cuota.devolver()
                return self._redirigir("no se pudo encolar: %s" % err)
            return self._redirigir("encolado %s" % ident)

        def _drenar(self, largo, tope=1 << 20):
            """Descarta el cuerpo que no se va a usar, sin tragarse un archivo entero."""
            queda = min(max(largo, 0), tope)
            while queda > 0:
                trozo = self.rfile.read(min(queda, 65536))
                if not trozo:
                    break
                queda -= len(trozo)

        def _redirigir(self, aviso):
            destino = "/?aviso=" + urllib.parse.quote(aviso)
            self.send_response(303)
            self.send_header("Location", destino)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            camino = self.path.split("?", 1)[0]

            # /salud va antes de la auth: el watchdog no tiene por que saber la clave.
            if camino == "/salud":
                return self._responder(200, "ok", "text/plain; charset=utf-8")

            if not autorizado(self.headers.get("Authorization"), credencial):
                return self._pedir_clave()

            if camino in ("/", "/index.html"):
                aviso = None
                if "?" in self.path:
                    q = urllib.parse.parse_qs(self.path.split("?", 1)[1])
                    aviso = (q.get("aviso") or [None])[0]
                # El formulario se dibuja para todos. Sólo desaparece si el motor no
                # está o si la cuota de la hora se agotó, y el mensaje dice cuál de las dos.
                sin_cuota = not self._ve_privadas() and cuota.restantes() <= 0
                return self._responder(
                    200,
                    render_indice(
                        # Las auditorías de clientes no salen a internet: la vitrina se
                        # abrió para demostrarla, no para publicar los puntajes de
                        # seguridad de nadie.
                        indice.fichas(incluir_privadas=self._ve_privadas()),
                        indice.colisiones(),
                        puede_lanzar=bool(cola) and not sin_cuota,
                        aviso=aviso,
                        motivo="cuota" if sin_cuota else "sin_motor",
                        puede_cancelar=bool(cola),
                    ),
                )

            if camino == "/api/cola.json" and cola:
                # Filtrado por la misma autoridad que el listado. Sin esto, un visitante
                # veía el sitio y la fecha de la auditoría de cliente que estuviera
                # corriendo — la fuga que el resto de los endpoints ya cerraba.
                visibles = self._visibles()
                estado = cola.situacion()
                estado["en_cola"] = [x for x in estado["en_cola"] if x in visibles]
                if estado["corriendo"] not in visibles:
                    estado["corriendo"] = None
                return self._responder(
                    200, json.dumps(estado, ensure_ascii=False),
                    "application/json; charset=utf-8",
                )

            if camino == "/api/indice.json":
                cuerpo = json.dumps(
                    {"auditorias": indice.fichas(incluir_privadas=self._ve_privadas())},
                    ensure_ascii=False, indent=1
                )
                return self._responder(200, cuerpo, "application/json; charset=utf-8")

            if camino.startswith("/a/"):
                return self._auditoria(camino[3:])

            self._responder(404, "No existe.", "text/plain; charset=utf-8")

        def _auditoria(self, resto):
            partes = resto.split("/", 1)
            ident = partes[0]
            pedido = partes[1] if len(partes) > 1 else ""

            # Con incluir_privadas=False una auditoría de cliente sencillamente NO existe
            # para quien llega por internet, aunque sepa el id. Filtrar sólo el listado
            # sería seguridad de escaparate.
            f = indice.por_id(ident, incluir_privadas=self._ve_privadas())
            if f is None:
                return self._responder(404, "Esa auditoría no existe.",
                                       "text/plain; charset=utf-8")

            base = Path(f["directorio"])
            if not pedido:
                # La raiz de una auditoria es su informe: informe.html es autocontenido
                # (cero referencias externas), asi que se entrega tal cual.
                destino = almacen.ruta_segura(base, "informe.html")
                if destino is None:
                    return self._responder(
                        404,
                        "Esta auditoría no tiene informe.html generado.",
                        "text/plain; charset=utf-8",
                    )
                return self._archivo(destino, "text/html; charset=utf-8")

            destino = almacen.ruta_segura(base, pedido)
            if destino is None:
                # 403 y no 404: que quede claro en el log que alguien pidio algo que no
                # se sirve, en vez de confundirlo con un archivo que falta.
                return self._responder(403, "No se sirve ese archivo.", "text/plain; charset=utf-8")
            return self._archivo(destino, almacen.tipo_de(destino.name))

    return Handler
