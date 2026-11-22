# Campaña de calibración del motor site-audit — diseño

**24-ago-2026** · Sub-proyecto A de 4 · Estado: aprobado

## Por qué

El motor tiene dos días de vida y ha auditado tres sitios, uno de los cuales es
`example.com`. Se calificó honestamente en **6/10** frente a las herramientas profesionales,
y la razón principal no es el código: es que **no ha tocado el mundo real**. Screaming Frog
vale lo que vale por quince años de casos raros; éste lleva tres.

Esta campaña existe para que se rompa **acá**, con sitios controlados, y no delante de un
cliente.

## Alcance

**Esto (A):** correr el motor contra un corpus de 15 sitios, verificar los resultados con tres
capas de comprobación, arreglar lo objetivo que aparezca, y entregar un informe de campaña.

**No es esto** — cada uno tendrá su propio diseño:
- **B.** Arnés de regresión (pruebas de oro). Depende de A: primero hay que saber qué congelar.
- **C.** Rediseño del informe (resumen ejecutivo de una página + parcialidad estructural).
- **D.** Histórico y monitoreo.

## Decisión: la vitrina orquesta, el arnés analiza

Se evaluaron tres formas:

| | Enfoque | Por qué no |
|---|---|---|
| A | `calibrar.sh` dentro de la skill | La skill es un entregable de cliente, no un banco de pruebas |
| B | Arnés con su propio corredor | Habría que escribir un segundo orquestador serial que ya existe |
| **C** | **La vitrina encola, el arnés lee** | **Elegido** |

La cola construida el 23-ago ya es serial (obligatorio: el eje 1 debe correr solo), ya
sobrevive reinicios, ya registra estado y deja todo en un almacén indexado. Además la
herramienta **se come su propia comida**: si 15 auditorías seguidas revientan la cola, eso ya
es un hallazgo de la campaña.

## Arquitectura

Tres unidades de trabajo más un punto de entrada, cada una testeable sola:

| Unidad | Qué hace | De qué depende |
|---|---|---|
| `calibracion/corpus.py` | Define los sitios y **levanta los casos hostiles en localhost** | stdlib |
| `calibracion/oraculos.py` | Verificadores independientes: `curl`, `openssl`, sitemap propio | `corpus` |
| `calibracion/veredicto.py` | Invariantes, diff de repetibilidad, informe de campaña | `oraculos` |
| `calibracion/campana.py` | Entrada: encola en la vitrina, espera, evalúa | las tres |

## El corpus

### 8 sitios reales

**La lista concreta no está en el repositorio**: son sitios de clientes, y una cartera de
clientes no va en el código. Vive en `calibracion/corpus.local.json`, fuera del control de
versiones (hay un `.example` al lado).

Lo que sí importa documentar es **qué formas de sitio cubre**, porque de eso depende que la
campaña sea representativa:

| Forma | Cuántos | Qué pone a prueba |
|---|---|---|
| WordPress + WooCommerce | 1 | Sitemaps con índice y sub-sitemaps, `<image:loc>`, muchos productos |
| WordPress con caché de página | 1 | La disociación entre lo que emite PHP y lo que emite el caché |
| SPA en Firebase, con login | 3 | 200 para toda ruta inexistente, sin sitemap útil, ruteo del lado del cliente |
| App detrás de Cloudflare | 1 | Cabeceras puestas en el borde, no en el origen |
| Estático detrás de un túnel | 1 | El caso simple, como control |
| Landing de una sola página | 1 | Que "1 de 1" no se confunda con un fallo de descubrimiento |

**Las tres SPAs están a propósito.** Es donde los crawlers se rompen: responden 200 a
cualquier ruta, así que el descubrimiento puede inventar páginas que no existen, y la
portada es la pantalla de acceso.

### 7 casos hostiles, servidos en localhost

No se sale a molestar sitios ajenos: se levantan servidores propios, reproducibles y
deterministas.

| Caso | Qué debe pasar |
|---|---|
| **Cabeceras solo en el 404** | Cabeceras en el 404, ninguna en el 200. La regla dura 1 debe anular la medición del 404 |
| 500 permanente | Se registra como hallazgo; no tumba la auditoría |
| Cadena de redirecciones | Se resuelve al documento final sin contarlo dos veces |
| HTML inválido | No revienta el parseo |
| Charset no-UTF8 | El texto no sale corrupto en el informe |
| Sitio lento | No cuelga; respeta sus propios topes de tiempo |
| Sin sitemap | Cae al descubrimiento por menú y lo declara |

