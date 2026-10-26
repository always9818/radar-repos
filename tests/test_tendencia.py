"""Pruebas de la fase 2 con datos simulados y un fragmento real de github.com/trending."""

from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest

from radar import db, ranking, tendencia
from radar.github import GitHubClient

HOY = date(2026, 10, 22)
FIXTURE = Path(__file__).parent / "fixtures" / "trending_daily.html"
UMBRALES = tendencia.cargar_umbrales()


def serie(*estrellas: int, hasta: date = HOY) -> list[tuple[date, int]]:
    """Fotos diarias consecutivas que terminan en `hasta`."""
    n = len(estrellas)
    return [(hasta - timedelta(days=n - 1 - i), e) for i, e in enumerate(estrellas)]


def cliente_gh(manejador, token: str = "") -> GitHubClient:
    return GitHubClient(token, httpx.Client(transport=httpx.MockTransport(manejador)), dormir=lambda s: None)


@pytest.fixture
def con(tmp_path):
    c = db.conectar(tmp_path / "radar.db")
    db.aplicar_esquema(c)
    yield c
    c.close()


def repo_api(n: int, estrellas: int = 1000, creado: str = "2024-01-01T00:00:00Z") -> dict:
    return {
        "id": n, "full_name": f"dueno{n}/repo{n}", "html_url": f"https://github.com/dueno{n}/repo{n}",
        "description": "d", "language": "Go", "topics": ["x"], "stargazers_count": estrellas,
        "forks_count": 1, "fork": False, "archived": False, "created_at": creado, "pushed_at": creado,
    }


# ---------- Clasificación (cálculo puro) ----------

def test_detecta_salto_de_500_estrellas_en_un_dia():
    # Venía ganando ~10/día y hoy ganó 500.
    r = tendencia.clasificar(serie(1000, 1010, 1020, 1030, 1040, 1050, 1060, 1560), HOY, UMBRALES)
    assert r["nivel"] == "explosivo"
    assert r["delta_dia"] == 500 and r["base_diaria"] == 10
    assert r["score"] == 50 and r["confianza"] == "alta"


def test_crecimiento_constante_es_estable():
    r = tendencia.clasificar(serie(*range(5000, 5800, 100)), HOY, UMBRALES)
    assert r["nivel"] == "estable" and r["score"] == 1.0


def test_repo_grande_que_duplica_su_ritmo_esta_en_alza():
    r = tendencia.clasificar(serie(10_000, 10_030, 10_060, 10_090, 10_150), HOY, UMBRALES)
    assert r["nivel"] == "en_alza" and r["score"] == 2.0


def test_piso_evita_falsos_explosivos_en_repos_quietos():
    # De 0 a 1 estrella/día: el score es enorme sin piso, pero el delta no llega al mínimo.
    r = tendencia.clasificar(serie(50, 50, 50, 51), HOY, UMBRALES)
    assert r["nivel"] == "estable"


def test_solo_dos_fotos_usa_umbral_sin_historial():
    assert tendencia.clasificar(serie(1000, 1150), HOY, UMBRALES)["nivel"] == "en_alza"
    assert tendencia.clasificar(serie(1000, 1050), HOY, UMBRALES)["nivel"] == "estable"
    assert tendencia.clasificar(serie(1000, 1150), HOY, UMBRALES)["confianza"] == "baja"


def test_una_sola_foto_sin_trending_es_sin_datos():
    assert tendencia.clasificar(serie(1000), HOY, UMBRALES)["nivel"] == "sin_datos"


def test_una_sola_foto_usa_estrellas_de_hoy_de_trending():
    r = tendencia.clasificar(serie(30_000), HOY, UMBRALES, trending_hoy=2012)
    assert r["nivel"] == "en_alza" and r["delta_dia"] == 2012


def test_repo_nuevo_rapido_sin_historial():
    creado = (HOY - timedelta(days=10)).isoformat() + "T00:00:00Z"
    r = tendencia.clasificar(serie(3000), HOY, UMBRALES, creado_gh=creado)
    assert r["nivel"] == "nuevo_rapido"


def test_repo_viejo_no_es_nuevo_rapido():
    r = tendencia.clasificar(serie(90_000), HOY, UMBRALES, creado_gh="2020-01-01T00:00:00Z")
    assert r["nivel"] == "sin_datos"


def test_hueco_de_dias_se_promedia():
    # Sin foto ayer: 300 estrellas en 3 días son 100/día, no 300.
    fotos = [(HOY - timedelta(days=3), 1000), (HOY, 1300)]
    assert tendencia.clasificar(fotos, HOY, UMBRALES)["delta_dia"] == 100


