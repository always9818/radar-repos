"""Fase 6 (local): corrida diaria completa, pensada para el Programador de tareas de Windows.

Corre cada recolector en su propio proceso, en este orden:
  ranking → hackernews → reddit → bluesky → tendencia → ia → panel (el panel traduce lo nuevo antes de exportar)

La tendencia va después de las redes para incluir los repos que ellas confirmaron hoy.

Loop de verificación:
  - Cada paso tiene tiempo máximo; si falla, se reintenta una vez tras una pausa (salvo el panel).
  - Si un paso falla, los demás siguen igual: un Reddit caído no debe dejar el día sin ranking.
  - Al final se comprueba que hoy haya fotos de estrellas y que el panel se haya regenerado.
  - Un candado evita dos corridas a la vez, y si hoy ya hubo una corrida completa sin fallas no se repite
    (Windows puede lanzarla de nuevo al encender la PC); --forzar la repite igual.
  - Todo queda en logs/AAAA-MM-DD.log (se guardan 30 días) y en la tabla `corridas` como tarea 'diario'.

Uso:  uv run radar-diario [--forzar] [--solo PASO ...]
Sale con código 0 si todos los pasos y verificaciones pasaron, y 1 si algo falló.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from radar import db
from radar.config import RAIZ, cargar_config

LOGS = RAIZ / "logs"
CANDADO = RAIZ / "data" / "diario.lock"
PANEL = RAIZ / "panel" / "data.js"
DIAS_DE_LOGS = 30
PAUSA_REINTENTO = 60


@dataclass(frozen=True)
class Paso:
    nombre: str
    modulo: str
    tiempo_max: int  # segundos
    reintentar: bool = True


PASOS = (
    Paso("ranking", "radar.ranking", 900),
    Paso("hackernews", "radar.hackernews", 600),
    Paso("reddit", "radar.reddit", 1800),  # sin cuenta, los límites de Reddit pueden sumar varios minutos de espera
    Paso("bluesky", "radar.bluesky", 300),
    Paso("tendencia", "radar.tendencia", 900),
    Paso("ia", "radar.ia", 1800),  # sin ANTHROPIC_API_KEY no hace nada y sale bien
    Paso("panel", "radar.panel", 1200, reintentar=False),
)


@dataclass
class ResultadoPaso:
    nombre: str
    ok: bool
    intentos: int
    segundos: float
    detalle: str = ""


@dataclass
class Informe:
    inicio: str
    pasos: list[ResultadoPaso] = field(default_factory=list)
    problemas: list[str] = field(default_factory=list)
    omitida: str = ""

    @property
    def ok(self) -> bool:
        return not self.problemas and all(p.ok for p in self.pasos)


# Ejecuta un paso y devuelve (código de salida, salida). Se inyecta en las pruebas.
Ejecutor = Callable[[Paso], tuple[int, str]]


def interprete() -> str:
    """python.exe aunque la tarea programada arranque con pythonw.exe: los pasos deben poder imprimir."""
    actual = Path(sys.executable)
    consola = actual.with_name("python.exe")
    return str(consola if actual.name.lower() == "pythonw.exe" and consola.exists() else actual)


def ejecutar_proceso(paso: Paso) -> tuple[int, str]:
    entorno = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    sin_ventana = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # que no aparezcan consolas en Windows
    try:
        r = subprocess.run(
            [interprete(), "-m", paso.modulo], cwd=RAIZ, env=entorno, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=paso.tiempo_max, creationflags=sin_ventana,
        )
    except subprocess.TimeoutExpired as e:
        salida = (e.stdout or "") if isinstance(e.stdout, str) else ""
        return 124, salida + f"\n⏱️  Superó el tiempo máximo de {paso.tiempo_max} s"
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def ultima_linea_util(salida: str) -> str:
    lineas = [l.strip() for l in salida.strip().splitlines() if l.strip()]
    errores = [l for l in lineas if l.startswith("❌")]
    return (errores[0] if errores else lineas[-1] if lineas else "")[:200]


def correr_paso(paso: Paso, ejecutor: Ejecutor, log: Callable[[str], None], dormir: Callable[[float], None]) -> ResultadoPaso:
    intentos = 2 if paso.reintentar else 1
    inicio = time.time()
    for intento in range(1, intentos + 1):
        log(f"\n===== {paso.nombre} (intento {intento}/{intentos}) · {datetime.now():%H:%M:%S} =====")
        codigo, salida = ejecutor(paso)
        log(salida.rstrip())
        if codigo == 0:
            return ResultadoPaso(paso.nombre, True, intento, round(time.time() - inicio, 1), ultima_linea_util(salida))
        log(f"→ salió con código {codigo}")
        if intento < intentos:
            dormir(PAUSA_REINTENTO)
    return ResultadoPaso(paso.nombre, False, intentos, round(time.time() - inicio, 1), ultima_linea_util(salida))


def ya_corrio_hoy(con, hoy: date) -> bool:
    """`inicio` está en UTC; se pasa a hora local antes de comparar con la fecha de hoy."""
    filas = con.execute(
        "SELECT inicio FROM corridas WHERE tarea = 'diario' AND estado = 'ok' ORDER BY id DESC LIMIT 5"
    ).fetchall()
    return any(datetime.fromisoformat(f[0]).astimezone().date() == hoy for f in filas)


def verificar_resultado(con, hoy: date, inicio_ts: float, pasos_corridos: set[str]) -> list[str]:
    problemas = []
    if "ranking" in pasos_corridos or "tendencia" in pasos_corridos:
        fotos = con.execute("SELECT COUNT(*) FROM snapshots WHERE fecha = ?", (hoy.isoformat(),)).fetchone()[0]
        if fotos == 0:
            problemas.append("No quedó ninguna foto de estrellas de hoy")
    if "panel" in pasos_corridos and (not PANEL.exists() or PANEL.stat().st_mtime < inicio_ts):
        problemas.append("El panel no se regeneró en esta corrida")
    return problemas


def limpiar_logs(hoy: date) -> None:
    limite = hoy - timedelta(days=DIAS_DE_LOGS)
    for archivo in LOGS.glob("*.log"):
        try:
            if date.fromisoformat(archivo.stem) < limite:
                archivo.unlink()
        except ValueError:
            continue


class Candado:
    """Evita dos corridas simultáneas. Un candado de más de 3 horas se considera abandonado."""

    def __init__(self, ruta: Path = CANDADO):
        self.ruta = ruta

    def __enter__(self):
        if self.ruta.exists() and time.time() - self.ruta.stat().st_mtime < 3 * 3600:
            raise RuntimeError("Ya hay una corrida diaria en curso")
        self.ruta.parent.mkdir(parents=True, exist_ok=True)
        self.ruta.write_text(str(os.getpid()), encoding="utf-8")
        return self

    def __exit__(self, *_):
        self.ruta.unlink(missing_ok=True)


def correr(
    con,
    hoy: date,
    pasos: tuple[Paso, ...] = PASOS,
    ejecutor: Ejecutor = ejecutar_proceso,
    log: Callable[[str], None] = print,
    dormir: Callable[[float], None] = time.sleep,
    forzar: bool = False,
) -> Informe:
    inf = Informe(inicio=db.ahora())
    if not forzar and ya_corrio_hoy(con, hoy):
        inf.omitida = "Ya hubo una corrida completa hoy (usa --forzar para repetirla)"
        log(inf.omitida)
        return inf
    inicio_ts = time.time()
    corrida = db.iniciar_corrida(con, "diario")
    try:
        for paso in pasos:
            inf.pasos.append(correr_paso(paso, ejecutor, log, dormir))
        inf.problemas = verificar_resultado(con, hoy, inicio_ts, {p.nombre for p in pasos})
    finally:
        detalle = json.dumps({"pasos": [asdict(p) for p in inf.pasos], "problemas": inf.problemas}, ensure_ascii=False)
        db.cerrar_corrida(con, corrida, inf.ok, detalle)
    return inf


def main() -> int:
    parser = argparse.ArgumentParser(description="Corrida diaria completa del radar")
    parser.add_argument("--forzar", action="store_true", help="correr aunque hoy ya haya una corrida completa")
    parser.add_argument("--solo", nargs="+", choices=[p.nombre for p in PASOS], help="correr solo estos pasos")
    parser.add_argument(
        "--omitir", nargs="+", default=os.getenv("RADAR_OMITIR", "").split() or None,
        choices=[p.nombre for p in PASOS],
        help="saltar estos pasos (también con la variable RADAR_OMITIR, p. ej. 'reddit' si bloquea los servidores de GitHub)",
    )
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure") and sys.stdout:
        sys.stdout.reconfigure(encoding="utf-8")

    hoy = date.today()
    LOGS.mkdir(parents=True, exist_ok=True)
    limpiar_logs(hoy)
    archivo_log = LOGS / f"{hoy.isoformat()}.log"
    pasos = tuple(
        p for p in PASOS if (not args.solo or p.nombre in args.solo) and p.nombre not in (args.omitir or [])
    )

    with archivo_log.open("a", encoding="utf-8") as f:
        def log(texto: str) -> None:
            f.write(texto + "\n")
            f.flush()
            if sys.stdout:  # con pythonw (tarea programada) no hay consola
                print(texto, flush=True)

        log(f"\n######## Corrida diaria · {datetime.now():%Y-%m-%d %H:%M:%S} ########")
        if args.omitir:
            log(f"Pasos omitidos: {', '.join(args.omitir)}")
        try:
            with Candado():
                cfg = cargar_config()
                con = db.conectar(cfg.database_path)
                try:
                    db.aplicar_esquema(con)
                    inf = correr(con, hoy, pasos, log=log, forzar=args.forzar or bool(args.solo))
                finally:
                    con.close()
        except RuntimeError as e:
            log(f"❌ {e}")
            return 1

        if inf.omitida:
            return 0
        log("\n======== Resumen ========")
        for p in inf.pasos:
            log(f"{'✅' if p.ok else '❌'} {p.nombre:<11} {p.segundos:>6.0f} s · intento {p.intentos} · {p.detalle}")
        for problema in inf.problemas:
            log(f"❌ {problema}")
        log("Corrida diaria completa." if inf.ok else "La corrida diaria tuvo fallas (revisa el detalle arriba).")
    return 0 if inf.ok else 1


if __name__ == "__main__":
    sys.exit(main())
