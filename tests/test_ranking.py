"""Pruebas de la fase 1 con una API de GitHub simulada (sin red ni token real)."""

from datetime import date

import httpx
import pytest

from radar import db, ranking
from radar.github import GitHubClient, GitHubError, LimiteAgotado
from radar.ranking import Ajustes, Consulta

HOY = date(2026, 10, 22)


def repo_falso(n: int, **cambios) -> dict:
    base = {
        "id": n,
        "full_name": f"dueno{n}/repo{n}",
        "html_url": f"https://github.com/dueno{n}/repo{n}",
        "description": f"Repo de prueba {n}",
        "language": "Python",
        "topics": ["llm", "agents"],
        "stargazers_count": 100_000 - n,
        "forks_count": 10,
        "fork": False,
        "archived": False,
        "created_at": "2025-01-01T00:00:00Z",
        "pushed_at": "2026-10-01T00:00:00Z",
    }
    base.update(cambios)
    return base


class GitHubFalso:
    """Simula /search/repositories (paginado) y /repos/{full_name}."""

    def __init__(self, items: list[dict], inexistentes: set[str] = frozenset()):
        self.items = items
        self.inexistentes = inexistentes
        self.busquedas = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/search/repositories":
            self.busquedas += 1
            pagina = int(request.url.params["page"])
            por_pagina = int(request.url.params["per_page"])
            lote = self.items[(pagina - 1) * por_pagina : pagina * por_pagina]
            return httpx.Response(200, json={"total_count": len(self.items), "incomplete_results": False, "items": lote})
        if request.url.path.startswith("/repos/"):
            nombre = request.url.path.removeprefix("/repos/")
            return httpx.Response(404 if nombre in self.inexistentes else 200, json={"full_name": nombre})
        raise AssertionError(f"Ruta inesperada {request.url}")


def cliente_gh(manejador, esperas: list | None = None, reloj=lambda: 1_000.0) -> GitHubClient:
    dormir = (lambda s: esperas.append(s)) if esperas is not None else (lambda s: None)
    return GitHubClient("ghp_prueba", httpx.Client(transport=httpx.MockTransport(manejador)), dormir=dormir, reloj=reloj)


@pytest.fixture
def con(tmp_path):
    c = db.conectar(tmp_path / "radar.db")
    db.aplicar_esquema(c)
    yield c
    c.close()


def ajustes(*consultas: Consulta, muestra: int = 2) -> Ajustes:
    return Ajustes(muestra_urls=muestra, consultas=list(consultas))


# ---------- Cliente de GitHub ----------

def test_paginacion_se_detiene_en_pagina_incompleta():
    falso = GitHubFalso([repo_falso(i) for i in range(1, 151)])
    items = cliente_gh(falso).buscar_repos("stars:>1", paginas=5)
    assert len(items) == 150 and falso.busquedas == 2


def test_paginacion_maximo_10_paginas():
    falso = GitHubFalso([repo_falso(i) for i in range(1, 2001)])
    assert len(cliente_gh(falso).buscar_repos("stars:>1", paginas=50)) == 1000


def test_espera_si_el_limite_se_renueva_pronto():
    llamadas, esperas = [], []

    def manejador(request):
        llamadas.append(1)
        if len(llamadas) == 1:
            return httpx.Response(403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1030"})
        return httpx.Response(200, json={"items": []})

    cliente_gh(manejador, esperas, reloj=lambda: 1_000.0).buscar_repos("x")
    assert esperas == [31.0]


def test_limite_lejano_lanza_limite_agotado():
    def manejador(request):
        return httpx.Response(403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "5000"})

    with pytest.raises(LimiteAgotado):
        cliente_gh(manejador).buscar_repos("x")


def test_limite_secundario_respeta_retry_after():
    llamadas, esperas = [], []

    def manejador(request):
        llamadas.append(1)
        return httpx.Response(429, headers={"retry-after": "5"}) if len(llamadas) == 1 else httpx.Response(200, json={"items": []})

    cliente_gh(manejador, esperas).buscar_repos("x")
    assert esperas == [5.0]


