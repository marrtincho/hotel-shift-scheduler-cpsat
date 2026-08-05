"""
tests/test_advertencias_estructuradas.py — Fuente única y estructurada de
advertencias del horario (app._calcular_advertencias), usada por igual en
/api/horario/generar, /api/horario/semana (GET) y /api/horario/turno (PUT)
para poder resaltar en el frontend las celdas concretas en conflicto.

Cada advertencia: {"tipo", "mensaje", "celdas": [{"nombre","dia_idx"}, ...]}.
"""

import copy
from dataclasses import dataclass, field
from datetime import date, timedelta

import pytest


SEMANA_INICIO = date(2099, 4, 6)  # Lunes en el futuro lejano


def _cfg(min_coverage=None, max_coverage=None):
    from modelo_base import DeptConfig
    return DeptConfig(
        dept_id="recepcion", dept_label="Recepción",
        night_fixed_days_off=[], night_cover_compensation=[],
        min_coverage=min_coverage or {}, max_coverage=max_coverage or {},
        no_night_roles=set(), fixed_roles={}, fixed_days_off={},
        cover_consecutive=True, night_rotation_key="recepcion",
        allow_non_consecutive_cover=False, prefer_next_week_for_cover=False,
    )


def _persona(nombre, turnos):
    return {"name": nombre, "role": "normal"}, turnos


# ═══════════════════════════════════════════════════════════════════════════
# 1. Unidad — cada tipo de advertencia por separado
# ═══════════════════════════════════════════════════════════════════════════

def test_cobertura_incluye_todas_las_celdas_de_ese_turno_y_dia():
    from app import _calcular_advertencias

    cfg = _cfg(min_coverage={"Mañana": 2})
    staff = [{"name": "Ana", "role": "normal"}, {"name": "Luis", "role": "normal"}]
    schedule = {
        "Ana":  ["Mañana", "Libre", "Libre", "Libre", "Libre", "Libre", "Libre"],
        "Luis": ["Libre", "Libre", "Libre", "Libre", "Libre", "Libre", "Libre"],
    }
    advertencias = _calcular_advertencias(schedule, staff, cfg)

    cobertura = [a for a in advertencias if a["tipo"] == "cobertura"]
    lunes_manana = next(a for a in cobertura if a["mensaje"].startswith("Lunes"))
    assert lunes_manana["mensaje"] == "Lunes – Mañana: 1 (mín 2, máx 99)"
    assert lunes_manana["celdas"] == [{"nombre": "Ana", "dia_idx": 0}]


def test_descanso_incluye_dia_previo_y_actual_de_la_persona():
    from app import _calcular_advertencias

    cfg = _cfg()
    staff = [{"name": "Ana", "role": "normal"}]
    schedule = {"Ana": ["Tarde", "Mañana", "Libre", "Libre", "Libre", "Libre", "Libre"]}
    advertencias = _calcular_advertencias(schedule, staff, cfg)

    descanso = [a for a in advertencias if a["tipo"] == "descanso"]
    assert len(descanso) == 1
    assert descanso[0]["mensaje"] == (
        "Ana: Lunes (Tarde) → Martes (Mañana) — menos de 9h de descanso"
    )
    assert descanso[0]["celdas"] == [
        {"nombre": "Ana", "dia_idx": 0}, {"nombre": "Ana", "dia_idx": 1},
    ]


def test_descanso_cruce_con_domingo_de_semana_anterior():
    from app import _calcular_advertencias

    cfg = _cfg()
    staff = [{"name": "Ana", "role": "normal"}]
    schedule = {"Ana": ["Mañana", "Libre", "Libre", "Libre", "Libre", "Libre", "Libre"]}
    advertencias = _calcular_advertencias(schedule, staff, cfg,
                                          prev_sunday_shifts={"Ana": "Noche"})

    descanso = [a for a in advertencias if a["tipo"] == "descanso"]
    assert len(descanso) == 1
    assert descanso[0]["mensaje"] == (
        "Ana: Domingo anterior (Noche) → Lunes (Mañana) — menos de 9h de descanso"
    )
    # El domingo de la semana pasada no tiene celda en esta parrilla: solo el lunes.
    assert descanso[0]["celdas"] == [{"nombre": "Ana", "dia_idx": 0}]


