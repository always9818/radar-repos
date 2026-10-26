"""Fase 5: exporta la base a panel/data.js para el panel web estático.

El panel es HTML + JS sin servidor: abre panel/index.html directo en el navegador o súbelo a cualquier
hosting estático (fase 6). Los datos van en data.js (no .json) para que también funcione con file://.

Mientras no exista el enriquecimiento con IA (fase 3), cada repo recibe una categoría provisional
por palabras clave de sus topics, nombre y descripción.

Uso:  uv run radar-panel
Sale con código 0 si la exportación pasó la validación y 1 si no.
"""

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from radar import db, traductor
from radar.config import RAIZ, cargar_config

DIR_PANEL = RAIZ / "panel"
NIVELES = ("explosivo", "en_alza", "nuevo_rapido", "estable", "sin_datos")
FUENTES_SOCIALES = ("github_trending", "hackernews", "reddit", "bluesky", "x", "tiktok")
MENCIONES_POR_FUENTE = 3

# Orden = prioridad en caso de empate. Se compara contra palabras sueltas (topics, nombre y descripción).
PALABRAS = {
    "agentes": {"agent", "agents", "ai-agent", "ai-agents", "agentic", "mcp", "mcp-server", "autonomous",
                "claude-code", "codex", "copilot", "skills", "multi-agent", "browser-use", "computer-use"},
    "seguridad": {"security", "pentest", "pentesting", "vulnerability", "exploit", "malware", "privacy",
                  "encryption", "cybersecurity", "audit", "osint", "hacking", "firewall", "password"},
    "multimedia": {"image", "images", "video", "audio", "music", "speech", "tts", "voice", "photo", "comfyui",
                   "stable-diffusion", "diffusion", "ffmpeg", "3d", "animation", "camera", "ocr"},
    "ia-llm": {"llm", "llms", "gpt", "openai", "deepseek", "transformer", "transformers", "inference", "rag",
               "machine-learning", "deep-learning", "ai", "model", "models", "pytorch", "nlp", "chatgpt",
               "ollama", "llama", "embedding", "embeddings", "fine-tuning", "neural-network", "artificial-intelligence"},
    "datos": {"database", "databases", "sql", "analytics", "data", "etl", "postgres", "postgresql", "vector",
              "sqlite", "redis", "nosql", "data-science", "pandas", "dataset", "visualization", "scraping"},
    "infra": {"docker", "kubernetes", "devops", "cloud", "terraform", "self-hosted", "selfhosted", "server",
              "monitoring", "linux", "homelab", "proxy", "networking", "nginx", "deployment", "ci", "observability"},
    "movil": {"android", "ios", "mobile", "flutter", "react-native", "swift", "kotlin"},
    "frontend": {"react", "vue", "css", "ui", "frontend", "tailwind", "tailwindcss", "svelte", "nextjs", "next",
                 "component", "components", "design", "html", "web", "javascript", "typescript", "font"},
    "backend": {"api", "backend", "framework", "http", "web-framework", "graphql", "rest", "nodejs", "express",
                "django", "fastapi", "microservices", "grpc", "php"},
    "devtools": {"cli", "terminal", "editor", "vscode", "git", "compiler", "linter", "ide", "developer-tools",
                 "neovim", "vim", "shell", "zsh", "debugger", "testing", "build-tool", "package-manager", "rust", "go"},
    "productividad": {"productivity", "notes", "todo", "calendar", "obsidian", "markdown", "note-taking",
                      "automation", "workflow", "knowledge-base", "pkm", "desktop", "planner"},
    "educacion": {"awesome", "awesome-list", "tutorial", "tutorials", "learning", "course", "roadmap",
                  "interview", "book", "books", "guide", "resources", "education", "cheatsheet", "list"},
}


SENSIBLE = re.compile(r"\b(nsfw|porn\w*|hentai|nude\w*|xxx|onlyfans|r18|18\+)", re.I)


def es_sensible(topics: list[str], nombre: str, descripcion: str | None) -> bool:
    """Contenido para adultos: el panel lo oculta por defecto."""
    return bool(SENSIBLE.search(" ".join([nombre, descripcion or "", *topics])))


def categoria_provisional(topics: list[str], nombre: str, descripcion: str | None) -> str:
    palabras = set(t.lower() for t in topics)
    palabras |= set(re.findall(r"[a-z0-9][a-z0-9+#.-]*", f"{nombre} {descripcion or ''}".lower().replace("/", " ")))
    # Los topics son etiquetas elegidas por el autor: pesan el doble que el texto libre.
    puntos = {
        cat: 2 * len(claves & set(t.lower() for t in topics)) + len(claves & palabras)
        for cat, claves in PALABRAS.items()
    }
    mejor = max(puntos.values())
    if mejor == 0:
        return "otros"
    return next(cat for cat in PALABRAS if puntos[cat] == mejor)