def test_reintenta_errores_500():
    llamadas = []

    def manejador(request):
        llamadas.append(1)
        return httpx.Response(502) if len(llamadas) < 3 else httpx.Response(200, json={"items": []})

    cliente_gh(manejador).buscar_repos("x")
    assert len(llamadas) == 3


def test_token_invalido():
    with pytest.raises(GitHubError, match="401"):
        cliente_gh(lambda r: httpx.Response(401)).buscar_repos("x")


def test_sin_conexion():
    def caido(request):
        raise httpx.ConnectError("sin red")

    with pytest.raises(GitHubError, match="Sin conexión"):
        cliente_gh(caido).buscar_repos("x")


# ---------- Configuración ----------

def test_config_real_se_carga_y_reemplaza_fechas():
    a = ranking.cargar_ajustes(hoy=HOY)
    assert len(a.consultas) >= 5
    nuevos = {c.nombre: c.q for c in a.consultas}
    assert nuevos["nuevos_7d"].startswith("created:>2026-10-15")
    assert nuevos["nuevos_30d"].startswith("created:>2026-09-22")
    assert all("{" not in c.q for c in a.consultas)


def test_config_rechaza_nombres_repetidos(tmp_path):
    archivo = tmp_path / "c.toml"
    archivo.write_text('[[consulta]]\nnombre="a"\nq="x"\n[[consulta]]\nnombre="a"\nq="y"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="repetido"):
        ranking.cargar_ajustes(archivo, HOY)


# ---------- Validación ----------

def test_validar_lote_sano():
    assert ranking.validar([ranking.normalizar(repo_falso(i)) for i in range(1, 11)], minimo=10) == []


def test_validar_detecta_problemas():
    repos = [ranking.normalizar(repo_falso(i)) for i in range(1, 4)]
    repos.append(ranking.normalizar(repo_falso(1)))  # duplicado
    repos.append(ranking.normalizar(repo_falso(9, html_url="https://github.com/otro/nombre")))
    repos.append(ranking.normalizar(repo_falso(10, stargazers_count=0)))
    problemas = " | ".join(ranking.validar(repos, minimo=50))
    assert "se esperaban al menos 50" in problemas
    assert "duplicados" in problemas
    assert "no coinciden" in problemas
    assert "sin estrellas" in problemas


def test_reintenta_si_el_primer_lote_viene_corto():
    completo = [repo_falso(i) for i in range(1, 31)]

    class Intermitente(GitHubFalso):
        def __call__(self, request):
            if request.url.path == "/search/repositories" and self.busquedas == 0:
                self.busquedas += 1
                return httpx.Response(200, json={"items": completo[:5], "incomplete_results": True})
            return super().__call__(request)

    falso = Intermitente(completo)
    repos, res = ranking.recolectar_consulta(cliente_gh(falso), Consulta("c", "q", minimo=30), ajustes())
    assert res.ok and res.intentos == 2 and len(repos) == 30


def test_falla_tras_tres_intentos():
    falso = GitHubFalso([repo_falso(i) for i in range(1, 6)])
    repos, res = ranking.recolectar_consulta(cliente_gh(falso), Consulta("c", "q", minimo=30), ajustes())
    assert not res.ok and res.intentos == 3 and repos == []
    assert falso.busquedas == 3


def test_muestra_detecta_repo_inexistente():
    items = [repo_falso(1), repo_falso(2)]
    falso = GitHubFalso(items, inexistentes={"dueno1/repo1", "dueno2/repo2"})
    _, res = ranking.recolectar_consulta(cliente_gh(falso), Consulta("c", "q", minimo=1), ajustes(muestra=2))
    assert not res.ok and "no responde" in res.problemas[0]


def test_filtra_forks_y_archivados():
    items = [repo_falso(1), repo_falso(2, fork=True), repo_falso(3, archived=True)]
    repos, res = ranking.recolectar_consulta(cliente_gh(GitHubFalso(items)), Consulta("c", "q", minimo=3), ajustes())
    assert res.ok and [r["id"] for r in repos] == [1]


# ---------- Guardado y corrida completa ----------

def test_guardar_es_idempotente_en_el_mismo_dia(con):
    consulta = Consulta("top", "stars:>1")
    repos = [ranking.normalizar(repo_falso(i)) for i in range(1, 6)]
    assert ranking.guardar(con, repos, consulta, HOY) == 5
    repos[0]["estrellas"] += 50
    assert ranking.guardar(con, repos, consulta, HOY) == 0
    assert con.execute("SELECT COUNT(*) FROM repos").fetchone()[0] == 5
    assert con.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 5
    assert con.execute("SELECT COUNT(*) FROM menciones").fetchone()[0] == 5
    estrellas = con.execute("SELECT estrellas FROM snapshots WHERE repo_id = 1").fetchone()[0]
    assert estrellas == repos[0]["estrellas"]


def test_un_snapshot_por_dia(con):
    consulta = Consulta("top", "stars:>1")
    repos = [ranking.normalizar(repo_falso(1))]
    ranking.guardar(con, repos, consulta, date(2026, 10, 21))
    repos[0]["estrellas"] += 300
    ranking.guardar(con, repos, consulta, HOY)
    filas = con.execute("SELECT fecha, estrellas FROM snapshots ORDER BY fecha").fetchall()
    assert [tuple(f) for f in filas] == [("2026-10-21", 99_999), ("2026-10-22", 100_299)]


def test_primera_vez_no_cambia_al_actualizar(con):
    consulta = Consulta("top", "stars:>1")
    repos = [ranking.normalizar(repo_falso(1))]
    ranking.guardar(con, repos, consulta, HOY)
    primera = con.execute("SELECT primera_vez FROM repos").fetchone()[0]
    con.execute("UPDATE repos SET primera_vez = '2000-01-01T00:00:00+00:00'")
    ranking.guardar(con, repos, consulta, HOY)
    assert primera and con.execute("SELECT primera_vez FROM repos").fetchone()[0] == "2000-01-01T00:00:00+00:00"


def test_misma_repo_en_dos_consultas_dos_menciones(con):
    repos = [ranking.normalizar(repo_falso(1))]
    ranking.guardar(con, repos, Consulta("top_global", "stars:>1"), HOY)
    ranking.guardar(con, repos, Consulta("top_python", "language:python"), HOY)
    assert con.execute("SELECT COUNT(*) FROM repos").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM menciones").fetchone()[0] == 2


def test_ejecutar_completo_registra_corrida_ok(con):
    falso = GitHubFalso([repo_falso(i) for i in range(1, 21)])
    a = ajustes(Consulta("a", "stars:>1", minimo=20), Consulta("b", "language:python", minimo=10))
    resultados = ranking.ejecutar(cliente_gh(falso), con, a, hoy=HOY)
    assert all(r.ok for r in resultados)
    assert [r.nuevos for r in resultados] == [20, 0]
    fila = con.execute("SELECT tarea, estado FROM corridas").fetchone()
    assert (fila["tarea"], fila["estado"]) == ("ranking", "ok")


def test_limite_agotado_marca_las_consultas_restantes(con):
    def manejador(request):
        return httpx.Response(403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "99999"})

    a = ajustes(Consulta("a", "x"), Consulta("b", "y"), Consulta("c", "z"))
    resultados = ranking.ejecutar(cliente_gh(manejador), con, a, hoy=HOY)
    assert [r.nombre for r in resultados] == ["a", "b", "c"]
    assert not any(r.ok for r in resultados)
    assert "límite agotado" in resultados[2].problemas[0]
    assert con.execute("SELECT estado FROM corridas").fetchone()[0] == "error"


def test_sin_guardar_no_escribe(con):
    falso = GitHubFalso([repo_falso(i) for i in range(1, 6)])
    resultados = ranking.ejecutar(cliente_gh(falso), None, ajustes(Consulta("a", "x", minimo=5)), hoy=HOY)
    assert resultados[0].ok
    assert con.execute("SELECT COUNT(*) FROM repos").fetchone()[0] == 0
