"""
tests/test_cupos.py — Sistema de cupos mensuales de peticiones de días libres.

Todas las pruebas usan un DATA_DIR temporal (nunca tocan datos reales).
"""

from datetime import date

import pytest


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Redirige DATA_DIR/BACKUP_DIR de herramientas y CUPOS_EXTRA_FILE de cupos
    al directorio temporal de la prueba."""
    backup = tmp_path / "backups"
    backup.mkdir()

    import herramientas
    import cupos
    monkeypatch.setattr(herramientas, "DATA_DIR",   str(tmp_path))
    monkeypatch.setattr(herramientas, "BACKUP_DIR", str(backup))
    monkeypatch.setattr(cupos, "CUPOS_EXTRA_FILE",  str(tmp_path / "cupos_extra.json"))
    return tmp_path


def _crear_peticion(nombre, semana, dias, origen="empleado", dept_id="recepcion"):
    from herramientas import cargar_peticiones, guardar_peticiones, _semana_str
    pet = cargar_peticiones(dept_id)
    key = _semana_str(semana)
    pet.setdefault(key, {})
    pet[key][nombre] = {
        "dias": dias, "la": [], "origen": origen, "estado": "activa",
        "creada_en": date.today().isoformat(),
    }
    guardar_peticiones(pet, dept_id)


# ─── Cupo básico: 2 por mes ───────────────────────────────────────────────────

def test_dos_peticiones_en_mes_ok_tercera_rechazada(data_dir):
    from cupos import validar_nueva_peticion

    # Marzo 2099 (futuro lejano): lunes 2, 9 y 16
    semana1 = date(2099, 3, 2)
    semana2 = date(2099, 3, 9)
    semana3 = date(2099, 3, 16)

    ok1, _ = validar_nueva_peticion("Ana", semana1, ["Lunes"], "empleado")
    assert ok1
    _crear_peticion("Ana", semana1, ["Lunes"])

    ok2, _ = validar_nueva_peticion("Ana", semana2, ["Martes"], "empleado")
    assert ok2
    _crear_peticion("Ana", semana2, ["Martes"])

    ok3, msg3 = validar_nueva_peticion("Ana", semana3, ["Miércoles"], "empleado")
    assert not ok3
    assert "2099-03" in msg3
    assert "cupo" in msg3.lower()


def test_peticion_creada_en_enero_para_marzo_consume_cupo_de_marzo(data_dir):
    from cupos import validar_nueva_peticion, cupo_usado

    semana_marzo = date(2099, 3, 2)  # Lunes de marzo, aunque se "cree" hoy (2026)
    _crear_peticion("Ana", semana_marzo, ["Lunes"])

    assert cupo_usado("Ana", "2099-03") == 1
    assert cupo_usado("Ana", "2026-01") == 0


def test_peticion_viernes_31_sabado_1_consume_un_dia_de_cada_mes(data_dir):
    from cupos import validar_nueva_peticion, cupo_usado

    # 2099-07-31 es viernes; el lunes de esa semana es 2099-07-27
    semana = date(2099, 7, 27)
    assert semana.weekday() == 0
    dias = ["Viernes", "Sábado"]  # 31 jul + 1 ago

    ok, msg = validar_nueva_peticion("Ana", semana, dias, "empleado")
    assert ok, msg
    _crear_peticion("Ana", semana, dias)

    assert cupo_usado("Ana", "2099-07") == 1
    assert cupo_usado("Ana", "2099-08") == 1


# ─── Cancelar libera el cupo ──────────────────────────────────────────────────

def test_cancelar_peticion_libera_cupo(data_dir):
    from herramientas import cargar_peticiones, guardar_peticiones, _semana_str
    from cupos import validar_nueva_peticion

    semana1 = date(2099, 3, 2)
    semana2 = date(2099, 3, 9)
    semana3 = date(2099, 3, 16)

    _crear_peticion("Ana", semana1, ["Lunes"])
    _crear_peticion("Ana", semana2, ["Martes"])

    ok3, _ = validar_nueva_peticion("Ana", semana3, ["Miércoles"], "empleado")
    assert not ok3

    # Cancelar la primera
    pet = cargar_peticiones("recepcion")
    pet[_semana_str(semana1)]["Ana"]["estado"] = "cancelada"
    guardar_peticiones(pet, "recepcion")

    ok3b, msg = validar_nueva_peticion("Ana", semana3, ["Miércoles"], "empleado")
    assert ok3b, msg


# ─── Cupo extra ────────────────────────────────────────────────────────────────

def test_cupo_extra_permite_peticiones_adicionales(data_dir):
    from cupos import validar_nueva_peticion, cargar_cupos_extra, guardar_cupos_extra

    semana1 = date(2099, 3, 2)
    semana2 = date(2099, 3, 9)
    semana3 = date(2099, 3, 16)
    semana4 = date(2099, 3, 23)

    cupos = cargar_cupos_extra()
    cupos.setdefault("Ana", {})["2099-03"] = 1
    guardar_cupos_extra(cupos)

    _crear_peticion("Ana", semana1, ["Lunes"])
    _crear_peticion("Ana", semana2, ["Martes"])

    ok3, msg3 = validar_nueva_peticion("Ana", semana3, ["Miércoles"], "empleado")
    assert ok3, msg3
    _crear_peticion("Ana", semana3, ["Miércoles"])

    ok4, msg4 = validar_nueva_peticion("Ana", semana4, ["Jueves"], "empleado")
    assert not ok4
    assert "2099-03" in msg4


# ─── Peticiones de jefe no consumen cupo ──────────────────────────────────────

def test_peticiones_origen_jefe_no_consumen_cupo(data_dir):
    from cupos import validar_nueva_peticion, cupo_usado

    semana1 = date(2099, 3, 2)
    semana2 = date(2099, 3, 9)
    semana3 = date(2099, 3, 16)
    semana4 = date(2099, 3, 23)

    for semana, dia in ((semana1, "Lunes"), (semana2, "Martes"),
                        (semana3, "Miércoles"), (semana4, "Jueves")):
        _crear_peticion("Ana", semana, [dia], origen="jefe")

    assert cupo_usado("Ana", "2099-03") == 0

    # Un empleado normal debería seguir pudiendo pedir sus 2 peticiones ese mes
    semana5 = date(2099, 3, 30)
    ok, msg = validar_nueva_peticion("Ana", semana5, ["Viernes"], "empleado")
    assert ok, msg


def test_rol_jefe_admin_siempre_ok_sin_cupo(data_dir):
    from cupos import validar_nueva_peticion

    semana1 = date(2099, 3, 2)
    semana2 = date(2099, 3, 9)
    semana3 = date(2099, 3, 16)
    semana4 = date(2099, 3, 23)
    for semana, dia in ((semana1, "Lunes"), (semana2, "Martes"), (semana3, "Miércoles")):
        _crear_peticion("Ana", semana, [dia], origen="empleado")

    ok, msg = validar_nueva_peticion("Ana", semana4, ["Jueves"], "admin")
    assert ok, msg
    ok2, msg2 = validar_nueva_peticion("Ana", semana4, ["Jueves"], "encargado")
    assert ok2, msg2


# ─── Migración de formato antiguo ─────────────────────────────────────────────

def test_migracion_formato_antiguo_no_computa_en_cupo(data_dir):
    from herramientas import guardar_json_atomico, _dept_file, cargar_peticiones
    from cupos import cupo_usado

    ruta = _dept_file("peticiones.json", "recepcion")
    # Formato antiguo: lista de días, sin origen/estado/creada_en
    guardar_json_atomico(ruta, {
        "2099-03-02": {"Ana": ["Lunes", "Martes"]},
        "2099-03-09": {"Pedro": {"dias": ["Miércoles"]}},
    })

    pet = cargar_peticiones("recepcion")
    ana = pet["2099-03-02"]["Ana"]
    pedro = pet["2099-03-09"]["Pedro"]

    for entrada in (ana, pedro):
        assert entrada["origen"] == "jefe"
        assert entrada["estado"] == "activa"
        assert "creada_en" in entrada

    # No computan en el cupo de empleado tras la migración (grandfathering)
    assert cupo_usado("Ana", "2099-03") == 0
    assert cupo_usado("Pedro", "2099-03") == 0

    # La migración se persiste en disco (no se repite cada carga)
    import json
    with open(ruta, encoding="utf-8") as f:
        en_disco = json.load(f)
    assert en_disco["2099-03-02"]["Ana"]["origen"] == "jefe"


# ─── Semana ya generada ────────────────────────────────────────────────────────

def test_peticion_sobre_semana_ya_generada_rechazada(data_dir):
    from herramientas import guardar_historial, _semana_str
    from cupos import validar_nueva_peticion

    semana = date(2099, 3, 2)
    guardar_historial({_semana_str(semana): {"schedule": {}, "staff": []}}, "recepcion")

    ok, msg = validar_nueva_peticion("Ana", semana, ["Lunes"], "empleado")
    assert not ok
    assert "publicada" in msg.lower()

    # También bloquea a jefe/admin
    ok2, msg2 = validar_nueva_peticion("Ana", semana, ["Lunes"], "admin")
    assert not ok2


# ─── Validación del valor de cupo extra ───────────────────────────────────────
# (misma función que usa el endpoint PUT /api/cupos/extra en app.py)

def test_validar_valor_cupo_extra_rechaza_negativos_y_no_numericos(data_dir):
    from cupos import validar_valor_cupo_extra

    ok_neg, valor_neg, _ = validar_valor_cupo_extra(-1)
    assert not ok_neg and valor_neg is None

    ok_str, valor_str, _ = validar_valor_cupo_extra("abc")
    assert not ok_str and valor_str is None

    ok_bool, valor_bool, _ = validar_valor_cupo_extra(True)
    assert not ok_bool

    ok_ok, valor_ok, _ = validar_valor_cupo_extra(2)
    assert ok_ok and valor_ok == 2

    ok_str_num, valor_str_num, _ = validar_valor_cupo_extra("3")
    assert ok_str_num and valor_str_num == 3

    ok_zero, valor_zero, _ = validar_valor_cupo_extra(0)
    assert ok_zero and valor_zero == 0
