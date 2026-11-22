# Vitrina de auditorías — diseño

**Fecha:** 2026-08-22 · **Fase 1 de 3** · Estado: aprobado

## Problema

La skill `site-audit` produce auditorías completas (informe HTML/PDF/markdown +
`resultados.json` + evidencia). Hoy esas auditorías no viven en ningún lado: las dos que
existen están en `<tmp>/.../scratchpad/`, que es efímero. Un reinicio del
M4 y se pierden. `find ~/Projects -name resultados.json` devuelve **cero**.

Además no hay forma de mirar una auditoría sin abrir la terminal, ni de comparar la de hoy
con la del mes pasado.

## Alcance

**Fase 1 (esto): vitrina.** Un sitio en `audit.voidspore.tech` que lista las auditorías que
existen en disco y las muestra. No las crea, no las borra, no las modifica.

**Fuera de alcance, con costura preparada:**
- Fase 2 (motor): lanzar auditorías desde el navegador. Necesita cola serial.
- Fase 3 (entregable): link con acceso para el cliente.

> **Estado al 25-ago-2026.** Fase 1 y fase 2 están construidas y en producción.
> **La fase 3 no se va a construir**, y la decisión es deliberada: hoy el informe se le
> manda al cliente como PDF y eso ya funciona: un link con acceso agrega usuarios,
> tokens y caducidad para resolver algo que no está roto. La costura de `autorizado()`
> se queda porque no cuesta nada tenerla, pero nadie está esperando esa fase.

## Decisión de fondo: la vitrina no es dueña de los archivos

`auditar.sh` ya define dónde vive una auditoría:

```
~/Projects/<proyecto>/docs/evidencia/<sitio>-YYYY-MM-DD/   si detecta el proyecto
./auditoria-<sitio>-YYYY-MM-DD/                            si no
```

La vitrina **no inventa una convención competidora ni mueve archivos**. Escanea una lista de
raíces configuradas y considera auditoría a todo directorio que contenga `resultados.json`.
El sistema de archivos es la fuente de verdad.

Consecuencia: mover, renombrar o borrar una auditoría desde el Finder simplemente funciona.
No hay base de datos que quede desincronizada, porque no hay base de datos.

## Arquitectura

Cuatro unidades, cada una con un propósito y testeable sola:

| Unidad | Qué hace | De qué depende |
|---|---|---|
| `vitrina/almacen.py` | Encuentra auditorías bajo las raíces. Resuelve rutas de forma segura. | stdlib |
| `vitrina/indice.py` | Lee `resultados.json` y arma la ficha de cada auditoría. Cachea por mtime. | `almacen` |
| `vitrina/web.py` | Auth, ruteo, servir archivos. | `indice`, `almacen` |
| `servidor.py` | Arranque: puerto, raíces, `ThreadingHTTPServer`. | `web` |

Sin framework, sin build, sin `npm install`, sin base de datos. Python stdlib. Es el mismo
molde que `vsl-motor-contenido/panel/servidor.py`, que ya lleva meses en producción en
`motor.voidspore.tech`.

Objetivo de compatibilidad: **Python 3.9** (`/usr/bin/python3` del M4). Sin `match`, sin
`X | Y` en anotaciones evaluadas.

## Rutas

| Ruta | Devuelve |
|---|---|
| `GET /` | Índice: todas las auditorías, agrupadas por sitio, la más nueva primero |
| `GET /a/<id>/` | `informe.html` de esa auditoría (es autocontenido: cero referencias externas) |
| `GET /a/<id>/<archivo>` | Un archivo de la auditoría: `informe.pdf`, `resultados.json`, el markdown |
| `GET /api/indice.json` | El índice en JSON. Costura para las fases 2 y 3 |
| `GET /salud` | `ok` sin auth, para el watchdog de launchd |

`<id>` es el nombre del directorio de la auditoría, que ya es único y legible
(`ejemplo.com-2026-08-22`). Si dos raíces tuvieran el mismo nombre de directorio, gana la
raíz declarada primero y la colisión se registra en el log.

## Ficha del índice

Todo sale de `resultados.json`, ningún campo se calcula ni se estima:

`sitio` · `cliente` · `fecha_iso` · `puntaje_global` · puntaje de los 6 ejes ·
`cobertura_paginas` (auditadas / descubiertas) · `duracion_segundos` · nº de hallazgos ·
qué artefactos existen (html / pdf / md).

**Regla heredada de la skill:** si `cobertura_paginas.muestra_parcial` es `true`, la ficha lo
declara. Una vitrina que muestre "6.6/10" sin decir que se midieron 15 de 47 páginas estaría
mintiendo igual que el informe que se arregló hoy.

Si una auditoría tiene `resultados.json` ilegible o incompleto, aparece en el índice marcada
como **ilegible** con el motivo. No se la esconde: esconder lo roto es cómo se pierde.

## Seguridad

TLS lo pone Cloudflare en el borde; el servidor escucha en `127.0.0.1:8096` y solo el túnel
llega. Igual que el panel del motor:

1. **Auth básica** contra `AUTH_FILE` (`~/.auditorias-auth`, formato `usuario:clave`). Sin
   `AUTH_FILE` el servidor arranca pero grita a stderr que está abierto.
2. **Path traversal:** toda ruta pedida se resuelve con `Path.resolve()` y se verifica que
   siga estando dentro de la raíz de esa auditoría. `../../.ssh/id_rsa` da 403.
3. **Symlinks:** se resuelven antes de comprobar; un symlink que apunte fuera de la raíz da
   403. Sin esto la comprobación anterior es decorativa.
4. Solo lectura. Ni `POST`, ni `DELETE`, ni escritura en disco.

## Costuras para las fases siguientes

- **Fase 2 (motor):** el índice lee `estado.json` si existe y usa su campo `estado`; cuando
  falta asume `listo`. Una auditoría corriendo aparecerá como tal sin tocar el indexador.
  `/api/` ya está reservado.
- **Fase 3 (entregable):** la autorización es **una sola función** (`autorizado(handler)`).
  Agregar un token por auditoría es cambiarla en un lugar.

## Pruebas

`unittest` de la stdlib (sin pytest: cero dependencias). Sobre un almacén falso en un
directorio temporal:

- El indexador encuentra las auditorías y saltea directorios sin `resultados.json`
- Una auditoría con JSON corrupto sale marcada ilegible, no tumba el índice
- `muestra_parcial` llega a la ficha
- **Path traversal bloqueado**, incluida la variante por symlink
- Sin credenciales → 401; con credenciales → 200
- `/salud` responde sin auth

## Despliegue

- `com.voidspore.auditorias.plist` en `~/Library/LaunchAgents`, `RunAtLoad` + `KeepAlive`,
  logs a `/tmp/auditorias.log` y `.err`
- Entrada de ingress en `~/.cloudflared/config.yml`: `audit.voidspore.tech` → `localhost:8096`,
  con respaldo `config.yml.bak-pre-audit-<epoch>` siguiendo la costumbre de la casa
- Registro DNS CNAME en Cloudflare para `audit`

## Riesgo

Bajo. Servicio nuevo en puerto libre, solo lectura, sin tocar los cinco servicios que ya
corren. Lo único con riesgo real es editar `config.yml` del túnel, que es compartido: por eso
se respalda antes y se valida la sintaxis después.