def test_serie_sin_foto_de_hoy_es_error():
    with pytest.raises(ValueError):
        tendencia.clasificar(serie(1, 2, hasta=HOY - timedelta(days=1)), HOY, UMBRALES)


# ---------- GitHub Trending ----------

def test_parsea_fragmento_real_de_trending():
    repos = tendencia.parsear_trending(FIXTURE.read_text(encoding="utf-8"))
    assert [r.full_name for r in repos] == ["cloudflare/security-audit-skill", "vllm-project/vllm", "stablyai/orca"]
    primero = repos[0]
    assert primero.id == 1273427276
    assert primero.estrellas == 37_319 and primero.forks == 2_330 and primero.estrellas_periodo == 2_012
    assert primero.lenguaje == "JavaScript"
    assert primero.descripcion.startswith("A coding-agent skill")
    assert repos[1].id == 599547518  # el botón de Sponsor no confunde el id
    assert tendencia.validar_trending(repos, minimo=3) == []


def test_trending_con_estructura_cambiada_falla_y_reintenta():
    llamadas = []

    def manejador(request):
        llamadas.append(1)
        return httpx.Response(200, text="<html><div class='nuevo-diseno'></div></html>")

    repos, problemas = tendencia.recolectar_trending(cliente_gh(manejador), "daily", minimo=5)
    assert repos == [] and "estructura" in problemas[0] and len(llamadas) == 3


def test_trending_no_envia_el_token():
    vistos = []

    def manejador(request):
        vistos.append(request.headers.get("Authorization"))
        return httpx.Response(200, text=FIXTURE.read_text(encoding="utf-8"))

    tendencia.recolectar_trending(cliente_gh(manejador, token="ghp_secreto"), "daily", minimo=3)
    assert vistos == [None]


def test_guardar_trending_no_pisa_topics_ni_fecha_de_creacion(con):
    ranking.guardar_repos(con, [ranking.normalizar(repo_api(599547518))], HOY - timedelta(days=1))
    repos = tendencia.parsear_trending(FIXTURE.read_text(encoding="utf-8"))
    tendencia.guardar_trending(con, repos, HOY)
    fila = con.execute("SELECT full_name, topics, creado_gh, estrellas FROM repos WHERE id = 599547518").fetchone()
    assert fila["full_name"] == "vllm-project/vllm"
    assert fila["topics"] == '["x"]' and fila["creado_gh"] == "2024-01-01T00:00:00Z"
    assert con.execute("SELECT COUNT(*) FROM menciones WHERE fuente = 'github_trending'").fetchone()[0] == 3


# ---------- Corrida completa ----------

def preparar_historial(con, dias: int = 7):
    """3 repos con 7 días de fotos; el repo 3 explota hoy."""
    for d in range(dias, 0, -1):
        f = HOY - timedelta(days=d)
        ranking.guardar_repos(con, [ranking.normalizar(repo_api(n, 1000 + 10 * (dias - d))) for n in (1, 2, 3)], f)


def test_ejecutar_completo_con_refresco(con):
    preparar_historial(con)
    # Hoy ninguno tiene foto: el refresco por id debe traerlas.
    hoy_api = {1: repo_api(1, 1070), 2: repo_api(2, 1075), 3: repo_api(3, 1700)}

    def manejador(request):
        if request.url.path.startswith("/repositories/"):
            return httpx.Response(200, json=hoy_api[int(request.url.path.rsplit("/", 1)[1])])
        raise AssertionError(request.url)

    inf = tendencia.ejecutar(cliente_gh(manejador, "ghp_x"), con, UMBRALES, HOY, con_trending=False)
    assert inf.ok, inf.problemas
    assert inf.refrescados == 3 and (inf.con_foto, inf.seguidos) == (3, 3) and inf.calculados == 3
    niveles = dict(con.execute("SELECT repo_id, nivel FROM tendencias WHERE fecha = ?", (HOY.isoformat(),)).fetchall())
    assert niveles == {1: "estable", 2: "estable", 3: "explosivo"}
    top = tendencia.destacados(con, HOY, 5)
    assert [r["full_name"] for r in top] == ["dueno3/repo3"]
    assert con.execute("SELECT estado FROM corridas WHERE tarea = 'tendencia'").fetchone()[0] == "ok"


