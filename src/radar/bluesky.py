"""Fase 4: menciones de repos en Bluesky (API pública de búsqueda, sin cuenta).

Pasos de cada corrida:
  1. Busca posts recientes que contienen "github.com": una página por orden (top y latest).
  2. Valida la respuesta (loop de verificación, hasta 3 intentos por página).
  3. Saca los links de las facets, del embed y del texto, filtra por likes mínimos
     y procesa las menciones con menciones.procesar.

Uso:  uv run radar-bluesky [--dias N]
Sale con código 0 si todas las verificaciones pasaron y 1 si alguna falló.
"""

import argparse
import json
import sqlite3
import sys
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

from radar import db, menciones
from radar.config import RAIZ, cargar_config
from radar.github import GitHubClient

AJUSTES = RAIZ / "config" / "redes.toml"
BUSQUEDA = "https://api.bsky.app/xrpc/app.bsky.feed.searchPosts"
INTENTOS = 3


class BlueskyError(Exception):
    pass


def cargar_ajustes(ruta: Path = AJUSTES) -> dict:
    return tomllib.loads(ruta.read_text(encoding="utf-8"))["bluesky"]


def _get_json(client: httpx.Client, params: dict, dormir: Callable[[float], None]) -> dict:
    error = ""
    for intento in range(1, INTENTOS + 1):
        try:
            resp = client.get(BUSQUEDA, params=params)
            if resp.status_code == 200:
                datos = resp.json()
                if isinstance(datos.get("posts"), list):
                    return datos
                error = "Respuesta sin lista de posts (la API pudo cambiar)"
            else:
                error = f"Bluesky respondió {resp.status_code}"
                if resp.status_code < 500 and resp.status_code != 429:
                    break
        except (httpx.TransportError, ValueError) as e:
            error = f"Sin conexión o respuesta inválida de Bluesky: {e}"
        if intento < INTENTOS:
            dormir(2 ** intento)
    raise BlueskyError(error)


def buscar_posts(client: httpx.Client, ajustes: dict, ahora: datetime, dormir=time.sleep) -> list[dict]:
    """Una página de 100 por orden: sin cuenta, la API bloquea con 403 las páginas con cursor."""
    desde = (ahora - timedelta(days=ajustes["dias"])).strftime("%Y-%m-%dT%H:%M:%SZ")
    posts: list[dict] = []
    for orden in ajustes["ordenes"]:
        params = {"q": ajustes["consulta"], "sort": orden, "since": desde, "limit": 100}
        posts += _get_json(client, params, dormir)["posts"]
    unicos: dict[str, dict] = {}
    for p in posts:
        if "uri" in p:
            unicos.setdefault(p["uri"], p)
    return list(unicos.values())


def validar_posts(posts: list[dict], minimo: int) -> list[str]:
    problemas = []
    if len(posts) < minimo:
        problemas.append(f"Llegaron {len(posts)} posts de Bluesky, se esperaban al menos {minimo}")
    rotos = [p.get("uri", "?") for p in posts if "record" not in p or "author" not in p]
    if rotos:
        problemas.append(f"Posts sin texto o autor (la API pudo cambiar): {', '.join(rotos[:3])}")
    return problemas


def url_post(post: dict) -> str:
    """at://did/app.bsky.feed.post/RKEY → https://bsky.app/profile/handle/post/RKEY"""
    rkey = post["uri"].rsplit("/", 1)[-1]
    autor = post["author"].get("handle") or post["author"].get("did")
    return f"https://bsky.app/profile/{autor}/post/{rkey}"


def links_del_post(post: dict) -> str:
    record = post.get("record", {})
    partes = [record.get("text", "")]
    for faceta in record.get("facets", []) or []:
        for f in faceta.get("features", []):
            if f.get("uri"):
                partes.append(f["uri"])
    for embed in (post.get("embed") or {}, record.get("embed") or {}):
        externo = embed.get("external") or {}
        if externo.get("uri"):
            partes.append(externo["uri"])
    return " ".join(partes)


