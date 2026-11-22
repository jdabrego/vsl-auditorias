"""cola — el motor: encola auditorías y las corre DE A UNA.

Por qué serial y no un pool: `auditar.sh` corre el eje 1 (performance) en solitario a
propósito, porque si comparte CPU con otro Chrome sus tiempos dejan de medir el sitio y
pasan a medir la máquina. Dos auditorías a la vez no son "más lentas": son *falsas*. Por
eso hay un solo obrero y una fila, no un pool.

Una auditoría tarda ~5 minutos, así que la petición HTTP no puede esperarla. El que
encola recibe el id al instante y el estado se sigue por `estado.json`, que es el mismo
archivo que el indexador ya sabía leer desde la fase 1.
"""

import json
import os
import re
import signal
import subprocess
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from . import almacen

# El motor de medición vive fuera de este repo. SITE_AUDIT_ENGINE apunta a él; sin la
# variable se busca en la ubicación de siempre.
SKILL = Path(os.environ.get("SITE_AUDIT_ENGINE")
             or Path.home() / "dotfiles" / "claude" / "skills" / "site-audit")
AUDITAR = SKILL / "scripts" / "auditar.sh"
INFORME = SKILL / "scripts" / "informe.mjs"
COMPARAR = SKILL / "scripts" / "comparar.mjs"

# Estados posibles de un trabajo. 'interrumpido' existe porque un reinicio del M4 mata al
# obrero: sin esa marca, un trabajo quedaría diciendo "corriendo" para siempre.
# Cuantas paginas mide una corrida "completa". auditar.sh se corta en 15 por defecto —
# un tope fijo en el codigo, no una decision por sitio— y por eso hasta un sitio de 38
# paginas sale declarado como MUESTRA PARCIAL.
#
# "Completo" no es infinito a proposito: por encima de este techo la auditoria dura horas
# y la cola es serial, asi que bloquearia todo lo demas. Si un sitio tiene mas paginas que
# esto, el informe SIGUE diciendo muestra parcial, que es la verdad.
PAGINAS_COMPLETO = 150

EN_COLA, CORRIENDO, LISTO, FALLO, INTERRUMPIDO, CANCELADO = (
    "en_cola", "corriendo", "listo", "fallo", "interrumpido", "cancelado",
)

_ETIQUETA = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOST = re.compile(r"^%s(?:\.%s)+$" % (_ETIQUETA, _ETIQUETA))


def normalizar_url(crudo):
    """Devuelve (url, sitio) o lanza ValueError.

    Se valida acá aunque auditar.sh también valide: este proceso arma rutas de disco con
    el nombre del sitio, así que un host raro no puede llegar tan lejos. No se usa shell
    en ningún punto (subprocess con lista), pero un nombre con '/' o '..' escribiría
    donde no debe, y eso no lo cubre el "no hay shell".
    """
    t = (crudo or "").strip()
    if not t:
        raise ValueError("hace falta una URL")
    if len(t) > 300:
        raise ValueError("esa URL es absurdamente larga")
    if "://" not in t:
        t = "https://" + t
    if not t.startswith(("http://", "https://")):
        raise ValueError("solo http o https")
    resto = t.split("://", 1)[1]
    host = resto.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if "@" in host:
        raise ValueError("no se admiten credenciales en la URL")
    host = host.split(":", 1)[0].lower()
    if not _HOST.match(host):
        raise ValueError("nombre de sitio inválido: " + host)
    sitio = host[4:] if host.startswith("www.") else host
    return t, sitio


def _tope_paginas(valor):
    """Normaliza el alcance pedido: None = el defecto de auditar.sh, o un entero valido.

    Se valida aca y no en la web porque lo que llega es texto de un formulario abierto a
    internet, y un tope enorme no es un error de tipeo: es una auditoria de horas en una
    cola serial que bloquea todo lo que venga detras.
    """
    if valor in (None, "", "muestra"):
        return None
    if valor == "completo":
        return PAGINAS_COMPLETO
    try:
        n = int(valor)
    except (TypeError, ValueError):
        raise ValueError("alcance inválido: " + str(valor))
    if n < 1 or n > PAGINAS_COMPLETO:
        raise ValueError("el tope de páginas va de 1 a %d" % PAGINAS_COMPLETO)
    return n


