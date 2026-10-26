"""Fase 1: recolector de ranking de GitHub.

Por cada consulta de config/consultas.toml:
  1. Busca los repos ordenados por estrellas.
  2. Valida el lote (loop de verificación). Si no pasa, reintenta hasta 3 veces.
  3. Guarda repos, la foto de estrellas del día y la mención 'github_ranking'.
Cada corrida queda en la tabla `corridas`.

Uso:  uv run radar-ranking [--consulta NOMBRE] [--sin-guardar]
Sale con código 0 si todas las consultas pasaron la validación y 1 si alguna falló.
"""

import argparse
import json
import random
import sqlite3
import sys
import tomllib
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlencode

from radar import db
from radar.config import RAIZ, cargar_config
from radar.github import GitHubClient, GitHubError, LimiteAgotado

CONSULTAS = RAIZ / "config" / "consultas.toml"
INTENTOS_VALIDACION = 3


@dataclass
class Consulta:
    nombre: str
    q: str
    paginas: int = 1
    minimo: int = 1


@dataclass
class Ajustes:
    excluir_forks: bool = True
    excluir_archivados: bool = True
    muestra_urls: int = 3
    consultas: list[Consulta] = field(default_factory=list)


@dataclass
class ResultadoConsulta:
    nombre: str
    ok: bool
    recibidos: int = 0
    guardados: int = 0
    nuevos: int = 0
    intentos: int = 0
    problemas: list[str] = field(default_factory=list)


def cargar_ajustes(ruta: Path = CONSULTAS, hoy: date | None = None) -> Ajustes:
    hoy = hoy or date.today()
    fechas = {f"hace_{d}d": (hoy - timedelta(days=d)).isoformat() for d in (7, 30, 90)}
    datos = tomllib.loads(ruta.read_text(encoding="utf-8"))
    general = datos.get("general", {})
    consultas = [
        Consulta(nombre=c["nombre"], q=c["q"].format(**fechas), paginas=c.get("paginas", 1), minimo=c.get("minimo", 1))
        for c in datos.get("consulta", [])
    ]
    nombres = [c.nombre for c in consultas]
    if len(nombres) != len(set(nombres)):
        raise ValueError("Hay consultas con nombre repetido en consultas.toml")
    return Ajustes(
        excluir_forks=general.get("excluir_forks", True),
        excluir_archivados=general.get("excluir_archivados", True),
        muestra_urls=general.get("muestra_urls", 3),
        consultas=consultas,
    )


def normalizar(item: dict) -> dict:
    return {
        "id": item["id"],
        "full_name": item["full_name"],
        "url": item["html_url"],
        "descripcion_gh": item.get("description"),
        "lenguaje": item.get("language"),
        "topics": json.dumps(item.get("topics", []), ensure_ascii=False),
        "estrellas": item.get("stargazers_count", 0),
        "forks": item.get("forks_count", 0),
        "es_fork": int(bool(item.get("fork"))),
        "archivado": int(bool(item.get("archived"))),
        "creado_gh": item.get("created_at"),
        "ultimo_push_gh": item.get("pushed_at"),
    }


def validar(repos: list[dict], minimo: int) -> list[str]:
    """Problemas de forma del lote. Lista vacía = lote sano."""
    problemas = []
    if len(repos) < minimo:
        problemas.append(f"Llegaron {len(repos)} repos, se esperaban al menos {minimo}")
    ids = [r["id"] for r in repos]
    if len(ids) != len(set(ids)):
        problemas.append(f"{len(ids) - len(set(ids))} repos duplicados en el lote")
    malas = [r["full_name"] for r in repos if r["url"] != f"https://github.com/{r['full_name']}"]
    if malas:
        problemas.append(f"URLs que no coinciden con el nombre: {', '.join(malas[:5])}")
    sin_estrellas = [r["full_name"] for r in repos if r["estrellas"] <= 0]
    if sin_estrellas:
        problemas.append(f"Repos sin estrellas en un ranking: {', '.join(sin_estrellas[:5])}")
    return problemas


def url_busqueda(q: str) -> str:
    return "https://github.com/search?" + urlencode({"q": q, "type": "repositories", "s": "stars", "o": "desc"})


def guardar(con: sqlite3.Connection, repos: list[dict], consulta: Consulta, hoy: date) -> int:
    """Guarda repos, foto del día y mención de la consulta. Devuelve cuántos eran nuevos para el radar."""
    nuevos = guardar_repos(con, repos, hoy)
    marca = db.ahora()
    for r in repos:
        # Una mención por repo y consulta; se refresca en cada corrida. interaccion = estrellas al capturar.
        con.execute(
            """
            INSERT INTO menciones (repo_id, fuente, post_url, autor, texto, interaccion, capturado)
            VALUES (?, 'github_ranking', ?, NULL, ?, ?, ?)
            ON CONFLICT (repo_id, post_url) DO UPDATE SET interaccion = excluded.interaccion,
                                                          capturado = excluded.capturado
            """,
            (r["id"], url_busqueda(consulta.q), consulta.nombre, r["estrellas"], marca),
        )
    con.commit()
    return nuevos