def a_menciones(posts: list[dict], likes_min: int) -> list[menciones.Mencion]:
    resultado = []
    for p in posts:
        likes, reposts = p.get("likeCount", 0) or 0, p.get("repostCount", 0) or 0
        if likes < likes_min:
            continue
        texto = p.get("record", {}).get("text", "")
        for full_name in menciones.extraer_repos(links_del_post(p)):
            resultado.append(
                menciones.Mencion(
                    full_name=full_name, fuente="bluesky", post_url=url_post(p),
                    autor=p["author"].get("handle"), texto=texto[:300],
                    publicado=p.get("record", {}).get("createdAt"), interaccion=likes + reposts,
                )
            )
    return resultado


@dataclass
class Informe:
    posts: int = 0
    con_alcance: int = 0
    menciones: int = 0
    directas: int = 0
    pendientes: int = 0
    resueltos: int = 0
    descartados: int = 0
    restantes: int = 0
    problemas: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problemas


def ejecutar(
    http: httpx.Client,
    gh: GitHubClient,
    con: sqlite3.Connection,
    ajustes: dict,
    ahora: datetime | None = None,
    hoy: date | None = None,
    dormir: Callable[[float], None] = time.sleep,
) -> Informe:
    ahora = ahora or datetime.now(timezone.utc)
    hoy = hoy or date.today()
    inf = Informe()
    corrida = db.iniciar_corrida(con, "bluesky")
    try:
        try:
            posts = buscar_posts(http, ajustes, ahora, dormir)
        except BlueskyError as e:
            inf.problemas.append(str(e))
            return inf
        inf.problemas = validar_posts(posts, ajustes["minimo_posts"])
        if inf.problemas:
            return inf
        inf.posts = len(posts)
        inf.con_alcance = sum(1 for p in posts if (p.get("likeCount") or 0) >= ajustes["likes_min"])

        lista = a_menciones(posts, ajustes["likes_min"])
        inf.menciones = len(lista)
        limite = ajustes["max_resolver_sin_token"] if gh.anonimo else ajustes["max_resolver"]
        p = menciones.procesar(con, gh, lista, hoy, limite)
        inf.directas, inf.pendientes = p.directas, p.pendientes
        res = p.resolucion
        inf.resueltos, inf.descartados, inf.restantes = res.resueltos, res.descartados, res.restantes
        inf.problemas += res.problemas
        if p.perdidas:
            inf.problemas.append(f"Menciones perdidas: {', '.join(p.perdidas[:5])}")
        return inf
    finally:
        db.cerrar_corrida(con, corrida, inf.ok, json.dumps(inf.__dict__, ensure_ascii=False))


def main() -> int:
    parser = argparse.ArgumentParser(description="Menciones de repos en Bluesky")
    parser.add_argument("--dias", type=int, help="ventana hacia atrás (por defecto la de redes.toml)")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    cfg = cargar_config()
    ajustes = cargar_ajustes()
    if args.dias:
        ajustes["dias"] = args.dias
    gh = GitHubClient(cfg.github_token)
    http = httpx.Client(timeout=20, headers={"User-Agent": "radar-repos"})
    con = db.conectar(cfg.database_path)
    try:
        db.aplicar_esquema(con)
        inf = ejecutar(http, gh, con, ajustes)
    finally:
        http.close()
        gh.close()
        con.close()

    print(f"Posts: {inf.posts} · con ≥{ajustes['likes_min']} likes: {inf.con_alcance} · menciones: {inf.menciones}")
    print(f"Directas: {inf.directas} · pendientes: {inf.pendientes} · confirmadas ahora: {inf.resueltos} · "
          f"descartadas: {inf.descartados} · siguen pendientes (todas las redes): {inf.restantes}")
    for p in inf.problemas:
        print(f"❌ {p}")
    print("\nBluesky actualizado." if inf.ok else "\nHubo fallas en Bluesky.")
    return 0 if inf.ok else 1


if __name__ == "__main__":
    sys.exit(main())