def _ahora():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def leer_estado(directorio):
    return almacen.leer_json(directorio, almacen.MARCA_TRABAJO)


def escribir_json(directorio, nombre, datos):
    """Escribe un JSON dentro de una auditoría de forma atómica.

    Atómico porque el índice lee estos archivos desde otro hilo: sin tmp + os.replace,
    un lector se topa con un JSON a medio escribir y lo descarta como corrupto — y el
    síntoma es que el dato *desaparece* de la ficha sin explicación.

    Tolerante a que el directorio ya no exista: si alguien borra una auditoría mientras
    corre —desde el Finder, o un temporal que se limpia— no hay nada que salvar, y
    tumbar el hilo obrero por eso sería peor.
    """
    ruta = Path(directorio) / nombre
    tmp = ruta.with_suffix(ruta.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(datos, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, ruta)
    except OSError:
        return False
    return True


def escribir_estado(directorio, **campos):
    """Actualiza estado.json con los campos dados y devuelve el estado resultante."""
    datos = leer_estado(directorio) or {}
    datos.update(campos)
    escribir_json(directorio, almacen.MARCA_TRABAJO, datos)
    return datos


class Cola:
    def __init__(self, base, registrar=None, max_paginas=None, base_publica=None):
        self.base = Path(base)
        self.base.mkdir(parents=True, exist_ok=True)
        self.base_publica = Path(base_publica) if base_publica else None
        if self.base_publica:
            self.base_publica.mkdir(parents=True, exist_ok=True)
        self.registrar = registrar or (lambda m: None)
        self.max_paginas = max_paginas
        self._fila = deque()
        self._cerrojo = threading.Lock()
        self._hay_trabajo = threading.Event()
        self._actual = None
        self._obrero = None
        self._proc = None       # el subproceso vivo, para poder matarlo al cancelar
        self._cancelados = set()

    # -- ciclo de vida ------------------------------------------------------
    def arrancar(self):
        self.sanear()
        self._obrero = threading.Thread(target=self._bucle, daemon=True)
        self._obrero.start()

    def _casas(self):
        return [c for c in (self.base, self.base_publica) if c]

    def sanear(self):
        """Un reinicio mata al obrero pero no a los estado.json que dejó abiertos."""
        for d in sorted(x for c in self._casas() for x in c.glob("*/")):
            e = leer_estado(d)
            if e and e.get("estado") in (CORRIENDO, EN_COLA):
                escribir_estado(
                    d,
                    estado=INTERRUMPIDO,
                    error="el servicio se reinició mientras esta auditoría estaba en curso",
                    terminado_en=_ahora(),
                )
                self.registrar("saneado: %s quedó %s" % (d.name, INTERRUMPIDO))

    # -- API ----------------------------------------------------------------
    def encolar(self, crudo, cliente=None, publica=False, paginas=None):
        """Encola una auditoría.

        `publica` decide DÓNDE cae, y por lo tanto quién la va a poder ver:

          - False (por defecto): en la casa privada. Es lo que se lanza desde el M4, o
            sea el trabajo de cliente.
          - True: en la casa pública, que es la que ve internet. Es lo que lanza un
            visitante probando la herramienta.

        El defecto es PRIVADO a propósito. Antes toda auditoría caía en la raíz pública
        sin importar quién la lanzara, así que cada trabajo de cliente hecho desde el M4
        quedaba visible en internet — pasó con una auditoría real el 24-ago-2026. Ante la
        duda, que no se publique.
        """
        url, sitio = normalizar_url(crudo)
        paginas = _tope_paginas(paginas)
        if publica and self.ya_es_privado(sitio):
            # Un sitio que YA tiene trabajo privado es trabajo de cliente, y no deja de
            # serlo porque lo escriba otra persona en el formulario público. El reparto
            # miraba sólo QUIÉN lanzaba, no DE QUIÉN era el sitio: el 25-ago-2026 alguien
            # escribió el dominio de un cliente desde internet y su auditoría quedó
            # publicada, con puntajes de seguridad incluidos.
            publica = False
            self.registrar("%s ya tiene auditorías privadas: la nueva NO se publica" % sitio)
        base = self.base_publica if (publica and self.base_publica) else self.base
        ident = "%s-%s" % (sitio, datetime.now().strftime("%Y-%m-%d"))
        # Dos auditorías del mismo sitio el mismo día no se pisan: la segunda es -2. Y se
        # comprueba en LAS DOS casas, no sólo en la de destino: el id tiene que ser único
        # en el índice, que deduplica por nombre de directorio. Con el mismo nombre en las
        # dos casas, una de las dos desaparece del listado y sólo queda una línea de
        # COLISIÓN en el log.
        nombre, n = ident, 2
        while any((c / nombre).exists() for c in self._casas()):
            nombre = "%s-%d" % (ident, n)
            n += 1
        directorio = base / nombre
        directorio.mkdir(parents=True)
        escribir_estado(
            directorio,
            estado=EN_COLA,
            url=url,
            sitio=sitio,
            cliente=cliente or None,
            paginas=paginas,
            encolado_en=_ahora(),
            paso="esperando turno",
        )
        with self._cerrojo:
            self._fila.append(directorio)
            posicion = len(self._fila)
        self._hay_trabajo.set()
        self.registrar("encolado %s (%s) · posición %d" % (directorio.name, url, posicion))
        return directorio.name

    def ya_es_privado(self, sitio):
        """¿Este sitio ya tiene alguna auditoría en la casa privada?

        La lista de clientes no se configura en ningún lado: el disco ya sabe cuáles son.
        Así no hay una lista que mantener y que se olvide justo con el cliente nuevo.
        """
        for d in self.base.glob("*/"):
            e = leer_estado(d) or {}
            suyo = e.get("sitio") or (almacen.leer_json(d, almacen.MARCA) or {}).get("sitio")
            if suyo == sitio:
                return True
        return False

    def cancelar(self, ident):
        """Cancela un trabajo, este esperando o corriendo.

        Encolar algo por error costaba cinco minutos de espera o borrar el directorio a
        mano. Al que espera se lo saca de la fila; al que corre se le manda SIGTERM y,
        si no se muere en 10 s, SIGKILL: `auditar.sh` levanta Chrome y no siempre baja
        a la primera.
        """
        with self._cerrojo:
            for d in list(self._fila):
                if d.name == ident:
                    self._fila.remove(d)
                    escribir_estado(d, estado=CANCELADO, terminado_en=_ahora(),
                                    paso=None, error="cancelada antes de empezar")
                    self.registrar("cancelado %s (estaba en cola)" % ident)
                    return True
            if self._actual is None or self._actual.name != ident:
                return False
            # Se marca DENTRO del cerrojo y antes de matar: aunque la senal no alcance a
            # nadie (el proceso puede estar entre dos fases), el obrero ve la marca y
            # abandona igual. Sin esto habia una ventana en la que cancelar devolvia True
            # y la auditoria seguia sus cinco minutos.
            self._cancelados.add(ident)
            proc = self._proc
        self._matar_grupo(proc)
        self.registrar("cancelado %s (estaba corriendo)" % ident)
        return True

    def _matar_grupo(self, proc):
        """Mata el ARBOL entero, no solo a bash.

        Dos razones, las dos comprobadas en auditar.sh:

        1. `trap 'matar_chromes' EXIT INT TERM` y el handler termina en `return 0`, sin
           salir: bash atiende el SIGTERM y SIGUE. Senalar solo al hijo directo no hace
           nada.
        2. Los ejes corren como `correr_eje ... &`, subshells con su propio node y su
           propio Chrome. Matar a bash los deja huerfanos, y como heredaron el stdout,
           la tuberia NUNCA da EOF: el hilo obrero se queda bloqueado leyendo y la cola
           se atasca hasta que la auditoria termina sola.

        Con start_new_session=True el hijo encabeza su propio grupo de procesos, y
        killpg alcanza a toda la descendencia.
        """
        if not proc or proc.poll() is not None:
            return
        try:
            gid = os.getpgid(proc.pid)
        except OSError:
            return
        for señal, espera in ((signal.SIGTERM, 6), (signal.SIGKILL, 4)):
            try:
                os.killpg(gid, señal)
            except OSError:
                return
            for _ in range(espera * 2):
                if proc.poll() is not None:
                    return
                time.sleep(0.5)

    def situacion(self):
        with self._cerrojo:
            return {
                "corriendo": self._actual.name if self._actual else None,
                "en_cola": [d.name for d in self._fila],
            }

    # -- obrero -------------------------------------------------------------
    def _bucle(self):
        while True:
            self._hay_trabajo.wait()
            with self._cerrojo:
                if not self._fila:
                    self._hay_trabajo.clear()
                    continue
                directorio = self._fila.popleft()
                self._actual = directorio
            try:
                self._correr(directorio)
            except Exception as err:  # el obrero NUNCA muere: un trabajo malo no mata la fila
                self.registrar("ERROR en %s: %r" % (directorio.name, err))
                escribir_estado(directorio, estado=FALLO, error=repr(err), terminado_en=_ahora())
            finally:
                self._cancelados.discard(directorio.name)
                with self._cerrojo:
                    self._actual = None
                    self._proc = None

    def _correr(self, directorio):
        e = leer_estado(directorio) or {}
        url = e.get("url")
        if self._fue_cancelado(directorio, "antes de empezar"):
            return
        escribir_estado(directorio, estado=CORRIENDO, iniciado_en=_ahora(), paso="midiendo")
        self.registrar("corriendo %s" % directorio.name)

        cmd = ["bash", str(AUDITAR), url, "--salida", str(directorio)]
        if e.get("cliente"):
            cmd += ["--cliente", e["cliente"]]
        # El alcance elegido al encolar manda sobre el tope global del entorno: uno es una
        # decision de esta auditoria y el otro un limite de la maquina.
        tope = e.get("paginas") or self.max_paginas
        if tope:
            cmd += ["--paginas", str(tope)]

        log = directorio / "corrida.log"
        codigo = self._ejecutar(cmd, log, directorio, "midiendo")
        if self._fue_cancelado(directorio, "a mitad de la medición"):
            return
        if codigo != 0:
            escribir_estado(
                directorio, estado=FALLO, terminado_en=_ahora(),
                error="auditar.sh terminó con código %s — ver corrida.log" % codigo,
                paso=None,
            )
            self.registrar("FALLO %s (código %s)" % (directorio.name, codigo))
            return

        resultados = directorio / almacen.MARCA
        if not resultados.is_file():
            escribir_estado(directorio, estado=FALLO, terminado_en=_ahora(),
                            error="la medición terminó sin resultados.json", paso=None)
            return

        if self._fue_cancelado(directorio, "antes de armar el informe"):
            return
        escribir_estado(directorio, paso="armando el informe")
        codigo = self._ejecutar(
            ["node", str(INFORME), str(resultados), "--salida", str(directorio)],
            log, directorio, "armando el informe",
        )
        if self._fue_cancelado(directorio, "mientras se armaba el informe"):
            return
        # La medición sirve aunque el informe falle: se marca lista con el aviso, en vez
        # de tirar a la basura cinco minutos de mediciones válidas.
        fallo_informe = (
            "la medición está completa pero el informe falló (código %s)" % codigo
            if codigo != 0 else None
        )

        # El delta contra la corrida anterior va ANTES de marcar LISTO, y en su propio
        # try: si falla, la auditoría ya está medida y no se pierde por una comparación.
        #
        # Antes iba después, y no se veía: el índice invalida su caché por el mtime de
        # resultados.json y estado.json, así que un comparacion.json escrito después del
        # último estado no cambiaba ninguna huella. La ficha quedaba cacheada SIN delta
        # hasta que algo más tocara el directorio.
        #
        # Depende sólo de resultados.json, así que también corre si el informe falló.
        escribir_estado(directorio, paso="comparando con la anterior")
        try:
            self.comparar_con_anterior(directorio, e.get("sitio") or "")
        except Exception as err:
            self.registrar("comparacion fallida (la auditoria sigue lista): %r" % err)

        escribir_estado(directorio, estado=LISTO, terminado_en=_ahora(), paso=None,
                        error=fallo_informe)
        self.registrar("listo %s" % directorio.name)

    def _fue_cancelado(self, directorio, cuando):
        """¿Se pidió cancelar este trabajo? Se consulta entre fases, no solo tras medir.

        Antes solo se miraba después de la medición: cancelar durante `informe.mjs`
        mataba el node, el codigo de salida no era cero, y el trabajo terminaba escrito
        como LISTO mientras al usuario se le decia "cancelada". Ademas el id se quedaba
        en el conjunto para siempre.
        """
        if directorio.name not in self._cancelados:
            return False
        self._cancelados.discard(directorio.name)
        escribir_estado(directorio, estado=CANCELADO, terminado_en=_ahora(),
                        paso=None, error="cancelada " + cuando)
        return True

    def anterior_de(self, sitio, excluir, destino=None):
        """La auditoría LISTA más reciente del mismo sitio, distinta de `excluir`.

        Mira las dos casas, pero en UN SOLO SENTIDO. Una auditoría privada puede
        compararse contra una corrida pública anterior del mismo sitio; una pública NO
        puede compararse contra una privada. El comparacion.json queda dentro del
        directorio que se compara, así que una comparación al revés publicaría el
        nombre, la fecha y el puntaje de una auditoría de cliente que el índice sí está
        ocultando bien.
        """
        casas = self._casas()
        if destino is not None and self.base_publica and almacen.dentro_de(
                self.base_publica, destino):
            casas = [self.base_publica]

        candidatas = []
        for casa in casas:
            for d in casa.glob("*/"):
                res = d / almacen.MARCA
                if d.name == excluir or not res.is_file():
                    continue
                e = leer_estado(d) or {}
                # Una corrida cancelada a mitad del informe deja resultados.json en
                # disco. Compararse contra ella sería medir el progreso contra un
                # trabajo que nadie dio por bueno. Sin estado.json es una auditoría
                # anterior al motor web, y esas sí valen: el sitio sale del resultado.
                if e.get("estado") and e["estado"] != LISTO:
                    continue
                suyo = e.get("sitio") or (almacen.leer_json(d, almacen.MARCA) or {}).get("sitio")
                if suyo != sitio:
                    continue
                try:
                    candidatas.append((res.stat().st_mtime, d))
                except OSError:
                    continue
        return max(candidatas)[1] if candidatas else None

    def comparar_con_anterior(self, directorio, sitio):
        """Compara esta corrida con la anterior del mismo sitio y guarda el delta.

        No calcula nada por su cuenta: llama a comparar.mjs, que ya sabe lo difícil —
        que un indicador que antes estaba NO_MEDIDO y ahora se mide NO es una mejora,
        sino que antes no se sabía.

        Es lo que convierte un diagnóstico suelto en un seguimiento: sin el "desde la
        última vez", cada informe empieza de cero y no hay nada que mostrarle al cliente
        después de arreglar.
        """
        d = Path(directorio)
        anterior = self.anterior_de(sitio, excluir=d.name, destino=d)
        if not anterior:
            return None
        try:
            p = subprocess.run(
                ["node", str(COMPARAR), str(d / almacen.MARCA),
                 str(anterior / almacen.MARCA), "--json"],
                capture_output=True, text=True, timeout=120, env=self._entorno(),
            )
            if p.returncode != 0 or not p.stdout.strip():
                self.registrar("no se pudo comparar %s con %s" % (d.name, anterior.name))
                return None
            datos = json.loads(p.stdout)
        except (OSError, ValueError, subprocess.SubprocessError) as err:
            self.registrar("comparación fallida en %s: %r" % (d.name, err))
            return None

        datos["contra"] = anterior.name
        escribir_json(d, "comparacion.json", datos)
        r = datos.get("resumen") or {}
        self.registrar("comparado %s contra %s · %s mejoran, %s empeoran" % (
            d.name, anterior.name, r.get("mejoran"), r.get("empeoran")))
        return datos

    def _entorno(self):
        """El entorno de los subprocesos, con la clave de PageSpeed si la hay.

        Se lee del ARCHIVO en cada corrida, nunca de una variable puesta a mano: así la
        clave no queda en el historial del shell ni dentro del plist, solo en ~/.psi-key
        con permisos 600. Sin ella el eje 1 no obtiene datos de campo (CrUX) y ese
        indicador sale NO_MEDIDO con su motivo, que es el comportamiento correcto.
        """
        entorno = dict(os.environ)
        ruta = os.environ.get("PSI_KEY_FILE")
        if ruta:
            try:
                clave = Path(ruta).expanduser().read_text(encoding="utf-8").strip()
                if clave:
                    entorno["PSI_KEY"] = clave
            except OSError:
                self.registrar("AVISO: no se pudo leer PSI_KEY_FILE (%s)" % ruta)
        return entorno

    def _ejecutar(self, cmd, log, directorio, etapa):
        """Corre el comando volcando su salida al log y publicando el avance.

        Sin shell=True: los argumentos van como lista, así que nada de lo que escriba el
        usuario se interpreta.
        """
        with open(log, "a", encoding="utf-8") as fh:
            fh.write("\n$ %s\n" % " ".join(cmd))
            fh.flush()
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                cwd=str(directorio), text=True, bufsize=1, env=self._entorno(),
                start_new_session=True,   # ver _matar_grupo: sin esto el cancelar no cancela
            )
            with self._cerrojo:
                self._proc = proc
                # El obrero fija _actual ANTES de lanzar el subproceso, asi que hay una
                # ventana en la que cancelar() ve el trabajo corriendo pero _proc todavia
                # es None: no mata a nadie y la auditoria sigue sus cinco minutos. Aca
                # ya existe el proceso, asi que si la marca llego en esa ventana, se
                # ejecuta ahora.
                cancelado_al_vuelo = directorio.name in self._cancelados
            if cancelado_al_vuelo:
                self._matar_grupo(proc)
            for linea in proc.stdout:
                fh.write(linea)
                fh.flush()
                paso = self._paso_de(linea)
                if paso:
                    escribir_estado(directorio, paso=paso)
            codigo = proc.wait()
            try:
                proc.stdout.close()   # si no, cada trabajo deja la tuberia abierta
            except OSError:
                pass
            with self._cerrojo:
                self._proc = None
            return codigo

    @staticmethod
    def _paso_de(linea):
        """Traduce la salida de auditar.sh a algo que se pueda enseñar."""
        limpia = re.sub(r"\x1b\[[0-9;]*m", "", linea).strip()
        m = re.match(r"^(\d)/6\s*·\s*(.+)$", limpia)
        if m:
            return "paso %s de 6 — %s" % (m.group(1), m.group(2)[:70])
        if limpia.startswith("descubrir-urls:"):
            return "descubriendo páginas"
        if limpia.startswith("✔ eje"):
            return limpia[:80]
        return None