El caso de cabeceras-solo-en-404 es el más valioso: convierte en **prueba permanente** el error que una agencia —y VSL,
dos veces— cometió en la vida real.

## Las tres capas de verificación

### Capa 1 — qué se cae

Crash, cuelgue, código de salida distinto de cero, o explosión de `NO_MEDIDO`: más del 20% de
los indicadores sin medir **en uno de los 8 sitios reales** es señal de que algo se rompió, no
de honestidad. En los casos hostiles un `NO_MEDIDO` alto puede ser la respuesta correcta, así
que ahí el umbral no aplica y cada caso declara de antemano qué espera.

### Capa 2 — invariantes internas

No necesitan verdad externa. Se comprueban sobre `resultados.json`:

- Ningún conteo por página supera el tamaño de la muestra
- Todo número tiene procedencia (`metodo`, `desde`, herramienta con versión)
- Los puntajes caen en `[0, 10]` y el global coincide con la suma ponderada declarada
- Si `cobertura_paginas.muestra_parcial` es cierto, el informe lo declara textualmente
- Ningún `NO_MEDIDO` sin `motivo_no_medido`
- Ningún `esfuerzo_horas` distinto de `NO_MEDIDO` (la guardia del 22-ago, verificada desde fuera)

### Capa 3 — oráculos externos, con su independencia declarada

**Esta distinción es el corazón del diseño.** No toda validación cruzada prueba lo mismo, y
confundirlas sería cometer en el informe de campaña el mismo pecado que la herramienta existe
para evitar.

| Eje | Oráculo | Qué prueba |
|---|---|---|
| 5 · cabeceras | `curl` directo sobre las mismas URLs | **Independiente**: no pasa por Chrome ni CDP |
| 5 · TLS | `openssl s_client` | **Independiente**: no pasa por SSL Labs |
| descubrimiento | sitemap parseado por separado | **Independiente** |
| 1 · performance | API de PageSpeed Insights | **Independiente**: otra máquina, otra red |
| 1 y 6 | JSON crudo de Lighthouse / axe | **Circular**: valida la EXTRACCIÓN, no la medición. Se declara así |

### Capa 4 — repetibilidad

Cada sitio se audita **dos veces** y se diffean con `comparar.mjs`, que ya existe. Todo delta
que no sea atribuible a variación de red es no-determinismo, o sea un defecto.

## Política de arreglos

Se arregla **lo objetivo**: crashes, cuelgues, extracciones mal hechas, invariantes rotas,
no-determinismo.

**No se toca** nada que cambie qué se considera "bien" o "mal": umbrales, ponderaciones,
criterios de severidad. Eso altera lo que se le dice al cliente y lo decide Daniel con la
evidencia delante. Si la campaña sugiere que un criterio está mal calibrado, va al informe
como recomendación, no como cambio.

## Bloqueo conocido

**No hay `PSI_KEY` en el entorno.** Es el único oráculo verdaderamente independiente de
performance (mide desde la infraestructura de Google, no desde el M4). Se saca gratis en
Google Cloud.

Sin ella, el eje 1 se queda con extracción + repetibilidad, y el informe de campaña **debe
declarar que el eje 1 no tiene validación independiente**. No se sustituye por nada ni se
disimula.

## Criterio de éxito

La campaña termina cuando, sobre los 15 sitios:

1. Ninguno crashea, cuelga ni supera su tope de tiempo
2. Cero invariantes rotas
3. Los oráculos independientes coinciden con el motor, o la discrepancia está explicada
4. La segunda corrida de cada sitio no muestra deltas inexplicados
5. Existe un informe de campaña que lista **cada defecto encontrado, arreglado o no**, y
   declara explícitamente qué quedó sin validación independiente

El punto 5 es el entregable. Una campaña que solo diga "todo bien" no sirve: lo que la hace
valiosa es la lista de lo que se rompió.

## Riesgo

Bajo para terceros: el motor **solo mide**, y los casos hostiles son locales. El riesgo real es
de tiempo de máquina — 15 sitios × 2 corridas × ~6 min ≈ **3 horas de M4 en serie**, más lo que
tomen los arreglos.
