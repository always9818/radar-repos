"""Fase 6: chequeo de salud del radar a lo largo de los días (no solo de una corrida).

Revisa:
  1. Integridad de la base (PRAGMA integrity_check) y que estén todas las tablas.
  2. Que la última corrida diaria completa no tenga más de --max-horas (por defecto 30).
  3. Cada fuente: si su última corrida falló es un aviso; si falló 3 veces seguidas, es un error.
  4. Con --comparar BASE_ANTERIOR: que no se hayan perdido repos, fotos ni menciones (se usa en la nube
     antes de guardar la base, para no pisar la buena con una dañada).

Con --respaldo DESTINO escribe una copia consistente de la base (API de respaldo de SQLite) para subirla.

Uso:  uv run radar-salud [--max-horas N] [--comparar RUTA] [--respaldo RUTA] [--solo-integridad]
Sale con código 0 si no hay errores (los avisos no hacen fallar) y 1 si hay alguno.
"""

import argparse
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from radar import db
from radar.config import cargar_config

FUENTES = ("ranking", "tendencia", "hackernews", "reddit", "bluesky", "traductor")
# "ia" no está: sin ANTHROPIC_API_KEY nunca registra corridas y eso es normal. Sus fallas se ven en el diario.
TABLAS_QUE_NO_DEBEN_ENCOGER = ("repos", "snapshots", "menciones")
FALLOS_SEGUIDOS_ERROR = 3


@dataclass
class Informe:
    errores: list[str] = field(default_factory=list)
    avisos: list[str] = field(default_factory=list)
    datos: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errores


def _horas_desde(iso: str, ahora: datetime) -> float:
    return (ahora - datetime.fromisoformat(iso)).total_seconds() / 3600


def revisar(
    con: sqlite3.Connection, max_horas: float = 30, ahora: datetime | None = None, solo_integridad: bool = False
) -> Informe:
    """solo_integridad=True revisa solo que la base esté sana (para decidir si se puede guardar):
    una fuente que lleva días fallando no debe impedir guardar los datos buenos del resto."""
    ahora = ahora or datetime.now(timezone.utc)
    inf = Informe()

    resultado = con.execute("PRAGMA integrity_check").fetchone()[0]
    if resultado != "ok":
        inf.errores.append(f"La base está dañada: {resultado}")
        return inf
    if faltan := db.tablas_faltantes(con):
        inf.errores.append(f"Faltan tablas: {', '.join(faltan)}")
        return inf

    for tabla in ("repos", "snapshots", "menciones", "menciones_pendientes", "traducciones"):
        inf.datos[tabla] = con.execute(f"SELECT COUNT(*) FROM {tabla}").fetchone()[0]
    if solo_integridad:
        return inf

    ultima = con.execute(
        "SELECT inicio FROM corridas WHERE tarea = 'diario' AND estado = 'ok' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if ultima is None:
        inf.errores.append("Nunca hubo una corrida diaria completa")
    elif (horas := _horas_desde(ultima[0], ahora)) > max_horas:
        inf.errores.append(f"La última corrida diaria completa fue hace {horas:.0f} h (máximo {max_horas:.0f} h)")

    for fuente in FUENTES:
        estados = [
            f[0] for f in con.execute(
                "SELECT estado FROM corridas WHERE tarea = ? AND estado != 'en_curso' ORDER BY id DESC LIMIT ?",
                (fuente, FALLOS_SEGUIDOS_ERROR),
            )
        ]
        if not estados:
            inf.avisos.append(f"{fuente}: todavía no ha corrido nunca")
        elif len(estados) == FALLOS_SEGUIDOS_ERROR and all(e == "error" for e in estados):
            inf.errores.append(f"{fuente}: lleva {FALLOS_SEGUIDOS_ERROR} corridas seguidas fallando")
        elif estados[0] == "error":
            inf.avisos.append(f"{fuente}: su última corrida falló")

    colgadas = con.execute(
        "SELECT tarea, inicio FROM corridas WHERE estado = 'en_curso' ORDER BY id DESC LIMIT 5"
    ).fetchall()
    for tarea, inicio in colgadas:
        if _horas_desde(inicio, ahora) > 3:
            inf.avisos.append(f"{tarea}: quedó una corrida sin cerrar desde {inicio[:16]} (proceso interrumpido)")

    if inf.datos["menciones_pendientes"] > 1000:
        inf.avisos.append(f"Hay {inf.datos['menciones_pendientes']} menciones esperando confirmación (¿falta GITHUB_TOKEN?)")
    return inf


def comparar(con: sqlite3.Connection, anterior: Path) -> list[str]:
    """Errores si la base nueva tiene menos filas que la anterior en tablas que solo crecen."""
    if not anterior.exists():
        return []
    previa = sqlite3.connect(anterior)
    try:
        errores = []
        for tabla in TABLAS_QUE_NO_DEBEN_ENCOGER:
            antes = previa.execute(f"SELECT COUNT(*) FROM {tabla}").fetchone()[0]
            ahora = con.execute(f"SELECT COUNT(*) FROM {tabla}").fetchone()[0]
            if ahora < antes:
                errores.append(f"La tabla {tabla} pasó de {antes} a {ahora} filas: no se guarda esta base")
        return errores
    finally:
        previa.close()


def respaldar(con: sqlite3.Connection, destino: Path) -> None:
    """Copia consistente aunque la base esté en modo WAL (copiar el archivo a mano podría salir a medias)."""
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.unlink(missing_ok=True)
    copia = sqlite3.connect(destino)
    try:
        con.backup(copia)
        copia.execute("PRAGMA journal_mode = DELETE")
    finally:
        copia.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Chequeo de salud del radar")
    parser.add_argument("--max-horas", type=float, default=30, help="antigüedad máxima de la última corrida diaria")
    parser.add_argument("--comparar", type=Path, help="base anterior: verifica que no se perdieron datos")
    parser.add_argument("--respaldo", type=Path, help="escribe aquí una copia consistente de la base")
    parser.add_argument("--solo-integridad", action="store_true", help="solo integridad y comparación (para guardar)")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure") and sys.stdout:
        sys.stdout.reconfigure(encoding="utf-8")

    cfg = cargar_config()
    con = db.conectar(cfg.database_path)
    try:
        db.aplicar_esquema(con)
        inf = revisar(con, args.max_horas, solo_integridad=args.solo_integridad)
        if args.comparar:
            inf.errores += comparar(con, args.comparar)
        if args.respaldo and inf.ok:
            respaldar(con, args.respaldo)
    finally:
        con.close()

    print(" · ".join(f"{k}: {v:,}" for k, v in inf.datos.items()))
    for a in inf.avisos:
        print(f"⚠️  {a}")
    for e in inf.errores:
        print(f"❌ {e}")
    if args.respaldo:
        print(f"💾 Respaldo escrito en {args.respaldo}" if inf.ok else "💾 No se escribió el respaldo por los errores")
    print("\nRadar sano." if inf.ok else "\nEl radar necesita atención.")
    return 0 if inf.ok else 1


if __name__ == "__main__":
    sys.exit(main())
