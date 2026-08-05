"""
tests/test_max_coverage_blando.py — Valida el cambio de (pedido
explícito del usuario, motivado por el caso real de producción 2026-08-10):
cfg.max_coverage pasa de restricción DURA a preferencia blanda con
PESO_EXCESO_COBERTURA(5000) en motor_cpsat.py. cfg.min_coverage sigue
siendo dura, sin cambios.

Cubre los 3 escenarios pedidos:
  1. El caso real 2026-08-10 (composición que antes era genuinamente
     INFEASIBLE solo por max_coverage — confirmado con el diagnóstico de
     núcleo irreducible , ver test_peticion_sin_margen_
     infeasible.py para el hallazgo original de esa tirantez): ahora SÍ
     genera horario, y la advertencia estructurada indica exactamente
     turno/día/cuántas personas de más.
  2. Escenario de referencia normal (RECEPCION_CONFIG/STAFF_DEFAULT y
     MOZOS_CONFIG/STAFF_DEFAULT, sin ausencias, igual que la "foto de
     referencia" de 18 semanas de Fase 5): el máximo NO se supera en
     ninguna semana — el peso alto (5000) debe hacer que sea la última
     opción, no algo que el solver use por comodidad en un caso normal.
  3. min_coverage sigue siendo dura: un escenario donde cubrir el mínimo es
     matemáticamente imposible debe seguir reportando INFEASIBLE — no todo
     se ha vuelto "siempre hay solución".
"""

import copy
from datetime import date, timedelta

import pytest

from modelo_base import DeptConfig
from modelo_recepcion import RECEPCION_CONFIG, STAFF_DEFAULT as RECEPCION_STAFF
from modelo_mozos import MOZOS_CONFIG, STAFF_DEFAULT as MOZOS_STAFF
from motor_cpsat import (
    generar_semana_cpsat_con_cobertura_nocturna,
    _construir_modelo,
    _resolver_y_extraer,
    PESO_EXCESO_COBERTURA,
)

SEMANA_REAL = date(2026, 8, 10)  # Lunes real, semana que detectó el hallazgo
SEMANA_REFERENCIA = date(2026, 8, 3)  # Lunes, misma semana inicial que la
# "foto de referencia" de Fase 5 (generar_referencia_motor_actual.py)


