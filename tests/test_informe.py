"""Arnés de regresión del informe: que lo arreglado no se vuelva a romper en silencio.

POR QUÉ NO SE COMPARA EL HTML BYTE A BYTE
─────────────────────────────────────────
Un informe lleva horas, rutas de evidencia y versiones de herramientas leídas en
ejecución. Comparar contra un archivo congelado rompería en cada corrida por razones
falsas, y una prueba que falla siempre se termina ignorando — que es peor que no tenerla.

Lo que se congela son las PROPIEDADES: cada defecto que se arregló el 24 y 25 de agosto,
convertido en una afirmación que el informe tiene que cumplir siempre. Si alguien revierte
un arreglo, o si Lighthouse cambia la forma de su JSON, esto se pone rojo.

El fixture es sintético a propósito: un `resultados.json` de cliente no va al repositorio.
Está construido para disparar todos los casos — muestra parcial, NO_MEDIDO con motivo, un
eje sin lectura comercial, unidades meta, textos sin tilde y menciones a la agencia externa.
"""

import html as html_mod
import json
import re
import shutil
import subprocess
import tempfile
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# La ruta a la skill se importa, no se vuelve a escribir. Este arnés se auto-saltea si
# no encuentra informe.mjs, así que una copia desincronizada no falla: se APAGA, y las
# pruebas de regresión del entregable pasan a ser cero cobertura con la suite en verde.
from vitrina.cola import INFORME  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "resultados-oro.json"


def _sin_base64(html):
    """Las imágenes van incrustadas como data: y su base64 contiene cualquier cosa.

    Sin quitarlas, buscar "HOW2" da falsos positivos: una captura real traía esa
    secuencia dentro del base64 y parecía una mención que no existía.
    """
    return re.sub(r"data:[^\"')]{200,}", "", html)


