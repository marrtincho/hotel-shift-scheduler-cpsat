"""
tests/test_categorias_presencia.py — Categorías con presencia parcial
(dias_presencia), paso 1: modelo de datos, validaciones y API.

Contrato (ver REGLAS.md y herramientas.py, MÓDULO 6):
  - dias_presencia: lista de 1-6 índices de día (0-6) o [] (sin presencia).
  - Con presencia activa: sin_noches se premarca por defecto (desmarcable) y
    excluir_de_equidad queda forzado a True.
  - No se combina con dias_libres_fijos ni se admite en roles protegidos.
  - Retrocompatible: un roles.json sin el campo se autocompleta con [].

Todas las pruebas usan un DATA_DIR temporal, igual que el resto de la suite.
"""

import json

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


# ─── crear_rol ──────────────────────────────────────────────────────────────

def test_crear_rol_con_presencia_premarca_sin_noches_y_excluye_equidad(data_dir):
    from herramientas import crear_rol, cargar_roles

    ok2, msg = crear_rol("refuerzo_finde", {"label": "Refuerzo finde",
                                            "dias_presencia": [5, 6]}, "recepcion")
    assert ok2, msg
    rol = cargar_roles("recepcion")["refuerzo_finde"]
    assert rol["dias_presencia"] == [5, 6]
    assert rol["sin_noches"] is True          # premarcado por defecto
    assert rol["excluir_de_equidad"] is True  # forzado con presencia


def test_crear_rol_con_presencia_sin_noches_explicito_se_respeta(data_dir):
    """El check "sin noches" solo se premarca: un contrato que sí cubre
    noches puede desmarcarlo explícitamente al crear."""
    from herramientas import crear_rol, cargar_roles

    ok2, msg = crear_rol("refuerzo_noches", {"label": "Refuerzo noches",
                                             "dias_presencia": [4, 5],
                                             "sin_noches": False}, "recepcion")
    assert ok2, msg
    rol = cargar_roles("recepcion")["refuerzo_noches"]
    assert rol["sin_noches"] is False
    assert rol["excluir_de_equidad"] is True


def test_crear_rol_sin_presencia_mantiene_defaults(data_dir):
    from herramientas import crear_rol, cargar_roles

    ok2, msg = crear_rol("apoyo", {"label": "Apoyo"}, "recepcion")
    assert ok2, msg
    rol = cargar_roles("recepcion")["apoyo"]
    assert rol["dias_presencia"] == []
    assert rol["sin_noches"] is False
    assert rol["excluir_de_equidad"] is False


def test_crear_rol_presencia_normaliza_orden(data_dir):
    from herramientas import crear_rol, cargar_roles

    ok2, msg = crear_rol("finde", {"label": "Finde", "dias_presencia": [6, 5]}, "recepcion")
    assert ok2, msg
    assert cargar_roles("recepcion")["finde"]["dias_presencia"] == [5, 6]


def test_crear_rol_presencia_incompatible_con_dias_libres_fijos(data_dir):
    from herramientas import crear_rol, cargar_roles

    ok2, _ = crear_rol("mixto", {"label": "Mixto", "dias_presencia": [5, 6],
                                 "dias_libres_fijos": [0, 1]}, "recepcion")
    assert not ok2
    assert "mixto" not in cargar_roles("recepcion")


# ─── validaciones de formato ────────────────────────────────────────────────

@pytest.mark.parametrize("valor", [
    [0, 0],                       # duplicados
    [0, 1, 2, 3, 4, 5, 6],        # 7 días: nunca toda la semana
    ["5", 6],                     # no enteros
    [True],                       # bool no es un día válido
    [7],                          # fuera de rango
    "Lunes",                      # no es lista
])
def test_validar_presencia_formatos_invalidos(data_dir, valor):
    from herramientas import validar_rol

    errores = validar_rol("nuevo_rol", {"dias_presencia": valor}, "recepcion", es_nuevo=True)
    assert errores


def test_validar_presencia_limites_validos(data_dir):
    from herramientas import validar_rol

    for valor in ([], None, [0], [0, 6], [1, 2, 3, 4, 5, 6]):
        assert not validar_rol("nuevo_rol", {"dias_presencia": valor},
                               "recepcion", es_nuevo=True)


# ─── editar_rol ─────────────────────────────────────────────────────────────

