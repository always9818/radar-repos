"""Piezas comunes a las fuentes sociales (HN, Reddit, Bluesky, X, TikTok).

1. Extraer links github.com/owner/repo de un texto.
2. Registrar cada mención: si el repo ya está en la base va directo a `menciones`;
   si no, queda en `menciones_pendientes` hasta confirmarlo en la API de GitHub.
3. Resolver pendientes con un tope de peticiones por corrida.
"""

import html
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date

from radar import db, ranking
from radar.github import GitHubClient, GitHubError, LimiteAgotado, NoEncontrado

# Primer segmento de github.com/<x>/... que no es un usuario u organización.
RESERVADOS = {
    "about", "apps", "blog", "collections", "contact", "customer-stories", "enterprise", "events",
    "explore", "features", "issues", "login", "marketplace", "new", "notifications", "orgs",
    "organizations", "pricing", "pulls", "readme", "search", "security", "settings", "site",
    "sponsors", "topics", "trending", "users", "join", "signup", "codespaces", "copilot", "solutions",
}
# El lookbehind evita subdominios como gist.github.com o x.github.com.
PATRON = re.compile(r"(?<![\w.-])(?:https?://)?(?:www\.)?github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/([A-Za-z0-9._-]{1,100})", re.I)
MAX_INTENTOS = 5


def extraer_repos(texto: str | None) -> list[str]:
    """'owner/repo' únicos (sin distinguir mayúsculas) en el orden en que aparecen."""
    if not texto:
        return []
    texto = html.unescape(texto)
    vistos: dict[str, str] = {}
    for owner, nombre in PATRON.findall(texto):
        nombre = nombre.rstrip(".,;:")
        if nombre.lower().endswith(".git"):
            nombre = nombre[:-4]
        if owner.lower() in RESERVADOS or nombre in ("", ".", ".."):
            continue
        full_name = f"{owner}/{nombre}"
        vistos.setdefault(full_name.lower(), full_name)
    return list(vistos.values())


@dataclass
class Mencion:
    full_name: str
    fuente: str
    post_url: str
    autor: str | None = None
    texto: str | None = None
    publicado: str | None = None
    interaccion: int = 0


def _repo_id(con: sqlite3.Connection, full_name: str) -> int | None:
    fila = con.execute("SELECT id FROM repos WHERE lower(full_name) = lower(?)", (full_name,)).fetchone()
    return fila[0] if fila else None


def _insertar_mencion(con: sqlite3.Connection, repo_id: int, m: Mencion, marca: str) -> None:
    con.execute(
        """
        INSERT INTO menciones (repo_id, fuente, post_url, autor, texto, publicado, interaccion, capturado)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (repo_id, post_url) DO UPDATE SET interaccion = excluded.interaccion,
                                                      capturado = excluded.capturado
        """,
        (repo_id, m.fuente, m.post_url, m.autor, m.texto, m.publicado, m.interaccion, marca),
    )


def registrar(con: sqlite3.Connection, menciones: list[Mencion]) -> tuple[int, int]:
    """Devuelve (directas a `menciones`, enviadas a pendientes)."""
    marca = db.ahora()
    directas = pendientes = 0
    for m in menciones:
        repo_id = _repo_id(con, m.full_name)
        if repo_id is not None:
            _insertar_mencion(con, repo_id, m, marca)
            directas += 1
            continue
        con.execute(
            """
            INSERT INTO menciones_pendientes (full_name, fuente, post_url, autor, texto, publicado, interaccion, capturado)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (full_name, post_url) DO UPDATE SET interaccion = excluded.interaccion,
                                                           capturado = excluded.capturado
            """,
            (m.full_name, m.fuente, m.post_url, m.autor, m.texto, m.publicado, m.interaccion, marca),
        )
        pendientes += 1
    con.commit()
    return directas, pendientes


@dataclass
class Resolucion:
    resueltos: int = 0
    descartados: int = 0
    restantes: int = 0
    problemas: list[str] = field(default_factory=list)
    ids: dict[str, int] = field(default_factory=dict)  # nombre mencionado (minúsculas) → id; cubre renombrados
    nombres_descartados: set[str] = field(default_factory=set)


