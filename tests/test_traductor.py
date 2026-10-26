"""Pruebas del traductor al español.

La mayoría usa un motor simulado (sin modelos). Las marcadas con `modelo_real` usan el modelo instalado
en modelos/ y se saltan si no está (uv run radar-traducir --descargar).
"""

import io
import json
import zipfile
from datetime import date

import pytest

from radar import db, menciones, panel, ranking, traductor

AJUSTES = traductor.cargar_ajustes()
REAL = traductor.MotorLocal(AJUSTES)
modelo_real = pytest.mark.skipif(not REAL.instalado("en_es"), reason="modelo en_es no instalado")


class MotorFalso:
    """Traduce con un diccionario de frases; lo que no conoce lo copia (como hace el modelo real)."""

    nombre = traductor.MOTOR_LOCAL

    def __init__(self, frases: dict[str, str], instalados=("en_es", "zh_en")):
        self.frases = {k.lower(): v for k, v in frases.items()}
        self.instalados = set(instalados)
        self.llamadas: list[tuple[str, list[str]]] = []

    def ruta(self, idioma):
        pares = ["en_es"] if idioma == "en" else [f"{idioma}_en", "en_es"]
        return pares if all(p in self.instalados for p in pares) else None

    def traducir(self, frases, par, beam):
        self.llamadas.append((par, frases))
        return [self.frases.get(f.lower().rstrip("."), f) for f in frases]


@pytest.fixture
def con(tmp_path):
    c = db.conectar(tmp_path / "radar.db")
    db.aplicar_esquema(c)
    yield c
    c.close()


# ---------- Detección de idioma ----------

@pytest.mark.parametrize(
    "texto, idioma",
    [
        ("A fast web framework for building APIs with Python", "en"),
        ("Un marco web rápido para construir APIs con Python", "es"),
        ("Herramienta de línea de comandos para gestionar contraseñas", "es"),
        ("中英文敏感词、语言检测", "zh"),
        ("歌詞から文字PVを自動で組み立てるブラウザアプリ", "ja"),
        ("AI 자율 침투 테스트 프레임워크", "ko"),
        ("Библиотека для машинного обучения", "ru"),
        ("🚀🔥", "es"),  # sin letras: nada que traducir
        ("PHP Interpreter", "en"),
    ],
)
def test_detectar_idioma(texto, idioma):
    assert traductor.detectar_idioma(texto) == idioma


# ---------- Piezas del traductor ----------

def test_limpiar_quita_marcas_del_tokenizador():
    assert traductor.limpiar("▁Hola▁mundo ⁇ ,▁bien") == "Hola mundo, bien"


def test_enmascarar_y_restaurar_respeta_la_escritura():
    texto, mapa = traductor.enmascarar("Scrapy is great. scrapy rocks with Python", ["Scrapy", "Python"])
    assert "Scrapy" not in texto and "Python" not in texto
    vuelta, ok = traductor.restaurar(texto.lower(), mapa)  # el modelo pudo pasar los marcadores a minúsculas
    assert ok and vuelta.startswith("Scrapy") and "scrapy rocks" in vuelta and vuelta.endswith("Python")


def test_restaurar_detecta_marcador_perdido():
    _, mapa = traductor.enmascarar("Scrapy for Python", ["Scrapy", "Python"])
    _, ok = traductor.restaurar("algo sin marcadores", mapa)
    assert not ok


def test_glosario_y_correcciones():
    salida = traductor.aplicar_glosario("Escrito en Pitón y tiposcript para vue", ["Vue", "TypeScript"], AJUSTES["correcciones"])
    assert salida == "Escrito en Python y TypeScript para Vue"


def test_recuperar_mayusculas():
    assert traductor.recuperar_mayusculas("deepseek abre su motor", "DeepSeek open sources") == "DeepSeek abre su motor"


@pytest.mark.parametrize(
    "origen, traducido, ok",
    [
        ("A fast web framework", "Un marco web rápido", True),
        ("A fast web framework", "", False),
        ("A fast web framework for Python", "A fast web framework for Python", False),  # copia
        ("Tool to manage the files of your project", "Tool to manage the files of your project", False),
        ("A fast web framework for building things", "Un ⁇ marco", False),
        ("A fast web framework for building great things today", "Marco", False),  # demasiado corto
    ],
)
def test_valida(origen, traducido, ok):
    assert traductor.valida(origen, traducido) is ok