def test_sin_prev_sunday_shifts_no_genera_advertencia_de_cruce():
    from app import _calcular_advertencias

    cfg = _cfg()
    staff = [{"name": "Ana", "role": "normal"}]
    schedule = {"Ana": ["Mañana", "Libre", "Libre", "Libre", "Libre", "Libre", "Libre"]}
    advertencias = _calcular_advertencias(schedule, staff, cfg)  # sin prev_sunday_shifts
    assert [a for a in advertencias if a["tipo"] == "descanso"] == []


def test_sin_asignar_es_celda_unica():
    from app import _calcular_advertencias

    cfg = _cfg()
    staff = [{"name": "Ana", "role": "normal"}]
    schedule = {"Ana": ["Mañana", "SIN_ASIGNAR", "Libre", "Libre", "Libre", "Libre", "Libre"]}
    advertencias = _calcular_advertencias(schedule, staff, cfg)

    sin_asignar = [a for a in advertencias if a["tipo"] == "sin_asignar"]
    assert len(sin_asignar) == 1
    assert sin_asignar[0]["mensaje"] == "Ana: Martes sin turno asignable"
    assert sin_asignar[0]["celdas"] == [{"nombre": "Ana", "dia_idx": 1}]


def test_sin_violaciones_devuelve_lista_vacia():
    from app import _calcular_advertencias

    cfg = _cfg(min_coverage={"Mañana": 1}, max_coverage={"Mañana": 1})
    staff = [{"name": "Ana", "role": "normal"}]
    # Mañana los 7 días: cumple min=max=1 cada día; sin cruces Tarde/Noche→Mañana.
    schedule = {"Ana": ["Mañana"] * 7}
    assert _calcular_advertencias(schedule, staff, cfg) == []


