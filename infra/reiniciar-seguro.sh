#!/usr/bin/env bash
# Reinicia la vitrina SOLO si se puede DEMOSTRAR que no hay nada corriendo.
#
# Existe porque el 23-ago-2026 un reinicio mató una auditoría de Daniel a los 40 s: se
# comprobó que no corriera "la que estábamos mirando", no la cola entera.
#
# Falla CERRADO, a propósito. La primera versión de este script trataba una respuesta
# vacía como "no hay nada que perder" y reiniciaba — justo el fallo que venía a evitar,
# porque con tres Chrome headless corriendo un timeout de curl es lo MÁS probable.
# Ahora, si no se puede probar que la cola está quieta, no se reinicia.
#
#   ./reiniciar-seguro.sh          -> se niega salvo que la cola esté demostrablemente vacía
#   ./reiniciar-seguro.sh --igual  -> reinicia igual, diciendo qué se pierde
set -uo pipefail
PUERTO="${PUERTO:-8096}"
SERVICIO="${SERVICIO:-com.example.auditorias}"
IGUAL="${1:-}"

no_reiniciar() {
  echo "$1"
  if [ "$IGUAL" = "--igual" ]; then
    echo "--igual: se reinicia de todos modos; lo que esté en curso quedará 'interrumpido'."
    return 0
  fi
  echo "NO se reinicia. Esperá, cancelá desde la web, o usá --igual si estás seguro."
  exit 1
}

cuerpo="$(curl -sS --max-time 20 -H 'X-Vitrina: 1' \
          ${AUTH_FILE:+-u "$(cat "$AUTH_FILE" 2>/dev/null)"} \
          "http://127.0.0.1:${PUERTO}/api/cola.json" 2>/dev/null)"
codigo=$?

if [ $codigo -ne 0 ]; then
  # Un curl que falla NO prueba que no haya nada: puede estar el M4 ahogado midiendo.
  no_reiniciar "no se pudo consultar la cola (curl salió con $codigo). No se puede probar que esté quieta."
elif [ -z "$cuerpo" ]; then
  no_reiniciar "la consulta a la cola volvió vacía. No se puede probar que esté quieta."
else
  ocupado="$(printf '%s' "$cuerpo" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception as e:
    print("RESPUESTA_ILEGIBLE: %s" % e)      # 404/401/HTML: tampoco prueba nada
    raise SystemExit(0)
if not isinstance(d, dict) or "corriendo" not in d:
    print("RESPUESTA_INESPERADA")
    raise SystemExit(0)
partes = []
if d.get("corriendo"): partes.append("corriendo: %s" % d["corriendo"])
if d.get("en_cola"):   partes.append("en cola: %s" % ", ".join(d["en_cola"]))
print(" · ".join(partes))')"

  case "$ocupado" in
    RESPUESTA_ILEGIBLE*|RESPUESTA_INESPERADA*)
      no_reiniciar "la cola respondió algo que no entiendo ($ocupado)." ;;
    "") : ;;  # cola demostrablemente vacía: adelante
    *) no_reiniciar "HAY TRABAJO ACTIVO -> $ocupado" ;;
  esac
fi

# unload + load, NO kickstart. `launchctl kickstart -k` reinicia el proceso pero
# reutiliza la configuracion YA CARGADA: los cambios del .plist no se aplican. El
# 25-ago-2026 se agrego PSI_KEY_FILE al plist, se "reinicio" con kickstart, y el
# servicio siguio corriendo sin esa variable — la auditoria decia "no hay PSI_KEY"
# con el archivo ya puesto y la clave funcionando. Se pierden 20 minutos buscando
# en el lugar equivocado.
PLIST="$HOME/Library/LaunchAgents/${SERVICIO}.plist"
if [ -f "$PLIST" ]; then
  launchctl unload "$PLIST" 2>/dev/null
  launchctl load "$PLIST"
else
  launchctl kickstart -k "gui/$(id -u)/${SERVICIO}"
fi
echo "reiniciado (plist recargado)."
