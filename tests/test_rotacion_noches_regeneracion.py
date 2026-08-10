"""
tests/test_rotacion_noches_regeneracion.py — La ronda de noches avanza una
vez por SEMANA, no una vez por clic en "Generar".

Bug original (reportado por el usuario): cpsat_estado_{dept}.json guardaba un
único estado_rotacion "el último", y cada generación lo leía y volvía a
guardarlo avanzado. Regenerar la MISMA semana (algo que se hace a menudo:
retocar una petición, probar otra configuración) hacía avanzar
`siguiente_turno` y sumar noches en `historico_balance` otra vez, así que la
ronda seguía corriendo sola y a la semana en curso le acababan tocando
compañeros distintos de los que ya figuraban en el horario publicado.

Arreglo (app._estado_rotacion_base_semana / _guardar_estado_cpsat): se guarda
el estado inicial y final de cada semana generada, y regenerar una semana
parte de su inicial ya registrado.
"""

import copy
import json
import os
from datetime import date, timedelta

import pytest


SEMANA_1 = date(2099, 6, 1)  # Lunes en el futuro lejano


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    backup = tmp_path / "backups"
    backup.mkdir()
    import herramientas
    import app as app_module
    monkeypatch.setattr(herramientas, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(herramientas, "BACKUP_DIR", str(backup))
    monkeypatch.setattr(herramientas, "INTERVENCIONES_FILE", str(tmp_path / "intervenciones.json"))
    monkeypatch.setattr(herramientas, "CONFIG_MOTOR_FILE", str(tmp_path / "config_motor.json"))
    monkeypatch.setattr(app_module, "DATA_DIR", str(tmp_path))
    monkeypatch.setenv("USAR_MOTOR_CPSAT", "1")
    return tmp_path


@pytest.fixture
def client(data_dir):
    import app as app_module
    from herramientas import guardar_plantilla
    from modelo_mozos import STAFF_DEFAULT as MOZOS_STAFF

    guardar_plantilla(copy.deepcopy(MOZOS_STAFF), "mozos")
    app_module.app.config["TESTING"] = True
    c = app_module.app.test_client()
    with c.session_transaction() as sess:
        sess["uid"] = "1"
        sess["username"] = "admin"
        sess["rol"] = "admin"
        sess["nombre"] = "Admin"
        sess["vinculado"] = None
    return c


def _generar(client, semana, dept="mozos"):
    r = client.post("/api/horario/generar",
                    json={"semana": semana.isoformat(), "dept": dept})
    assert r.status_code == 200, r.get_json()
    return r.get_json()["data"]["schedule"]


def _estado(data_dir, dept="mozos"):
    ruta = os.path.join(str(data_dir), f"cpsat_estado_{dept}.json")
    with open(ruta, encoding="utf-8") as f:
        return json.load(f)


def _noches_por_persona(schedule):
    return {n: t.count("Noche") for n, t in schedule.items() if "Noche" in t}


def test_regenerar_la_misma_semana_no_avanza_la_ronda(client, data_dir):
    """El caso que reportó el usuario: generar la misma semana tres veces
    seguidas debe dejar la ronda exactamente donde estaba tras la primera."""
    _generar(client, SEMANA_1)
    tras_primera = _estado(data_dir)["estado_rotacion"]

    for _ in range(2):
        _generar(client, SEMANA_1)

    tras_tercera = _estado(data_dir)["estado_rotacion"]

    assert tras_tercera["siguiente_turno"] == tras_primera["siguiente_turno"]
    assert tras_tercera["historico_balance"] == tras_primera["historico_balance"], (
        "las noches se siguen sumando al regenerar la misma semana: "
        f"{tras_primera['historico_balance']} -> {tras_tercera['historico_balance']}"
    )
    assert tras_tercera["deudas"] == tras_primera["deudas"]


def test_regenerar_la_misma_semana_mantiene_a_los_mismos_cubriendo(client, data_dir):
    """La razón de negocio del arreglo: si esta semana les toca a Fulano y
    Mengano, siguen siendo ellos por muchas veces que se regenere."""
    primera = _noches_por_persona(_generar(client, SEMANA_1))
    assert primera, "precondición: alguien tiene que cubrir Noche"

    for _ in range(3):
        assert _noches_por_persona(_generar(client, SEMANA_1)) == primera


def test_una_semana_nueva_si_avanza_la_ronda(client, data_dir):
    """Contrapartida: el arreglo no puede congelar la ronda. Semanas
    DISTINTAS siguen encadenándose y repartiendo las noches."""
    _generar(client, SEMANA_1)
    _generar(client, SEMANA_1)  # regeneración: no debe contar
    balance_1 = _estado(data_dir)["estado_rotacion"]["historico_balance"]

    _generar(client, SEMANA_1 + timedelta(weeks=1))
    balance_2 = _estado(data_dir)["estado_rotacion"]["historico_balance"]

    total_1 = sum(v.get("covers", 0) for v in balance_1.values())
    total_2 = sum(v.get("covers", 0) for v in balance_2.values())
    assert total_2 > total_1, (
        "una semana nueva debe seguir sumando noches al histórico "
        f"({total_1} -> {total_2})"
    )


def test_semana_nueva_parte_del_final_de_la_anterior_no_de_su_regeneracion(
        client, data_dir):
    """La semana 2 debe heredar el estado FINAL de la semana 1, sea cual sea
    el número de veces que se regeneró la semana 1."""
    import app as app_module

    _generar(client, SEMANA_1)
    final_semana_1 = copy.deepcopy(_estado(data_dir)["semanas"]["2099-06-01"]["final"])

    _generar(client, SEMANA_1)
    _generar(client, SEMANA_1)

    cfg = app_module._get_dept_config("mozos")
    staff = app_module.cargar_plantilla("mozos")
    base_semana_2 = app_module._estado_rotacion_base_semana(
        "mozos", SEMANA_1 + timedelta(weeks=1), staff, cfg)

    assert base_semana_2["siguiente_turno"] == final_semana_1["siguiente_turno"]
    assert base_semana_2["historico_balance"] == final_semana_1["historico_balance"]


def test_reordenar_la_ronda_a_mano_se_aplica_al_regenerar(client, data_dir):
    """El estado rebobinado son los CONTADORES, no la configuración: si el
    admin reordena la ronda desde /api/rotacion-noches, ese orden nuevo debe
    aplicarse aunque la semana ya estuviera generada."""
    import app as app_module

    _generar(client, SEMANA_1)

    r = client.get("/api/rotacion-noches?dept=mozos")
    assert r.status_code == 200, r.get_json()
    orden = [e["nombre"] for e in r.get_json()["data"]["orden"]]
    assert len(orden) > 1, "precondición: hace falta más de una persona en la ronda"

    nuevo = list(reversed(orden))
    r = client.put("/api/rotacion-noches", json={"dept": "mozos", "orden": nuevo})
    assert r.status_code == 200, r.get_json()

    cfg = app_module._get_dept_config("mozos")
    staff = app_module.cargar_plantilla("mozos")
    base = app_module._estado_rotacion_base_semana("mozos", SEMANA_1, staff, cfg)
    assert base["orden"] == nuevo

    _generar(client, SEMANA_1)
    assert _estado(data_dir)["estado_rotacion"]["orden"] == nuevo


def test_estado_antiguo_sin_semanas_sigue_funcionando(client, data_dir):
    """Compatibilidad con el fichero que ya está en producción: solo trae
    "estado_rotacion", sin "semanas" -- la primera generación debe partir de
    ahí y empezar a registrar snapshots desde ese momento."""
    import app as app_module
    from herramientas import guardar_json_atomico

    cfg = app_module._get_dept_config("mozos")
    staff = app_module.cargar_plantilla("mozos")
    antiguo = app_module._estado_rotacion_inicial_cpsat(staff, cfg)
    antiguo["historico_balance"] = {antiguo["orden"][0]: {"covers": 7, "pending": 0}}
    guardar_json_atomico(app_module._ruta_estado_cpsat("mozos"),
                         {"estado_rotacion": antiguo})

    base = app_module._estado_rotacion_base_semana("mozos", SEMANA_1, staff, cfg)
    assert base["historico_balance"] == antiguo["historico_balance"]

    _generar(client, SEMANA_1)
    assert "2099-06-01" in _estado(data_dir)["semanas"]