# ═══════════════════════════════════════════════════════════════════════════
# 2. Integración — los tres endpoints unificados
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    backup = tmp_path / "backups"
    backup.mkdir()
    import herramientas
    import cupos
    import app as app_module
    monkeypatch.setattr(herramientas, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(herramientas, "BACKUP_DIR", str(backup))
    monkeypatch.setattr(herramientas, "INTERVENCIONES_FILE", str(tmp_path / "intervenciones.json"))
    monkeypatch.setattr(cupos, "CUPOS_EXTRA_FILE", str(tmp_path / "cupos_extra.json"))
    # CONFIG_MOTOR_FILE (interruptor del motor CP-SAT) se calcula sobre
    # herramientas.DATA_DIR en el momento de IMPORTAR el módulo -- sin
    # aislarlo aquí también, motor_cpsat_activo() leería el fichero REAL
    # de producción (data/config_motor.json) como fallback, contaminando
    # este test con el estado real del switch en vez de quedar aislado.
    monkeypatch.setattr(herramientas, "CONFIG_MOTOR_FILE", str(tmp_path / "config_motor.json"))
    monkeypatch.setattr(app_module, "DATA_DIR", str(tmp_path))
    monkeypatch.delenv("USAR_MOTOR_CPSAT", raising=False)
    return tmp_path


@pytest.fixture
def client(data_dir):
    import app as app_module
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def _sesion(client, *, uid, username, rol, nombre, vinculado):
    with client.session_transaction() as sess:
        sess["uid"] = uid
        sess["username"] = username
        sess["rol"] = rol
        sess["nombre"] = nombre
        sess["vinculado"] = vinculado


def _es_estructura_valida(advertencias):
    if not isinstance(advertencias, list):
        return False
    for a in advertencias:
        if not isinstance(a, dict):
            return False
        if set(a.keys()) != {"tipo", "mensaje", "celdas"}:
            return False
        if a["tipo"] not in ("cobertura", "descanso", "sin_asignar"):
            return False
        if not isinstance(a["mensaje"], str):
            return False
        if not all(set(c.keys()) == {"nombre", "dia_idx"} for c in a["celdas"]):
            return False
    return True


def test_generar_devuelve_advertencias_estructuradas(client, data_dir, monkeypatch):
    """Fuerza un resultado de generación con una violación de descanso
    conocida (vía un doble monkeypatch de generate_schedule_valido), para
    comprobar que /api/horario/generar ya no expone la lista de strings de
    validar_schedule sino el mismo formato estructurado que los otros dos
    endpoints."""
    from herramientas import guardar_plantilla
    import modelo_base

    nombres = [f"P{i}" for i in range(10)]
    guardar_plantilla([{"name": n, "role": "normal", "preferences": []} for n in nombres],
                       "recepcion")

    schedule_fijo = {
        n: (["Tarde", "Mañana", "Libre", "Libre", "Libre", "Libre", "Libre"] if n == "P0"
            else ["Libre"] * 7)
        for n in nombres
    }

    def fake_generate_schedule_valido(staff_list, week_start, cfg, prev_week_covers=None,
                                       prev_sunday_shifts=None, prev_last_shifts=None,
                                       max_intentos=50):
        return schedule_fijo, staff_list, week_start, {}, {}, ["violacion string interna irrelevante"]

    monkeypatch.setattr(modelo_base, "generate_schedule_valido", fake_generate_schedule_valido)

    _sesion(client, uid="1", username="admin", rol="admin", nombre="Admin", vinculado=None)
    r = client.post("/api/horario/generar", json={
        "semana": SEMANA_INICIO.isoformat(), "dept": "recepcion",
    })
    assert r.status_code == 200
    advertencias = r.get_json()["data"]["advertencias"]

    assert _es_estructura_valida(advertencias)
    descanso = [a for a in advertencias if a["tipo"] == "descanso"]
    assert descanso and descanso[0]["celdas"] == [
        {"nombre": "P0", "dia_idx": 0}, {"nombre": "P0", "dia_idx": 1},
    ]
    # No es la lista de strings interna de generate_schedule_valido.
    assert "violacion string interna irrelevante" not in advertencias


def test_los_tres_endpoints_devuelven_la_misma_estructura_ante_el_mismo_conflicto(client, data_dir):
    """Semana ya generada (seed directo de historial) con una violación de
    descanso conocida: GET /horario/semana y PUT /horario/turno (con una
    edición que no toca la celda conflictiva) deben reportar exactamente
    la misma advertencia estructurada."""
    from herramientas import guardar_plantilla, cargar_historial, guardar_historial, _semana_str

    nombres = [f"P{i}" for i in range(10)]
    guardar_plantilla([{"name": n, "role": "normal", "preferences": []} for n in nombres],
                       "recepcion")

    schedule_fijo = {
        n: (["Tarde", "Mañana", "Libre", "Libre", "Libre", "Libre", "Libre"] if n == "P0"
            else ["Libre"] * 7)
        for n in nombres
    }
    historial = cargar_historial("recepcion")
    historial[_semana_str(SEMANA_INICIO)] = {
        "schedule": schedule_fijo,
        "staff": [{"name": n, "role": "normal"} for n in nombres],
    }
    guardar_historial(historial, "recepcion")

    _sesion(client, uid="1", username="admin", rol="admin", nombre="Admin", vinculado=None)

    r_get = client.get(f"/api/horario/semana?dept=recepcion&semana={SEMANA_INICIO.isoformat()}")
    adv_get = r_get.get_json()["data"]["advertencias"]
    assert _es_estructura_valida(adv_get)
    descanso_get = [a for a in adv_get if a["tipo"] == "descanso"]
    assert descanso_get == [{
        "tipo": "descanso",
            "mensaje": "P0: Lunes (Tarde) → Martes (Mañana) — menos de 9h de descanso",
        "celdas": [{"nombre": "P0", "dia_idx": 0}, {"nombre": "P0", "dia_idx": 1}],
    }]

    # Edición de un turno que no afecta a la celda en conflicto (P1, Miércoles).
    r_put = client.put("/api/horario/turno", json={
        "dept": "recepcion", "semana": SEMANA_INICIO.isoformat(),
        "nombre": "P1", "dia_idx": 2, "turno": "Mañana",
        "justificacion": "Ajuste de prueba sin impacto en la celda conflictiva",
    })
    assert r_put.status_code == 200
    adv_put = r_put.get_json()["data"]["advertencias"]
    assert _es_estructura_valida(adv_put)
    descanso_put = [a for a in adv_put if a["tipo"] == "descanso"]
    assert descanso_put == descanso_get
