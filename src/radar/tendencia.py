"""Fase 2: detector de tendencia.

Pasos de cada corrida:
  1. Lee github.com/trending (sin gastar peticiones de la API) y guarda esos repos con su foto del día.
  2. Refresca uno a uno los repos seguidos que hoy no tienen foto (por id, vía API).
  3. Calcula velocidad y aceleración de estrellas y clasifica cada repo.
  4. Loop de verificación: trending legible, cobertura de fotos de hoy y un score por cada repo con foto.

Uso:  uv run radar-tendencia [--sin-trending] [--top N]
Sale con código 0 si todas las verificaciones pasaron y 1 si alguna falló.
"""

import argparse
import html
import json
import re
import sqlite3
import sys
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from radar import db, ranking
from radar.config import RAIZ, cargar_config
from radar.github import GitHubClient, GitHubError, LimiteAgotado

UMBRALES = RAIZ / "config" / "tendencia.toml"
TRENDING_URL = "https://github.com/trending?since={periodo}"
INTENTOS_TRENDING = 3


def cargar_umbrales(ruta: Path = UMBRALES) -> dict:
    return tomllib.loads(ruta.read_text(encoding="utf-8"))


# ---------- GitHub Trending ----------

@dataclass
class RepoTrending:
    id: int
    full_name: str
    descripcion: str | None
    lenguaje: str | None
    estrellas: int
    forks: int
    estrellas_periodo: int
    periodo: str


def _numero(texto: str | None) -> int:
    return int(texto.replace(",", "")) if texto else 0


def parsear_trending(pagina: str, periodo: str = "daily") -> list[RepoTrending]:
    repos = []
    for bloque in re.findall(r'<article class="Box-row">(.*?)</article>', pagina, re.S):
        nombre = re.search(r'<h2[^>]*>\s*<a[^>]*?href="/([^"/]+/[^"/]+)"', bloque)
        repo_id = re.search(r"&quot;repository_id&quot;:(\d+)", bloque) or re.search(
            r"&quot;record_id&quot;:(\d+)", bloque
        )
        if not nombre or not repo_id:
            continue
        full_name = nombre.group(1)
        desc = re.search(r'<p class="col-9[^"]*">\s*(.*?)\s*</p>', bloque, re.S)
        lenguaje = re.search(r'itemprop="programmingLanguage">([^<]+)<', bloque)
        estrellas = re.search(rf'href="/{re.escape(full_name)}/stargazers".*?</svg>\s*([\d,]+)', bloque, re.S)
        forks = re.search(rf'href="/{re.escape(full_name)}/forks".*?</svg>\s*([\d,]+)', bloque, re.S)
        periodo_txt = re.search(r"([\d,]+)\s+stars?\s+(?:today|this week|this month)", bloque)
        repos.append(
            RepoTrending(
                id=int(repo_id.group(1)),
                full_name=full_name,
                descripcion=html.unescape(re.sub(r"<[^>]+>", "", desc.group(1))).strip() if desc else None,
                lenguaje=lenguaje.group(1).strip() if lenguaje else None,
                estrellas=_numero(estrellas.group(1) if estrellas else None),
                forks=_numero(forks.group(1) if forks else None),
                estrellas_periodo=_numero(periodo_txt.group(1) if periodo_txt else None),
                periodo=periodo,
            )
        )
    return repos


def validar_trending(repos: list[RepoTrending], minimo: int) -> list[str]:
    problemas = []
    if len(repos) < minimo:
        problemas.append(f"Trending trajo {len(repos)} repos (mínimo {minimo}); la página pudo cambiar de estructura")
    ids = [r.id for r in repos]
    if len(ids) != len(set(ids)):
        problemas.append("Repos duplicados en trending")
    sin_estrellas = [r.full_name for r in repos if r.estrellas <= 0]
    if sin_estrellas:
        problemas.append(f"No se pudieron leer las estrellas de: {', '.join(sin_estrellas[:5])}")
    return problemas


def recolectar_trending(gh: GitHubClient, periodo: str, minimo: int) -> tuple[list[RepoTrending], list[str]]:
    problemas: list[str] = []
    for _ in range(INTENTOS_TRENDING):
        try:
            repos = parsear_trending(gh.pagina_html(TRENDING_URL.format(periodo=periodo)), periodo)
        except GitHubError as e:
            problemas = [f"No se pudo abrir trending ({periodo}): {e}"]
            continue
        problemas = validar_trending(repos, minimo)
        if not problemas:
            return repos, []
    return [], problemas


