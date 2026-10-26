"""Pruebas de la fase 4 (extracción de links, pendientes y Hacker News) con APIs simuladas."""

from datetime import date

import httpx
import pytest

from radar import db, hackernews, menciones, ranking
from radar.github import GitHubClient

HOY = date(2026, 10, 22)
AHORA = 1_792_000_000
AJUSTES = hackernews.cargar_ajustes()


@pytest.fixture
def con(tmp_path):
    c = db.conectar(tmp_path / "radar.db")
    db.aplicar_esquema(c)
    yield c
    c.close()


def repo_api(n: int, full_name: str, estrellas: int = 500) -> dict:
    return {
        "id": n, "full_name": full_name, "html_url": f"https://github.com/{full_name}", "description": "d",
        "language": "Rust", "topics": [], "stargazers_count": estrellas, "forks_count": 1, "fork": False,
        "archived": False, "created_at": "2026-10-01T00:00:00Z", "pushed_at": "2026-10-20T00:00:00Z",
    }


def historia(i: int, url: str | None = None, texto: str | None = None, puntos: int = 50) -> dict:
    return {
        "objectID": str(i), "title": f"Historia {i}", "url": url, "story_text": texto, "author": f"autor{i}",
        "points": puntos, "num_comments": 3, "created_at": "2026-10-21T12:00:00Z", "created_at_i": AHORA - 3600,
    }


class Simulador:
    """Algolia (dos búsquedas) + API de GitHub en un solo transporte."""

    def __init__(self, con_link: list[dict], show_hn: list[dict], github: dict[str, dict], fallas_algolia: int = 0):
        self.con_link, self.show_hn, self.github = con_link, show_hn, github
        self.fallas_algolia = fallas_algolia
        self.llamadas_github = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "hn.algolia.com":
            if self.fallas_algolia:
                self.fallas_algolia -= 1
                return httpx.Response(503)
            hits = self.show_hn if request.url.params.get("tags") == "show_hn" else self.con_link
            return httpx.Response(200, json={"hits": hits, "nbPages": 1, "page": 0})
        if request.url.host == "api.github.com":
            self.llamadas_github += 1
            nombre = request.url.path.removeprefix("/repos/").lower()
            item = self.github.get(nombre)
            return httpx.Response(200, json=item) if item else httpx.Response(404)
        raise AssertionError(request.url)


def clientes(sim: Simulador, token: str = "ghp_x") -> tuple[httpx.Client, GitHubClient]:
    transporte = httpx.MockTransport(sim)
    return httpx.Client(transport=transporte), GitHubClient(token, httpx.Client(transport=transporte), dormir=lambda s: None)


def correr(con, sim, token="ghp_x", ajustes=None):
    http, gh = clientes(sim, token)
    return hackernews.ejecutar(http, gh, con, ajustes or {**AJUSTES, "minimo_historias": 1}, AHORA, HOY, dormir=lambda s: None)


# ---------- Extracción de links ----------

@pytest.mark.parametrize(
    "texto, esperado",
    [
        ("https://github.com/astral-sh/uv", ["astral-sh/uv"]),
        ("https://github.com/astral-sh/uv/tree/main/docs", ["astral-sh/uv"]),
        ("git clone https://github.com/torvalds/linux.git", ["torvalds/linux"]),
        ("mira github.com/vercel/next.js.", ["vercel/next.js"]),
        ("http://www.github.com/a/b y https://GitHub.com/A/B", ["a/b"]),
        ("https://github.com/features/copilot https://github.com/sponsors/x", []),
        ("https://github.com/solo-usuario", []),
        ("https://gist.github.com/a/123", []),
        ("Repo: https:&#x2F;&#x2F;github.com&#x2F;dueno&#x2F;mi-repo y más", ["dueno/mi-repo"]),
        (None, []),
    ],
)
def test_extraer_repos(texto, esperado):
    assert menciones.extraer_repos(texto) == esperado


# ---------- Registro y pendientes ----------

def test_repo_conocido_va_directo_y_desconocido_a_pendientes(con):
    ranking.guardar_repos(con, [ranking.normalizar(repo_api(1, "Conocido/Repo"))], HOY)
    lista = [
        menciones.Mencion("conocido/repo", "hackernews", "https://news.ycombinator.com/item?id=1", interaccion=10),
        menciones.Mencion("nuevo/repo", "hackernews", "https://news.ycombinator.com/item?id=2", interaccion=20),
    ]
    assert menciones.registrar(con, lista) == (1, 1)
    assert menciones.registrar(con, lista) == (1, 1)  # repetir no duplica
    assert con.execute("SELECT COUNT(*) FROM menciones").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM menciones_pendientes").fetchone()[0] == 1


