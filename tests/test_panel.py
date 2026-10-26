"""Pruebas del exportador del panel (fase 5)."""

import json
from datetime import date

import pytest

from radar import db, menciones, panel, ranking, traductor

HOY = date(2026, 10, 22)


class MotorEco:
    """Motor de traducción falso: antepone 'ES ' para distinguir lo traducido."""

    nombre = traductor.MOTOR_LOCAL

    def ruta(self, idioma):
        return ["en_es"]

    def traducir(self, frases, par, beam):
        return [f"ES {f}" for f in frases]


@pytest.fixture
def con(tmp_path):
    c = db.conectar(tmp_path / "radar.db")
    db.aplicar_esquema(c)
    yield c
    c.close()


def repo(n: int, nombre: str, topics=(), desc="d", estrellas=100) -> dict:
    return ranking.normalizar({
        "id": n, "full_name": nombre, "html_url": f"https://github.com/{nombre}", "description": desc,
        "language": "Python", "topics": list(topics), "stargazers_count": estrellas, "forks_count": 1,
        "fork": False, "archived": False, "created_at": "2026-10-01T00:00:00Z", "pushed_at": "2026-10-20T00:00:00Z",
    })


def poblar(con):
    ranking.guardar(con, [repo(1, "a/agente", ["ai-agents", "mcp"], estrellas=500),
                          repo(2, "b/notas", ["notes", "markdown"])], ranking.Consulta("top", "q"), HOY)
    menciones.registrar(con, [
        menciones.Mencion("a/agente", "hackernews", "https://news.ycombinator.com/item?id=1", texto="Show HN", interaccion=300),
        menciones.Mencion("a/agente", "hackernews", "https://news.ycombinator.com/item?id=2", interaccion=10),
        menciones.Mencion("a/agente", "reddit", "https://www.reddit.com/r/x/comments/1", interaccion=5),
    ])
    con.execute(
        "INSERT INTO tendencias (repo_id, fecha, delta_dia, nivel, confianza, dias_historial, en_trending) "
        "VALUES (1, ?, 250, 'en_alza', 'baja', 2, 1)", (HOY.isoformat(),),
    )
    con.commit()
    traductor.traducir_pendientes(con, MotorEco(), traductor.cargar_ajustes())


@pytest.mark.parametrize(
    "topics, nombre, desc, esperado",
    [
        (["ai-agents", "mcp"], "x/y", "", "agentes"),
        ([], "x/llm-server", "Fast inference for LLM models", "ia-llm"),
        (["react", "ui", "components"], "x/y", "", "frontend"),
        (["awesome", "awesome-list"], "x/awesome-vue", "A curated list", "educacion"),
        (["security", "pentest"], "x/y", "", "seguridad"),
        ([], "x/y", "nada que ver", "otros"),
        # Los topics pesan más que el texto libre.
        (["database", "sql"], "x/y", "a web ui for your data", "datos"),
    ],
)
def test_categoria_provisional(topics, nombre, desc, esperado):
    assert panel.categoria_provisional(topics, nombre, desc) == esperado


def test_marca_contenido_sensible():
    assert panel.es_sensible([], "x/nsfw-gen", "")
    assert panel.es_sensible(["porn"], "x/y", "")
    assert not panel.es_sensible(["llm"], "x/sussex-tools", "Essex expressions")  # sin falsos positivos por subcadena


def test_exportar_arma_cada_repo(con):
    poblar(con)
    datos = panel.exportar(con)
    assert panel.validar(datos) == []
    a = next(r for r in datos["repos"] if r["nombre"] == "a/agente")
    assert a["url"] == "https://github.com/a/agente"
    assert a["categoria"] == "agentes" and a["categoria_ia"] is False and a["ejemplos"] == []
    assert a["nivel"] == "en_alza" and a["delta_dia"] == 250 and a["en_trending"] is True
    assert a["ranking"] == ["top"]
    assert [m["interaccion"] for m in a["social"]["hackernews"]] == [300, 10]  # más interacción primero
    assert a["total_social"] == 3
    assert a["descripcion"] == "ES d"  # la descripción llega traducida
    assert a["social"]["hackernews"][0]["texto"].startswith("ES ")
    b = next(r for r in datos["repos"] if r["nombre"] == "b/notas")
    assert b["nivel"] == "sin_datos" and b["categoria"] == "productividad"
    assert datos["fecha_tendencia"] == HOY.isoformat()
    assert sum(c["total"] for c in datos["categorias"]) == 2


def test_enriquecimiento_de_ia_reemplaza_lo_provisional(con):
    poblar(con)
    con.execute(
        "INSERT INTO enriquecimiento (repo_id, categoria, descripcion_uso, ejemplo_1, ejemplo_2, modelo, generado) "
        "VALUES (2, 'devtools', 'Sirve para X', 'Ejemplo uno', 'Ejemplo dos', 'claude-haiku-5-5', 'x')"
    )
    b = next(r for r in panel.exportar(con)["repos"] if r["id"] == 2)
    assert (b["categoria"], b["categoria_ia"], b["uso"]) == ("devtools", True, "Sirve para X")
    assert b["ejemplos"] == ["Ejemplo uno", "Ejemplo dos"]


def test_validar_detecta_problemas(con):
    poblar(con)
    datos = panel.exportar(con)
    datos["repos"][0]["url"] = "javascript:alert(1)"
    datos["repos"][1]["nivel"] = "inventado"
    problemas = " | ".join(panel.validar(datos))
    assert "URL inválida" in problemas and "Nivel desconocido" in problemas


def test_validar_base_vacia(con):
    assert "radar-ranking" in panel.validar(panel.exportar(con))[0]


def test_escribir_no_permite_cerrar_el_script(con, tmp_path):
    poblar(con)
    datos = panel.exportar(con)
    datos["repos"][0]["descripcion"] = "</script><script>alert(1)</script>"
    destino = panel.escribir(datos, tmp_path)
    texto = destino.read_text(encoding="utf-8")
    assert "</script>" not in texto
    assert texto.startswith("window.RADAR = ") and texto.rstrip().endswith(";")
    recuperado = json.loads(texto[len("window.RADAR = "):].rstrip().rstrip(";"))
    assert recuperado["repos"][0]["descripcion"] == "</script><script>alert(1)</script>"
