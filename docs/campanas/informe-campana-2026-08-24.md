# Informe de la campaña de calibración

**24-ago-2026** · 15 objetivos × 2 corridas = **30 auditorías** · Diseño:
`docs/specs/2026-08-24-campana-calibracion-diseno.md`

## Resultado

| Criterio de éxito | Resultado |
|---|---|
| 1. Ninguno crashea, cuelga ni excede su tope | **30/30 terminaron `listo`** |
| 2. Cero invariantes rotas | **0** (tras corregir una invariante mía, ver abajo) |
| 3. Los oráculos independientes coinciden | **Sí**, y exacto en descubrimiento |
| 4. Sin deltas inexplicados en la segunda corrida | **Sí**: 7/7 hostiles idénticos, 6/8 reales dentro de Δ0,1. Los 2 restantes, explicados |
| 5. Informe con cada defecto, arreglado o no | Este documento |

## El resultado que más importa: el caso de cabeceras-solo-en-404

El corpus incluye una réplica local dun error real, documentado, cometido sobre un sitio WordPress con caché de página,
y que VSL repitió dos veces: un servidor que **emite cabeceras de seguridad en el 404 y
ninguna en el 200**, porque el 404 ejecuta PHP y la página real sale del caché.

El motor lo resolvió bien:

```
eje5.x_frame_options          mal · presente_en: 0, de: 3
eje5.x_content_type_options   mal · presente_en: 0, de: 3
eje5.referrer_policy          mal · presente_en: 0, de: 3

medición anulada: "la URL devolvió 404, no 200. REGLA DURA 1: las cabeceras
solo se miden sobre respuestas 200"
```

Reportó las páginas reales como desprotegidas —que es la verdad— y dejó constancia de por
qué descartó el 404. **La trampa que hizo caer a una agencia está cerrada y ahora tiene una
prueba permanente que lo demuestra.**

## Hallazgo sobre un sitio de cliente

Un sitio del corpus puntuó **5,2 en seguridad** en la primera corrida y **6,9** en la
segunda. Mismas páginas, misma cobertura, el mismo día.

La causa no es el motor: cuando falla el caché de página y la portada la genera PHP, la
respuesta sale sin las cabeceras de seguridad y filtra la versión del runtime. Cuando sale
del caché, las trae. Es el mismo fenómeno del caso de cabeceras-solo-en-404 ocurriendo en producción.

El motor lo capturó nombrando la página exacta, y no se reprodujo en ocho peticiones
posteriores: es intermitente y raro.

*El detalle, con el sitio y los valores, está en la documentación privada de ese cliente.
No va en este repositorio.*

## Defectos encontrados

### En el motor — 1, arreglado

**`eje5-seguridad.mjs`: perfil de Chrome apuntando a un scratchpad muerto.** El
`--user-data-dir` por defecto estaba harcodeado a
`<tmp>/.../6468ae74-.../site-audit-pruebas/chrome-5` — el directorio de
trabajo de la sesión en que se construyó la skill. Efímero, atado a una sesión que ya no
existe, y sólo seguía funcionando porque el propio eje lo recreaba en cada corrida. Los otros
cinco ejes usaban `os.tmpdir()/site-audit-chrome-<puerto>`.

Arreglado: ahora hace lo mismo que sus hermanos. Es un arreglo objetivo, no toca criterios.

### En el arnés de calibración — 3, todos míos

Vale la pena listarlos porque son la mitad de lo que la campaña enseñó: **el verificador se
equivocó más veces que lo verificado.**

1. **El oráculo de sitemap contaba imágenes como páginas.** Los sitemaps de WooCommerce
   incluyen `<image:loc>` por cada foto de producto. Mi parser recorría el árbol entero
   buscando etiquetas `loc` y contó **131 "páginas" donde había 47**. Estuve a punto de
   denunciar que el motor se perdía los 62 productos de una tienda del corpus. Corregido, el oráculo
   ahora coincide **exacto** con el motor: 47 vs 47 y 31 vs 31.
2. **El oráculo de TLS usaba `-brief`**, que el `openssl` de macOS (LibreSSL 3.3.6) no
   soporta. Y hay una trampa peor detrás: LibreSSL imprime `New, TLSv1/SSLv3` incluso en
   conexiones TLS 1.3, así que leer esa línea habría reportado "protocolo obsoleto" en sitios
   perfectamente sanos.
3. **Una invariante demasiado estricta.** Exigía que todo puntaje de eje estuviera en [0,10],
   sin contemplar que un eje ENTERO puede estar legítimamente `NO_MEDIDO`: pasa en los sitios
   de localhost, donde PageSpeed no puede llegar y el eje 1 se niega a puntuar. Produjo **14
   falsos positivos**, uno por cada corrida hostil.

Los tres están arreglados y los tres dejaron prueba de regresión.

## Cobertura de la verificación

| Capa | Estado |
|---|---|
| Caídas y cuelgues | 30/30 sin incidentes |
| Invariantes internas (6) | 0 fallos. Probadas con 10 mutaciones deliberadas: 10 detectadas |
| Oráculo `curl` (cabeceras) | Sin discrepancias en los sitios reales |
| Oráculo `openssl` (TLS) | Funcionando tras el arreglo: TLS 1.3 en un sitio, 1.2 en otro |
| Oráculo sitemap (descubrimiento) | Coincidencia **exacta** con el motor |
| Repetibilidad | 7/7 hostiles idénticos · 6/8 reales dentro de Δ0,1 |

### Lo que quedó SIN validación independiente

**El eje 1 (performance), que es el de mayor peso en el puntaje global (0,25).** El único
oráculo verdaderamente independiente sería la API de PageSpeed Insights —otra máquina, otra
red— y **no hay `PSI_KEY` en el entorno**. Contrastar contra el JSON crudo de Lighthouse
validaría la extracción, no la medición, y llamarlo validación cruzada sería mentir.

Lo mismo, en menor grado, para el eje 6: `HTML_CodeSniffer` no está instalado en el M4, así
que el segundo motor de accesibilidad que la rúbrica pide sale `NO_MEDIDO` con su motivo.

## Deltas de la segunda corrida, explicados

| Sitio | Δ global | Explicación |
|---|---|---|
| un sitio WordPress + WooCommerce del corpus | 0,5 | El flapping de cabeceras de la home (arriba). Del sitio, no del motor |
| `voidspore.tech` | 0,1 | eje6 6,9→5,9: axe detecta distinto según qué renderiza la SPA |
| Los otros 6 | ≤0,1 | Ruido de red |

Los dos movimientos de eje están dentro de lo que un sitio dinámico puede cambiar entre dos
corridas; ninguno viene de un cálculo inestable.

## Qué cambia esto en la nota

Antes de la campaña la herramienta valía **6/10**, y la razón declarada era que había
auditado tres sitios y no había tocado el mundo. Ahora lleva **quince objetivos, treinta
corridas, siete casos hostiles reproducibles y un defecto real detectado en producción de un
cliente**, con un solo defecto propio encontrado y arreglado.

No la sube a 9 por sí sola: falta el arnés de regresión (sub-proyecto B) que congele estos
resultados, y falta la `PSI_KEY` para que el eje más pesado tenga validación independiente.
Pero sí cierra el hueco que justificaba el 6.
