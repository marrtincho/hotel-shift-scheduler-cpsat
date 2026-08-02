"""
tests/test_cobertura_jefatura.py — Un rol puede cubrir la ausencia de otro
(caso de uso: subjefe cubre al jefe), configurable vía roles.json
(cubre_ausencia_de / cobertura_semana_completa / excluir_de_equidad).

Todas las pruebas usan semanas en futuro lejano (año 2099) y un DATA_DIR
temporal, nunca tocan datos reales de producción. Los escenarios de un solo
día usan generate_schedule_valido (con reintentos, como en producción) para
poder afirmar "sin violaciones" de forma determinista — generate_schedule de
una sola pasada no lo garantiza, por eso también existe la versión "_valido".
La prueba de equidad a largo plazo usa generate_schedule directamente, igual
que test_equidad_findes.py, ya que ahí lo relevante es la tendencia agregada
en muchas semanas, no que cada semana individual sea perfecta.
"""

import copy
from datetime import date, timedelta

import pytest

from modelo_base import (
    DAYS, generate_schedule, generate_schedule_valido, _get_weekend_ratios,
)

SEMANA_INICIO = date(2099, 6, 1)  # Lunes en futuro lejano


# ─── Fixture ─────────────────────────────────────────────────────────────────

@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    backup = tmp_path / "backups"
    backup.mkdir()
    import herramientas
    monkeypatch.setattr(herramientas, "DATA_DIR",      str(tmp_path))
    monkeypatch.setattr(herramientas, "BACKUP_DIR",    str(backup))
    monkeypatch.setattr(herramientas, "FESTIVOS_FILE", str(tmp_path / "festivos.json"))
    return tmp_path


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _cfg_con_cobertura(semana_completa: bool = False, cubrir_dias_libres_fijos: bool = False):
    """RECEPCION_CONFIG con subjefe configurado para cubrir la ausencia del
    jefe, tal y como lo dejaría _get_dept_config al leer roles.json con las
    casillas activadas."""
    from modelo_recepcion import RECEPCION_CONFIG
    cfg = copy.deepcopy(RECEPCION_CONFIG)
    cfg.cobertura_roles = {"subjefe": {"cubre": "jefe", "semana_completa": semana_completa,
                                        "dias_libres_fijos": cubrir_dias_libres_fijos}}
    cfg.roles_excluidos_equidad = {"night_fixed", "jefe", "subjefe"}
    return cfg


def _plantilla():
    from modelo_recepcion import STAFF_DEFAULT
    return copy.deepcopy(STAFF_DEFAULT)


def _plantilla_holgada():
    """STAFF_DEFAULT + 1 persona normal extra. Con el jefe ausente toda la
    semana, el subjefe queda fijado a Mañana todos sus días de trabajo y dos
    personas (jefe ausente + subjefe fijado) dejan muy poco margen para
    cubrir Tarde en los días que el sustituto nocturno también resta
    flexibilidad — un cuerpo extra evita ese cuello de botella de plantilla
    (no relacionado con la lógica de cobertura en sí, solo con el margen de
    personal disponible)."""
    staff = _plantilla()
    staff.append({"name": "Extra", "role": "normal", "preferences": []})
    return staff


def _assert_cobertura_finde_ok(schedule, cfg):
    """Sábado y Domingo nunca deben quedar con menos personas disponibles
    (no libres) que el mínimo total de cobertura — mismo criterio que
    test_equidad_findes.test_cobertura_minima_finde."""
    total_minimo = sum(cfg.min_coverage.values())
    for day_idx in [5, 6]:
        disponibles = sum(1 for t in schedule.values() if t[day_idx] != "Libre")
        assert disponibles >= total_minimo, (
            f"Personas disponibles insuficientes en día {day_idx}: "
            f"{disponibles} < {total_minimo}\n{schedule}"
        )


# ═══════════════════════════════════════════════════════════════════════════
# 1. Ausencia de un día suelto (petición del jefe)
# ═══════════════════════════════════════════════════════════════════════════