class Textos:
    """Entrega cada texto en español y cuenta los que todavía no pasaron por el traductor."""

    def __init__(self, con: sqlite3.Connection, originales: list[str]):
        self.es = traductor.en_espanol(con, originales)
        self.sin_procesar: set[str] = set()

    def __call__(self, original: str | None) -> str | None:
        if not original or not original.strip():
            return None
        if original not in self.es:
            self.sin_procesar.add(original)
            return None
        return self.es[original]  # None si no se pudo traducir: el panel no lo muestra


def _menciones(con: sqlite3.Connection) -> dict[int, dict]:
    por_repo: dict[int, dict] = {}
    filas = con.execute(
        """
        SELECT repo_id, fuente, post_url, autor, texto, publicado, interaccion FROM menciones
        ORDER BY repo_id, fuente, interaccion DESC
        """
    )
    for f in filas:
        repo = por_repo.setdefault(f["repo_id"], {"ranking": [], "social": {}, "total_social": 0})
        if f["fuente"] == "github_ranking":
            repo["ranking"].append(f["texto"])
            continue
        lista = repo["social"].setdefault(f["fuente"], [])
        repo["total_social"] += 1
        if len(lista) < MENCIONES_POR_FUENTE:
            lista.append({
                "url": f["post_url"], "texto": f["texto"], "autor": f["autor"],
                "fecha": (f["publicado"] or "")[:10], "interaccion": f["interaccion"],
            })
    return por_repo


def exportar(con: sqlite3.Connection) -> dict:
    fecha_tendencia = con.execute("SELECT MAX(fecha) FROM tendencias").fetchone()[0]
    tendencias = {
        f["repo_id"]: dict(f)
        for f in con.execute("SELECT * FROM tendencias WHERE fecha = ?", (fecha_tendencia,))
    }
    enriquecidos = {f["repo_id"]: dict(f) for f in con.execute("SELECT * FROM enriquecimiento")}
    categorias = {f["slug"]: f["nombre"] for f in con.execute("SELECT slug, nombre FROM categorias")}
    menciones = _menciones(con)
    filas_repos = con.execute("SELECT * FROM repos WHERE archivado = 0 ORDER BY estrellas DESC").fetchall()

    # Todo lo que se muestra pasa por las traducciones: nada llega al panel en inglés.
    originales = [r["descripcion_gh"] for r in filas_repos if r["descripcion_gh"]]
    originales += [
        m["texto"] for rm in menciones.values() for fuente, lista in rm["social"].items()
        if fuente in traductor.FUENTES_CON_TEXTO for m in lista if m["texto"]
    ]
    espanol = Textos(con, originales)
    for rm in menciones.values():
        for fuente, lista in rm["social"].items():
            for m in lista:
                if fuente not in traductor.FUENTES_CON_TEXTO:
                    m["texto"] = ""  # p. ej. "2012 stars today" de trending: el panel usa su propio indicador
                    continue
                # Se recorta después de traducir: el texto recortado no coincidiría con su traducción guardada.
                m["texto"] = (espanol(m["texto"]) or "")[:160]

    repos = []
    for r in filas_repos:
        topics = json.loads(r["topics"] or "[]")
        t = tendencias.get(r["id"], {})
        e = enriquecidos.get(r["id"])
        m = menciones.get(r["id"], {"ranking": [], "social": {}, "total_social": 0})
        repos.append({
            "id": r["id"],
            "nombre": r["full_name"],
            "url": r["url"],
            "descripcion": espanol(r["descripcion_gh"]),
            "descripcion_oculta": bool(r["descripcion_gh"] and r["descripcion_gh"].strip()) and espanol(r["descripcion_gh"]) is None,
            "lenguaje": r["lenguaje"],
            "topics": topics[:8],
            "estrellas": r["estrellas"],
            "forks": r["forks"],
            "creado": (r["creado_gh"] or "")[:10] or None,
            "visto_desde": r["primera_vez"][:10],
            "categoria": e["categoria"] if e else categoria_provisional(topics, r["full_name"], r["descripcion_gh"]),
            "categoria_ia": bool(e),
            "uso": e["descripcion_uso"] if e else None,
            "ejemplos": [e["ejemplo_1"], e["ejemplo_2"]] if e else [],
            "nivel": t.get("nivel", "sin_datos"),
            "delta_dia": round(t["delta_dia"]) if t.get("delta_dia") is not None else None,
            "score": t.get("score"),
            "confianza": t.get("confianza"),
            "en_trending": bool(t.get("en_trending")),
            "ranking": sorted(set(m["ranking"])),
            "social": m["social"],
            "total_social": m["total_social"],
            "sensible": es_sensible(topics, r["full_name"], r["descripcion_gh"]),
        })

    conteo = Counter(r["categoria"] for r in repos)
    corridas = [
        dict(f)
        for f in con.execute(
            """
            SELECT tarea, estado, MAX(inicio) AS inicio FROM corridas
            WHERE tarea != 'healthcheck_prueba' GROUP BY tarea ORDER BY tarea
            """
        )
    ]
    return {
        "generado": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fecha_tendencia": fecha_tendencia,
        "categorias": [{"slug": s, "nombre": n, "total": conteo.get(s, 0)} for s, n in categorias.items()],
        "pendientes": con.execute("SELECT COUNT(DISTINCT lower(full_name)) FROM menciones_pendientes").fetchone()[0],
        "sin_traducir": len(espanol.sin_procesar),
        "corridas": corridas,
        "repos": repos,
    }


