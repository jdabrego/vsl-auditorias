# Plan de implementación — campaña de calibración

Diseño: `2026-08-24-campana-calibracion-diseno.md`

## Orden

| # | Paso | Por qué en este orden |
|---|---|---|
| 1 | `corpus.py` — sitios y servidores hostiles | Sin corpus no hay nada que correr |
| 2 | Probar los 7 hostiles a mano | Si el andamio miente, toda la campaña miente |
| 3 | `veredicto.py` — invariantes | Se puede aplicar ya sobre las 4 auditorías que existen |
| 4 | Correr invariantes sobre lo existente | Hallazgos gratis antes de gastar 3 h de máquina |
| 5 | `oraculos.py` — curl, openssl, sitemap | La capa cara; después de la barata |
| 6 | `campana.py` — orquestador | Une todo |
| 7 | Lanzar la campaña | 3 h de M4 |
| 8 | Arreglar lo objetivo, reejecutar lo afectado | Política del diseño |
| 9 | Informe de campaña | El entregable |

El paso 4 es deliberado: las invariantes no necesitan corridas nuevas, así que se estrenan
sobre las auditorías del 22 y 23 de agosto. Si algo está roto, se sabe **antes** de las tres
horas, no después.

## Qué se considera terminado

Los cinco criterios de éxito del diseño, y el informe listando cada defecto — arreglado o no.
