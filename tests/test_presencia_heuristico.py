"""
tests/test_presencia_heuristico.py — Presencia parcial en el motor heurístico
(modelo_base.py, paso 3).

Mismos invariantes que tests/test_presencia_cpsat.py (paso 2), ahora sobre
assign_days_off / assign_shifts / generate_schedule_valido:
  - Una persona cuyo rol tiene dias_presencia trabaja SIEMPRE en sus días de
    presencia — solo una vacación/petición concedida (forced_days_off /
    extra_days_off) puede librarlos.
  - Sus días fuera de contrato quedan en el estado "Fuera", que no cuenta
    como Libre ni entra en las rotaciones.
  - Es la excepción a la rotación de 2 libres: jamás recibe un par de libres
    ni participa de la red de reasignación.
  - Una concesión sobre un día fuera NO convierte la celda en Libre: sigue
    siendo Fuera (ese día no está contratado).
  - Sin ningún rol con presencia (presencia_roles vacío), no aparece ninguna
    celda "Fuera" y el resto del equipo conserva su patrón de 2 libres.

La mayoría de los tests llaman a assign_days_off / assign_shifts directamente
(unidades, sin rotación nocturna de por medio); el test de integración usa
generate_schedule_valido con un DATA_DIR temporal.
"""

import copy
import random
from datetime import date

import pytest

from modelo_base import (DAYS, DeptConfig, SHIFTS, FUERA, assign_days_off,
                         assign_shifts, generate_schedule_valido,
                         _marcar_cobertura_ausencias)

SEMANA = date(2099, 2, 3)  # lunes en futuro lejano (mismo criterio que el resto de tests)

# Turnos de trabajo reales (todo SHIFTS menos Libre) — el contrato de
# presencia exige que los días de presencia caigan aquí, nunca en Libre ni
# en Fuera.
TURNOS_TRABAJO = [s for s in SHIFTS if s != "Libre"]