def test_dia_suelto_de_ausencia_subjefe_cubre_solo_ese_dia(data_dir):
    cfg = _cfg_con_cobertura(semana_completa=False)
    staff = _plantilla()
    juan = next(p for p in staff if p["name"] == "Juan")
    juan["forced_days_off"] = ["Martes"]

    full_schedule, staff_final, *_, violaciones = generate_schedule_valido(staff, SEMANA_INICIO, cfg)

    laura = next(p for p in staff_final if p["name"] == "Laura")
    assert laura["cubre_rol"] == "jefe"
    assert laura["dias_cobertura_jefatura"] == [1]  # Martes
    assert not laura.get("cobertura_total_semana")

    assert full_schedule["Laura"][1] == "Mañana"  # cubre el turno fijo del jefe
    assert len(laura["days_off"]) == 2  # rotación normal, no forzada a semana completa

    assert violaciones == []
    _assert_cobertura_finde_ok(full_schedule, cfg)


# ═══════════════════════════════════════════════════════════════════════════
# 2. Días libres FIJOS del jefe (Sáb+Dom): no son ausencia
# ═══════════════════════════════════════════════════════════════════════════

def test_dias_libres_fijos_del_jefe_no_cuentan_como_ausencia(data_dir):
    """Test clave contra el error de tratar los libres fijos como ausencia:
    si se tratasen como tal, el subjefe acabaría trabajando todos los
    findes del año."""
    cfg = _cfg_con_cobertura(semana_completa=False)
    staff = _plantilla()  # Juan (jefe) sin peticiones ni vacaciones

    full_schedule, staff_final, *_, violaciones = generate_schedule_valido(staff, SEMANA_INICIO, cfg)

    laura = next(p for p in staff_final if p["name"] == "Laura")
    assert laura["cubre_rol"] == "jefe"
    assert laura["dias_cobertura_jefatura"] == []
    assert not laura.get("cobertura_total_semana")

    # El jefe conserva sus libres fijos Sáb+Dom con normalidad.
    assert full_schedule["Juan"][5] == "Libre"
    assert full_schedule["Juan"][6] == "Libre"

    assert violaciones == []
    _assert_cobertura_finde_ok(full_schedule, cfg)


# ═══════════════════════════════════════════════════════════════════════════
# 3. Ausencia de semana completa — Opción A (cobertura_semana_completa=False)
# ═══════════════════════════════════════════════════════════════════════════

def test_semana_completa_opcion_a_no_fuerza_presencia_en_finde(data_dir):
    cfg = _cfg_con_cobertura(semana_completa=False)
    staff = _plantilla_holgada()
    juan = next(p for p in staff if p["name"] == "Juan")
    juan["forced_days_off"] = list(DAYS)  # vacaciones toda la semana

    full_schedule, staff_final, *_, violaciones = generate_schedule_valido(staff, SEMANA_INICIO, cfg)

    laura = next(p for p in staff_final if p["name"] == "Laura")
    assert laura["dias_cobertura_jefatura"] == list(range(7))
    assert not laura.get("cobertura_total_semana")  # la casilla está desactivada

    # En todos los días que SÍ trabaja, hace el turno fijo del jefe (Mañana).
    for d, turno in enumerate(full_schedule["Laura"]):
        if turno != "Libre":
            assert turno == "Mañana", f"Día {d}: Laura hizo {turno} en vez de Mañana"

    # Conserva su rotación normal de 2 libres (puede caerle el finde: no hay
    # exclusión dura de Sáb+Dom en la opción A).
    assert len(laura["days_off"]) == 2

    assert violaciones == []
    _assert_cobertura_finde_ok(full_schedule, cfg)


# ═══════════════════════════════════════════════════════════════════════════
# 4. Ausencia de semana completa — Opción B (cobertura_semana_completa=True)
# ═══════════════════════════════════════════════════════════════════════════