def _mover_pendientes(con: sqlite3.Connection, full_name: str, repo_id: int) -> None:
    filas = con.execute("SELECT * FROM menciones_pendientes WHERE lower(full_name) = lower(?)", (full_name,)).fetchall()
    for f in filas:
        m = Mencion(f["full_name"], f["fuente"], f["post_url"], f["autor"], f["texto"], f["publicado"], f["interaccion"])
        _insertar_mencion(con, repo_id, m, f["capturado"])
    con.execute("DELETE FROM menciones_pendientes WHERE lower(full_name) = lower(?)", (full_name,))


def resolver_pendientes(gh: GitHubClient, con: sqlite3.Connection, hoy: date, limite: int) -> Resolucion:
    """Confirma en la API los repos pendientes con más interacción primero."""
    res = Resolucion()
    nombres = [
        f[0]
        for f in con.execute(
            """
            SELECT MIN(full_name) FROM menciones_pendientes
            GROUP BY lower(full_name) ORDER BY MAX(interaccion) DESC LIMIT ?
            """,
            (limite,),
        )
    ]
    for nombre in nombres:
        repo_id = _repo_id(con, nombre)  # otra fuente pudo haberlo traído ya
        if repo_id is None:
            try:
                item = gh.obtener_repo(nombre)
            except NoEncontrado:
                con.execute("DELETE FROM menciones_pendientes WHERE lower(full_name) = lower(?)", (nombre,))
                con.commit()
                res.descartados += 1
                res.nombres_descartados.add(nombre.lower())
                continue
            except LimiteAgotado as e:
                res.problemas.append(f"Resolución detenida: {e}")
                break
            except GitHubError as e:
                con.execute(
                    "UPDATE menciones_pendientes SET intentos = intentos + 1 WHERE lower(full_name) = lower(?)", (nombre,)
                )
                con.execute("DELETE FROM menciones_pendientes WHERE intentos >= ?", (MAX_INTENTOS,))
                con.commit()
                res.problemas.append(f"{nombre}: {e}")
                continue
            ranking.guardar_repos(con, [ranking.normalizar(item)], hoy)
            repo_id = item["id"]
        _mover_pendientes(con, nombre, repo_id)
        con.commit()
        res.resueltos += 1
        res.ids[nombre.lower()] = repo_id
    res.restantes = con.execute("SELECT COUNT(DISTINCT lower(full_name)) FROM menciones_pendientes").fetchone()[0]
    return res


@dataclass
class Procesado:
    directas: int
    pendientes: int
    resolucion: Resolucion
    perdidas: list[str]


def procesar(con: sqlite3.Connection, gh: GitHubClient, lista: list[Mencion], hoy: date, limite: int) -> Procesado:
    """Registra, confirma pendientes y verifica que ninguna mención se haya perdido."""
    directas, pendientes = registrar(con, lista)
    res = resolver_pendientes(gh, con, hoy, limite)
    return Procesado(directas, pendientes, res, perdidas(con, lista, res))


def perdidas(con: sqlite3.Connection, lista: list[Mencion], res: Resolucion) -> list[str]:
    """Menciones extraídas que no quedaron ni en `menciones` ni en pendientes, ni se descartaron por 404."""
    faltan = []
    for m in lista:
        clave = m.full_name.lower()
        if clave in res.nombres_descartados:
            continue
        repo_id = res.ids.get(clave) or _repo_id(con, m.full_name)
        guardada = repo_id is not None and con.execute(
            "SELECT 1 FROM menciones WHERE repo_id = ? AND post_url = ?", (repo_id, m.post_url)
        ).fetchone()
        pendiente = con.execute(
            "SELECT 1 FROM menciones_pendientes WHERE lower(full_name) = ? AND post_url = ?", (clave, m.post_url)
        ).fetchone()
        if not guardada and not pendiente:
            faltan.append(f"{m.full_name} ({m.post_url})")
    return faltan