def test_cobertura_baja_con_token_es_falla(con):
    preparar_historial(con)
    # Forzamos 1 refresco para dejar 2 repos sin foto.
    umbrales = {**UMBRALES, "cobertura": {**UMBRALES["cobertura"], "max_refrescos": 1}}

    def manejador(request):
        return httpx.Response(200, json=repo_api(int(request.url.path.rsplit("/", 1)[1]), 2000))

    inf = tendencia.ejecutar(cliente_gh(manejador, "ghp_x"), con, umbrales, HOY, con_trending=False)
    assert not inf.ok and "Cobertura baja: 1/3" in inf.problemas[0]
    assert con.execute("SELECT estado FROM corridas WHERE tarea = 'tendencia'").fetchone()[0] == "error"


def test_sin_token_cobertura_baja_es_solo_aviso(con):
    preparar_historial(con)
    umbrales = {**UMBRALES, "cobertura": {**UMBRALES["cobertura"], "max_refrescos_sin_token": 1}}

    def manejador(request):
        return httpx.Response(200, json=repo_api(int(request.url.path.rsplit("/", 1)[1]), 2000))

    inf = tendencia.ejecutar(cliente_gh(manejador), con, umbrales, HOY, con_trending=False)
    assert inf.ok, inf.problemas
    assert "Cobertura baja: 1/3" in inf.avisos[0] and "normal sin GITHUB_TOKEN" in inf.avisos[0]
    assert inf.calculados == 1  # igual se calculó la tendencia de lo que sí tiene foto
    assert con.execute("SELECT estado FROM corridas WHERE tarea = 'tendencia'").fetchone()[0] == "ok"


@pytest.mark.parametrize("token, ok", [("", True), ("ghp_x", False)])
def test_limite_agotado_al_refrescar(con, token, ok):
    """El caso real del 23 y 25 de octubre: ranking y redes gastan el cupo y el refresco se queda sin peticiones."""
    preparar_historial(con)

    def manejador(request):
        return httpx.Response(403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "99999999999"})

    inf = tendencia.ejecutar(cliente_gh(manejador, token), con, UMBRALES, HOY, con_trending=False)
    assert inf.ok is ok
    mensajes = inf.avisos if ok else inf.problemas
    assert any(m.startswith("Refresco detenido") for m in mensajes)


def test_repo_borrado_deja_de_seguirse(con):
    preparar_historial(con)

    def manejador(request):
        n = int(request.url.path.rsplit("/", 1)[1])
        return httpx.Response(404) if n == 2 else httpx.Response(200, json=repo_api(n, 1100))

    inf = tendencia.ejecutar(cliente_gh(manejador, "ghp_x"), con, UMBRALES, HOY, con_trending=False)
    assert inf.ok, inf.problemas
    assert (inf.con_foto, inf.seguidos) == (2, 2)
    assert con.execute("SELECT archivado FROM repos WHERE id = 2").fetchone()[0] == 1


def test_repo_archivado_con_foto_no_rompe_la_verificacion(con):
    """Caso real del 25-oct: un repo archivado con foto de hoy entra al cálculo pero no a la cobertura."""
    preparar_historial(con)

    def manejador(request):
        n = int(request.url.path.rsplit("/", 1)[1])
        return httpx.Response(200, json=repo_api(n, 1100))

    tendencia.ejecutar(cliente_gh(manejador, "ghp_x"), con, UMBRALES, HOY, con_trending=False)
    con.execute("UPDATE repos SET archivado = 1 WHERE id = 3")
    con.commit()
    inf = tendencia.ejecutar(cliente_gh(manejador, "ghp_x"), con, UMBRALES, HOY, con_trending=False)
    assert inf.ok, inf.problemas
    assert (inf.seguidos, inf.calculados) == (2, 3)


def test_sin_repos_seguidos_avisa(con):
    inf = tendencia.ejecutar(cliente_gh(lambda r: httpx.Response(500)), con, UMBRALES, HOY, con_trending=False)
    assert not inf.ok and "radar-ranking" in inf.problemas[0]


def test_ejecutar_con_trending_marca_en_trending(con):
    def manejador(request):
        if request.url.host == "github.com":
            return httpx.Response(200, text=FIXTURE.read_text(encoding="utf-8"))
        raise AssertionError(request.url)

    umbrales = {**UMBRALES, "trending": {"minimo_repos": 3, "periodos": ["daily"]}}
    inf = tendencia.ejecutar(cliente_gh(manejador, "ghp_x"), con, umbrales, HOY)
    assert inf.ok, inf.problemas
    assert inf.trending == 3
    filas = con.execute("SELECT en_trending, estrellas_hoy_trending, nivel FROM tendencias").fetchall()
    assert all(f["en_trending"] == 1 for f in filas)
    # Sin historial propio, el "N stars today" alcanza para en_alza.
    assert {f["nivel"] for f in filas} == {"en_alza"}