def test_semana_completa_opcion_b_presencia_total_sin_finde(data_dir):
    cfg = _cfg_con_cobertura(semana_completa=True)
    staff = _plantilla_holgada()
    juan = next(p for p in staff if p["name"] == "Juan")
    juan["forced_days_off"] = list(DAYS)

    full_schedule, staff_final, *_, violaciones = generate_schedule_valido(staff, SEMANA_INICIO, cfg)

    laura = next(p for p in staff_final if p["name"] == "Laura")
    assert laura["cobertura_total_semana"] is True

    for d, turno in enumerate(full_schedule["Laura"]):
        if turno != "Libre":
            assert turno == "Mañana", f"Día {d}: Laura hizo {turno} en vez de Mañana"

    # Presencia de mando en fin de semana: sus 2 libres nunca caen en Sáb+Dom.
    dias_libres = {d for d in range(7) if full_schedule["Laura"][d] == "Libre"}
    assert dias_libres.isdisjoint({5, 6}), f"Laura libra en fin de semana: {dias_libres}"
    assert len(dias_libres) == 2

    assert violaciones == []
    _assert_cobertura_finde_ok(full_schedule, cfg)


# ═══════════════════════════════════════════════════════════════════════════
# 5. Rol "jefe" inexistente en la plantilla → ausencia de semana completa
# ═══════════════════════════════════════════════════════════════════════════

def test_jefe_inexistente_opcion_a(data_dir):
    cfg = _cfg_con_cobertura(semana_completa=False)
    staff = [p for p in _plantilla_holgada() if p["name"] != "Juan"]  # sin jefe

    full_schedule, staff_final, *_, violaciones = generate_schedule_valido(staff, SEMANA_INICIO, cfg)

    laura = next(p for p in staff_final if p["name"] == "Laura")
    assert laura["dias_cobertura_jefatura"] == list(range(7))
    assert not laura.get("cobertura_total_semana")
    for d, turno in enumerate(full_schedule["Laura"]):
        if turno != "Libre":
            assert turno == "Mañana"

    assert violaciones == []
    _assert_cobertura_finde_ok(full_schedule, cfg)


def test_jefe_inexistente_opcion_b(data_dir):
    cfg = _cfg_con_cobertura(semana_completa=True)
    staff = [p for p in _plantilla_holgada() if p["name"] != "Juan"]  # sin jefe

    full_schedule, staff_final, *_, violaciones = generate_schedule_valido(staff, SEMANA_INICIO, cfg)

    laura = next(p for p in staff_final if p["name"] == "Laura")
    assert laura["cobertura_total_semana"] is True
    for d, turno in enumerate(full_schedule["Laura"]):
        if turno != "Libre":
            assert turno == "Mañana"
    dias_libres = {d for d in range(7) if full_schedule["Laura"][d] == "Libre"}
    assert dias_libres.isdisjoint({5, 6})

    assert violaciones == []
    _assert_cobertura_finde_ok(full_schedule, cfg)


# ═══════════════════════════════════════════════════════════════════════════
# 5b. cubrir_dias_libres_fijos: el cubridor también cubre el finde fijo
# ═══════════════════════════════════════════════════════════════════════════

def test_cubrir_dias_libres_fijos_activo_subjefe_trabaja_el_finde(data_dir):
    """Con la casilla activa y el jefe presente sin ausencias reales, el
    subjefe cubre igualmente Sáb+Dom (los días libres FIJOS del jefe) en el
    turno fijo del jefe, y sus propios libres pasan a ser entre semana."""
    cfg = _cfg_con_cobertura(semana_completa=False, cubrir_dias_libres_fijos=True)
    staff = _plantilla_holgada()

    full_schedule, staff_final, *_, violaciones = generate_schedule_valido(staff, SEMANA_INICIO, cfg)

    laura = next(p for p in staff_final if p["name"] == "Laura")
    assert {5, 6}.issubset(set(laura["dias_cobertura_jefatura"]))
    assert not laura.get("cobertura_total_semana")  # independiente de esta otra casilla

    assert full_schedule["Laura"][5] == "Mañana"
    assert full_schedule["Laura"][6] == "Mañana"

    dias_libres = sorted(d for d in range(7) if full_schedule["Laura"][d] == "Libre")
    assert len(dias_libres) == 2
    assert set(dias_libres).isdisjoint({5, 6}), f"Laura libra en fin de semana: {dias_libres}"
    assert dias_libres[1] - dias_libres[0] == 1, f"Libres no consecutivos: {dias_libres}"

    assert violaciones == []
    _assert_cobertura_finde_ok(full_schedule, cfg)