def test_editar_rol_anade_presencia_fuerza_excluir_equidad(data_dir):
    from herramientas import crear_rol, editar_rol, cargar_roles

    crear_rol("apoyo", {"label": "Apoyo"}, "recepcion")
    ok2, msg = editar_rol("apoyo", {"dias_presencia": [1, 3, 5],
                                    "excluir_de_equidad": False}, "recepcion")
    assert ok2, msg
    rol = cargar_roles("recepcion")["apoyo"]
    assert rol["dias_presencia"] == [1, 3, 5]
    assert rol["excluir_de_equidad"] is True


def test_editar_rol_quita_presencia_y_permite_reequilibrar(data_dir):
    from herramientas import crear_rol, editar_rol, cargar_roles

    crear_rol("apoyo", {"label": "Apoyo", "dias_presencia": [5, 6]}, "recepcion")
    ok2, msg = editar_rol("apoyo", {"dias_presencia": [],
                                    "excluir_de_equidad": False}, "recepcion")
    assert ok2, msg
    rol = cargar_roles("recepcion")["apoyo"]
    assert rol["dias_presencia"] == []
    assert rol["excluir_de_equidad"] is False


def test_presencia_rechazada_en_roles_protegidos(data_dir):
    from herramientas import validar_rol, editar_rol, cargar_roles

    # "normal": editar_rol filtra el campo (como turno fijo / días libres):
    # la edición no falla, pero la presencia se ignora.
    ok2, _ = editar_rol("normal", {"dias_presencia": [5, 6]}, "recepcion")
    assert ok2
    assert cargar_roles("recepcion")["normal"]["dias_presencia"] == []

    # "night_fixed": protegido con validación explícita (no hay filtro previo).
    ok2, msg = editar_rol("night_fixed", {"dias_presencia": [5, 6],
                                          "dias_libres_fijos": None}, "recepcion")
    assert not ok2
    assert "protegidos" in msg

    errores = validar_rol("normal", {"dias_presencia": [5, 6]}, "recepcion", es_nuevo=False)
    assert any("protegidos" in e for e in errores)


# ─── retrocompatibilidad ────────────────────────────────────────────────────

def test_roles_sin_campo_se_autocompletan(data_dir):
    """Un roles.json guardado antes de dias_presencia debe cargarse sin errores
    y con todos los roles completados a []."""
    import herramientas

    ruta = herramientas._dept_file("roles.json", "recepcion")
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump({
            "jefe": {"label": "Jefe", "icon": "👑", "turno_fijo": "Mañana",
                     "dias_libres_fijos": [5, 6], "dias_libres_sustituto": None,
                     "sin_noches": True, "unico": True, "protegido": False,
                     "borrable": True},
            "normal": {"label": "Empleado", "icon": "", "turno_fijo": None,
                       "dias_libres_fijos": None, "dias_libres_sustituto": None,
                       "sin_noches": False, "unico": False, "protegido": True,
                       "borrable": False},
        }, f)

    roles = herramientas.cargar_roles("recepcion")
    assert roles["jefe"]["dias_presencia"] == []
    assert roles["normal"]["dias_presencia"] == []
    # Y el resto de defaults de retrocompatibilidad siguen aplicándose
    assert roles["jefe"]["excluir_de_equidad"] is False


# ─── API y DeptConfig ───────────────────────────────────────────────────────

def test_api_crear_rol_con_presencia(client, data_dir):
    _sesion(client, uid="1", username="admin", rol="admin", nombre="Admin")

    r = client.post("/api/roles", json={
        "dept": "recepcion", "id": "refuerzo_finde", "label": "Refuerzo finde",
        "dias_presencia": [5, 6], "sin_noches": True,
    })
    assert r.status_code == 200
    assert r.get_json()["ok"] is True

    r_get = client.get("/api/roles?dept=recepcion")
    rol = r_get.get_json()["data"]["refuerzo_finde"]
    assert rol["dias_presencia"] == [5, 6]
    assert rol["sin_noches"] is True
    assert rol["excluir_de_equidad"] is True


def test_dept_config_expone_presencia_roles(data_dir):
    import app as app_module
    from herramientas import crear_rol

    crear_rol("refuerzo_finde", {"label": "Refuerzo finde",
                                 "dias_presencia": [5, 6]}, "recepcion")
    cfg = app_module._get_dept_config("recepcion")
    assert cfg.presencia_roles == {"refuerzo_finde": {5, 6}}
    # Y sin roles con presencia, el parche queda vacío (cero efecto en motores)
    cfg_mozos = app_module._get_dept_config("mozos")
    assert cfg_mozos.presencia_roles == {}