def guardar_repos(con: sqlite3.Connection, repos: list[dict], hoy: date) -> int:
    """Inserta o actualiza los repos y su foto de estrellas del día. Devuelve cuántos eran nuevos."""
    marca = db.ahora()
    existentes = {
        fila[0]
        for fila in con.execute(
            f"SELECT id FROM repos WHERE id IN ({','.join('?' * len(repos))})", [r["id"] for r in repos]
        )
    } if repos else set()
    for r in repos:
        con.execute(
            """
            INSERT INTO repos (id, full_name, url, descripcion_gh, lenguaje, topics, estrellas, forks,
                               es_fork, archivado, creado_gh, ultimo_push_gh, primera_vez, ultima_vez)
            VALUES (:id, :full_name, :url, :descripcion_gh, :lenguaje, :topics, :estrellas, :forks,
                    :es_fork, :archivado, :creado_gh, :ultimo_push_gh, :marca, :marca)
            ON CONFLICT (id) DO UPDATE SET
                full_name = excluded.full_name, url = excluded.url, descripcion_gh = excluded.descripcion_gh,
                lenguaje = excluded.lenguaje, topics = excluded.topics, estrellas = excluded.estrellas,
                forks = excluded.forks, es_fork = excluded.es_fork, archivado = excluded.archivado,
                ultimo_push_gh = excluded.ultimo_push_gh, ultima_vez = excluded.ultima_vez
            """,
            {**r, "marca": marca},
        )
        con.execute(
            """
            INSERT INTO snapshots (repo_id, fecha, estrellas, forks) VALUES (?, ?, ?, ?)
            ON CONFLICT (repo_id, fecha) DO UPDATE SET estrellas = excluded.estrellas, forks = excluded.forks
            """,
            (r["id"], hoy.isoformat(), r["estrellas"], r["forks"]),
        )
    con.commit()
    return len([r for r in repos if r["id"] not in existentes])


def recolectar_consulta(gh: GitHubClient, consulta: Consulta, ajustes: Ajustes) -> tuple[list[dict], ResultadoConsulta]:
    """Busca y valida con hasta 3 intentos. Devuelve los repos filtrados y el resultado."""
    res = ResultadoConsulta(consulta.nombre, ok=False)
    for intento in range(1, INTENTOS_VALIDACION + 1):
        res.intentos = intento
        crudos = [normalizar(i) for i in gh.buscar_repos(consulta.q, paginas=consulta.paginas)]
        res.recibidos = len(crudos)
        problemas = validar(crudos, consulta.minimo)
        # Muestra al azar: que los repos existan de verdad (la búsqueda puede traer índices viejos).
        if not problemas and crudos:
            for r in random.sample(crudos, min(ajustes.muestra_urls, len(crudos))):
                if not gh.repo_existe(r["full_name"]):
                    problemas.append(f"El repo {r['full_name']} no responde en la API")
        if not problemas:
            repos = [
                r
                for r in crudos
                if not (ajustes.excluir_forks and r["es_fork"]) and not (ajustes.excluir_archivados and r["archivado"])
            ]
            res.ok, res.problemas = True, []
            return repos, res
        res.problemas = problemas
    return [], res


def ejecutar(
    gh: GitHubClient,
    con: sqlite3.Connection | None,
    ajustes: Ajustes,
    hoy: date | None = None,
    solo: str | None = None,
) -> list[ResultadoConsulta]:
    hoy = hoy or date.today()
    consultas = [c for c in ajustes.consultas if solo is None or c.nombre == solo]
    if not consultas:
        raise ValueError(f"No existe la consulta '{solo}'")
    corrida = db.iniciar_corrida(con, "ranking") if con else None
    resultados: list[ResultadoConsulta] = []
    try:
        for i, consulta in enumerate(consultas):
            try:
                repos, res = recolectar_consulta(gh, consulta, ajustes)
            except LimiteAgotado as e:
                # Sin límite no tiene sentido seguir con las demás consultas.
                resultados.append(ResultadoConsulta(consulta.nombre, ok=False, problemas=[str(e)]))
                resultados.extend(
                    ResultadoConsulta(c.nombre, ok=False, problemas=["No se corrió: límite agotado"])
                    for c in consultas[i + 1 :]
                )
                break
            except GitHubError as e:
                resultados.append(ResultadoConsulta(consulta.nombre, ok=False, problemas=[str(e)]))
                continue
            if res.ok and con:
                res.nuevos = guardar(con, repos, consulta, hoy)
            res.guardados = len(repos) if res.ok else 0
            resultados.append(res)
    finally:
        if con and corrida:
            ok = len(resultados) == len(consultas) and all(r.ok for r in resultados)
            detalle = json.dumps([r.__dict__ for r in resultados], ensure_ascii=False)
            db.cerrar_corrida(con, corrida, ok, detalle)
    return resultados


def main() -> int:
    parser = argparse.ArgumentParser(description="Recolector de ranking de GitHub")
    parser.add_argument("--consulta", help="correr solo esta consulta (por nombre)")
    parser.add_argument("--sin-guardar", action="store_true", help="buscar y validar sin escribir en la base")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    cfg = cargar_config()
    ajustes = cargar_ajustes()
    gh = GitHubClient(cfg.github_token)
    if gh.anonimo:
        # Sin token solo hay 60 peticiones/hora: se verifica 1 repo por consulta en lugar de varios.
        ajustes.muestra_urls = min(ajustes.muestra_urls, 1)
        print("⚠️  Modo sin token: 10 búsquedas/min y 60 peticiones/hora. Puede tardar unos minutos.\n")
    con = None if args.sin_guardar else db.conectar(cfg.database_path)
    try:
        if con:
            db.aplicar_esquema(con)
        resultados = ejecutar(gh, con, ajustes, solo=args.consulta)
    finally:
        gh.close()
        if con:
            con.close()

    for r in resultados:
        if r.ok:
            print(f"✅ {r.nombre:<16} {r.guardados} repos ({r.nuevos} nuevos) · intento {r.intentos}")
        else:
            print(f"❌ {r.nombre:<16} {' | '.join(r.problemas)}")
    todo_ok = all(r.ok for r in resultados)
    print("\nRanking actualizado." if todo_ok else "\nHubo fallas en el ranking.")
    return 0 if todo_ok else 1


if __name__ == "__main__":
    sys.exit(main())