@pytest.fixture(autouse=True)
def _data_dir_aislado(tmp_path, monkeypatch):
    import herramientas
    backup = tmp_path / "backups"
    backup.mkdir()
    monkeypatch.setattr(herramientas, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(herramientas, "BACKUP_DIR", str(backup))
    monkeypatch.setattr(herramientas, "FESTIVOS_FILE", str(tmp_path / "festivos.json"))
    # El heurístico usa random.shuffle en la rotación de turnos y pares:
    # semilla fija para que el resultado sea reproducible test a test.
    random.seed(20260203)
    return tmp_path


def _cfg(**overrides) -> DeptConfig:
    base = dict(
        dept_id="recepcion", dept_label="Recepción",
        night_fixed_days_off=[3, 4], night_cover_compensation=[5, 6],
        min_coverage={"Mañana": 1, "Tarde": 1, "Noche": 0, "Partido": 0},
        max_coverage={"Mañana": 4, "Tarde": 4, "Noche": 1, "Partido": 1},
        no_night_roles={"jefe", "refuerzo"},
        fixed_roles={"jefe": "Mañana"}, fixed_days_off={"jefe": [5, 6]},
        cover_consecutive=True, night_rotation_key="recepcion",
        allow_non_consecutive_cover=False, prefer_next_week_for_cover=False,
        roles_excluidos_equidad=set(),
    )
    base.update(overrides)
    return DeptConfig(**base)


def _staff(ref_extra: dict = None) -> list:
    """Jefe + 2 normales + Ref (rol "refuerzo", configurado por cada test)."""
    return [
        {"name": "Jefe", "role": "jefe"},
        {"name": "Ana", "role": "normal"},
        {"name": "Luis", "role": "normal"},
        {"name": "Ref", "role": "refuerzo", **(ref_extra or {})},
    ]


def _resolver(cfg: DeptConfig, staff: list) -> tuple:
    """assign_days_off + assign_shifts sin cobertura nocturna (night_cover={}).
    Devuelve (schedule, violaciones), igual que assign_shifts."""
    sched_libres = assign_days_off(staff, {}, cfg)
    return assign_shifts(staff, sched_libres, {}, cfg)


# ═══════════════════════════════════════════════════════════════════════════
# 1. assign_days_off: días fuera fijados a "Fuera", sin pares ni libres
# ═══════════════════════════════════════════════════════════════════════════

def test_assign_days_off_fija_fuera_sin_pares_ni_libres():
    cfg = _cfg(presencia_roles={"refuerzo": {5, 6}})
    staff = _staff()
    sched = assign_days_off(staff, {}, cfg)

    ref = sched["Ref"]
    for d in range(5):
        assert ref[d] == FUERA, (
            f"Ref debía quedar Fuera el día {d}: {ref}"
        )
    # Los días de presencia NO se marcan aquí: quedan vacíos para que
    # assign_shifts les asigne turno de trabajo.
    assert ref[5] == "" and ref[6] == "", ref
    assert "Libre" not in ref, f"Ref no debe recibir libres: {ref}"

    # El resto del equipo conserva su semana normal: Ana/Luis su par de 2
    # libres, el jefe sus dos días fijos (Sáb+Dom).
    assert sched["Ana"].count("Libre") == 2, sched["Ana"]
    assert sched["Luis"].count("Libre") == 2, sched["Luis"]
    assert sched["Jefe"][5] == sched["Jefe"][6] == "Libre"
    assert sched["Jefe"].count("Libre") == 2


# ═══════════════════════════════════════════════════════════════════════════
# 2. assign_shifts: trabajo garantizado en presencia, Fuera intacta
# ═══════════════════════════════════════════════════════════════════════════

def test_assign_shifts_trabaja_presencia_y_respeta_fuera():
    cfg = _cfg(presencia_roles={"refuerzo": {5, 6}})
    staff = _staff()
    sched, violaciones = _resolver(cfg, staff)

    ref = sched["Ref"]
    for d in range(5):
        assert ref[d] == FUERA, ref
    for d in (5, 6):
        assert ref[d] in TURNOS_TRABAJO, (
            f"Ref debía trabajar obligatoriamente el día {d}: {ref}"
        )
    assert "Libre" not in ref, ref

    # Nadie más ve celdas "Fuera".
    for nombre in ("Jefe", "Ana", "Luis"):
        assert FUERA not in sched[nombre], sched[nombre]


# ═══════════════════════════════════════════════════════════════════════════
# 3-4. Solo una concesión (vacación/petición) libra un día de presencia
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("campo, dia_nombre, idx", [
    ("forced_days_off", "Sábado", 5),
    ("extra_days_off", "Domingo", 6),
])
def test_concesion_libra_dia_de_presencia(campo, dia_nombre, idx):
    cfg = _cfg(presencia_roles={"refuerzo": {5, 6}})
    staff = _staff({campo: [dia_nombre]})
    sched, _ = _resolver(cfg, staff)

    assert sched["Ref"][idx] == "Libre", (
        f"La concesión ({campo}={dia_nombre}) debía librar ese día de "
        f"presencia: {sched['Ref']}"
    )
    # El OTRO día de presencia sigue siendo trabajo obligatorio.
    otro = 6 if idx == 5 else 5
    assert sched["Ref"][otro] in TURNOS_TRABAJO, sched["Ref"]
    for d in range(5):
        assert sched["Ref"][d] == FUERA, sched["Ref"]


# ═══════════════════════════════════════════════════════════════════════════
# 5. Concesión sobre un día fuera: sigue Fuera, no consume libre
# ═══════════════════════════════════════════════════════════════════════════

def test_concesion_en_dia_fuera_no_lo_convierte_en_libre():
    """Mismo criterio que el motor CP-SAT (la celda allí está fijada a
    FUERA pase lo que pase): un día no contratado no se convierte en Libre
    aunque coincida con una vacación/petición."""
    cfg = _cfg(presencia_roles={"refuerzo": {5, 6}})
    staff = _staff({"forced_days_off": ["Lunes"]})
    sched, _ = _resolver(cfg, staff)

    assert sched["Ref"][0] == FUERA, sched["Ref"]
    assert "Libre" not in sched["Ref"], sched["Ref"]
    assert sched["Ref"][5] in TURNOS_TRABAJO and sched["Ref"][6] in TURNOS_TRABAJO


# ═══════════════════════════════════════════════════════════════════════════
# 6. Sin presencia: ninguna celda "Fuera" (no-regresión)
# ═══════════════════════════════════════════════════════════════════════════

def test_sin_roles_con_presencia_cero_celdas_fuera():
    cfg = _cfg()  # presencia_roles vacío
    staff = _staff()
    sched, _ = _resolver(cfg, staff)

    for p in staff:
        assert FUERA not in sched[p["name"]], sched[p["name"]]
        assert all(t in SHIFTS for t in sched[p["name"]]), sched[p["name"]]
    assert sched["Ana"].count("Libre") == 2, sched["Ana"]
    assert sched["Luis"].count("Libre") == 2, sched["Luis"]


# ═══════════════════════════════════════════════════════════════════════════
# 7. Un rol con presencia cubre la ausencia del jefe, pero solo en sus días
# ═══════════════════════════════════════════════════════════════════════════

def test_rol_con_presencia_cubre_ausencia_del_jefe_solo_en_sus_dias():
    """Ref cubre al Jefe (cobertura_roles) con presencia Lun-Mié: en esos
    días assign_shifts le fija el turno del jefe (Mañana); en sus días fuera
    la celda es Fuera y ninguna regla de turnos lo toca."""
    cfg = _cfg(
        presencia_roles={"refuerzo": {0, 1, 2}},
        cobertura_roles={"refuerzo": {
            "cubre": "jefe", "dias_libres_fijos": False,
            "semana_completa": False,
        }},
    )
    staff = [
        {"name": "Jefe", "role": "jefe",
         "forced_days_off": ["Lunes", "Martes", "Miércoles"]},
        {"name": "Ana", "role": "normal"},
        {"name": "Luis", "role": "normal"},
        {"name": "Ref", "role": "refuerzo"},
    ]
    _marcar_cobertura_ausencias(staff, cfg)
    sched, _ = _resolver(cfg, staff)

    for d in (0, 1, 2):
        assert sched["Ref"][d] == "Mañana", (
            f"Ref debía cubrir la ausencia del jefe el día {d}: {sched['Ref']}"
        )
    for d in (3, 4, 5, 6):
        assert sched["Ref"][d] == FUERA, (
            f"Fuera de sus días de presencia Ref no interviene el día {d}: {sched['Ref']}"
        )


# ═══════════════════════════════════════════════════════════════════════════
# 8. Integración: semana completa por generate_schedule_valido con refuerzo
# ═══════════════════════════════════════════════════════════════════════════

def test_integracion_semana_completa_con_refuerzo_finde():
    """Plantilla realista (jefe + turno noche + 2 normales + refuerzo de
    finde): el refuerzo aparece Fuera Lun-Vie, trabaja Sáb+Dom y la semana
    sale sin violaciones de cobertura."""
    cfg = _cfg(presencia_roles={"refuerzo": {5, 6}})
    staff = [
        {"name": "Jefe", "role": "jefe"},
        {"name": "NF", "role": "night_fixed"},
        {"name": "Ana", "role": "normal"},
        {"name": "Luis", "role": "normal"},
        {"name": "Ref", "role": "refuerzo"},
    ]
    sched, staff_final, _, _, _, violaciones = generate_schedule_valido(
        copy.deepcopy(staff), SEMANA, cfg, max_intentos=50)

    ref = sched["Ref"]
    for d in range(5):
        assert ref[d] == FUERA, ref
    for d in (5, 6):
        assert ref[d] in TURNOS_TRABAJO, ref
    assert "Libre" not in ref, ref

    assert not violaciones, (
        "La semana con refuerzo de finde no debe tener violaciones:\n"
        + "\n".join(f"  · {v}" for v in violaciones)
    )

    # Cobertura mínima de Mañana/Tarde cumplida los 7 días.
    for d in range(7):
        for turno in ("Mañana", "Tarde"):
            n = sum(1 for p in staff_final if sched[p["name"]][d] == turno)
            assert n >= cfg.min_coverage[turno], (
                f"{DAYS[d]} {turno}: {n} personas (mínimo {cfg.min_coverage[turno]})"
            )
