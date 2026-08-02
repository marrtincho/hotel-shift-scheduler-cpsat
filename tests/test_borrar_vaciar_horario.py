"""
tests/test_borrar_vaciar_horario.py — Dos acciones nuevas sobre una semana
ya generada, ambas vía /api/horario:

  - DELETE /api/horario/semana ("Borrar"): elimina el horario generado de
    la semana, dejándola como si nunca se hubiera generado. Disponible
    para encargado y admin (permiso "eliminar_horario_semana").
  - POST /api/horario/vaciar ("Asignar a mano"): sustituye el horario de
    la semana por uno en blanco (todas las celdas SIN_ASIGNAR), sin pasar
    por el solver, para que se rellene celda a celda con
    /api/horario/turno. Solo admin (permiso "vaciar_horario_manual").

Mismo patrón de fixtures que el resto de la suite (DATA_DIR aislado en
tmp_path, sesión forjada en el test client de Flask).
"""

import copy
from datetime import date, timedelta

import pytest


SEMANA = date(2099, 7, 6)  # Lunes en el futuro lejano


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    backup = tmp_path / "backups"
    backup.mkdir()
    import herramientas
    import app as app_module
    monkeypatch.setattr(herramientas, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(herramientas, "BACKUP_DIR", str(backup))
    monkeypatch.setattr(herramientas, "INTERVENCIONES_FILE", str(tmp_path / "intervenciones.json"))
    monkeypatch.setattr(app_module, "DATA_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def client(data_dir):
    import app as app_module
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def _sesion(client, *, uid, username, rol, nombre, vinculado=None):
    with client.session_transaction() as sess:
        sess["uid"] = uid
        sess["username"] = username
        sess["rol"] = rol
        sess["nombre"] = nombre
        sess["vinculado"] = vinculado


def _seed_plantilla(dept_id="recepcion"):
    from herramientas import guardar_plantilla
    from modelo_recepcion import STAFF_DEFAULT as RECEPCION_STAFF
    guardar_plantilla(copy.deepcopy(RECEPCION_STAFF), dept_id)
    return [p["name"] for p in RECEPCION_STAFF]


def _generar(client, semana=SEMANA, dept="recepcion"):
    return client.post("/api/horario/generar", json={"semana": semana.isoformat(), "dept": dept})


# ═══════════════════════════════════════════════════════════════════════════
# Permisos
# ═══════════════════════════════════════════════════════════════════════════

def test_empleado_no_puede_borrar_ni_vaciar(client, data_dir):
    _seed_plantilla()
    _sesion(client, uid="1", username="encargado", rol="encargado", nombre="Encargado")
    assert _generar(client).status_code == 200
    _sesion(client, uid="2", username="ana", rol="empleado", nombre="Ana", vinculado="Ana")

    r = client.delete(f"/api/horario/semana?semana={SEMANA.isoformat()}&dept=recepcion")
    assert r.status_code == 403

    r = client.post("/api/horario/vaciar", json={"semana": SEMANA.isoformat(), "dept": "recepcion"})
    assert r.status_code == 403


def test_encargado_puede_borrar_pero_no_vaciar(client, data_dir):
    _seed_plantilla()
    _sesion(client, uid="1", username="encargado", rol="encargado", nombre="Encargado")
    assert _generar(client).status_code == 200

    r = client.post("/api/horario/vaciar", json={"semana": SEMANA.isoformat(), "dept": "recepcion"})
    assert r.status_code == 403

    r = client.delete(f"/api/horario/semana?semana={SEMANA.isoformat()}&dept=recepcion")
    assert r.status_code == 200
    assert r.get_json()["ok"] is True


def test_admin_puede_borrar_y_vaciar(client, data_dir):
    _seed_plantilla()
    _sesion(client, uid="1", username="admin", rol="admin", nombre="Admin")
    assert _generar(client).status_code == 200

    r = client.post("/api/horario/vaciar", json={"semana": SEMANA.isoformat(), "dept": "recepcion"})
    assert r.status_code == 200

    r = client.delete(f"/api/horario/semana?semana={SEMANA.isoformat()}&dept=recepcion")
    assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════════════════
# Comportamiento — borrar
# ═══════════════════════════════════════════════════════════════════════════

def test_borrar_deja_la_semana_como_nunca_generada(client, data_dir):
    _seed_plantilla()
    _sesion(client, uid="1", username="admin", rol="admin", nombre="Admin")
    assert _generar(client).status_code == 200

    r = client.get(f"/api/horario/semana?dept=recepcion&semana={SEMANA.isoformat()}")
    assert r.get_json()["data"]["existe"] is True

    r = client.delete(f"/api/horario/semana?semana={SEMANA.isoformat()}&dept=recepcion")
    assert r.status_code == 200
    assert r.get_json()["data"]["existe"] is False

    r = client.get(f"/api/horario/semana?dept=recepcion&semana={SEMANA.isoformat()}")
    assert r.get_json()["data"]["existe"] is False


def test_borrar_semana_sin_horario_devuelve_404(client, data_dir):
    _seed_plantilla()
    _sesion(client, uid="1", username="admin", rol="admin", nombre="Admin")
    r = client.delete(f"/api/horario/semana?semana={SEMANA.isoformat()}&dept=recepcion")
    assert r.status_code == 404


def test_borrar_semana_pasada_bloqueada_solo_admin(client, data_dir):
    _seed_plantilla()
    semana_pasada = date(2020, 1, 6)  # Lunes en el pasado lejano
    _sesion(client, uid="1", username="admin", rol="admin", nombre="Admin")
    assert _generar(client, semana=semana_pasada).status_code == 200

    _sesion(client, uid="2", username="encargado", rol="encargado", nombre="Encargado")
    r = client.delete(f"/api/horario/semana?semana={semana_pasada.isoformat()}&dept=recepcion")
    assert r.status_code == 403

    _sesion(client, uid="1", username="admin", rol="admin", nombre="Admin")
    r = client.delete(f"/api/horario/semana?semana={semana_pasada.isoformat()}&dept=recepcion")
    assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════════════════
# Comportamiento — vaciar
# ═══════════════════════════════════════════════════════════════════════════

def test_vaciar_produce_horario_en_blanco_editable_a_mano(client, data_dir):
    nombres = _seed_plantilla()
    _sesion(client, uid="1", username="admin", rol="admin", nombre="Admin")

    r = client.post("/api/horario/vaciar", json={"semana": SEMANA.isoformat(), "dept": "recepcion"})
    assert r.status_code == 200
    data = r.get_json()["data"]

    assert set(data["schedule"].keys()) == set(nombres)
    for turnos in data["schedule"].values():
        assert turnos == ["SIN_ASIGNAR"] * 7

    # El historial queda persistido -- no es solo la respuesta de la petición.
    r2 = client.get(f"/api/horario/semana?dept=recepcion&semana={SEMANA.isoformat()}")
    data2 = r2.get_json()["data"]
    assert data2["existe"] is True
    assert data2["schedule"][nombres[0]] == ["SIN_ASIGNAR"] * 7

    # Y se puede rellenar a mano con el endpoint de edición existente.
    r3 = client.put("/api/horario/turno", json={
        "dept": "recepcion", "semana": SEMANA.isoformat(),
        "nombre": nombres[0], "dia_idx": 0, "turno": "Mañana",
        "justificacion": "Asignación manual de prueba",
    })
    assert r3.status_code == 200
    assert r3.get_json()["data"]["schedule"][nombres[0]][0] == "Mañana"


def test_vaciar_sustituye_un_horario_ya_generado(client, data_dir):
    nombres = _seed_plantilla()
    _sesion(client, uid="1", username="admin", rol="admin", nombre="Admin")
    r = _generar(client)
    assert r.status_code == 200
    # El horario generado normalmente no es todo SIN_ASIGNAR.
    generado = r.get_json()["data"]["schedule"]
    assert any(t != "SIN_ASIGNAR" for turnos in generado.values() for t in turnos)

    r = client.post("/api/horario/vaciar", json={"semana": SEMANA.isoformat(), "dept": "recepcion"})
    assert r.status_code == 200
    for turnos in r.get_json()["data"]["schedule"].values():
        assert turnos == ["SIN_ASIGNAR"] * 7