def test_cubrir_dias_libres_fijos_desactivado_no_cambia_comportamiento_previo(data_dir):
    """No regresión: con la casilla desactivada (default), el subjefe no
    trabaja el finde por este motivo y puede librar Sáb+Dom con normalidad
    — mismo comportamiento que antes de añadir esta casilla."""
    cfg = _cfg_con_cobertura(semana_completa=False, cubrir_dias_libres_fijos=False)
    staff = _plantilla()  # Juan (jefe) sin peticiones ni vacaciones

    full_schedule, staff_final, *_, violaciones = generate_schedule_valido(staff, SEMANA_INICIO, cfg)

    laura = next(p for p in staff_final if p["name"] == "Laura")
    assert laura["dias_cobertura_jefatura"] == []  # sin ausencia real, sin casilla: nada que cubrir

    assert violaciones == []
    _assert_cobertura_finde_ok(full_schedule, cfg)


def test_cubrir_dias_libres_fijos_mas_semana_completa_combinadas(data_dir):
    """Ambas casillas activas + jefe de vacaciones toda la semana: presencia
    total en Mañana los 7 días menos sus 2 libres entre semana."""
    cfg = _cfg_con_cobertura(semana_completa=True, cubrir_dias_libres_fijos=True)
    staff = _plantilla_holgada()
    juan = next(p for p in staff if p["name"] == "Juan")
    juan["forced_days_off"] = list(DAYS)

    full_schedule, staff_final, *_, violaciones = generate_schedule_valido(staff, SEMANA_INICIO, cfg)

    laura = next(p for p in staff_final if p["name"] == "Laura")
    assert laura["cobertura_total_semana"] is True
    assert set(laura["dias_cobertura_jefatura"]) == set(range(7))

    for d, turno in enumerate(full_schedule["Laura"]):
        if turno != "Libre":
            assert turno == "Mañana", f"Día {d}: Laura hizo {turno} en vez de Mañana"

    dias_libres = sorted(d for d in range(7) if full_schedule["Laura"][d] == "Libre")
    assert len(dias_libres) == 2
    assert set(dias_libres).isdisjoint({5, 6})

    assert violaciones == []
    _assert_cobertura_finde_ok(full_schedule, cfg)


def test_cobertura_finde_respeta_max_coverage_sin_romper_limite(data_dir):
    """Si añadir al cubridor a la Mañana del sábado superaría max_coverage,
    NO se supera el máximo: se le asigna otro turno legal en su lugar (o,
    si no lo hubiera, quedaría reflejado como advertencia estructurada,
    nunca como ruptura silenciosa del límite). Se usan aquí assign_days_off
    y assign_shifts directamente (sin reintentos) para un escenario
    mínimo y determinista: un tercer rol fijo "otro" ya ocupa el único
    hueco de Mañana el sábado antes de que se procese la cobertura."""
    from modelo_base import DeptConfig, _marcar_cobertura_ausencias, assign_days_off, assign_shifts

    cfg = DeptConfig(
        dept_id="recepcion", dept_label="Recepción",
        night_fixed_days_off=[], night_cover_compensation=[],
        min_coverage={"Mañana": 1}, max_coverage={"Mañana": 1},
        no_night_roles={"jefe", "subjefe"}, fixed_roles={"jefe": "Mañana", "otro": "Mañana"},
        fixed_days_off={"jefe": [5, 6], "otro": [0, 1]},
        cover_consecutive=True, night_rotation_key="test_max_cobertura",
        allow_non_consecutive_cover=False, prefer_next_week_for_cover=False,
        cobertura_roles={"subjefe": {"cubre": "jefe", "semana_completa": False,
                                      "dias_libres_fijos": True}},
        roles_excluidos_equidad={"jefe", "subjefe"},
    )
    staff = [
        {"name": "Juan",  "role": "jefe",    "preferences": []},
        {"name": "Laura", "role": "subjefe", "preferences": []},
        {"name": "Otro",  "role": "otro",    "preferences": []},  # ya ocupa Mañana el sábado
    ]

    _marcar_cobertura_ausencias(staff, cfg)
    laura = next(p for p in staff if p["name"] == "Laura")
    assert 5 in laura["dias_cobertura_jefatura"]

    days_off_sched = assign_days_off(staff, {}, cfg)
    full_schedule, violaciones = assign_shifts(staff, days_off_sched, {}, cfg)

    conteo_sabado_manana = sum(1 for t in full_schedule.values() if t[5] == "Mañana")
    assert conteo_sabado_manana <= cfg.max_coverage["Mañana"], (
        "Se superó el máximo de cobertura de Mañana el sábado"
    )
    assert full_schedule["Laura"][5] != "SIN_ASIGNAR"
    assert violaciones == []


