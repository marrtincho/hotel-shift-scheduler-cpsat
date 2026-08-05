"""
tests/test_diagnostico_infactibilidad.py — Valida el diagnóstico automático
de infactibilidad (Tema 3, cierre de pendientes Fase
5) de motor_cpsat.py: _diagnosticar_infactibilidad /
_construir_modelo_diagnostico, usando la API de ortools confirmada
disponible en la versión instalada (9.15.6755) — CpModel.AddAssumptions +
CpSolver.SufficientAssumptionsForInfeasibility — para encontrar el
subconjunto MÍNIMO de restricciones duras "cedibles" (vacaciones/
peticiones agrupadas por persona, cobertura mínima por día/turno) que
entran en conflicto, en vez de solo lanzar RuntimeError con el status del
solver.

Cubre los 2 escenarios pedidos:
  1. Escenario construido a mano, infactible de antemano (3 personas de
     vacaciones simultáneas + sin ningún night_fixed, dejando cobertura
     mínima de Noche sin gente suficiente un día concreto): el diagnóstico
     debe señalar EXACTAMENTE las restricciones reales en conflicto, ni
     más (irrelevantes) ni menos (incompleto).
  2. Un caso FACTIBLE nunca activa este camino (sin falsos positivos).

Además (revisión posterior, medición de tiempos): confirma que el
mecanismo (AddAssumptions + SufficientAssumptionsForInfeasibility) es
rápido en el escenario de prueba y que TIENE un límite de tiempo propio
(TIEMPO_MAXIMO_DIAGNOSTICO_SEGUNDOS) — medido empíricamente ANTES de
añadirlo: un escenario más complejo (60 personas, vacaciones solapadas al
azar) que resulta ser FACTIBLE tardaba ~90s en que CP-SAT decidiera
SAT/UNSAT sin límite, frente a ~0.07s para una infactibilidad real a la
misma escala — la lentitud está en decidir cuando el modelo reducido
SÍ tiene solución pero es difícil de encontrar/demostrar, no en extraer
el conflicto una vez que Solve() ya confirmó INFEASIBLE. Sin el límite,
un caso así se comería el timeout de 60s de gunicorn sin devolver nada.
"""

from datetime import date

import pytest

from modelo_base import DAYS, DeptConfig
from motor_cpsat import (
    generar_semana_cpsat,
    _diagnosticar_infactibilidad,
    TIEMPO_MAXIMO_DIAGNOSTICO_SEGUNDOS,
)

SEMANA_INICIO = date(2099, 3, 3)  # Lunes en futuro lejano