def test_resolver_respeta_el_limite_y_prioriza_interaccion(con):
    lista = [
        menciones.Mencion(f"d{i}/r{i}", "hackernews", f"https://news.ycombinator.com/item?id={i}", interaccion=i)
        for i in range(1, 6)
    ]
    menciones.registrar(con, lista)
    sim = Simulador([], [], {f"d{i}/r{i}": repo_api(i, f"d{i}/r{i}") for i in range(1, 6)})
    _, gh = clientes(sim)
    res = menciones.resolver_pendientes(gh, con, HOY, limite=2)
    assert (res.resueltos, res.restantes) == (2, 3)
    ids = {f[0] for f in con.execute("SELECT repo_id FROM menciones")}
    assert ids == {5, 4}  # los de más puntos primero


def test_resolver_descarta_repos_que_no_existen(con):
    menciones.registrar(con, [menciones.Mencion("no/existe", "hackernews", "u1")])
    _, gh = clientes(Simulador([], [], {}))
    res = menciones.resolver_pendientes(gh, con, HOY, limite=10)
    assert res.descartados == 1 and res.restantes == 0


def test_repo_renombrado_no_cuenta_como_perdido(con):
    lista = [menciones.Mencion("viejo/nombre", "hackernews", "u1")]
    menciones.registrar(con, lista)
    _, gh = clientes(Simulador([], [], {"viejo/nombre": repo_api(7, "nuevo/nombre")}))
    res = menciones.resolver_pendientes(gh, con, HOY, limite=10)
    assert menciones.perdidas(con, lista, res) == []
    assert con.execute("SELECT full_name FROM repos WHERE id = 7").fetchone()[0] == "nuevo/nombre"


def test_sin_token_respeta_limite_de_resolucion(con):
    hits = [historia(i, url=f"https://github.com/d{i}/r{i}") for i in range(1, 31)]
    sim = Simulador(hits, [], {f"d{i}/r{i}": repo_api(i, f"d{i}/r{i}") for i in range(1, 31)})
    inf = correr(con, sim, token="")
    assert inf.ok, inf.problemas
    assert inf.resueltos == AJUSTES["max_resolver_sin_token"] == sim.llamadas_github
    assert inf.restantes == 30 - AJUSTES["max_resolver_sin_token"]


# ---------- Corrida completa ----------

def test_corrida_completa(con):
    ranking.guardar_repos(con, [ranking.normalizar(repo_api(1, "ya/conocido"))], HOY)
    con_link = [
        historia(1, url="https://github.com/ya/conocido", puntos=300),
        historia(2, url="https://github.com/nuevo/repo/blob/main/README.md", puntos=120),
        historia(3, url="https://github.com/fantasma/borrado", puntos=40),
    ]
    show_hn = [
        historia(2, url="https://github.com/nuevo/repo"),  # duplicada con la búsqueda anterior
        historia(4, texto="Show HN: lo hice, código en https:&#x2F;&#x2F;github.com&#x2F;nuevo&#x2F;repo"),
        historia(5, texto="sin links a github"),
    ]
    sim = Simulador(con_link, show_hn, {"nuevo/repo": repo_api(2, "nuevo/repo")})
    inf = correr(con, sim)
    assert inf.ok, inf.problemas
    assert inf.historias == 5 and inf.con_links == 4 and inf.menciones == 4
    assert (inf.directas, inf.pendientes) == (1, 3)
    assert (inf.resueltos, inf.descartados, inf.restantes) == (1, 1, 0)
    filas = con.execute("SELECT r.full_name, m.interaccion FROM menciones m JOIN repos r ON r.id = m.repo_id "
                        "WHERE m.fuente = 'hackernews' ORDER BY m.interaccion DESC").fetchall()
    assert [tuple(f) for f in filas] == [("ya/conocido", 300), ("nuevo/repo", 120), ("nuevo/repo", 50)]
    # El repo confirmado ya tiene su foto de hoy para el detector de tendencia.
    assert con.execute("SELECT COUNT(*) FROM snapshots WHERE repo_id = 2").fetchone()[0] == 1
    assert con.execute("SELECT estado FROM corridas WHERE tarea = 'hackernews'").fetchone()[0] == "ok"


def test_algolia_caida_se_reintenta(con):
    sim = Simulador([historia(1, url="https://github.com/a/b")], [], {"a/b": repo_api(1, "a/b")}, fallas_algolia=2)
    assert correr(con, sim).ok


def test_algolia_siempre_caida_falla_y_registra_error(con):
    sim = Simulador([], [], {}, fallas_algolia=99)
    inf = correr(con, sim)
    assert not inf.ok and "503" in inf.problemas[0]
    assert con.execute("SELECT estado FROM corridas").fetchone()[0] == "error"


def test_pocas_historias_es_falla(con):
    inf = correr(con, Simulador([historia(1)], [], {}), ajustes={**AJUSTES, "minimo_historias": 5})
    assert not inf.ok and "al menos 5" in inf.problemas[0]


def test_historias_sin_titulo_es_falla(con):
    rota = historia(1, url="https://github.com/a/b")
    del rota["title"]
    inf = correr(con, Simulador([rota], [], {}))
    assert not inf.ok and "sin título" in inf.problemas[0]