def test_solo_nombres_no_se_traduce():
    assert traductor.solo_nombres("Nano vLLM", [])
    assert traductor.solo_nombres("Visual Studio Code", AJUSTES["glosario"])
    assert traductor.solo_nombres("CVE-2026-1 CVE-2026-2", [])
    assert not traductor.solo_nombres("Media manager for your files", [])


# ---------- Traducción completa (motor simulado) ----------

def test_protege_urls_emojis_y_codigo():
    motor = MotorFalso({"a tool for n1": "una herramienta para N1", "see": "ver"})
    salida = traductor.traducir_texto(
        motor, "🔥 A tool for Python. See https://github.com/a/b `pip install x`", "en", 2, 600, ["Python"]
    )
    assert salida.startswith("🔥 una herramienta para Python") or salida.startswith("🔥 Una herramienta para Python")
    assert "https://github.com/a/b" in salida and "`pip install x`" in salida
    traducidas = [f for _, frases in motor.llamadas for f in frases]
    assert not any("https://" in f or "pip install" in f or "🔥" in f for f in traducidas)


def test_protege_links_sin_https_y_hashtags():
    motor = MotorFalso({"look at": "mira"})
    salida = traductor.traducir_texto(motor, "Look at github.com/php/php-src/commit/1 #opensource", "en", 2, 600, ["GitHub", "PHP"])
    assert "github.com/php/php-src/commit/1" in salida and "#opensource" in salida


def test_elige_la_variante_que_si_traduce():
    # Como el modelo real: copia el título tal cual y solo lo traduce enmarcado en "Description: …".
    motor = MotorFalso({"description: free and open source search engine": "Descripción: Motor de búsqueda libre"})
    salida = traductor.traducir_texto(motor, "Free and Open Source Search Engine", "en", 2, 600)
    assert salida == "Motor de búsqueda libre"


def test_nombre_del_repo_no_se_traduce():
    motor = MotorFalso({"n1, a fast scraping framework": "N1, un marco rápido de raspado"})
    salida = traductor.traducir_texto(motor, "Scrapy, a fast scraping framework", "en", 2, 600, [], {}, ["scrapy", "scrapy"])
    assert salida == "Scrapy, un marco rápido de raspado"


def test_chino_pasa_por_ingles():
    motor = MotorFalso({"深度学习笔记": "deep learning notes", "deep learning notes": "notas de aprendizaje profundo"})
    assert traductor.traducir_texto(motor, "深度学习笔记", "zh", 2, 600) == "notas de aprendizaje profundo"
    assert [par for par, _ in motor.llamadas] == ["zh_en", "en_es"]


def test_sin_modelo_devuelve_none():
    motor = MotorFalso({}, instalados=("en_es",))
    assert traductor.traducir_texto(motor, "歌詞から文字", "ja", 2, 600) is None


def test_si_falla_la_verificacion_devuelve_none():
    motor = MotorFalso({})  # copia todo
    assert traductor.traducir_texto(motor, "Tool to manage the files of your project", "en", 2, 600) is None


# ---------- Corrida sobre la base ----------

def repo(n, nombre, desc):
    return ranking.normalizar({
        "id": n, "full_name": nombre, "html_url": f"https://github.com/{nombre}", "description": desc,
        "language": "Python", "topics": [], "stargazers_count": 10, "forks_count": 1, "fork": False,
        "archived": False, "created_at": "2026-10-01T00:00:00Z", "pushed_at": "2026-10-20T00:00:00Z",
    })


FRASES = {
    "a tool for your files": "Una herramienta para tus archivos",
    "show hn: my tool": "Muestra HN: mi herramienta",
}


def poblar(con):
    ranking.guardar_repos(con, [
        repo(1, "a/uno", "A tool for your files"),
        repo(2, "b/dos", "Herramienta de línea de comandos para tus archivos"),
        repo(3, "c/tres", "歌詞から文字PVを自動で"),
        repo(4, "d/cuatro", None),
    ], date(2026, 10, 22))
    menciones.registrar(con, [menciones.Mencion("a/uno", "hackernews", "https://news.ycombinator.com/item?id=1", texto="Show HN: My tool")])


def test_traducir_pendientes_y_no_repetir(con):
    poblar(con)
    motor = MotorFalso(FRASES)
    inf = traductor.traducir_pendientes(con, motor, AJUSTES)
    assert inf.ok, inf.problemas
    assert (inf.pendientes, inf.traducidos, inf.ya_en_espanol, inf.sin_modelo) == (4, 2, 1, {"ja": 1})
    llamadas = len(motor.llamadas)
    again = traductor.traducir_pendientes(con, motor, AJUSTES)
    assert again.pendientes == 0 and len(motor.llamadas) == llamadas  # nada se traduce dos veces


