"""Healthcheck de la fase 0: si algo falla aquí, ninguna otra fase debe correr.

Verifica:
  1. Que las variables de entorno obligatorias existan.
  2. Que el token de GitHub sea válido y quede límite de peticiones.
  3. Que la clave de Anthropic sea válida y el modelo configurado exista.
  4. Que la base de datos abra, tenga todas las tablas y acepte escrituras.

Uso:  uv run radar-healthcheck [--json]
Sale con código 0 si todo está bien y 1 si algo falló.
"""

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass

import httpx

from radar import db
from radar.config import Config, cargar_config

GITHUB_API = "https://api.github.com"
ANTHROPIC_API = "https://api.anthropic.com"
INTENTOS = 3


@dataclass
class Resultado:
    nombre: str
    ok: bool
    mensaje: str
    aviso: bool = False


def _get_con_reintentos(client: httpx.Client, url: str, headers: dict) -> httpx.Response:
    """GET con espera creciente ante fallas de red o errores 5xx del servidor."""
    for intento in range(1, INTENTOS + 1):
        try:
            resp = client.get(url, headers=headers)
            if resp.status_code < 500 or intento == INTENTOS:
                return resp
        except httpx.TransportError:
            if intento == INTENTOS:
                raise
        time.sleep(2 ** (intento - 1))
    raise AssertionError("inalcanzable")


def revisar_entorno(cfg: Config) -> Resultado:
    faltan = [
        nombre
        for nombre, valor in (("GITHUB_TOKEN", cfg.github_token), ("ANTHROPIC_API_KEY", cfg.anthropic_api_key))
        if not valor
    ]
    if faltan:
        return Resultado("entorno", False, f"Faltan en .env: {', '.join(faltan)}")
    return Resultado("entorno", True, "Variables obligatorias presentes")


def revisar_github(cfg: Config, client: httpx.Client) -> Resultado:
    if not cfg.github_token:
        return Resultado("github", False, "Sin GITHUB_TOKEN, no se puede probar")
    headers = {
        "Authorization": f"Bearer {cfg.github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        resp = _get_con_reintentos(client, f"{GITHUB_API}/rate_limit", headers)
    except httpx.TransportError as e:
        return Resultado("github", False, f"Sin conexión con GitHub: {e}")
    if resp.status_code == 401:
        return Resultado("github", False, "Token inválido o vencido (401)")
    if resp.status_code != 200:
        return Resultado("github", False, f"Respuesta inesperada {resp.status_code}")

    recursos = resp.json()["resources"]
    core = recursos["core"]["remaining"]
    busqueda = recursos["search"]["remaining"]
    mensaje = f"Token válido · core {core}/{recursos['core']['limit']} · search {busqueda}/{recursos['search']['limit']}"
    if core < cfg.github_min_remaining:
        return Resultado("github", True, mensaje + f" · quedan menos de {cfg.github_min_remaining}", aviso=True)
    return Resultado("github", True, mensaje)


def revisar_anthropic(cfg: Config, client: httpx.Client) -> Resultado:
    if not cfg.anthropic_api_key:
        return Resultado("anthropic", False, "Sin ANTHROPIC_API_KEY, no se puede probar")
    headers = {"x-api-key": cfg.anthropic_api_key, "anthropic-version": "2023-06-01"}
    try:
        resp = _get_con_reintentos(client, f"{ANTHROPIC_API}/v1/models/{cfg.anthropic_model}", headers)
    except httpx.TransportError as e:
        return Resultado("anthropic", False, f"Sin conexión con Anthropic: {e}")
    if resp.status_code == 401:
        return Resultado("anthropic", False, "Clave de API inválida (401)")
    if resp.status_code == 404:
        return Resultado("anthropic", False, f"El modelo '{cfg.anthropic_model}' no existe o no está disponible")
    if resp.status_code != 200:
        return Resultado("anthropic", False, f"Respuesta inesperada {resp.status_code}")
    return Resultado("anthropic", True, f"Clave válida · modelo {cfg.anthropic_model} disponible")


def revisar_db(cfg: Config) -> Resultado:
    try:
        con = db.conectar(cfg.database_path)
        try:
            db.aplicar_esquema(con)
            faltan = db.tablas_faltantes(con)
            if faltan:
                return Resultado("base_de_datos", False, f"Faltan tablas: {', '.join(faltan)}")
            # Prueba de escritura y lectura real.
            corrida = db.iniciar_corrida(con, "healthcheck_prueba")
            db.cerrar_corrida(con, corrida, True, "prueba de escritura")
            fila = con.execute("SELECT estado FROM corridas WHERE id = ?", (corrida,)).fetchone()
            con.execute("DELETE FROM corridas WHERE id = ?", (corrida,))
            con.commit()
            if fila is None or fila["estado"] != "ok":
                return Resultado("base_de_datos", False, "La escritura de prueba no se pudo leer")
            categorias = con.execute("SELECT COUNT(*) FROM categorias").fetchone()[0]
        finally:
            con.close()
    except Exception as e:  # cualquier fallo de disco o SQL cuenta como error
        return Resultado("base_de_datos", False, f"Error: {e}")
    return Resultado("base_de_datos", True, f"{cfg.database_path.name} OK · {categorias} categorías")


def ejecutar(cfg: Config, client: httpx.Client | None = None) -> list[Resultado]:
    propio = client is None
    client = client or httpx.Client(timeout=15)
    try:
        return [
            revisar_entorno(cfg),
            revisar_github(cfg, client),
            revisar_anthropic(cfg, client),
            revisar_db(cfg),
        ]
    finally:
        if propio:
            client.close()


def registrar(cfg: Config, resultados: list[Resultado]) -> None:
    """Deja constancia en la bitácora; si la base no abre, no se puede registrar."""
    if not any(r.nombre == "base_de_datos" and r.ok for r in resultados):
        return
    con = db.conectar(cfg.database_path)
    try:
        corrida = db.iniciar_corrida(con, "healthcheck")
        ok = all(r.ok for r in resultados)
        db.cerrar_corrida(con, corrida, ok, json.dumps([asdict(r) for r in resultados], ensure_ascii=False))
    finally:
        con.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Healthcheck del radar de repos")
    parser.add_argument("--json", action="store_true", help="salida en JSON")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    cfg = cargar_config()
    resultados = ejecutar(cfg)
    registrar(cfg, resultados)
    todo_ok = all(r.ok for r in resultados)

    if args.json:
        print(json.dumps({"ok": todo_ok, "resultados": [asdict(r) for r in resultados]}, ensure_ascii=False, indent=2))
    else:
        for r in resultados:
            icono = "❌" if not r.ok else ("⚠️ " if r.aviso else "✅")
            print(f"{icono} {r.nombre:<14} {r.mensaje}")
        print("\nTodo en verde: puedes pasar a la siguiente fase." if todo_ok else "\nHay fallas: corrígelas antes de seguir.")
    return 0 if todo_ok else 1


if __name__ == "__main__":
    sys.exit(main())
