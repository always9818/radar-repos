"""Pruebas de Reddit y Bluesky (fase 4) con fragmentos reales de cada red y APIs simuladas."""

import json
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import pytest

from radar import bluesky, db, reddit
from radar.github import GitHubClient

HOY = date(2026, 10, 22)
AHORA = datetime(2026, 10, 23, 4, 0, tzinfo=timezone.utc)
FIXTURES = Path(__file__).parent / "fixtures"
RSS = (FIXTURES / "reddit_top.xml").read_text(encoding="utf-8")
BSKY = json.loads((FIXTURES / "bluesky_search.json").read_text(encoding="utf-8"))


@pytest.fixture
def con(tmp_path):
    c = db.conectar(tmp_path / "radar.db")
    db.aplicar_esquema(c)
    yield c
    c.close()


def repo_api(nombre: str, n: int) -> dict:
    return {
        "id": n, "full_name": nombre, "html_url": f"https://github.com/{nombre}", "description": "d",
        "language": "Python", "topics": [], "stargazers_count": 100, "forks_count": 1, "fork": False,
        "archived": False, "created_at": "2026-10-01T00:00:00Z", "pushed_at": "2026-10-20T00:00:00Z",
    }


def github_falso(request: httpx.Request) -> httpx.Response | None:
    if request.url.host != "api.github.com":
        return None
    nombre = request.url.path.removeprefix("/repos/")
    return httpx.Response(200, json=repo_api(nombre, abs(hash(nombre.lower())) % 10**9))


def clientes(manejador, token="ghp_x"):
    def todo(request):
        return github_falso(request) or manejador(request)

    t = httpx.MockTransport(todo)
    return httpx.Client(transport=t), GitHubClient(token, httpx.Client(transport=t), dormir=lambda s: None)


# ---------- Reddit ----------

AJ_REDDIT = {**reddit.cargar_ajustes(), "subreddits": ["opensource", "github"], "busqueda": ""}


def test_parsea_rss_real_de_reddit():
    entradas = reddit.parsear(RSS)
    assert len(entradas) == 6
    assert entradas[0].url.startswith("https://www.reddit.com/r/opensource/comments/")
    assert entradas[0].autor.startswith("/u/") and entradas[0].publicado.startswith("2026-10-")
    menciones = reddit.a_menciones(entradas)
    assert [m.full_name for m in menciones][:3] == ["storytold/photocraft", "igtoth/tracedraw", "morluto/rea"]
    assert menciones[0].interaccion == 6  # primero del top de 6
    assert {m.fuente for m in menciones} == {"reddit"}


@pytest.mark.parametrize("texto", ["<html>bloqueado</html>", "no es xml", '<rss version="2.0"><channel/></rss>'])
def test_feed_invalido_es_error(texto):
    with pytest.raises(reddit.RedditError):
        reddit.parsear(texto)


def test_feeds_incluye_busqueda_y_subreddits():
    nombres = [n for n, _ in reddit.feeds({**AJ_REDDIT, "busqueda": "url:github.com"})]
    assert nombres == ["busqueda", "r/opensource", "r/github"]


def test_reddit_respeta_pausa_y_reintenta_429(con):
    esperas, llamadas = [], []

    def manejador(request):
        llamadas.append(str(request.url))
        if len(llamadas) == 1:
            return httpx.Response(429, headers={"retry-after": "30"})
        return httpx.Response(200, text=RSS)

    http, gh = clientes(manejador)
    inf = reddit.ejecutar(http, gh, con, AJ_REDDIT, HOY, dormir=esperas.append)
    assert inf.ok, inf.problemas
    assert inf.feeds_ok == 2 and len(llamadas) == 3
    assert 30.0 in esperas and AJ_REDDIT["pausa_segundos"] in esperas
    assert inf.segundos_espera == 30.0 + AJ_REDDIT["pausa_segundos"]  # queda registrado cuánto se esperó


def test_reddit_no_duplica_el_mismo_post_en_dos_feeds(con):
    http, gh = clientes(lambda r: httpx.Response(200, text=RSS))
    inf = reddit.ejecutar(http, gh, con, AJ_REDDIT, HOY, dormir=lambda s: None)
    assert inf.ok, inf.problemas
    assert inf.menciones == 5  # 5 repos en el fragmento, aunque el feed se leyó dos veces
    assert con.execute("SELECT COUNT(*) FROM menciones WHERE fuente = 'reddit'").fetchone()[0] == 5


def test_reddit_cobertura_baja_falla_pero_guarda_lo_leido(con):
    def manejador(request):
        return httpx.Response(200, text=RSS) if "opensource" in str(request.url) else httpx.Response(403)

    http, gh = clientes(manejador)
    aj = {**AJ_REDDIT, "subreddits": ["opensource", "github", "rust", "golang"]}
    inf = reddit.ejecutar(http, gh, con, aj, HOY, dormir=lambda s: None)
    assert not inf.ok and "1/4" in inf.problemas[0]
    assert len(inf.fallidos) == 3 and "403" in inf.fallidos[0]
    assert inf.menciones == 5
    assert con.execute("SELECT estado FROM corridas WHERE tarea = 'reddit'").fetchone()[0] == "error"


