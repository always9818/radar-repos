"""Pruebas del chequeo de salud (fase 6)."""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from radar import db, salud

AHORA = datetime(2026, 10, 23, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def con(tmp_path):
    c = db.conectar(tmp_path / "radar.db")
    db.aplicar_esquema(c)
    yield c
    c.close()


def corrida(con, tarea, estado, hace_horas):
    inicio = (AHORA - timedelta(hours=hace_horas)).isoformat(timespec="seconds")
    con.execute("INSERT INTO corridas (tarea, inicio, estado) VALUES (?, ?, ?)", (tarea, inicio, estado))
    con.commit()


def sano(con):
    for f in salud.FUENTES:
        corrida(con, f, "ok", 5)
    corrida(con, "diario", "ok", 5)


def test_radar_sano(con):
    sano(con)
    inf = salud.revisar(con, ahora=AHORA)
    assert inf.ok and inf.avisos == [], (inf.errores, inf.avisos)


def test_sin_corrida_diaria_reciente_es_error(con):
    sano(con)
    con.execute("UPDATE corridas SET inicio = ? WHERE tarea = 'diario'", ((AHORA - timedelta(hours=40)).isoformat(),))
    inf = salud.revisar(con, max_horas=30, ahora=AHORA)
    assert not inf.ok and "hace 40 h" in inf.errores[0]


def test_nunca_corrio_es_error(con):
    assert "Nunca hubo" in salud.revisar(con, ahora=AHORA).errores[0]


def test_una_falla_es_aviso_y_tres_seguidas_error(con):
    sano(con)
    corrida(con, "reddit", "error", 4)
    inf = salud.revisar(con, ahora=AHORA)
    assert inf.ok and "reddit: su última corrida falló" in inf.avisos
    corrida(con, "reddit", "error", 3)
    corrida(con, "reddit", "error", 2)
    inf = salud.revisar(con, ahora=AHORA)
    assert not inf.ok and "reddit: lleva 3 corridas seguidas fallando" in inf.errores


def test_corrida_colgada_es_aviso(con):
    sano(con)
    corrida(con, "ranking", "en_curso", 6)
    assert any("sin cerrar" in a for a in salud.revisar(con, ahora=AHORA).avisos)


def test_solo_integridad_ignora_fuentes_caidas(con):
    for h in (3, 2, 1):
        corrida(con, "reddit", "error", h)
    assert not salud.revisar(con, ahora=AHORA).ok
    assert salud.revisar(con, ahora=AHORA, solo_integridad=True).ok


def test_comparar_detecta_perdida_de_datos(con, tmp_path):
    con.execute("INSERT INTO repos (id, full_name, url, primera_vez, ultima_vez) VALUES (1, 'a/b', 'u', 'x', 'x')")
    con.execute("INSERT INTO repos (id, full_name, url, primera_vez, ultima_vez) VALUES (2, 'c/d', 'u', 'x', 'x')")
    con.commit()
    anterior = tmp_path / "anterior.db"
    salud.respaldar(con, anterior)
    assert salud.comparar(con, anterior) == []
    con.execute("DELETE FROM repos WHERE id = 2")
    con.commit()
    assert "pasó de 2 a 1" in salud.comparar(con, anterior)[0]


def test_comparar_sin_base_anterior(con, tmp_path):
    assert salud.comparar(con, tmp_path / "no-existe.db") == []


def test_respaldo_es_una_base_completa_y_sin_wal(con, tmp_path):
    sano(con)
    destino = tmp_path / "respaldo.db"
    salud.respaldar(con, destino)
    copia = sqlite3.connect(destino)
    assert copia.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert copia.execute("SELECT COUNT(*) FROM corridas").fetchone()[0] == len(salud.FUENTES) + 1
    copia.close()