def guardar_trending(con: sqlite3.Connection, repos: list[RepoTrending], hoy: date) -> None:
    """La página no trae topics ni fecha de creación: si el repo ya existe, no se pisan esos datos."""
    marca = db.ahora()
    for r in repos:
        con.execute(
            """
            INSERT INTO repos (id, full_name, url, descripcion_gh, lenguaje, estrellas, forks, primera_vez, ultima_vez)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET
                full_name = excluded.full_name, url = excluded.url,
                descripcion_gh = COALESCE(excluded.descripcion_gh, repos.descripcion_gh),
                lenguaje = COALESCE(excluded.lenguaje, repos.lenguaje),
                estrellas = excluded.estrellas, forks = excluded.forks, ultima_vez = excluded.ultima_vez
            """,
            (r.id, r.full_name, f"https://github.com/{r.full_name}", r.descripcion, r.lenguaje,
             r.estrellas, r.forks, marca, marca),
        )
        con.execute(
            """
            INSERT INTO snapshots (repo_id, fecha, estrellas, forks) VALUES (?, ?, ?, ?)
            ON CONFLICT (repo_id, fecha) DO UPDATE SET estrellas = excluded.estrellas, forks = excluded.forks
            """,
            (r.id, hoy.isoformat(), r.estrellas, r.forks),
        )
        unidad = {"daily": "today", "weekly": "this week", "monthly": "this month"}[r.periodo]
        con.execute(
            """
            INSERT INTO menciones (repo_id, fuente, post_url, texto, interaccion, capturado)
            VALUES (?, 'github_trending', ?, ?, ?, ?)
            ON CONFLICT (repo_id, post_url) DO UPDATE SET texto = excluded.texto,
                interaccion = excluded.interaccion, capturado = excluded.capturado
            """,
            (r.id, TRENDING_URL.format(periodo=r.periodo), f"{r.estrellas_periodo} stars {unidad}",
             r.estrellas_periodo, marca),
        )
    con.commit()


# ---------- Cobertura de fotos ----------

def _desde(hoy: date, dias: int) -> str:
    return (hoy - timedelta(days=dias)).isoformat()


def repos_sin_foto(con: sqlite3.Connection, hoy: date, dias: int, limite: int) -> list[int]:
    filas = con.execute(
        """
        SELECT id FROM repos
        WHERE ultima_vez >= ? AND archivado = 0
          AND id NOT IN (SELECT repo_id FROM snapshots WHERE fecha = ?)
        ORDER BY estrellas DESC LIMIT ?
        """,
        (_desde(hoy, dias), hoy.isoformat(), limite),
    )
    return [f[0] for f in filas]


def refrescar_faltantes(gh: GitHubClient, con: sqlite3.Connection, hoy: date, dias: int, limite: int) -> tuple[int, list[str]]:
    refrescados, problemas = 0, []
    for repo_id in repos_sin_foto(con, hoy, dias, limite):
        try:
            item = gh.obtener_repo_por_id(repo_id)
        except LimiteAgotado as e:
            problemas.append(f"Refresco detenido: {e}")
            break
        except GitHubError as e:
            problemas.append(f"Repo {repo_id}: {e}")
            continue
        if item is None:  # borrado o privado: deja de seguirse
            con.execute("UPDATE repos SET archivado = 1 WHERE id = ?", (repo_id,))
            con.commit()
            continue
        ranking.guardar_repos(con, [ranking.normalizar(item)], hoy)
        refrescados += 1
    return refrescados, problemas


def cobertura(con: sqlite3.Connection, hoy: date, dias: int) -> tuple[int, int]:
    """(repos seguidos con foto de hoy, repos seguidos)."""
    seguidos, con_foto = con.execute(
        """
        SELECT COUNT(*), COUNT(s.repo_id) FROM repos r
        LEFT JOIN snapshots s ON s.repo_id = r.id AND s.fecha = ?
        WHERE r.ultima_vez >= ? AND r.archivado = 0
        """,
        (hoy.isoformat(), _desde(hoy, dias)),
    ).fetchone()
    return con_foto, seguidos


# ---------- Cálculo ----------

