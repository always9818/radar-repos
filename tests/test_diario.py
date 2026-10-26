"""Pruebas de la corrida diaria con pasos simulados (sin lanzar procesos reales)."""

import time
from datetime import date, datetime, timedelta, timezone

import pytest

from radar import db, diario
from radar.diario import Paso

HOY = date.today()  # las corridas se registran con la hora real, así que "hoy" debe ser el día real
PASOS = (Paso("ranking", "x", 10), Paso("reddit", "x", 10), Paso("panel", "x", 10, reintentar=False))


@pytest.fixture
def con(tmp_path):
    c = db.conectar(tmp_path / "radar.db")
    db.aplicar_esquema(c)
    yield c
    c.close()


@pytest.fixture(autouse=True)
def sin_verificacion_de_archivos(monkeypatch):
    # La verificación final mira la base y panel/data.js reales; aquí se prueba por separado.
    monkeypatch.setattr(diario, "verificar_resultado", lambda *a: [])


class Ejecutor:
    def __init__(self, codigos: dict[str, list[int]]):
        self.codigos = codigos
        self.llamadas: list[str] = []

    def __call__(self, paso):
        self.llamadas.append(paso.nombre)
        codigo = self.codigos.get(paso.nombre, [0]).pop(0) if self.codigos.get(paso.nombre) else 0
        return codigo, f"salida de {paso.nombre}\n{'❌ algo falló' if codigo else '✅ listo'}"


def correr(con, ejecutor, **kw):
    return diario.correr(con, HOY, PASOS, ejecutor, log=lambda t: None, dormir=lambda s: None, **kw)


def test_todo_ok_registra_corrida(con):
    ej = Ejecutor({})
    inf = correr(con, ej)
    assert inf.ok and ej.llamadas == ["ranking", "reddit", "panel"]
    assert [p.detalle for p in inf.pasos][0] == "✅ listo"
    assert con.execute("SELECT estado FROM corridas WHERE tarea = 'diario'").fetchone()[0] == "ok"


def test_reintenta_una_vez_y_sigue(con):
    ej = Ejecutor({"reddit": [1, 0]})
    inf = correr(con, ej)
    assert inf.ok
    assert ej.llamadas == ["ranking", "reddit", "reddit", "panel"]
    assert [p.intentos for p in inf.pasos] == [1, 2, 1]


def test_un_paso_caido_no_detiene_los_demas(con):
    ej = Ejecutor({"ranking": [1, 1]})
    inf = correr(con, ej)
    assert not inf.ok
    assert ej.llamadas == ["ranking", "ranking", "reddit", "panel"]
    assert inf.pasos[0].detalle == "❌ algo falló"
    assert con.execute("SELECT estado FROM corridas WHERE tarea = 'diario'").fetchone()[0] == "error"


def test_el_panel_no_se_reintenta(con):
    ej = Ejecutor({"panel": [1, 0]})
    inf = correr(con, ej)
    assert not inf.ok and ej.llamadas.count("panel") == 1


def test_no_repite_si_ya_corrio_hoy(con):
    correr(con, Ejecutor({}))
    ej = Ejecutor({})
    inf = correr(con, ej)
    assert inf.omitida and ej.llamadas == []
    assert correr(con, Ejecutor({}), forzar=True).pasos  # --forzar la repite


def test_una_corrida_fallida_no_cuenta_como_hecha(con):
    correr(con, Ejecutor({"ranking": [1, 1]}))
    ej = Ejecutor({})
    correr(con, ej)
    assert ej.llamadas  # se vuelve a intentar el mismo día


def test_ya_corrio_hoy_usa_la_fecha_local(con):
    # Una corrida a las 23:30 hora local puede caer en el día siguiente en UTC.
    local = datetime.combine(HOY, datetime.min.time()).replace(hour=23, minute=30).astimezone()
    con.execute(
        "INSERT INTO corridas (tarea, inicio, estado) VALUES ('diario', ?, 'ok')",
        (local.astimezone(timezone.utc).isoformat(timespec="seconds"),),
    )
    assert diario.ya_corrio_hoy(con, HOY)
    assert not diario.ya_corrio_hoy(con, HOY + timedelta(days=1))


def test_candado_evita_corridas_simultaneas(tmp_path):
    ruta = tmp_path / "diario.lock"
    with diario.Candado(ruta):
        with pytest.raises(RuntimeError, match="en curso"):
            with diario.Candado(ruta):
                pass
    assert not ruta.exists()


def test_candado_abandonado_se_ignora(tmp_path):
    import os

    ruta = tmp_path / "diario.lock"
    ruta.write_text("123")
    viejo = time.time() - 4 * 3600
    os.utime(ruta, (viejo, viejo))
    with diario.Candado(ruta):
        assert ruta.read_text() != "123"


def test_limpiar_logs_guarda_30_dias(tmp_path, monkeypatch):
    monkeypatch.setattr(diario, "LOGS", tmp_path)
    for d in (1, 29, 31, 60):
        (tmp_path / f"{(HOY - timedelta(days=d)).isoformat()}.log").write_text("x")
    (tmp_path / "otro.log").write_text("x")
    diario.limpiar_logs(HOY)
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(
        [f"{(HOY - timedelta(days=d)).isoformat()}.log" for d in (1, 29)] + ["otro.log"]
    )


def test_ultima_linea_util_prefiere_errores():
    assert diario.ultima_linea_util("a\n❌ falló X\nb\nResumen final") == "❌ falló X"
    assert diario.ultima_linea_util("a\n\nResumen final\n") == "Resumen final"


def test_interprete_cambia_pythonw_por_python(monkeypatch, tmp_path):
    (tmp_path / "python.exe").write_text("")
    monkeypatch.setattr(diario.sys, "executable", str(tmp_path / "pythonw.exe"))
    assert diario.interprete().endswith("python.exe")
