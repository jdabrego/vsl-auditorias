"""Arnés de las piezas que CALCULAN: comparar.mjs y puntuar.mjs.

POR QUÉ ESTAS DOS, Y POR QUÉ APARTE DEL RESTO
─────────────────────────────────────────────
Los seis medidores necesitan Chrome, red y dos minutos. Estas dos no: toman un JSON y
devuelven otro. Son deterministas, corren en milisegundos, y encierran las reglas más
fáciles de romper en silencio — porque un error acá no revienta nada, solo cambia un
número que nadie va a poder distinguir de uno bueno.

LA REGLA QUE MÁS IMPORTA
────────────────────────
Un indicador que antes estaba NO_MEDIDO y ahora se mide **no es una mejora**: es que antes
no se sabía. Si eso se rompe, el informe le dice a un cliente que mejoró en algo que nadie
había medido nunca — y como el número se ve razonable, nadie lo va a notar.

Lo mismo al revés: que hoy no se pudiera medir algo que ayer sí no es un empeoramiento del
sitio, es un hueco de la medición.
"""

import json
import os
import subprocess
import sys
import tempfile
import shutil
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vitrina.cola import SKILL  # noqa: E402

COMPARAR = SKILL / "scripts" / "comparar.mjs"
PUNTUAR = SKILL / "scripts" / "puntuar.mjs"

ENTORNO = dict(os.environ, PATH="/opt/homebrew/bin:" + os.environ.get("PATH", ""))
NO_MEDIDO = "NO_MEDIDO"

HAY_MOTOR = COMPARAR.is_file()


def indicador(ident, valor, estado, direccion=None, unidad="s"):
    d = {
        "id": ident, "nombre": ident, "valor": valor, "unidad": unidad,
        "estado": estado, "medido_en_iso": "2026-08-26T00:00:00-04:00", "valido": True,
    }
    if direccion:
        d["umbrales"] = {"direccion": direccion, "bien": "x", "mejorar": "y"}
    return d


def documento(indicadores, global_=7.0, paginas=3):
    """El resultados.json mínimo que comparar.mjs sabe leer.

    `paginas_auditadas` lleva la LISTA de URLs, no solo el conteo, porque es de ahí de
    donde el comparador saca si las dos corridas miraron el mismo conjunto. La primera
    versión de este fixture solo ponía los números de `cobertura_paginas` y dejaba la
    lista vacía en las dos: salían iguales, y la prueba acusaba al motor de algo que
    faltaba en la prueba.
    """
    urls = [{"url": "https://x.example/p%d" % i} for i in range(paginas)]
    return {
        "sitio": "x.example", "url": "https://x.example/",
        "fecha_iso": "2026-08-26T00:00:00-04:00",
        "puntaje_global": global_, "version_esquema": "1.0",
        "paginas_auditadas": urls,
        "cobertura_paginas": {"auditadas": paginas, "descubiertas": paginas,
                              "muestra_parcial": False},
        "ponderaciones": {"eje1": 1.0},
        "ejes": {"eje1": {
            "id": "eje1", "nombre": "Performance y tiempos de carga",
            "puntaje": global_, "hallazgos": [],
            "indicadores": {i["id"]: i for i in indicadores},
        }},
    }