# ═══════════════════════════════════════════════════════════════════════════
# 6. Equidad a largo plazo: subjefe fuera del balanceo, resto equilibrado
# ═══════════════════════════════════════════════════════════════════════════

def _simular(n_semanas, cfg, plantilla=None):
    from herramientas import registrar_semana

    plantilla   = plantilla if plantilla is not None else _plantilla()
    prev_covers = {}
    prev_sunday = {}

    for i in range(n_semanas):
        week_start = SEMANA_INICIO + timedelta(weeks=i)
        staff = copy.deepcopy(plantilla)

        sch, staff_final, _, _, next_covers, _ = generate_schedule(
            staff_list         = staff,
            week_start         = week_start,
            cfg                = cfg,
            prev_week_covers   = prev_covers,
            prev_sunday_shifts = prev_sunday,
        )

        registrar_semana(week_start, sch, staff_final, forzar=True, dept_id="recepcion")

        prev_covers = next_covers
        prev_sunday = {p["name"]: sch[p["name"]][6] for p in staff_final}


def _contar_finds(historial, nombres):
    finds = {n: 0 for n in nombres}
    for entrada in historial.values():
        sch       = entrada.get("schedule", {})
        staff_sem = entrada.get("staff", [])
        nf = next((p for p in staff_sem if p.get("role") == "night_fixed"), None)
        dias_cob: set = set()
        if nf:
            nf_t     = sch.get(nf["name"], [])
            dias_cob = {i for i, t in enumerate(nf_t) if t == "Libre"}
        for nombre in nombres:
            t = sch.get(nombre, [])
            if len(t) < 7:
                continue
            if t[5] == "Libre" and t[6] == "Libre":
                fue_comp = bool(dias_cob and any(t[d] == "Noche" for d in dias_cob))
                if not fue_comp:
                    finds[nombre] += 1
    return finds


def test_equidad_15_semanas_subjefe_fuera_del_balanceo(data_dir):
    from herramientas import cargar_historial

    cfg = _cfg_con_cobertura(semana_completa=False)
    _simular(15, cfg)

    historial = cargar_historial("recepcion")
    staff_actual = _plantilla()

    # El subjefe no aparece en absoluto en los ratios de equidad.
    ratios = _get_weekend_ratios("recepcion", staff_actual)
    assert "Laura" not in ratios

    # Los roles NO excluidos de equidad siguen equilibrados entre sí.
    excluidos = {"jefe", "subjefe", "night_fixed"}
    nombres   = [p["name"] for p in staff_actual if p["role"] not in excluidos]
    finds     = _contar_finds(historial, nombres)
    diff      = max(finds.values()) - min(finds.values())
    assert diff <= 2, f"Desequilibrio de fines de semana > 2: {finds}"