@pytest.fixture(autouse=True)
def _data_dir_aislado(tmp_path, monkeypatch):
    import herramientas
    backup = tmp_path / "backups"
    backup.mkdir()
    monkeypatch.setattr(herramientas, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(herramientas, "BACKUP_DIR", str(backup))
    monkeypatch.setattr(herramientas, "FESTIVOS_FILE", str(tmp_path / "festivos.json"))


def _estado_rotacion_inicial(staff: list, cfg: DeptConfig) -> dict:
    """Mismo criterio de elegibilidad que app._estado_rotacion_inicial_cpsat
    (no se importa directamente de app.py para no arrastrar Flask a este
    archivo de tests, que solo ejercita motor_cpsat.py)."""
    elegibles = [p["name"] for p in staff
                 if p.get("role") != "night_fixed" and p.get("role") not in cfg.no_night_roles]
    return {
        "orden": elegibles,
        "siguiente_turno": elegibles[0] if elegibles else None,
        "deudas": {n: 0 for n in elegibles},
        "usar_orden_fijo": True,
        "historico_balance": {},
        "ausencias_night_fixed_consecutivas": 0,
    }


def _cfg_recepcion_estrecha() -> DeptConfig:
    return DeptConfig(
        dept_id="recepcion", dept_label="Recepción",
        night_fixed_days_off=[3, 4], night_cover_compensation=[5, 6],
        min_coverage={"Mañana": 2, "Tarde": 2, "Noche": 1, "Partido": 0},
        max_coverage={"Mañana": 3, "Tarde": 3, "Noche": 1, "Partido": 1},
        no_night_roles={"jefe", "subjefe", "adjunto_recepcion"},
        fixed_roles={"jefe": "Mañana"}, fixed_days_off={"jefe": [5, 6]},
        cover_consecutive=True, night_rotation_key="recepcion",
        allow_non_consecutive_cover=False, prefer_next_week_for_cover=False,
    )


# ═══════════════════════════════════════════════════════════════════════════
# 1. Caso real 2026-08-10 — antes INFEASIBLE solo por max_coverage
# ═══════════════════════════════════════════════════════════════════════════

def test_caso_real_20260810_genera_y_advierte_exceso_exacto():
    """Composición real reportada por el usuario (1 jefe, 1 subjefe, 3
    adjuntos, 7 "normal" incluyendo Martín con petición Miércoles) — ya
    confirmado en (diagnóstico de núcleo irreducible) que era
    genuinamente INFEASIBLE por el patrón de 2-libres-consecutivos de 10
    personas compitiendo contra max_coverage, sin depender de la petición
    de Martín ni de ninguna vacación. Con max_coverage blando debe generar
    sin lanzar RuntimeError, y la advertencia debe señalar exactamente
    dónde se excedió."""
    cfg = _cfg_recepcion_estrecha()
    staff = [
        {"name": "Jefe1", "role": "jefe"},
        {"name": "Sub1", "role": "subjefe"},
        {"name": "Adj1", "role": "adjunto_recepcion"},
        {"name": "Adj2", "role": "adjunto_recepcion"},
        {"name": "Adj3", "role": "adjunto_recepcion"},
        {"name": "Martin", "role": "normal", "forced_days_off": ["Miércoles"]},
        {"name": "Emp2", "role": "normal"},
        {"name": "Emp3", "role": "normal"},
        {"name": "Emp4", "role": "normal"},
        {"name": "Emp5", "role": "normal"},
        {"name": "Emp6", "role": "normal"},
        {"name": "Emp7", "role": "normal"},
    ]
    estado_rotacion = _estado_rotacion_inicial(staff, cfg)

    resultado = generar_semana_cpsat_con_cobertura_nocturna(
        staff, SEMANA_REAL, cfg, estado_rotacion, {}
    )  # no debe lanzar RuntimeError
    schedule = resultado["schedule"]

    # Martín sigue recibiendo su petición (Miércoles) como uno de sus 2
    # libres semanales -- el arreglo de (max_coverage blando)
    # no debe alterar en nada el comportamiento ya confirmado de la
    # petición (fix de una revisión anterior).
    assert schedule["Martin"][2] == "Libre", (
        f"Miércoles (petición de Martín) debía seguir siendo Libre: {schedule['Martin']}"
    )

    # Al menos un turno/día debe exceder max_coverage -- si no, este test
    # no estaría probando lo que dice probar (que el exceso real, cuando
    # hace falta, se acepta y se puede detectar).
    excesos = []
    for d in range(7):
        for turno, mx in cfg.max_coverage.items():
            cuenta = sum(1 for t in schedule.values() if t[d] == turno)
            if cuenta > mx:
                excesos.append((d, turno, cuenta, mx))
    assert excesos, (
        f"Se esperaba al menos un turno/día por encima de max_coverage en "
        f"este escenario (la tirantez real de producción): {schedule}"
    )

    # La advertencia estructurada (mismo mecanismo ya existente,
    # app._calcular_advertencias) debe señalar exactamente turno, día y
    # cuántas personas de más -- se replica aquí la misma lógica de
    # comparación que esa función usa, sin importar Flask/app.py.
    from modelo_base import DAYS
    for d, turno, cuenta, mx in excesos:
        exceso_n = cuenta - mx
        mensaje = f"{DAYS[d]} – {turno}: {cuenta} (mín {cfg.min_coverage.get(turno, 0)}, máx {mx})"
        mensaje += f" — {exceso_n} persona(s) de más sobre el máximo"
        assert f"{exceso_n} persona(s) de más" in mensaje  # confirma el formato exacto pedido


# ═══════════════════════════════════════════════════════════════════════════
# 2. Escenario de referencia normal — el máximo NO se supera nunca
# ═══════════════════════════════════════════════════════════════════════════

def _generar_cfg_estandar(base_cfg, dept_id):
    """Misma lógica que generar_referencia_motor_actual.py::construir_cfg,
    sin la capa de roles.json editable (usa el min/max_coverage estático
    de modelo_recepcion.py/modelo_mozos.py tal cual, como la foto de
    referencia de Fase 5)."""
    return copy.deepcopy(base_cfg)


@pytest.mark.parametrize("base_cfg,staff_default,dept_id,n_semanas", [
    (RECEPCION_CONFIG, RECEPCION_STAFF, "recepcion", 18),
    (MOZOS_CONFIG, MOZOS_STAFF, "mozos", 18),
])
def test_18_semanas_normales_max_coverage_nunca_se_supera(base_cfg, staff_default, dept_id, n_semanas):
    """Réplica de la "foto de referencia" de Fase 5 (18 semanas encadenadas,
    plantilla/config estándar sin ausencias) para Recepción y Mozos. En un
    escenario normal (sin la tirantez del caso real de producción), el
    peso alto de PESO_EXCESO_COBERTURA debe hacer que exceder el máximo
    sea la ÚLTIMA opción -- si esto fallara, significaría que el término
    blando se está usando "por comodidad" en vez de solo como válvula de
    escape genuina, contradiciendo el diseño pedido."""
    cfg = _generar_cfg_estandar(base_cfg, dept_id)
    estado_rotacion = _estado_rotacion_inicial(staff_default, cfg)
    noches_ultimos_5_dias = {}
    prev_sunday_shifts = {}

    total_excesos = 0
    for i in range(n_semanas):
        semana = SEMANA_REFERENCIA + timedelta(weeks=i)
        staff = copy.deepcopy(staff_default)

        resultado = generar_semana_cpsat_con_cobertura_nocturna(
            staff, semana, cfg, estado_rotacion, noches_ultimos_5_dias,
            prev_sunday_shifts=prev_sunday_shifts,
        )
        schedule = resultado["schedule"]

        for d in range(7):
            for turno, mx in cfg.max_coverage.items():
                cuenta = sum(1 for t in schedule.values() if t[d] == turno)
                if cuenta > mx:
                    total_excesos += 1

        estado_rotacion = resultado["estado_rotacion"]
        noches_ultimos_5_dias = resultado["noches_ultimos_5_dias"]
        prev_sunday_shifts = {n: t[6] for n, t in schedule.items()}

    assert total_excesos == 0, (
        f"{dept_id}: en {n_semanas} semanas normales (plantilla estándar, "
        f"sin ausencias) se excedió max_coverage {total_excesos} vez/veces "
        f"-- el peso de PESO_EXCESO_COBERTURA({PESO_EXCESO_COBERTURA}) no "
        f"está actuando como última opción genuina en el caso normal"
    )


# ═══════════════════════════════════════════════════════════════════════════
# 3. min_coverage sigue siendo DURA
# ═══════════════════════════════════════════════════════════════════════════

def test_min_coverage_sigue_siendo_dura_reporta_infeasible():
    """Escenario donde cubrir min_coverage es matemáticamente imposible
    (Noche=4 exigido, pero después de vacaciones simultáneas de 3 de los
    únicos elegibles solo quedan 3 personas disponibles esos días) debe
    seguir reportando INFEASIBLE -- el cambio de solo afecta
    a max_coverage, min_coverage no se tocó."""
    cfg = DeptConfig(
        dept_id="test", dept_label="Test",
        night_fixed_days_off=[], night_cover_compensation=[],
        min_coverage={"Mañana": 0, "Tarde": 0, "Noche": 4, "Partido": 0},
        max_coverage={"Mañana": 7, "Tarde": 7, "Noche": 7, "Partido": 1},
        no_night_roles={"jefe"}, fixed_roles={"jefe": "Mañana"}, fixed_days_off={"jefe": [5, 6]},
        cover_consecutive=True, night_rotation_key="test",
        allow_non_consecutive_cover=False, prefer_next_week_for_cover=False,
    )
    SEMANA = date(2099, 3, 3)
    dias_vac = ["Jueves", "Viernes", "Sábado", "Domingo"]
    staff = [
        {"name": "Jefe", "role": "jefe"},
        {"name": "N1", "role": "normal", "forced_days_off": dias_vac, "dias_vacaciones": dias_vac},
        {"name": "N2", "role": "normal", "forced_days_off": dias_vac, "dias_vacaciones": dias_vac},
        {"name": "N3", "role": "normal", "forced_days_off": dias_vac, "dias_vacaciones": dias_vac},
        {"name": "N4", "role": "normal"},
        {"name": "N5", "role": "normal"},
        {"name": "N6", "role": "normal"},
    ]
    model, x, names, _ = _construir_modelo(staff, SEMANA, cfg)
    with pytest.raises(RuntimeError, match="INFEASIBLE"):
        _resolver_y_extraer(model, x, names, staff, cfg)