@pytest.fixture(autouse=True)
def _data_dir_aislado(tmp_path, monkeypatch):
    import herramientas
    backup = tmp_path / "backups"
    backup.mkdir()
    monkeypatch.setattr(herramientas, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(herramientas, "BACKUP_DIR", str(backup))
    monkeypatch.setattr(herramientas, "FESTIVOS_FILE", str(tmp_path / "festivos.json"))


def _cfg(**overrides) -> DeptConfig:
    base = dict(
        dept_id="test", dept_label="Test",
        night_fixed_days_off=[], night_cover_compensation=[],
        min_coverage={"Mañana": 0, "Tarde": 0, "Noche": 4, "Partido": 0},
        max_coverage={"Mañana": 7, "Tarde": 7, "Noche": 7, "Partido": 1},
        no_night_roles={"jefe"}, fixed_roles={"jefe": "Mañana"}, fixed_days_off={"jefe": [5, 6]},
        cover_consecutive=True, night_rotation_key="test",
        allow_non_consecutive_cover=False, prefer_next_week_for_cover=False,
    )
    base.update(overrides)
    return DeptConfig(**base)


# ─── 1. Escenario construido a mano, infactible de antemano ───────────────

def test_diagnostico_identifica_conflicto_real_3_vacaciones_mas_cobertura_noche():
    """Sin ningún night_fixed en la plantilla (así que TODOS los "normal"
    son candidatos a Noche, ver cfg.no_night_roles) y con cobertura mínima
    de Noche=4, si 3 de los 6 "normal" tienen un bloque de vacaciones que
    se solapa (Jueves-Domingo, simultáneo para los 3), esos días solo
    quedan 3 candidatos elegibles (N4/N5/N6) para una exigencia de 4 —
    infactible de antemano, sabido al construir el escenario, no
    descubierto por casualidad.

    El diagnóstico debe señalar exactamente: los 3 bloques de vacación
    (N1/N2/N3) y la línea de cobertura mínima de Noche del día concreto en
    conflicto — ni de más (p.ej. no debe mencionar a Jefe, a N4/N5/N6, ni
    a Mañana/Tarde, que no participan del conflicto real) ni de menos (los
    3 nombres deben aparecer, no un subconjunto de 1 o 2)."""
    cfg = _cfg()
    staff = [
        {"name": "Jefe", "role": "jefe"},
        {"name": "N1", "role": "normal",
         "forced_days_off": ["Jueves", "Viernes", "Sábado", "Domingo"],
         "dias_vacaciones": ["Jueves", "Viernes", "Sábado", "Domingo"]},
        {"name": "N2", "role": "normal",
         "forced_days_off": ["Jueves", "Viernes", "Sábado", "Domingo"],
         "dias_vacaciones": ["Jueves", "Viernes", "Sábado", "Domingo"]},
        {"name": "N3", "role": "normal",
         "forced_days_off": ["Jueves", "Viernes", "Sábado", "Domingo"],
         "dias_vacaciones": ["Jueves", "Viernes", "Sábado", "Domingo"]},
        {"name": "N4", "role": "normal"},
        {"name": "N5", "role": "normal"},
        {"name": "N6", "role": "normal"},
    ]

    with pytest.raises(RuntimeError) as excinfo:
        generar_semana_cpsat(staff, SEMANA_INICIO, cfg)

    mensaje = str(excinfo.value)
    assert "status=INFEASIBLE" in mensaje, f"debía reportar INFEASIBLE: {mensaje}"
    assert "Diagnóstico automático — combinación mínima irreducible en conflicto" in mensaje, (
        f"el diagnóstico debía activarse y confirmar irreducibilidad "
        f"(staff_list/cfg disponibles en generar_semana_cpsat, escenario "
        f"pequeño con tiempo de sobra para completar la reducción): {mensaje}"
    )

    # Completo: los 3 nombres realmente en vacación simultánea deben
    # aparecer todos.
    for nombre in ("N1", "N2", "N3"):
        assert f"{nombre}: Jueves-Domingo forzado libre" in mensaje, (
            f"faltaba el bloque de vacación de {nombre} en el diagnóstico: {mensaje}"
        )
    assert "Cobertura mínima Noche=4" in mensaje, (
        f"faltaba la línea de cobertura mínima de Noche en conflicto: {mensaje}"
    )

    # Mínimo: nadie ajeno al conflicto real debe aparecer mencionado.
    for ajeno in ("Jefe", "N4", "N5", "N6"):
        assert ajeno not in mensaje, (
            f"{ajeno} no participa del conflicto real (no está de "
            f"vacaciones ni es necesario para la cobertura de Noche esos "
            f"días); no debía aparecer en el diagnóstico: {mensaje}"
        )
    assert "Mañana" not in mensaje and "Tarde" not in mensaje, (
        f"Mañana/Tarde tienen mínimo 0 en este escenario, no participan "
        f"del conflicto; no debían aparecer: {mensaje}"
    )


# ─── 2. Caso FACTIBLE nunca activa este camino ─────────────────────────────

def test_caso_factible_no_activa_diagnostico():
    """Con personal suficiente, generar_semana_cpsat debe devolver un
    horario con normalidad — el diagnóstico (que solo se invoca dentro de
    la rama INFEASIBLE de _resolver_y_extraer/_con_extra) nunca debe
    activarse, y por tanto tampoco puede aparecer ningún RuntimeError."""
    cfg = _cfg(
        min_coverage={"Mañana": 1, "Tarde": 1, "Noche": 1, "Partido": 0},
    )
    staff = [{"name": "Jefe", "role": "jefe"}] + [
        {"name": f"N{i}", "role": "normal"} for i in range(1, 7)
    ]

    schedule = generar_semana_cpsat(staff, SEMANA_INICIO, cfg)  # no debe lanzar

    assert set(schedule) == {"Jefe", "N1", "N2", "N3", "N4", "N5", "N6"}
    for nombre, turnos in schedule.items():
        assert len(turnos) == 7
        assert all(t for t in turnos), f"{nombre} tiene alguna celda vacía: {turnos}"

    # Confirmación directa y adicional: llamando al diagnóstico él mismo
    # sobre este staff/cfg factible (fuera de la rama INFEASIBLE, como lo
    # haría alguien investigando manualmente), debe reconocer que NO hay
    # infactibilidad que diagnosticar — no debe inventar un "conflicto"
    # sobre un escenario que sí tiene solución.
    mensaje = _diagnosticar_infactibilidad(staff, cfg)
    assert "no se pudo reproducir la infactibilidad" in mensaje, (
        f"con un escenario genuinamente factible, _diagnosticar_infactibilidad "
        f"debía admitir que no hay infactibilidad que reproducir (el modelo "
        f"reducido de diagnóstico también es factible), no inventar un "
        f"conflicto: {mensaje}"
    )


# ─── 3. Tiempo del mecanismo: rápido en el caso simple, acotado en uno duro ─

def test_diagnostico_es_rapido_en_el_escenario_de_3_vacaciones():
    """El escenario de prueba del test 1 (7 personas) debe diagnosticarse
    en muy poco tiempo — confirma que el mecanismo no añade una demora
    perceptible en el caso típico/simple que motivó Tema 3."""
    import time

    cfg = _cfg()
    staff = [
        {"name": "Jefe", "role": "jefe"},
        {"name": "N1", "role": "normal",
         "forced_days_off": ["Jueves", "Viernes", "Sábado", "Domingo"],
         "dias_vacaciones": ["Jueves", "Viernes", "Sábado", "Domingo"]},
        {"name": "N2", "role": "normal",
         "forced_days_off": ["Jueves", "Viernes", "Sábado", "Domingo"],
         "dias_vacaciones": ["Jueves", "Viernes", "Sábado", "Domingo"]},
        {"name": "N3", "role": "normal",
         "forced_days_off": ["Jueves", "Viernes", "Sábado", "Domingo"],
         "dias_vacaciones": ["Jueves", "Viernes", "Sábado", "Domingo"]},
        {"name": "N4", "role": "normal"},
        {"name": "N5", "role": "normal"},
        {"name": "N6", "role": "normal"},
    ]

    inicio = time.perf_counter()
    mensaje = _diagnosticar_infactibilidad(staff, cfg)
    duracion = time.perf_counter() - inicio

    assert "combinación mínima irreducible en conflicto" in mensaje
    assert duracion < 2.0, (
        f"el diagnóstico del escenario de 3 vacaciones simultáneas tardó "
        f"{duracion:.3f}s — medido en Fase 5 en ~0.01s; un salto grande "
        f"aquí sería una regresión de rendimiento, no solo de resultado"
    )


def test_diagnostico_tiene_limite_de_tiempo_propio_y_no_cuelga_en_caso_complejo():
    """Verifica que _diagnosticar_infactibilidad NO se queda sin límite de
    tiempo: con un escenario deliberadamente más complejo (60 personas,
    vacaciones solapadas de forma dispersa) que resulta ser FACTIBLE — el
    caso lento medido en (~90s sin límite) — y con
    TIEMPO_MAXIMO_DIAGNOSTICO_SEGUNDOS monkeypateado a un valor pequeño
    (para que el test sea rápido sin dejar de ejercitar de verdad la rama
    de timeout), confirma que la función devuelve un mensaje explícito de
    "no concluyó a tiempo" en vez de colgarse, y que efectivamente respeta
    el límite (no tarda mucho más que el límite fijado)."""
    import random
    import time
    import motor_cpsat

    limite_test = 1.0
    monkeypatch_original = motor_cpsat.TIEMPO_MAXIMO_DIAGNOSTICO_SEGUNDOS
    motor_cpsat.TIEMPO_MAXIMO_DIAGNOSTICO_SEGUNDOS = limite_test
    try:
        rng = random.Random(42)
        cfg = _cfg(
            min_coverage={"Mañana": 15, "Tarde": 15, "Noche": 10, "Partido": 0},
            max_coverage={"Mañana": 60, "Tarde": 60, "Noche": 60, "Partido": 10},
            no_night_roles=set(), fixed_roles={}, fixed_days_off={},
        )
        staff = []
        for i in range(60):
            p = {"name": f"P{i}", "role": "normal"}
            if rng.random() < 0.6:
                inicio_dia = rng.randint(0, 4)
                largo = rng.randint(2, 3)
                dias = [DAYS[d] for d in range(inicio_dia, min(inicio_dia + largo, 7))]
                p["forced_days_off"] = dias
                p["dias_vacaciones"] = dias
            staff.append(p)

        t0 = time.perf_counter()
        mensaje = motor_cpsat._diagnosticar_infactibilidad(staff, cfg)
        duracion = time.perf_counter() - t0

        assert "sigue siendo INFACTIBLE" in mensaje, (
            f"el mensaje de timeout debe dejar explícito que el horario "
            f"SIGUE siendo infactible (no sonar a 'no se encontró ningún "
            f"problema') — no lo dice: {mensaje!r}"
        )
        assert "no estuvo disponible a tiempo" in mensaje, (
            f"con el límite reducido a {limite_test}s, este escenario "
            f"(demostrado lento — ~90s sin límite) debía agotar el tiempo "
            f"y devolver el mensaje explícito de timeout, no colgarse ni "
            f"devolver otra cosa: {mensaje!r}"
        )
        assert duracion < limite_test + 2.0, (
            f"la función debía respetar el límite de tiempo propio "
            f"({limite_test}s) y no tardar mucho más — tardó {duracion:.2f}s"
        )
    finally:
        motor_cpsat.TIEMPO_MAXIMO_DIAGNOSTICO_SEGUNDOS = monkeypatch_original


def test_constante_de_tiempo_maximo_es_razonable_frente_al_timeout_de_gunicorn():
    """Confirma que TIEMPO_MAXIMO_DIAGNOSTICO_SEGUNDOS, sumado al tope del
    modelo principal (20s, ver _resolver_y_extraer), deja margen real bajo
    el timeout de 60s configurado en gunicorn.conf.py para el despliegue
 — si alguien sube esta constante sin pensar en esa relación, este
    test debe fallar en vez de dejar pasar la regresión en silencio."""
    from motor_cpsat import _resolver_y_extraer
    import inspect

    tope_modelo_principal = 20.0  # ver docstring de _resolver_y_extraer
    timeout_gunicorn = 60.0
    margen_minimo = 15.0  # deja hueco para el resto de la petición HTTP

    assert TIEMPO_MAXIMO_DIAGNOSTICO_SEGUNDOS > 0, (
        "el diagnóstico debe tener un límite de tiempo propio, no 0/deshabilitado"
    )
    total = tope_modelo_principal + TIEMPO_MAXIMO_DIAGNOSTICO_SEGUNDOS
    assert total <= timeout_gunicorn - margen_minimo, (
        f"modelo principal ({tope_modelo_principal}s) + diagnóstico "
        f"({TIEMPO_MAXIMO_DIAGNOSTICO_SEGUNDOS}s) = {total}s deja menos de "
        f"{margen_minimo}s de margen bajo el timeout de gunicorn "
        f"({timeout_gunicorn}s) — demasiado ajustado"
    )