def _texto_visible(html):
    """Sólo lo que un lector ve en la lámina.

    Quitar las etiquetas no alcanza: deja dentro el CSS, los scripts y los comentarios
    del HTML, que sí llevan palabras sin tilde ("Capa 2 (presentacion)..."). Sin esto la
    prueba de acentos falla por texto que ningún cliente lee jamás — pasó al escribirla.
    """
    t = _sin_base64(html)
    t = re.sub(r"<!--.*?-->", " ", t, flags=re.S)
    t = re.sub(r"<style\b[^>]*>.*?</style>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<script\b[^>]*>.*?</script>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    # DESESCAPAR, o el barrido busca lo que no puede encontrar.
    #
    # En el HTML una comilla va como &quot; y un & como &amp;. Buscar `{"` sobre el HTML
    # crudo devuelve CERO aunque el JSON esté ahí en la cara del cliente — un cero que no
    # significa nada, y peor que no buscar, porque da por limpio lo que no lo está.
    #
    # Afectaba a las 18 pruebas de este arnés, no solo a la del JSON: cualquiera que
    # busque un texto con comillas, comillas simples, & o < estaba mirando otra cosa.
    return html_mod.unescape(t)


@unittest.skipUnless(INFORME.is_file(), "la skill site-audit no está en esta máquina")
class TestInformeOro(unittest.TestCase):
    """Genera el informe una vez y afirma sobre él."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        shutil.copy(FIXTURE, cls.tmp / "resultados.json")
        r = subprocess.run(
            ["node", str(INFORME), str(cls.tmp / "resultados.json"),
             "--salida", str(cls.tmp), "--sin-pdf", "--sin-capturas"],
            capture_output=True, text=True, timeout=180,
        )
        cls.salida_cmd = r.stdout + r.stderr
        cls.codigo = r.returncode
        html = cls.tmp / "informe.html"
        # Una sola lectura: el archivo lleva las capturas incrustadas en base64 y pesa
        # megas, y con dos expresiones separadas cualquiera puede cambiar el encoding en
        # una y no en la otra.
        crudo = html.read_text(encoding="utf-8") if html.is_file() else ""
        cls.html = _sin_base64(crudo)
        cls.texto = _texto_visible(crudo)
        mds = sorted(cls.tmp.glob("AUDITORIA-*.md"))
        cls.md = mds[0].read_text(encoding="utf-8") if mds else ""

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_genera_sin_error(self):
        self.assertEqual(self.codigo, 0, self.salida_cmd[-600:])
        self.assertTrue(self.html, "no se generó informe.html")
        self.assertTrue(self.md, "no se generó el markdown")

    def test_ninguna_lamina_desborda(self):
        # El propio generador comprueba el encaje y lo dice por salida.
        self.assertNotIn("problema de encaje", self.salida_cmd)

    # ── Los nueve defectos, uno por prueba ────────────────────────────────

    def test_no_nombra_a_la_agencia_externa(self):
        """El fixture SÍ la menciona en fuentes y método: el filtro tiene que quitarla."""
        self.assertIn("HOW2", FIXTURE.read_text(encoding="utf-8"), "el fixture perdió el caso")
        self.assertEqual(self.html.count("HOW2"), 0, "la agencia externa llegó al HTML")
        self.assertEqual(self.md.count("HOW2"), 0, "la agencia externa llegó al markdown")

    def test_no_imprime_identificadores_del_motor(self):
        """'eje1.lcp_movil · valor' bajo cada cifra es jerga en la cara del cliente."""
        fugas = re.findall(r"eje\d\.[a-z_0-9]+\s*·\s*valor", self.texto)
        self.assertEqual(fugas, [], "identificadores internos visibles: %s" % fugas[:3])

    def test_no_imprime_json_crudo(self):
        """Un valor compuesto tiene que leerse, no serializarse.

        Los medidores guardan valores como {webp_avif:0, total:50} porque el dato lo
        necesita entero. Cuando eso entra en el TEXTO de una lámina, `JSON.stringify` no
        comunica nada: el 26-ago-2026 un informe entregado a un cliente traía literalmente
        {"nombre":"jQuery","version":"3.7.1"… en mitad de una cifra.

        Se pasó por alto tres veces porque el barrido buscaba `{"` sobre el HTML, donde
        las comillas van como &quot;. Por eso `_texto_visible` desescapa: si no, esta
        prueba pasaría siempre sin mirar nada.
        """
        for nombre, cuerpo in (("informe", self.texto), ("markdown", self.md)):
            fugas = re.findall(r'\{\s*"[a-z_]+"\s*:.{0,40}', cuerpo)
            self.assertEqual(fugas, [], "%s: JSON crudo en la cara del cliente: %s"
                             % (nombre, fugas[:2]))

    def test_no_se_limpio_de_mas(self):
        """La otra dirección del mismo arreglo, y la que nadie mira.

        El 25-ago se sacaron las rutas absolutas de la máquina del anexo. El riesgo de un
        arreglo así es pasarse: si además desaparece el archivo de evidencia de cada dato,
        el informe deja de ser verificable — y eso rompe la promesa central de la
        herramienta igual que una fuga, solo que en silencio y sin que ninguna prueba de
        limpieza se entere, porque están todas buscando lo que SOBRA.

        Nadie recalcula un informe que no puede auditar. Si esto se pone rojo, el problema
        no es esta prueba.
        """
        with open(FIXTURE, "r", encoding="utf-8") as fh:
            datos = json.load(fh)
        esperados = set()
        for eje in (datos.get("ejes") or {}).values():
            for ind in (eje.get("indicadores") or {}).values():
                fuente = ind.get("fuente_archivo")
                if ind.get("estado") not in (None, "no_medido") and fuente:
                    esperados.add(fuente)
        self.assertTrue(esperados, "el fixture no declara ni un fuente_archivo: esta prueba "
                                   "no estaría comprobando nada")
        faltan = sorted(f for f in esperados if f not in self.md)
        self.assertEqual(faltan, [], "el anexo dejó de citar %d de %d archivos de evidencia. "
                         "Se limpió de más y esos datos ya no se pueden verificar: %s"
                         % (len(faltan), len(esperados), faltan[:4]))

    def test_no_imprime_rutas_de_evidencia_en_las_laminas_de_eje(self):
        """Las rutas van en el anexo, no bajo cada hallazgo."""
        # El anexo sí las lleva; se comprueba que no aparezcan pegadas a un hallazgo.
        self.assertNotRegex(self.texto, r"CR[IÍ]TICO\s+\S*eje\d-evidencia/")

    def test_no_hay_caja_vacia_de_lectura_comercial(self):
        """Sólo eje5 trae lectura en el fixture: los otros cinco no deben anunciar el hueco."""
        self.assertNotIn("no emitió lectura comercial", self.html)
        self.assertNotIn("no emitió lectura comercial", self.md)

    def test_no_corta_frases_a_media_palabra(self):
        """recortar() debe cortar en oración, cláusula o al menos palabra completa."""
        malos = re.findall(r"[a-záéíóúñ]{2,}…\s*[a-záéíóúñ]", self.texto)
        self.assertEqual(malos, [], "cortes a media palabra: %s" % malos[:3])

    def test_pone_las_tildes(self):
        """Los medidores escriben sin acentos; la capa de presentación los repone.

        Con LÍMITE DE PALABRA, no subcadena: "mediciones" en plural va sin tilde y es
        correcta, pero contiene "medicion". Buscar la subcadena marcaba como defecto una
        palabra bien escrita — pasó al escribir esta prueba.
        """
        for palabra in ("Clasificacion", "Emulacion", "medicion", "OPINION",
                        "Navegacion", "movil", "paginas",
                        # nombres de eje: salen en el titulo y el pie de cada lamina
                        "Tecnologia", "librerias", "tecnico"):
            self.assertNotRegex(self.texto, r"\b%s\b" % palabra,
                                "quedó sin tilde: %s" % palabra)
        self.assertIn("Clasificación", self.texto + self.md)
        self.assertIn("mediciones", self.texto.lower(),
                      "el plural correcto se perdió: no hay que 'arreglar' lo que está bien")

    def test_no_acentua_identificadores_ni_rutas(self):
        """El arreglo de tildes NO debe tocar ids ni nombres de archivo."""
        self.assertIn("eje1.lcp_movil", self.html, "se acentuó un identificador")
        self.assertNotIn("eje1.lcp_móvil", self.html)

    def test_no_imprime_unidades_meta(self):
        """'C letra' no significa nada: 'letra' es una unidad del esquema, no del mundo."""
        self.assertNotRegex(self.texto, r"\bC\s+letra\b")

    def test_el_esfuerzo_nunca_es_un_numero(self):
        """La guardia del 22-ago, verificada sobre el producto terminado."""
        self.assertNotRegex(self.texto, r"≈\s*\d+\s*h\b")

    def test_no_dice_NO_MEDIDO_con_guion_bajo(self):
        self.assertNotIn("NO_MEDIDO", self.texto)
        self.assertIn("NO MEDIDO", self.texto)

    # ── Lo que SÍ tiene que estar ─────────────────────────────────────────

    def test_declara_la_muestra_parcial(self):
        """El fixture audita 15 de 47: el informe está obligado a decirlo."""
        self.assertIn("parcial", self.texto.lower())
        self.assertIn("15 de 47", self.texto)
        self.assertIn("parcial", self.md.lower())

    def test_trae_el_veredicto(self):
        self.assertIn("veredicto", self.html, "falta el bloque del veredicto")

    def test_explica_los_hallazgos_en_castellano(self):
        """Cada hallazgo abre con qué significa, antes del dato forense."""
        self.assertIn("h-sig", self.html, "ningún hallazgo trae explicación")
        self.assertIn("Core Web Vitals", self.texto)

    def test_nunca_traduce_a_dinero(self):
        """La regla dura de significado.mjs: se explica lo medido, no lo que cuesta."""
        for patron in (r"\$\s*\d", r"\bUSD\b", r"\bd[oó]lares\b",
                       r"te cuesta", r"p[eé]rdida estimada"):
            self.assertNotRegex(self.texto, patron, "el informe habló de dinero: %s" % patron)

    def test_declara_las_mediciones_invalidadas(self):
        """El fixture trae una anulada por la regla dura 1: tiene que verse."""
        self.assertRegex(self.md, r"(?i)(regla dura 1|medici[oó]n(es)? (invalidada|descartada))")

    def test_el_puntaje_global_cuadra_con_los_ejes(self):
        doc = json.loads(FIXTURE.read_text(encoding="utf-8"))
        suma = sum(doc["ejes"][k]["puntaje"] * w for k, w in doc["ponderaciones"].items())
        self.assertAlmostEqual(suma, doc["puntaje_global"], delta=0.06)
        self.assertIn("6.6", self.texto)


if __name__ == "__main__":
    unittest.main()