def clasificar(
    serie: list[tuple[date, int]],
    hoy: date,
    umbrales: dict,
    creado_gh: str | None = None,
    trending_hoy: int | None = None,
) -> dict:
    """Clasifica un repo a partir de sus fotos diarias (ordenadas, la última debe ser la de hoy)."""
    serie = sorted(serie)
    if not serie or serie[-1][0] != hoy:
        raise ValueError("La serie debe terminar en la foto de hoy")
    estrellas_hoy = serie[-1][1]
    delta = base = score = None

    if len(serie) >= 2:
        fecha_prev, estrellas_prev = serie[-2]
        delta = (estrellas_hoy - estrellas_prev) / max((hoy - fecha_prev).days, 1)
        ventana = [(f, e) for f, e in serie[:-1] if f >= fecha_prev - timedelta(days=7)]
        if len(ventana) >= 2:
            dias = max((ventana[-1][0] - ventana[0][0]).days, 1)
            base = max((ventana[-1][1] - ventana[0][1]) / dias, 0.0)
            score = delta / max(base, umbrales["piso_base"])
    elif trending_hoy is not None:
        delta = float(trending_hoy)  # sin historial propio, se usa el "N stars today" de la página

    exp, alza, nuevo = umbrales["explosivo"], umbrales["en_alza"], umbrales["nuevo_rapido"]
    es_nuevo_rapido = False
    if creado_gh:
        edad = (hoy - datetime.fromisoformat(creado_gh.replace("Z", "+00:00")).date()).days
        if edad <= nuevo["edad_max_dias"]:
            es_nuevo_rapido = estrellas_hoy / max(edad, 1) >= nuevo["estrellas_por_dia_min"]

    if score is not None and score >= exp["score_min"] and delta >= exp["delta_min"]:
        nivel = "explosivo"
    elif score is not None and score >= alza["score_min"] and delta >= alza["delta_min"]:
        nivel = "en_alza"
    elif score is None and delta is not None and delta >= alza["delta_sin_historial"]:
        nivel = "en_alza"
    elif es_nuevo_rapido:
        nivel = "nuevo_rapido"
    elif delta is None:
        nivel = "sin_datos"
    else:
        nivel = "estable"

    dias_historial = len(serie)
    confianza = "alta" if dias_historial >= 7 else "media" if dias_historial >= 3 else "baja"
    return {
        "delta_dia": delta,
        "base_diaria": base,
        "score": round(score, 2) if score is not None else None,
        "nivel": nivel,
        "confianza": confianza,
        "dias_historial": dias_historial,
    }


def calcular(con: sqlite3.Connection, hoy: date, umbrales: dict, trending: dict[int, int] | None = None) -> int:
    """Calcula y guarda la tendencia de hoy de cada repo con foto de hoy. Devuelve cuántos calculó.

    trending: {repo_id: estrellas de hoy} de la página daily leída en esta corrida.
    """
    trending = trending or {}
    repos = con.execute(
        "SELECT r.id, r.creado_gh FROM repos r JOIN snapshots s ON s.repo_id = r.id AND s.fecha = ?",
        (hoy.isoformat(),),
    ).fetchall()
    for repo in repos:
        serie = [
            (date.fromisoformat(f["fecha"]), f["estrellas"])
            for f in con.execute(
                "SELECT fecha, estrellas FROM snapshots WHERE repo_id = ? AND fecha BETWEEN ? AND ?",
                (repo["id"], _desde(hoy, 15), hoy.isoformat()),
            )
        ]
        en_trending = repo["id"] in trending
        r = clasificar(serie, hoy, umbrales, repo["creado_gh"], trending.get(repo["id"]))
        con.execute(
            """
            INSERT INTO tendencias (repo_id, fecha, delta_dia, base_diaria, score, nivel, confianza,
                                    dias_historial, en_trending, estrellas_hoy_trending)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (repo_id, fecha) DO UPDATE SET
                delta_dia = excluded.delta_dia, base_diaria = excluded.base_diaria, score = excluded.score,
                nivel = excluded.nivel, confianza = excluded.confianza, dias_historial = excluded.dias_historial,
                en_trending = excluded.en_trending, estrellas_hoy_trending = excluded.estrellas_hoy_trending
            """,
            (repo["id"], hoy.isoformat(), r["delta_dia"], r["base_diaria"], r["score"], r["nivel"],
             r["confianza"], r["dias_historial"], int(en_trending), trending.get(repo["id"])),
        )
    con.commit()
    return len(repos)


# ---------- Corrida completa ----------

@dataclass
class Informe:
    trending: int = 0
    refrescados: int = 0
    con_foto: int = 0
    seguidos: int = 0
    calculados: int = 0
    problemas: list[str] = field(default_factory=list)
    avisos: list[str] = field(default_factory=list)  # no hacen fallar la corrida

    @property
    def ok(self) -> bool:
        return not self.problemas


