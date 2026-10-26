"""Fase 4: menciones de repos en Hacker News.

Pasos de cada corrida:
  1. Busca en la API de Algolia las historias de los últimos días con link a GitHub
     y los "Show HN" que traen el link en el texto.
  2. Valida la respuesta (loop de verificación, hasta 3 intentos).
  3. Extrae los repos y registra cada mención (directa si el repo ya está en la base, pendiente si no).
  4. Confirma pendientes en la API de GitHub y verifica que ninguna mención se haya perdido.

Uso:  uv run radar-hackernews [--dias N]
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
from datetime import date
from pathlib import Path

import httpx

from radar import db, menciones
from radar.config import RAIZ, cargar_config
from radar.github import GitHubClient

ALGOLIA = "https://hn.algolia.com/api/v1/search_by_date"
AJUSTES = RAIZ / "config" / "hackernews.toml"
INTENTOS = 3
ITEM_URL = "https://news.ycombinator.com/item?id={id}"


class HNError(Exception):
    pass


def cargar_ajustes(ruta: Path = AJUSTES) -> dict:
    return tomllib.loads(ruta.read_text(encoding="utf-8"))


def _get_json(client: httpx.Client, params: dict, dormir: Callable[[float], None]) -> dict:
    for intento in range(1, INTENTOS + 1):
        try:
            resp = client.get(ALGOLIA, params=params)
            if resp.status_code == 200:
                return resp.json()
            error = f"Algolia respondió {resp.status_code}"
        except (httpx.TransportError, ValueError) as e:
            error = f"Sin conexión o respuesta inválida de Algolia: {e}"
        if intento < INTENTOS:
            dormir(2 ** (intento - 1))
    raise HNError(error)


def _paginar(client: httpx.Client, params: dict, max_paginas: int, dormir) -> list[dict]:
    hits: list[dict] = []
    for pagina in range(max_paginas):
        datos = _get_json(client, {**params, "page": pagina, "hitsPerPage": 100}, dormir)
        hits.extend(datos.get("hits", []))
        if pagina + 1 >= datos.get("nbPages", 0):
            break
    return hits


def buscar_historias(client: httpx.Client, desde_ts: int, ajustes: dict, dormir=time.sleep) -> list[dict]:
    """Historias únicas (por objectID) de las dos búsquedas."""
    con_link = _paginar(
        client,
        {
            "query": "github.com",
            "restrictSearchableAttributes": "url",
            "tags": "story",
            "numericFilters": f"created_at_i>{desde_ts},points>={ajustes['puntos_min']}",
        },
        ajustes["max_paginas"],
        dormir,
    )
    show_hn = _paginar(
        client,
        {"tags": "show_hn", "numericFilters": f"created_at_i>{desde_ts},points>={ajustes['puntos_min_show_hn']}"},
        ajustes["max_paginas"],
        dormir,
    )
    unicas: dict[str, dict] = {}
    for h in con_link + show_hn:
        if "objectID" in h:
            unicas.setdefault(h["objectID"], h)
    return list(unicas.values())


def validar_historias(historias: list[dict], minimo: int) -> list[str]:
    problemas = []
    if len(historias) < minimo:
        problemas.append(f"Llegaron {len(historias)} historias de HN, se esperaban al menos {minimo}")
    rotas = [h.get("objectID", "?") for h in historias if not h.get("title") or "created_at_i" not in h]
    if rotas:
        problemas.append(f"Historias sin título o fecha (la API pudo cambiar): {', '.join(map(str, rotas[:5]))}")
    return problemas


def a_menciones(historias: list[dict]) -> list[menciones.Mencion]:
    resultado = []
    for h in historias:
        repos = menciones.extraer_repos(" ".join(filter(None, [h.get("url"), h.get("story_text")])))
        for full_name in repos:
            resultado.append(
                menciones.Mencion(
                    full_name=full_name,
                    fuente="hackernews",
                    post_url=ITEM_URL.format(id=h["objectID"]),
                    autor=h.get("author"),
                    texto=h.get("title"),
                    publicado=h.get("created_at"),
                    interaccion=h.get("points") or 0,
                )
            )
    return resultado


@dataclass
class Informe:
    historias: int = 0
    con_links: int = 0
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
    ahora_ts: int | None = None,
    hoy: date | None = None,
    dormir=time.sleep,
) -> Informe:
    ahora_ts = ahora_ts or int(time.time())
    hoy = hoy or date.today()
    inf = Informe()
    corrida = db.iniciar_corrida(con, "hackernews")
    try:
        desde = ahora_ts - ajustes["dias"] * 86_400
        historias: list[dict] = []
        for _ in range(INTENTOS):
            try:
                historias = buscar_historias(http, desde, ajustes, dormir)
            except HNError as e:
                inf.problemas = [str(e)]
                continue
            inf.problemas = validar_historias(historias, ajustes["minimo_historias"])
            if not inf.problemas:
                break
        if inf.problemas:
            return inf
        inf.historias = len(historias)

        lista = a_menciones(historias)
        inf.menciones = len(lista)
        inf.con_links = len({m.post_url for m in lista})
        limite = ajustes["max_resolver_sin_token"] if gh.anonimo else ajustes["max_resolver"]
        p = menciones.procesar(con, gh, lista, hoy, limite)
        inf.directas, inf.pendientes = p.directas, p.pendientes
        res = p.resolucion
        inf.resueltos, inf.descartados, inf.restantes = res.resueltos, res.descartados, res.restantes
        inf.problemas += res.problemas
        # Verificación final: lo descartado por 404 es esperado; cualquier otra pérdida es un error.
        if p.perdidas:
            inf.problemas.append(f"Menciones perdidas: {', '.join(p.perdidas[:5])}")
        return inf
    finally:
        db.cerrar_corrida(con, corrida, inf.ok, json.dumps(inf.__dict__, ensure_ascii=False))


def destacados(con: sqlite3.Connection, desde: str, limite: int) -> list[sqlite3.Row]:
    return con.execute(
        """
        SELECT r.full_name, r.url, r.estrellas, m.texto, m.interaccion, m.post_url
        FROM menciones m JOIN repos r ON r.id = m.repo_id
        WHERE m.fuente = 'hackernews' AND m.publicado >= ?
        ORDER BY m.interaccion DESC LIMIT ?
        """,
        (desde, limite),
    ).fetchall()


def main() -> int:
    parser = argparse.ArgumentParser(description="Menciones de repos en Hacker News")
    parser.add_argument("--dias", type=int, help="ventana hacia atrás (por defecto la de hackernews.toml)")
    parser.add_argument("--top", type=int, default=10, help="cuántas menciones destacadas mostrar")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    cfg = cargar_config()
    ajustes = cargar_ajustes()
    if args.dias:
        ajustes["dias"] = args.dias
    gh = GitHubClient(cfg.github_token)
    if gh.anonimo:
        print(f"⚠️  Modo sin token: se confirman hasta {ajustes['max_resolver_sin_token']} repos nuevos por corrida.\n")
    http = httpx.Client(timeout=20, headers={"User-Agent": "radar-repos"})
    con = db.conectar(cfg.database_path)
    try:
        db.aplicar_esquema(con)
        inf = ejecutar(http, gh, con, ajustes)
        desde = date.fromtimestamp(time.time() - ajustes["dias"] * 86_400).isoformat()
        top = destacados(con, desde, args.top)
    finally:
        http.close()
        gh.close()
        con.close()

    print(f"Historias: {inf.historias} · con link a GitHub: {inf.con_links} · menciones: {inf.menciones}")
    print(f"Directas: {inf.directas} · pendientes: {inf.pendientes} · confirmadas ahora: {inf.resueltos} · "
          f"descartadas (no existen): {inf.descartados} · siguen pendientes: {inf.restantes}\n")
    for r in top:
        print(f"🟧 {r['interaccion']:>4} pts · {r['full_name']:<40} {r['estrellas']:,} ★")
        print(f"   {r['texto'][:90]}")
        print(f"   {r['url']}  ·  {r['post_url']}")
    for p in inf.problemas:
        print(f"❌ {p}")
    print("\nHacker News actualizado." if inf.ok else "\nHubo fallas en Hacker News.")
    return 0 if inf.ok else 1


if __name__ == "__main__":
    sys.exit(main())