def validar(datos: dict) -> list[str]:
    problemas = []
    repos = datos["repos"]
    if not repos:
        return ["No hay repos para exportar: corre primero radar-ranking"]
    nombres = [r["nombre"].lower() for r in repos]
    if len(nombres) != len(set(nombres)):
        problemas.append("Hay repos duplicados en la exportación")
    slugs = {c["slug"] for c in datos["categorias"]}
    for r in repos:
        if not re.fullmatch(r"https://github\.com/[A-Za-z0-9-]+/[A-Za-z0-9._-]+", r["url"] or ""):
            problemas.append(f"URL inválida para copiar: {r['nombre']} → {r['url']}")
        if r["url"].lower() != f"https://github.com/{r['nombre']}".lower():
            problemas.append(f"La URL no coincide con el nombre: {r['nombre']}")
        if r["categoria"] not in slugs:
            problemas.append(f"Categoría desconocida '{r['categoria']}' en {r['nombre']}")
        if r["nivel"] not in NIVELES:
            problemas.append(f"Nivel desconocido '{r['nivel']}' en {r['nombre']}")
        if r["categoria_ia"] and len(r["ejemplos"]) != 2:
            problemas.append(f"{r['nombre']} tiene categoría de IA pero no sus 2 ejemplos")
        if len(problemas) > 20:
            break
    if sum(c["total"] for c in datos["categorias"]) != len(repos):
        problemas.append("El conteo por categoría no suma el total de repos")
    if datos.get("sin_traducir"):
        problemas.append(
            f"{datos['sin_traducir']} textos todavía no pasaron por el traductor. "
            "Corre: uv run radar-traducir (o --descargar si faltan los modelos)"
        )
    return problemas


def escribir(datos: dict, directorio: Path = DIR_PANEL) -> Path:
    directorio.mkdir(parents=True, exist_ok=True)
    destino = directorio / "data.js"
    contenido = json.dumps(datos, ensure_ascii=False, separators=(",", ":"))
    # "</" escapado para que el JSON no pueda cerrar un <script> si se incrusta en HTML.
    destino.write_text(f"window.RADAR = {contenido.replace('</', '<\\/')};\n", encoding="utf-8")
    return destino


def main() -> int:
    argparse.ArgumentParser(description="Exporta los datos del panel web").parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    cfg = cargar_config()
    con = db.conectar(cfg.database_path)
    try:
        db.aplicar_esquema(con)
        # Traduce lo nuevo antes de exportar, si el modelo está instalado.
        ajustes = traductor.cargar_ajustes()
        motor = traductor.MotorLocal(ajustes)
        if motor.instalado("en_es"):
            inf = traductor.traducir_pendientes(con, motor, ajustes)
            if inf.pendientes:
                print(f"🌐 Traducidos al español: {inf.traducidos} nuevos ({inf.segundos} s)")
        datos = exportar(con)
    finally:
        con.close()
    problemas = validar(datos)
    for p in problemas:
        print(f"❌ {p}")
    if problemas:
        print("\nNo se escribió el panel: la exportación no pasó la validación.")
        return 1
    destino = escribir(datos)
    tendencia = sum(1 for r in datos["repos"] if r["nivel"] in ("explosivo", "en_alza", "nuevo_rapido"))
    print(f"✅ {len(datos['repos'])} repos · {tendencia} en tendencia · {destino.stat().st_size // 1024} KB")
    print(f"   Abre: {DIR_PANEL / 'index.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
