"""Fase 4: menciones de repos en Reddit (feeds RSS públicos, sin cuenta).

Pasos de cada corrida:
  1. Lee el RSS "top de la semana" de cada subreddit configurado y de la búsqueda global url:github.com,
     con pausa entre feeds porque Reddit limita fuerte las peticiones anónimas.
  2. Valida cada feed (Atom legible y con entradas); reintenta ante 429 o errores del servidor.
  3. Extrae los repos y los procesa con menciones.procesar (registrar, confirmar y verificar pérdidas).

El RSS no trae votos: la interacción es la posición inversa en el top (el primero de 25 vale 25).

Uso:  uv run radar-reddit
Sale con código 0 si se leyó al menos `cobertura_minima` de los feeds y no se perdió ninguna mención.
"""

import argparse
import json
import sqlite3
import sys
import time
import tomllib
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from urllib.parse import quote

import httpx

from radar import db, menciones
from radar.config import RAIZ, cargar_config
from radar.github import GitHubClient

AJUSTES = RAIZ / "config" / "redes.toml"
ATOM = {"a": "http://www.w3.org/2005/Atom"}
INTENTOS = 3
UA = "radar-repos/0.1 (monitor de repos open source)"


class RedditError(Exception):
    pass


def cargar_ajustes(ruta: Path = AJUSTES) -> dict:
    return tomllib.loads(ruta.read_text(encoding="utf-8"))["reddit"]


def feeds(ajustes: dict) -> list[tuple[str, str]]:
    """(nombre, url) de cada feed a leer."""
    t = ajustes["periodo"]
    lista = []
    if ajustes.get("busqueda"):
        q = quote(ajustes["busqueda"])
        lista.append(("busqueda", f"https://www.reddit.com/search/.rss?q={q}&sort=top&t={t}&limit=100"))
    lista += [(f"r/{s}", f"https://www.reddit.com/r/{s}/top/.rss?t={t}&limit=100") for s in ajustes["subreddits"]]
    return lista


def descargar(client: httpx.Client, url: str, espera_429: float, dormir: Callable[[float], None]) -> str:
    error = ""
    for intento in range(1, INTENTOS + 1):
        try:
            resp = client.get(url)
        except httpx.TransportError as e:
            error = f"sin conexión: {e}"
        else:
            if resp.status_code == 200:
                return resp.text
            error = f"Reddit respondió {resp.status_code}"
            if resp.status_code not in (429,) and resp.status_code < 500:
                break  # 403/404: reintentar no ayuda
            if resp.status_code == 429:
                dormir(float(resp.headers.get("retry-after", espera_429)))
                continue
        if intento < INTENTOS:
            dormir(2 ** intento)
    raise RedditError(error)


@dataclass
class Entrada:
    titulo: str
    url: str
    autor: str | None
    publicado: str | None
    contenido: str


def parsear(xml_texto: str) -> list[Entrada]:
    try:
        raiz = ET.fromstring(xml_texto)
    except ET.ParseError as e:
        raise RedditError(f"El feed no es XML válido: {e}") from e
    if raiz.tag != "{http://www.w3.org/2005/Atom}feed":
        raise RedditError("El feed no es Atom (Reddit pudo cambiar el formato)")
    entradas = []
    for e in raiz.findall("a:entry", ATOM):
        link = e.find("a:link", ATOM)
        titulo = e.find("a:title", ATOM)
        if link is None or titulo is None:
            continue
        autor = e.find("a:author/a:name", ATOM)
        publicado = e.find("a:published", ATOM)
        contenido = e.find("a:content", ATOM)
        entradas.append(
            Entrada(
                titulo=titulo.text or "",
                url=link.get("href", ""),
                autor=autor.text if autor is not None else None,
                publicado=publicado.text if publicado is not None else None,
                contenido=(contenido.text or "") if contenido is not None else "",
            )
        )
    return entradas


def a_menciones(entradas: list[Entrada]) -> list[menciones.Mencion]:
    total = len(entradas)
    resultado = []
    for posicion, e in enumerate(entradas):
        for full_name in menciones.extraer_repos(f"{e.contenido} {e.titulo}"):
            resultado.append(
                menciones.Mencion(
                    full_name=full_name, fuente="reddit", post_url=e.url, autor=e.autor,
                    texto=e.titulo, publicado=e.publicado, interaccion=total - posicion,
                )
            )
    return resultado