def test_reddit_un_feed_caido_no_tumba_la_corrida(con):
    def manejador(request):
        return httpx.Response(503) if "golang" in str(request.url) else httpx.Response(200, text=RSS)

    http, gh = clientes(manejador)
    aj = {**AJ_REDDIT, "subreddits": ["opensource", "github", "rust", "golang"]}
    inf = reddit.ejecutar(http, gh, con, aj, HOY, dormir=lambda s: None)
    assert inf.ok, inf.problemas  # 3/4 = 75 % supera el mínimo de 70 %
    assert inf.fallidos == ["r/golang: Reddit respondió 503"]


# ---------- Bluesky ----------

AJ_BSKY = {**bluesky.cargar_ajustes(), "minimo_posts": 3}


def test_parsea_posts_reales_de_bluesky():
    menciones = bluesky.a_menciones(BSKY["posts"], likes_min=0)
    assert [m.full_name for m in menciones][:2] == ["breakzplatform/rota-da-democracia", "php/php-src"]
    assert menciones[0].post_url == "https://bsky.app/profile/joseli.to/post/3mydfpkdhbc2i"
    assert menciones[0].interaccion == BSKY["posts"][0]["likeCount"] + BSKY["posts"][0]["repostCount"]


def test_likes_minimos_filtran_posts():
    todos = bluesky.a_menciones(BSKY["posts"], likes_min=0)
    filtrados = bluesky.a_menciones(BSKY["posts"], likes_min=40)
    assert len(filtrados) < len(todos)
    assert all(m.interaccion >= 40 for m in filtrados)


def test_link_en_embed_externo():
    post = {
        "uri": "at://did:plc:x/app.bsky.feed.post/abc", "author": {"handle": "yo.bsky.social"}, "likeCount": 10,
        "record": {"text": "miren esto", "createdAt": "2026-10-22T00:00:00Z"},
        "embed": {"$type": "app.bsky.embed.external#view", "external": {"uri": "https://github.com/a/b"}},
    }
    assert [m.full_name for m in bluesky.a_menciones([post], 5)] == ["a/b"]


def test_bluesky_una_busqueda_por_orden_sin_cursor_y_sin_duplicados(con):
    vistas = []

    def manejador(request):
        vistas.append((request.url.params["sort"], request.url.params.get("cursor")))
        assert request.url.params["since"] == "2026-10-16T04:00:00Z"
        return httpx.Response(200, json={"posts": BSKY["posts"], "cursor": "c1"})

    http, gh = clientes(manejador)
    inf = bluesky.ejecutar(http, gh, con, AJ_BSKY, AHORA, HOY, dormir=lambda s: None)
    assert inf.ok, inf.problemas
    assert vistas == [("top", None), ("latest", None)]  # nunca pide páginas con cursor
    assert inf.posts == len(BSKY["posts"])  # las dos búsquedas trajeron los mismos posts


def test_bluesky_caido_reintenta_y_falla(con):
    llamadas = []

    def manejador(request):
        llamadas.append(1)
        return httpx.Response(502)

    http, gh = clientes(manejador)
    inf = bluesky.ejecutar(http, gh, con, AJ_BSKY, AHORA, HOY, dormir=lambda s: None)
    assert not inf.ok and "502" in inf.problemas[0] and len(llamadas) == 3
    assert con.execute("SELECT estado FROM corridas WHERE tarea = 'bluesky'").fetchone()[0] == "error"


def test_bluesky_403_no_reintenta(con):
    llamadas = []

    def manejador(request):
        llamadas.append(1)
        return httpx.Response(403)

    http, gh = clientes(manejador)
    assert not bluesky.ejecutar(http, gh, con, AJ_BSKY, AHORA, HOY, dormir=lambda s: None).ok
    assert len(llamadas) == 1


def test_bluesky_formato_cambiado_es_falla(con):
    http, gh = clientes(lambda r: httpx.Response(200, json={"resultados": []}))
    inf = bluesky.ejecutar(http, gh, con, AJ_BSKY, AHORA, HOY, dormir=lambda s: None)
    assert not inf.ok and "cambiar" in inf.problemas[0]


def test_bluesky_pocos_posts_es_falla(con):
    http, gh = clientes(lambda r: httpx.Response(200, json={"posts": BSKY["posts"][:1]}))
    inf = bluesky.ejecutar(http, gh, con, AJ_BSKY, AHORA, HOY, dormir=lambda s: None)
    assert not inf.ok and "al menos 3" in inf.problemas[0]