def test_si_cambia_la_descripcion_se_traduce_de_nuevo(con):
    poblar(con)
    motor = MotorFalso(FRASES)
    traductor.traducir_pendientes(con, motor, AJUSTES)
    con.execute("UPDATE repos SET descripcion_gh = 'Show HN: My tool' WHERE id = 2")  # ya traducido por la mención
    con.execute("UPDATE repos SET descripcion_gh = 'A tool for your files and more' WHERE id = 1")
    assert [t for t, _ in traductor.textos_a_traducir(con)] == ["A tool for your files and more"]


def test_reintentar_incluye_los_fallidos(con):
    poblar(con)
    traductor.traducir_pendientes(con, MotorFalso(FRASES, instalados=("en_es",)), AJUSTES)
    assert traductor.textos_a_traducir(con) == []
    assert [t for t, _ in traductor.textos_a_traducir(con, reintentar=True)] == ["歌詞から文字PVを自動で"]


def test_panel_solo_muestra_espanol(con):
    poblar(con)
    sin_traducir = panel.exportar(con)
    assert any("no pasaron por el traductor" in p for p in panel.validar(sin_traducir))

    traductor.traducir_pendientes(con, MotorFalso(FRASES), AJUSTES)
    datos = panel.exportar(con)
    assert panel.validar(datos) == []
    por_id = {r["id"]: r for r in datos["repos"]}
    assert por_id[1]["descripcion"] == "Una herramienta para tus archivos"
    assert por_id[2]["descripcion"] == "Herramienta de línea de comandos para tus archivos"  # ya era español
    assert por_id[3]["descripcion"] is None and por_id[3]["descripcion_oculta"] is True  # japonés sin modelo
    assert por_id[4]["descripcion"] is None and por_id[4]["descripcion_oculta"] is False  # sin descripción
    assert por_id[1]["social"]["hackernews"][0]["texto"] == "Muestra HN: mi herramienta"


# ---------- Instalación de modelos ----------

def paquete(meta: dict, archivos: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("pkg/metadata.json", json.dumps(meta))
        for nombre, contenido in archivos.items():
            z.writestr(nombre, contenido)
    return buf.getvalue()


def test_instalar_paquete_valido(tmp_path):
    datos = paquete({"from_code": "en", "to_code": "es"},
                    {"pkg/model/model.bin": b"x", "pkg/sentencepiece.model": b"y", "pkg/stanza/otro.bin": b"z"})
    traductor.instalar_paquete(datos, "en_es", tmp_path)
    assert (tmp_path / "en_es" / "model" / "model.bin").exists()
    assert not (tmp_path / "en_es" / "stanza").exists()  # solo lo necesario


def test_instalar_rechaza_idioma_equivocado(tmp_path):
    datos = paquete({"from_code": "fr", "to_code": "en"}, {"pkg/model/model.bin": b"x", "pkg/sentencepiece.model": b"y"})
    with pytest.raises(ValueError, match="no en→es"):
        traductor.instalar_paquete(datos, "en_es", tmp_path)


def test_instalar_rechaza_rutas_sospechosas(tmp_path):
    datos = paquete({"from_code": "en", "to_code": "es"},
                    {"pkg/model/../../../fuera.bin": b"x", "pkg/sentencepiece.model": b"y"})
    with pytest.raises(ValueError):
        traductor.instalar_paquete(datos, "en_es", tmp_path)
    assert not (tmp_path.parent / "fuera.bin").exists()


# ---------- Con el modelo real ----------

@modelo_real
@pytest.mark.parametrize(
    "texto, nombres, contiene",
    [
        ("Scrapy, a fast high-level web crawling & scraping framework for Python.", ["scrapy"], ["Scrapy", "Python", "marco"]),
        ("JavaScript Style Guide", ["javascript"], ["Guía de estilo", "JavaScript"]),
        ("Written in Rust and TypeScript", [], ["Escrito", "Rust", "TypeScript"]),
        ("This is the repo for Vue 2. For Vue 3, go to https://github.com/vuejs/core", ["vue"], ["Vue 2", "https://github.com/vuejs/core"]),
    ],
)
def test_modelo_real(texto, nombres, contiene):
    salida = traductor.traducir_texto(
        REAL, texto, "en", AJUSTES["beam"], AJUSTES["max_caracteres"], AJUSTES["glosario"], AJUSTES["correcciones"], nombres
    )
    assert salida is not None
    for parte in contiene:
        assert parte in salida, salida
    assert not traductor.sigue_en_ingles(salida)