@dataclass
class Informe:
    feeds: int = 0
    feeds_ok: int = 0
    entradas: int = 0
    menciones: int = 0
    directas: int = 0
    pendientes: int = 0
    resueltos: int = 0
    descartados: int = 0
    restantes: int = 0
    segundos_espera: float = 0.0  # pausas entre feeds + esperas por límite (429)
    fallidos: list[str] = field(default_factory=list)  # feeds que no se pudieron leer (aviso)
    problemas: list[str] = field(default_factory=list)  # fallas que hacen fallar la corrida

    @property
    def ok(self) -> bool:
        return not self.problemas


def ejecutar(
    http: httpx.Client,
    gh: GitHubClient,
    con: sqlite3.Connection,
    ajustes: dict,
    hoy: date | None = None,
    dormir: Callable[[float], None] = time.sleep,
) -> Informe:
    hoy = hoy or date.today()
    inf = Informe()
    corrida = db.iniciar_corrida(con, "reddit")
    dormir_original = dormir

    def dormir(segundos: float) -> None:  # noqa: F811 — misma función, pero lleva la cuenta de lo esperado
        inf.segundos_espera += segundos
        dormir_original(segundos)

    try:
        lista_feeds = feeds(ajustes)
        inf.feeds = len(lista_feeds)
        todas: list[menciones.Mencion] = []
        for i, (nombre, url) in enumerate(lista_feeds):
            if i:
                dormir(ajustes["pausa_segundos"])
            try:
                entradas = parsear(descargar(http, url, ajustes["espera_429"], dormir))
            except RedditError as e:
                inf.fallidos.append(f"{nombre}: {e}")
                continue
            if not entradas:
                inf.fallidos.append(f"{nombre}: feed vacío")
                continue
            inf.feeds_ok += 1
            inf.entradas += len(entradas)
            todas += a_menciones(entradas)

        if inf.feeds and inf.feeds_ok / inf.feeds < ajustes["cobertura_minima"]:
            inf.problemas.append(
                f"Solo se leyeron {inf.feeds_ok}/{inf.feeds} feeds (mínimo {ajustes['cobertura_minima']:.0%})"
            )

        # Un mismo post puede salir en la búsqueda y en su subreddit: se queda la mención con más interacción.
        unicas: dict[tuple[str, str], menciones.Mencion] = {}
        for m in todas:
            clave = (m.full_name.lower(), m.post_url)
            if clave not in unicas or m.interaccion > unicas[clave].interaccion:
                unicas[clave] = m
        lista = list(unicas.values())
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
    argparse.ArgumentParser(description="Menciones de repos en Reddit").parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    cfg = cargar_config()
    ajustes = cargar_ajustes()
    gh = GitHubClient(cfg.github_token)
    n = len(feeds(ajustes))
    print(f"Leyendo {n} feeds de Reddit con {ajustes['pausa_segundos']} s de pausa (≈{n * ajustes['pausa_segundos'] // 60 + 1} min)…\n")
    http = httpx.Client(timeout=20, headers={"User-Agent": UA}, follow_redirects=True)
    con = db.conectar(cfg.database_path)
    try:
        db.aplicar_esquema(con)
        inf = ejecutar(http, gh, con, ajustes)
    finally:
        http.close()
        gh.close()
        con.close()

    print(f"Feeds leídos: {inf.feeds_ok}/{inf.feeds} · entradas: {inf.entradas} · menciones: {inf.menciones} · "
          f"esperas: {inf.segundos_espera:.0f} s ({inf.feeds - 1} pausas de {ajustes['pausa_segundos']} s + límites de Reddit)")
    print(f"Directas: {inf.directas} · pendientes: {inf.pendientes} · confirmadas ahora: {inf.resueltos} · "
          f"descartadas: {inf.descartados} · siguen pendientes (todas las redes): {inf.restantes}")
    for f in inf.fallidos:
        print(f"⚠️  {f}")
    for p in inf.problemas:
        print(f"❌ {p}")
    print("\nReddit actualizado." if inf.ok else "\nHubo fallas en Reddit.")
    return 0 if inf.ok else 1


if __name__ == "__main__":
    sys.exit(main())