def test_15_semanas_cubrir_dias_libres_fijos_siempre_2_libres_y_cobertura_ok(data_dir):
    """Con la casilla activa de forma permanente, el subjefe nunca debe
    quedarse sin sus dos días libres semanales, y la cobertura mínima de
    todos los turnos debe respetarse en las 15 semanas — usa
    generate_schedule_valido (con reintentos) semana a semana para poder
    afirmar esto de forma determinista, ya que una sola pasada no lo
    garantiza."""
    from modelo_recepcion import RECEPCION_CONFIG
    from herramientas import registrar_semana

    cfg = _cfg_con_cobertura(semana_completa=False, cubrir_dias_libres_fijos=True)
    plantilla   = _plantilla_holgada()
    prev_covers = {}
    prev_sunday = {}

    for i in range(15):
        week_start = SEMANA_INICIO + timedelta(weeks=i)
        staff = copy.deepcopy(plantilla)

        sch, staff_final, _, _, next_covers, violaciones = generate_schedule_valido(
            staff_list         = staff,
            week_start         = week_start,
            cfg                = cfg,
            prev_week_covers   = prev_covers,
            prev_sunday_shifts = prev_sunday,
        )
        assert violaciones == [], f"Semana {week_start}: violaciones {violaciones}"

        dias_libres_laura = [d for d in range(7) if sch["Laura"][d] == "Libre"]
        assert len(dias_libres_laura) == 2, (
            f"Semana {week_start}: Laura no tiene exactamente 2 libres: {sch['Laura']}"
        )
        assert set(dias_libres_laura).isdisjoint({5, 6}), (
            f"Semana {week_start}: Laura libra en fin de semana: {dias_libres_laura}"
        )
        _assert_cobertura_finde_ok(sch, cfg)

        registrar_semana(week_start, sch, staff_final, forzar=True, dept_id="recepcion")
        prev_covers = next_covers
        prev_sunday = {p["name"]: sch[p["name"]][6] for p in staff_final}

    # También excluida del balanceo de equidad con la casilla activa.
    ratios = _get_weekend_ratios("recepcion", plantilla)
    assert "Laura" not in ratios


# ═══════════════════════════════════════════════════════════════════════════
# 7. Retrocompatibilidad: roles.json sin los campos nuevos
# ═══════════════════════════════════════════════════════════════════════════

def test_roles_json_antiguo_se_carga_sin_error_con_defaults(data_dir):
    import json
    from herramientas import cargar_roles, _dept_file

    # roles.json "antiguo": sin cubre_ausencia_de / cobertura_semana_completa /
    # excluir_de_equidad, tal y como lo dejaría una versión anterior.
    roles_antiguos = {
        "night_fixed": {"label": "Turno Noche fijo", "icon": "★", "turno_fijo": "Noche",
                         "dias_libres_fijos": [3, 4], "dias_libres_sustituto": [5, 6],
                         "sin_noches": False, "unico": True, "protegido": True, "borrable": False},
        "jefe":        {"label": "Jefe", "icon": "👑", "turno_fijo": "Mañana",
                         "dias_libres_fijos": [5, 6], "dias_libres_sustituto": None,
                         "sin_noches": True, "unico": True, "protegido": False, "borrable": True},
        "subjefe":     {"label": "Subjefe", "icon": "⭐", "turno_fijo": None,
                         "dias_libres_fijos": None, "dias_libres_sustituto": None,
                         "sin_noches": True, "unico": False, "protegido": False, "borrable": True},
        "normal":      {"label": "Empleado", "icon": "", "turno_fijo": None,
                         "dias_libres_fijos": None, "dias_libres_sustituto": None,
                         "sin_noches": False, "unico": False, "protegido": True, "borrable": False},
    }
    with open(_dept_file("roles.json", "recepcion"), "w", encoding="utf-8") as f:
        json.dump(roles_antiguos, f, ensure_ascii=False, indent=2)

    roles = cargar_roles("recepcion")  # no debe lanzar excepción

    for rid, r in roles.items():
        assert r["cubre_ausencia_de"] is None
        assert r["cobertura_semana_completa"] is False
        assert r["cubrir_dias_libres_fijos"] is False
        assert r["excluir_de_equidad"] is False

    # Sin cobertura configurada, el comportamiento de generación es el de
    # antes del cambio: cfg vacío de cobertura_roles/roles_excluidos_equidad
    # no marca a nadie.
    from modelo_recepcion import RECEPCION_CONFIG
    cfg = copy.deepcopy(RECEPCION_CONFIG)  # cobertura_roles={}, roles_excluidos_equidad=set() por default
    full_schedule, staff_final, *_, violaciones = generate_schedule_valido(_plantilla(), SEMANA_INICIO, cfg)
    laura = next(p for p in staff_final if p["name"] == "Laura")
    assert "cubre_rol" not in laura
    assert "dias_cobertura_jefatura" not in laura
    assert violaciones == []
