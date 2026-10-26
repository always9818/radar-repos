"""Conexión a la base de datos y aplicación del esquema."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ESQUEMA = Path(__file__).with_name("schema.sql")
TABLAS = (
    "repos", "snapshots", "menciones", "menciones_pendientes", "categorias",
    "enriquecimiento", "tendencias", "traducciones", "corridas",
)


def ahora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def conectar(ruta: Path) -> sqlite3.Connection:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(ruta)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    return con


def aplicar_esquema(con: sqlite3.Connection) -> None:
    con.executescript(ESQUEMA.read_text(encoding="utf-8"))
    con.commit()


def tablas_faltantes(con: sqlite3.Connection) -> list[str]:
    existentes = {
        fila["name"]
        for fila in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    return [t for t in TABLAS if t not in existentes]


def iniciar_corrida(con: sqlite3.Connection, tarea: str) -> int:
    cur = con.execute(
        "INSERT INTO corridas (tarea, inicio, estado) VALUES (?, ?, 'en_curso')",
        (tarea, ahora()),
    )
    con.commit()
    return cur.lastrowid


def cerrar_corrida(con: sqlite3.Connection, corrida_id: int, ok: bool, detalle: str) -> None:
    con.execute(
        "UPDATE corridas SET fin = ?, estado = ?, detalle = ? WHERE id = ?",
        (ahora(), "ok" if ok else "error", detalle, corrida_id),
    )
    con.commit()
