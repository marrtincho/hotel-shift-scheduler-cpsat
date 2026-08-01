"""
tests/test_permisos.py — Permisos por rol sobre /api/plantilla.

Cubre la corrección del bug: el rol "encargado" tenía "ver_plantilla" pero
no "editar_plantilla" en auth.PERMISOS, así que no podía añadir, editar ni
eliminar empleados aunque los botones de la interfaz estuvieran visibles
para él. La corrección añade "editar_plantilla" a PERMISOS["encargado"].

Todas las pruebas usan un DATA_DIR temporal y sesiones forjadas en el test
client de Flask (sin login real), igual que el resto de la suite.
"""

import pytest


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


def _seed_plantilla(nombres, dept_id="recepcion"):
    from herramientas import guardar_plantilla
    guardar_plantilla(
        [{"name": n, "role": "normal", "preferences": []} for n in nombres],
        dept_id,
    )


# ─── El encargado puede editar la plantilla (bug corregido) ────────────────

def test_encargado_puede_crear_empleado(client, data_dir):
    _seed_plantilla(["Ana"])
    _sesion(client, uid="1", username="encargado", rol="encargado", nombre="Encargado")

    r = client.post("/api/plantilla", json={"name": "Luis", "role": "normal"})
    assert r.status_code == 200
    assert r.get_json()["ok"] is True


def test_encargado_puede_editar_empleado(client, data_dir):
    _seed_plantilla(["Ana"])
    _sesion(client, uid="1", username="encargado", rol="encargado", nombre="Encargado")

    r = client.put("/api/plantilla/0", json={"name": "Ana María"})
    assert r.status_code == 200
    assert r.get_json()["ok"] is True


def test_encargado_puede_eliminar_empleado(client, data_dir):
    _seed_plantilla(["Ana", "Luis"])
    _sesion(client, uid="1", username="encargado", rol="encargado", nombre="Encargado")

    r = client.delete("/api/plantilla/0")
    assert r.status_code == 200
    assert r.get_json()["ok"] is True


# ─── El empleado sigue sin poder tocar la plantilla ─────────────────────────

def test_empleado_no_puede_crear_empleado(client, data_dir):
    _seed_plantilla(["Ana"])
    _sesion(client, uid="2", username="ana", rol="empleado", nombre="Ana", vinculado="Ana")

    r = client.post("/api/plantilla", json={"name": "Luis", "role": "normal"})
    assert r.status_code == 403


def test_empleado_no_puede_editar_empleado(client, data_dir):
    _seed_plantilla(["Ana"])
    _sesion(client, uid="2", username="ana", rol="empleado", nombre="Ana", vinculado="Ana")

    r = client.put("/api/plantilla/0", json={"name": "Ana María"})
    assert r.status_code == 403


def test_empleado_no_puede_eliminar_empleado(client, data_dir):
    _seed_plantilla(["Ana"])
    _sesion(client, uid="2", username="ana", rol="empleado", nombre="Ana", vinculado="Ana")

    r = client.delete("/api/plantilla/0")
    assert r.status_code == 403


# ─── El encargado NO gana permisos más allá de la plantilla ────────────────

def test_encargado_sigue_sin_gestionar_usuarios(client, data_dir):
    _sesion(client, uid="1", username="encargado", rol="encargado", nombre="Encargado")

    r_get  = client.get("/api/usuarios")
    r_post = client.post("/api/usuarios", json={
        "username": "nuevo", "nombre": "Nuevo", "password": "x1234567", "rol": "empleado",
    })
    assert r_get.status_code == 403
    assert r_post.status_code == 403


def test_encargado_sigue_sin_eliminar_historial(client, data_dir):
    _sesion(client, uid="1", username="encargado", rol="encargado", nombre="Encargado")

    r = client.post("/api/historial/limpiar", json={"dept": "recepcion"})
    assert r.status_code == 403


# ─── Las validaciones de plantilla se aplican igual para encargado y admin ──

def test_validaciones_de_plantilla_aplican_igual_para_encargado(client, data_dir):
    """Un rol 'único' (p.ej. jefe) no puede duplicarse, tanto si lo intenta
    el encargado como el admin — la regla de negocio no distingue por rol."""
    from herramientas import guardar_plantilla
    guardar_plantilla(
        [{"name": "Juan", "role": "jefe", "preferences": []}],
        "recepcion",
    )

    _sesion(client, uid="1", username="encargado", rol="encargado", nombre="Encargado")
    r_enc = client.post("/api/plantilla", json={"name": "Pedro", "role": "jefe"})
    assert r_enc.status_code == 400
    assert "solo puede asignarse a una persona" in r_enc.get_json()["error"]

    _sesion(client, uid="2", username="admin", rol="admin", nombre="Admin")
    r_admin = client.post("/api/plantilla", json={"name": "Pedro", "role": "jefe"})
    assert r_admin.status_code == 400
    assert "solo puede asignarse a una persona" in r_admin.get_json()["error"]