@unittest.skipUnless(HAY_MOTOR, "la skill site-audit no está en esta máquina")
class _ConComparar(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def comparar(self, nuevo, viejo):
        (self.tmp / "n.json").write_text(json.dumps(nuevo), encoding="utf-8")
        (self.tmp / "v.json").write_text(json.dumps(viejo), encoding="utf-8")
        p = subprocess.run(
            ["node", str(COMPARAR), str(self.tmp / "n.json"), str(self.tmp / "v.json"), "--json"],
            capture_output=True, text=True, timeout=90, env=ENTORNO)
        self.assertEqual(p.returncode, 0, "comparar.mjs falló: " + p.stderr[-400:])
        return json.loads(p.stdout)

    def fila(self, r, ident):
        for f in r.get("filas") or []:
            if f["id"] == ident:
                return f
        return None

    def no_comparable(self, r, ident):
        for f in r.get("no_comparables") or []:
            if f.get("id") == ident:
                return f
        return None


class TestNoMedidoNoEsMejora(_ConComparar):
    """La regla que el informe le enseña a un cliente y no puede fallar."""

    def test_lo_que_antes_no_se_media_no_cuenta_como_mejora(self):
        r = self.comparar(
            documento([indicador("eje1.lcp", 2.0, "bien", "menor_mejor")]),
            documento([indicador("eje1.lcp", NO_MEDIDO, "no_medido", "menor_mejor")]))
        self.assertIsNone(
            self.fila(r, "eje1.lcp"),
            "un indicador que antes estaba NO_MEDIDO entró a la tabla de comparación")
        nc = self.no_comparable(r, "eje1.lcp")
        self.assertIsNotNone(nc, "no quedó declarado como no comparable: desapareció sin más")
        self.assertIn("no es una mejora", (nc.get("motivo") or "").lower())

    def test_lo_que_hoy_no_se_pudo_medir_no_cuenta_como_empeoramiento(self):
        """Un hueco de la medición no es un retroceso del sitio."""
        r = self.comparar(
            documento([indicador("eje1.lcp", NO_MEDIDO, "no_medido", "menor_mejor")]),
            documento([indicador("eje1.lcp", 2.0, "bien", "menor_mejor")]))
        self.assertIsNone(self.fila(r, "eje1.lcp"))
        self.assertIsNotNone(self.no_comparable(r, "eje1.lcp"))

    def test_no_medido_en_las_dos_no_inventa_nada(self):
        r = self.comparar(
            documento([indicador("eje1.lcp", NO_MEDIDO, "no_medido")]),
            documento([indicador("eje1.lcp", NO_MEDIDO, "no_medido")]))
        self.assertIsNone(self.fila(r, "eje1.lcp"))
        self.assertIsNotNone(self.no_comparable(r, "eje1.lcp"))

    def test_los_no_comparables_no_cuentan_en_el_resumen(self):
        """Si contaran como mejora, el resumen que ve el cliente sube solo."""
        r = self.comparar(
            documento([indicador("eje1.lcp", 2.0, "bien", "menor_mejor")]),
            documento([indicador("eje1.lcp", NO_MEDIDO, "no_medido", "menor_mejor")]))
        self.assertEqual((r.get("resumen") or {}).get("mejoran"), 0,
                         "el resumen contó como mejora algo que nadie había medido antes")


class TestElSentidoSaleDeLaDireccionDeclarada(_ConComparar):
    """Sin dirección declarada NO se inventa el veredicto: bajar no siempre es mejorar."""

    def sentido(self, nuevo_val, viejo_val, direccion, estado_n="bien", estado_v="mal"):
        r = self.comparar(
            documento([indicador("eje1.x", nuevo_val, estado_n, direccion)]),
            documento([indicador("eje1.x", viejo_val, estado_v, direccion)]))
        f = self.fila(r, "eje1.x")
        self.assertIsNotNone(f, "el indicador no llegó a la tabla")
        return f["sentido"]

    def test_menor_mejor_bajando_es_mejora(self):
        self.assertEqual(self.sentido(2.0, 5.0, "menor_mejor"), "MEJORA")

    def test_menor_mejor_subiendo_es_empeora(self):
        self.assertEqual(self.sentido(5.0, 2.0, "menor_mejor", "mal", "bien"), "EMPEORA")

    def test_mayor_mejor_subiendo_es_mejora(self):
        self.assertEqual(self.sentido(90, 40, "mayor_mejor"), "MEJORA")

    def test_mayor_mejor_bajando_es_empeora(self):
        self.assertEqual(self.sentido(40, 90, "mayor_mejor", "mal", "bien"), "EMPEORA")

    def test_sin_direccion_no_se_inventa_un_veredicto_numerico(self):
        """El número cambia y los dos estados son iguales: nadie puede decir si es mejor.

        Es el caso que el propio comparador marca '~' — cambió el valor, no el veredicto.
        Que salga MEJORA acá sería adivinar en qué dirección se mejora un indicador que no
        lo declara.
        """
        s = self.sentido(40, 90, None, "mejorar", "mejorar")
        self.assertIn(s, ("~", "=?"),
                      "inventó el veredicto '%s' para un indicador sin dirección declarada" % s)

    def test_el_mismo_valor_no_es_ni_mejora_ni_empeora(self):
        self.assertEqual(self.sentido(3.0, 3.0, "menor_mejor", "mejorar", "mejorar"), "=")


class TestElDeltaGlobal(_ConComparar):
    """El número grande que la vitrina enseña en la ficha."""

    def test_la_diferencia_global_es_la_resta(self):
        r = self.comparar(documento([indicador("eje1.x", 1, "bien", "menor_mejor")], 8.0),
                          documento([indicador("eje1.x", 2, "mal", "menor_mejor")], 6.0))
        g = r["global"]
        self.assertEqual(g["antes"], 6.0)
        self.assertEqual(g["despues"], 8.0)
        self.assertEqual(g["diferencia"], 2.0)
        self.assertEqual(g["sentido"], "MEJORA")

    def test_bajar_el_puntaje_global_es_empeora(self):
        r = self.comparar(documento([indicador("eje1.x", 2, "mal", "menor_mejor")], 5.0),
                          documento([indicador("eje1.x", 1, "bien", "menor_mejor")], 8.0))
        self.assertEqual(r["global"]["sentido"], "EMPEORA")
        self.assertEqual(r["global"]["diferencia"], -3.0)

    def test_declara_si_se_comparo_el_mismo_conjunto_de_paginas(self):
        """15 páginas contra 47 no es el mismo sitio medido dos veces.

        Sin esta declaración, cualquier conteo que baje parece una mejora cuando lo único
        que pasó es que se miraron menos páginas.
        """
        r = self.comparar(documento([indicador("eje1.x", 1, "bien", "menor_mejor")], paginas=15),
                          documento([indicador("eje1.x", 2, "mal", "menor_mejor")], paginas=47))
        self.assertIn("paginas", r)
        self.assertIs((r["paginas"] or {}).get("mismo_conjunto"), False,
                      "comparó 15 páginas contra 47 sin declarar que no es el mismo conjunto")


class TestEsDeterminista(_ConComparar):
    """La misma entrada dos veces tiene que dar la misma salida.

    Suena obvio y no lo es: basta una fecha, un recorrido de objeto sin orden o un redondeo
    dependiente del entorno para que dos corridas del mismo JSON difieran, y entonces el
    delta que se le enseña al cliente depende de cuándo se corrió.
    """

    def test_dos_corridas_del_mismo_json_dan_lo_mismo(self):
        n = documento([indicador("eje1.a", 2.0, "bien", "menor_mejor"),
                       indicador("eje1.b", NO_MEDIDO, "no_medido"),
                       indicador("eje1.c", 40, "mejorar", "mayor_mejor")], 7.5)
        v = documento([indicador("eje1.a", 5.0, "mal", "menor_mejor"),
                       indicador("eje1.b", 1, "bien"),
                       indicador("eje1.c", 90, "bien", "mayor_mejor")], 6.0)
        uno, dos = self.comparar(n, v), self.comparar(n, v)
        self.assertEqual(json.dumps(uno, sort_keys=True), json.dumps(dos, sort_keys=True))


# ── puntuar.mjs ──────────────────────────────────────────────────────────────

PESOS = {"eje1": 0.25, "eje2": 0.15, "eje3": 0.15, "eje4": 0.15, "eje5": 0.20, "eje6": 0.10}
PUNTOS = {"bien": 10, "mejorar": 5, "mal": 0}


def doc_multieje(por_eje):
    """`por_eje`: {'eje1': ['bien', 'mal', 'no_medido'], ...} — el estado de cada indicador."""
    ejes = {}
    for eid, estados in por_eje.items():
        inds = {}
        for n, est in enumerate(estados):
            val = NO_MEDIDO if est == "no_medido" else n
            i = indicador("%s.i%d" % (eid, n), val, est)
            if est == "no_medido":
                i["motivo_no_medido"] = "no se pudo medir en la prueba"
            inds[i["id"]] = i
        ejes[eid] = {"id": eid, "nombre": eid, "hallazgos": [], "indicadores": inds}
    return {
        "sitio": "x.example", "url": "https://x.example/",
        "fecha_iso": "2026-08-26T00:00:00-04:00", "version_esquema": "1.0",
        "paginas_auditadas": [{"url": "https://x.example/"}],
        "cobertura_paginas": {"auditadas": 1, "descubiertas": 1, "muestra_parcial": False},
        "ejes": ejes,
    }


@unittest.skipUnless(HAY_MOTOR, "la skill site-audit no está en esta máquina")
class TestPuntuar(unittest.TestCase):
    """El número que el cliente ve en la portada.

    Las tres reglas están escritas en la cabecera de puntuar.mjs. Nunca habían tenido una
    prueba, y son de las que se rompen sin hacer ruido: un NO_MEDIDO que empezara a contar
    como cero bajaría todos los puntajes de golpe y el informe seguiría pareciendo normal.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def puntuar(self, doc, pesos=None):
        ruta = self.tmp / "resultados.json"
        ruta.write_text(json.dumps(doc), encoding="utf-8")
        cmd = ["node", str(PUNTUAR), str(ruta)]
        if pesos is not None:
            pr = self.tmp / "pesos.json"
            pr.write_text(json.dumps(pesos), encoding="utf-8")
            cmd += ["--pesos", str(pr)]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=90, env=ENTORNO)
        return p, (json.loads(ruta.read_text(encoding="utf-8")) if p.returncode == 0 else None)

    def test_un_no_medido_no_cuenta_como_cero(self):
        """La regla: los NO_MEDIDO salen del denominador, no puntúan NI penalizan.

        Dos ejes con los MISMOS indicadores medidos; el segundo trae además uno que no se
        pudo medir. Si ese contara como cero, el segundo puntuaría más bajo — y el informe
        estaría castigando al sitio por un hueco de la herramienta.
        """
        _p, a = self.puntuar(doc_multieje({e: ["bien", "bien"] for e in PESOS}))
        _q, b = self.puntuar(doc_multieje(
            {e: (["bien", "bien", "no_medido"] if e == "eje1" else ["bien", "bien"])
             for e in PESOS}))
        self.assertIsNotNone(a); self.assertIsNotNone(b)
        self.assertEqual(a["ejes"]["eje1"]["puntaje"], b["ejes"]["eje1"]["puntaje"],
                         "un indicador NO_MEDIDO cambió el puntaje del eje: está contando")

    def test_bajo_el_60_por_ciento_medido_el_eje_no_pone_nota(self):
        """Media verdad, cero nota: con 1 de 4 medido no se emite puntaje, se emite NO_MEDIDO."""
        _p, d = self.puntuar(doc_multieje(
            {e: (["bien", "no_medido", "no_medido", "no_medido"] if e == "eje1"
                 else ["bien", "bien"]) for e in PESOS}))
        self.assertEqual(d["ejes"]["eje1"]["puntaje"], NO_MEDIDO,
                         "puntuó un eje del que solo pudo medir 1 de 4 indicadores")

    def test_todo_bien_da_diez(self):
        _p, d = self.puntuar(doc_multieje({e: ["bien", "bien"] for e in PESOS}))
        self.assertEqual(d["puntaje_global"], 10)

    def test_todo_mal_da_cero_y_eso_si_es_una_afirmacion(self):
        """Cero acá SÍ significa algo: se midió y salió mal. Es lo contrario de NO_MEDIDO."""
        _p, d = self.puntuar(doc_multieje({e: ["mal", "mal"] for e in PESOS}))
        self.assertEqual(d["puntaje_global"], 0)

    def test_el_global_es_la_suma_ponderada_de_los_ejes(self):
        """Si el informe publica la fórmula y los pesos, la fórmula tiene que dar su número.

        GRIETA CONOCIDA, medida el 26-ago-2026 y NO arreglada a propósito.

        Con los ejes en 10/0/5/10/0/10 —enteros exactos, sin redondeo de por medio— la
        suma ponderada da 5,7500 clavado. El motor publica 5,7. No es doble redondeo:
        es acumulación en coma flotante (0.15 * 5 = 0.7499999…), la suma aterriza en
        5,749999… y cae para abajo. Quien recalcule a mano desde el anexo obtiene 5,8.

        Por qué no se arregló acá: cambiar el redondeo mueve TODOS los puntajes futuros
        hasta 0,1, y eso mete un delta falso contra cada auditoría ya entregada. Es una
        decisión de producto, no una corrección de código, y le toca a Daniel.

        La tolerancia de 0,1 es exactamente el tamaño de la grieta. Si algún día se agranda,
        esta prueba lo dice.
        """
        _p, d = self.puntuar(doc_multieje(
            {"eje1": ["bien", "bien"], "eje2": ["mal", "mal"], "eje3": ["mejorar", "mejorar"],
             "eje4": ["bien", "bien"], "eje5": ["mal", "mal"], "eje6": ["bien", "bien"]}))
        esperado = sum(d["ejes"][e]["puntaje"] * PESOS[e] for e in PESOS)
        self.assertAlmostEqual(d["puntaje_global"], esperado, delta=0.1,
                               msg="el global se despegó de la suma de los pesos publicados")

    def test_publica_las_ponderaciones_que_uso(self):
        """Sin los pesos publicados, el puntaje no se puede recalcular ni discutir."""
        _p, d = self.puntuar(doc_multieje({e: ["bien"] for e in PESOS}))
        self.assertEqual(d.get("ponderaciones"), PESOS)

    def test_rechaza_ponderaciones_que_no_suman_uno(self):
        """Con pesos que no suman 1 el 'sobre 10' deja de significar sobre 10."""
        p, _d = self.puntuar(doc_multieje({e: ["bien"] for e in PESOS}),
                             pesos=dict(PESOS, eje1=0.50))
        self.assertNotEqual(p.returncode, 0, "aceptó ponderaciones que suman 1.25")
        self.assertIn("suman", (p.stderr or "").lower())

    def test_es_determinista(self):
        entrada = doc_multieje({"eje1": ["bien", "no_medido"], "eje2": ["mal", "mejorar"],
                                "eje3": ["bien"], "eje4": ["mejorar"], "eje5": ["mal"],
                                "eje6": ["bien"]})
        _a, uno = self.puntuar(entrada)
        _b, dos = self.puntuar(entrada)
        self.assertEqual(uno["puntaje_global"], dos["puntaje_global"])
        self.assertEqual({e: v["puntaje"] for e, v in uno["ejes"].items()},
                         {e: v["puntaje"] for e, v in dos["ejes"].items()})


if __name__ == "__main__":
    unittest.main()
