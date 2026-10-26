"""Pruebas del healthcheck sin tocar la red: las APIs se simulan con MockTransport."""

import httpx
import pytest

from radar import db, healthcheck
from radar.config import Config

RATE_LIMIT_OK = {
    "resources": {
        "core": {"limit": 5000, "remaining": 4990},
        "search": {"limit": 30, "remaining": 30},
    }
}


@pytest.fixture(autouse=True)
def sin_esperas(monkeypatch):
    monkeypatch.setattr(healthcheck.time, "sleep", lambda _: None)


def hacer_cfg(tmp_path, **cambios) -> Config:
    base = dict(
        github_token="ghp_prueba",
        anthropic_api_key="sk-ant-prueba",
        anthropic_model="claude-haiku-5-5",
        database_path=tmp_path / "radar.db",
        github_min_remaining=500,
    )
    base.update(cambios)
    return Config(**base)


def cliente(manejador) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(manejador))


def apis_sanas(request: httpx.Request) -> httpx.Response:
    if request.url.host == "api.github.com":
        assert request.headers["Authorization"] == "Bearer ghp_prueba"
        return httpx.Response(200, json=RATE_LIMIT_OK)
    if request.url.host == "api.anthropic.com":
        assert request.headers["x-api-key"] == "sk-ant-prueba"
        return httpx.Response(200, json={"id": "claude-haiku-5-5"})
    raise AssertionError(f"Host inesperado: {request.url}")


def test_todo_en_verde(tmp_path):
    resultados = healthcheck.ejecutar(hacer_cfg(tmp_path), cliente(apis_sanas))
    assert all(r.ok for r in resultados), resultados
    assert not any(r.aviso for r in resultados)


def test_faltan_variables_no_llama_a_la_red(tmp_path):
    def prohibido(request):
        raise AssertionError("no debía llamar a la red")

    cfg = hacer_cfg(tmp_path, github_token="", anthropic_api_key="")
    r = {x.nombre: x for x in healthcheck.ejecutar(cfg, cliente(prohibido))}
    assert not r["entorno"].ok
    assert "GITHUB_TOKEN" in r["entorno"].mensaje and "ANTHROPIC_API_KEY" in r["entorno"].mensaje
    assert not r["github"].ok and not r["anthropic"].ok
    assert r["base_de_datos"].ok


def test_token_github_invalido(tmp_path):
    r = healthcheck.revisar_github(hacer_cfg(tmp_path), cliente(lambda req: httpx.Response(401)))
    assert not r.ok and "401" in r.mensaje


def test_limite_bajo_de_github_da_aviso(tmp_path):
    datos = {"resources": {"core": {"limit": 5000, "remaining": 100}, "search": {"limit": 30, "remaining": 5}}}
    r = healthcheck.revisar_github(hacer_cfg(tmp_path), cliente(lambda req: httpx.Response(200, json=datos)))
    assert r.ok and r.aviso


def test_reintenta_ante_error_500(tmp_path):
    llamadas = []

    def inestable(request):
        llamadas.append(1)
        return httpx.Response(503) if len(llamadas) < 3 else httpx.Response(200, json=RATE_LIMIT_OK)

    r = healthcheck.revisar_github(hacer_cfg(tmp_path), cliente(inestable))
    assert r.ok and len(llamadas) == 3


def test_sin_conexion_tras_reintentos(tmp_path):
    def caido(request):
        raise httpx.ConnectError("sin red")

    r = healthcheck.revisar_github(hacer_cfg(tmp_path), cliente(caido))
    assert not r.ok and "Sin conexión" in r.mensaje


def test_modelo_inexistente(tmp_path):
    r = healthcheck.revisar_anthropic(hacer_cfg(tmp_path), cliente(lambda req: httpx.Response(404)))
    assert not r.ok and "claude-haiku-5-5" in r.mensaje


def test_esquema_idempotente_y_completo(tmp_path):
    con = db.conectar(tmp_path / "radar.db")
    db.aplicar_esquema(con)
    db.aplicar_esquema(con)  # una segunda vez no debe fallar ni duplicar categorías
    assert db.tablas_faltantes(con) == []
    assert con.execute("SELECT COUNT(*) FROM categorias").fetchone()[0] == 13
    con.close()


def test_menciones_rechaza_fuente_desconocida(tmp_path):
    import sqlite3

    con = db.conectar(tmp_path / "radar.db")
    db.aplicar_esquema(con)
    con.execute(
        "INSERT INTO repos (id, full_name, url, primera_vez, ultima_vez) VALUES (1, 'a/b', 'https://github.com/a/b', 'x', 'x')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO menciones (repo_id, fuente, post_url, capturado) VALUES (1, 'facebook', 'u', 'x')"
        )
    con.close()


def test_registra_corrida_en_bitacora(tmp_path):
    cfg = hacer_cfg(tmp_path)
    resultados = healthcheck.ejecutar(cfg, cliente(apis_sanas))
    healthcheck.registrar(cfg, resultados)
    con = db.conectar(cfg.database_path)
    fila = con.execute("SELECT tarea, estado FROM corridas").fetchone()
    con.close()
    assert (fila["tarea"], fila["estado"]) == ("healthcheck", "ok")