def ejecutar(
    gh: GitHubClient,
    con: sqlite3.Connection,
    umbrales: dict,
    hoy: date | None = None,
    con_trending: bool = True,
) -> Informe:
    hoy = hoy or date.today()
    inf = Informe()
    corrida = db.iniciar_corrida(con, "tendencia")
    trending_hoy: dict[int, int] = {}
    try:
        if con_trending:
            for periodo in umbrales["trending"]["periodos"]:
                repos, problemas = recolectar_trending(gh, periodo, umbrales["trending"]["minimo_repos"])
                inf.problemas += problemas
                guardar_trending(con, repos, hoy)
                inf.trending += len(repos)
                if periodo == "daily":
                    trending_hoy = {r.id: r.estrellas_periodo for r in repos}

        # Sin token, GitHub da 60 peticiones/hora que ya gastaron ranking y redes: quedarse sin cupo para
        # refrescar (y la cobertura baja que eso deja) es lo esperado, no una falla. Con token sí lo es.
        sin_cupo = inf.avisos if gh.anonimo else inf.problemas
        cob = umbrales["cobertura"]
        limite = cob["max_refrescos_sin_token"] if gh.anonimo else cob["max_refrescos"]
        inf.refrescados, problemas = refrescar_faltantes(gh, con, hoy, cob["dias_seguimiento"], limite)
        for p in problemas:
            (sin_cupo if p.startswith("Refresco detenido") else inf.problemas).append(p)

        inf.con_foto, inf.seguidos = cobertura(con, hoy, cob["dias_seguimiento"])
        if inf.seguidos == 0:
            inf.problemas.append("No hay repos seguidos: corre primero radar-ranking")
        elif inf.con_foto / inf.seguidos < cob["minima"]:
            sin_cupo.append(
                f"Cobertura baja: {inf.con_foto}/{inf.seguidos} repos con foto de hoy "
                f"(mínimo {cob['minima']:.0%}); faltan refrescos"
                + (" — normal sin GITHUB_TOKEN" if gh.anonimo else "")
            )

        inf.calculados = calcular(con, hoy, umbrales, trending_hoy)
        # Se compara contra todas las fotos de hoy: la cobertura solo cuenta los repos seguidos (no archivados y
        # vistos en los últimos días), pero también se calcula la tendencia de los demás que tienen foto.
        fotos_hoy = con.execute("SELECT COUNT(*) FROM snapshots WHERE fecha = ?", (hoy.isoformat(),)).fetchone()[0]
        if inf.calculados != fotos_hoy:
            inf.problemas.append(f"Se calcularon {inf.calculados} tendencias para {fotos_hoy} repos con foto de hoy")
    except Exception as e:
        inf.problemas.append(f"Error inesperado: {e}")
        raise
    finally:
        db.cerrar_corrida(con, corrida, inf.ok, json.dumps(inf.__dict__, ensure_ascii=False))
    return inf


def destacados(con: sqlite3.Connection, hoy: date, limite: int) -> list[sqlite3.Row]:
    orden = "CASE t.nivel WHEN 'explosivo' THEN 0 WHEN 'en_alza' THEN 1 WHEN 'nuevo_rapido' THEN 2 END"
    return con.execute(
        f"""
        SELECT r.full_name, r.url, r.estrellas, t.nivel, t.delta_dia, t.score, t.confianza, t.en_trending
        FROM tendencias t JOIN repos r ON r.id = t.repo_id
        WHERE t.fecha = ? AND t.nivel IN ('explosivo', 'en_alza', 'nuevo_rapido')
        ORDER BY {orden}, t.delta_dia DESC
        LIMIT ?
        """,
        (hoy.isoformat(), limite),
    ).fetchall()


def main() -> int:
    parser = argparse.ArgumentParser(description="Detector de tendencia")
    parser.add_argument("--sin-trending", action="store_true", help="no leer github.com/trending")
    parser.add_argument("--top", type=int, default=15, help="cuántos destacados mostrar")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    cfg = cargar_config()
    gh = GitHubClient(cfg.github_token)
    if gh.anonimo:
        print("⚠️  Modo sin token: solo se refrescan pocos repos por corrida.\n")
    con = db.conectar(cfg.database_path)
    hoy = date.today()
    try:
        db.aplicar_esquema(con)
        inf = ejecutar(gh, con, cargar_umbrales(), hoy, con_trending=not args.sin_trending)
        top = destacados(con, hoy, args.top)
    finally:
        gh.close()
        con.close()

    print(f"Trending leídos: {inf.trending} · refrescados: {inf.refrescados} · "
          f"cobertura: {inf.con_foto}/{inf.seguidos} · tendencias calculadas: {inf.calculados}\n")
    iconos = {"explosivo": "🚀", "en_alza": "📈", "nuevo_rapido": "🌱"}
    for r in top:
        delta = f"+{r['delta_dia']:.0f}/día" if r["delta_dia"] is not None else "—"
        extra = " · en trending" if r["en_trending"] else ""
        print(f"{iconos[r['nivel']]} {r['full_name']:<45} {delta:>10} · {r['estrellas']:,} ★ · confianza {r['confianza']}{extra}")
        print(f"   {r['url']}")
    if not top:
        print("Sin repos destacados hoy.")
    for a in inf.avisos:
        print(f"⚠️  {a}")
    for p in inf.problemas:
        print(f"❌ {p}")
    print("\nTendencias actualizadas." if inf.ok else "\nHubo fallas en el detector de tendencia.")
    return 0 if inf.ok else 1


if __name__ == "__main__":
    sys.exit(main())
