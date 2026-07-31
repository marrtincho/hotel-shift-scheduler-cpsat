"""
tests/test_prioridad_continuidad_sobrecobertura.py — Valida el rediseño de
prioridades de Fase 5 (sesión posterior a fijar num_search_workers=1 +
sorted()): orden de negocio estricto B (continuidad) > I (sobre-cobertura
diurna, término NUEVO) > D (uso de Partido).

Motivo del cambio (ver docstring de motor_cpsat.py, sección "rediseño de
prioridades"): antes, con B=5 < D=20 y sin ningún coste por sobre-cubrir
Mañana/Tarde, el solver prefería romper continuidad o apilar personal de
sobra en un turno diurno antes que usar Partido — lo contrario de lo que
pedía el negocio.

Todos los escenarios usan ObjectiveValue() explícito comparando
alternativas EXPLÍCITAMENTE fijadas (mismo patrón que
test_termino_e_cede_cuando_la_alternativa_cuesta_mas_de_200 y el
aislamiento D-vs-B del informe de Fase 5) — no solo el resultado final del
schedule óptimo, aunque también se verifica.
"""

from datetime import date

import pytest
from ortools.sat.python import cp_model

from modelo_base import DeptConfig
from motor_cpsat import (
    _construir_modelo, _resolver_y_extraer,
    PESO_ROTURAS_CONTINUIDAD, PESO_SOBRECOBERTURA_DIURNA, PESO_USO_PARTIDO,
)

SEMANA = date(2099, 2, 3)  # Lunes en futuro lejano


@pytest.fixture(autouse=True)
def _data_dir_aislado(tmp_path, monkeypatch):
    """Aísla DATA_DIR — igual que test_motor_cpsat.py: los términos A/C
    leen historial.json/roles.json vía herramientas."""
    import herramientas
    backup = tmp_path / "backups"
    backup.mkdir()
    monkeypatch.setattr(herramientas, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(herramientas, "BACKUP_DIR", str(backup))
    monkeypatch.setattr(herramientas, "FESTIVOS_FILE", str(tmp_path / "festivos.json"))


def _cfg(**overrides) -> DeptConfig:
    base = dict(
        dept_id="test", dept_label="Test", night_fixed_days_off=[],
        night_cover_compensation=[], min_coverage={}, max_coverage={},
        no_night_roles=set(), fixed_roles={}, fixed_days_off={},
        cover_consecutive=True, night_rotation_key="test",
        allow_non_consecutive_cover=False, prefer_next_week_for_cover=False,
        cobertura_roles={}, roles_excluidos_equidad=set(),
    )
    base.update(overrides)
    return DeptConfig(**base)


def _obj(model, x, names) -> float:
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 30.0
    status = solver.Solve(model)
    assert status == cp_model.OPTIMAL, cp_model.CpSolver().StatusName(status)
    return solver.ObjectiveValue()


# ─── Escenario 1 (Paso 2): I vs D — reproduce el ejemplo de negocio ────────

def test_sobrante_va_a_partido_no_a_sobrecubrir_manana_o_tarde():
    """min_coverage Mañana=2/Tarde=2/Noche=1, una persona de sobra ese día,
    sin presión de continuidad (P6 solo trabaja ese día, Libre el resto).
    Resultado de negocio esperado: 2 Mañana + 2 Tarde + 1 Noche + 1 Partido
    — NO 3 Mañana + 2 Tarde + 1 Noche."""
    cfg = _cfg(
        min_coverage={"Mañana": 2, "Tarde": 2, "Noche": 1, "Partido": 0},
        max_coverage={"Mañana": 3, "Tarde": 3, "Noche": 1, "Partido": 1},
    )
    staff = [{"name": n, "role": "normal"} for n in
             ("M1", "M2", "T1", "T2", "N1", "P6")]

    def _modelo_base():
        model, x, names, _ = _construir_modelo(staff, SEMANA, cfg)
        # M1/M2/T1/T2/N1 fijos en su turno TODOS los días (satisfacen el
        # mínimo por sí solos toda la semana, con o sin P6) — solo P6
        # trabaja el Lunes (día a comparar) y libra el resto, aislando el
        # día 0 de cualquier interacción de continuidad/equidad con otros
        # días para P6, sin dejar sin cobertura los demás días.
        for name, turno in (("M1", "Mañana"), ("M2", "Mañana"),
                             ("T1", "Tarde"), ("T2", "Tarde"), ("N1", "Noche")):
            for d in range(7):
                model.Add(x[name, d, turno] == 1)
        model.Add(x["P6", 0, "Libre"] == 0)  # P6 debe trabajar el Lunes
        for d in range(1, 7):
            model.Add(x["P6", d, "Libre"] == 1)
        return model, x, names

    # Opción real: P6 lo decide el solver.
    model, x, names = _modelo_base()
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 30.0
    status = solver.Solve(model)
    assert status == cp_model.OPTIMAL
    turno_p6 = next(s for s in ("Mañana", "Tarde", "Noche", "Partido")
                     if solver.Value(x["P6", 0, s]) == 1)
    assert turno_p6 == "Partido", (
        f"Con 1 persona de sobra y Mañana/Tarde ya en su mínimo, se esperaba "
        f"que el sobrante fuera a Partido (coste D={PESO_USO_PARTIDO}) en vez "
        f"de sobre-cubrir Mañana/Tarde (coste I={PESO_SOBRECOBERTURA_DIURNA} "
        f"por persona de más) — P6 terminó en {turno_p6!r}"
    )
    obj_partido = solver.ObjectiveValue()

    # Contrafactual: P6 forzado a sobre-cubrir Mañana en vez de Partido.
    model2, x2, names2 = _modelo_base()
    model2.Add(x2["P6", 0, "Mañana"] == 1)
    obj_sobrecobertura = _obj(model2, x2, names2)

    delta = obj_sobrecobertura - obj_partido
    assert delta == PESO_SOBRECOBERTURA_DIURNA - PESO_USO_PARTIDO, (
        f"El delta de ObjectiveValue (sobrecobertura vs Partido) debía ser "
        f"exactamente I-D = {PESO_SOBRECOBERTURA_DIURNA - PESO_USO_PARTIDO} "
        f"(1 unidad de sobre-cobertura menos 1 uso de Partido, sin ningún "
        f"otro término interviniendo) — fue {delta}"
    )
    assert obj_sobrecobertura > obj_partido


# ─── Escenario 2 (Paso 2): B vs I — la continuidad gana, acepta sobre-cobertura ─

def test_continuidad_gana_sobre_evitar_sobrecobertura():
    """P_manana trabajó Mañana el lunes. El martes, Z_manana YA cubre el
    mínimo de Mañana por sí solo (con o sin P_manana) — así que si
    P_manana sigue en Mañana, sobre-cubre por 1 (I). La ÚNICA forma de que
    P_manana no sobre-cubra nada es moverlo a Tarde, donde V_reserva (un
    tercer empleado, genuinamente disponible) deja de hacer falta — pero
    eso rompe la continuidad de P_manana (B). Con B > I, el solver debe
    PRESERVAR la continuidad (P_manana sigue en Mañana, V_reserva cubre
    Tarde) y aceptar la sobre-cobertura, no al revés.

    Se usa la transición Mañana(lunes)->Tarde(martes), NO Tarde->Mañana:
    esta última está bloqueada por la restricción DURA de fase 1
    "no-Mañana-tras-Tarde/Noche" (NO_MORNING_AFTER) — intentarlo primero
    hizo el contrafactual INFEASIBLE por esa restricción, no por el
    término B (confirmado al depurar este test); Mañana->Tarde no tiene
    ninguna restricción dura equivalente, así que aísla de verdad el
    término B sin chocar con fase 1."""
    cfg = _cfg(
        min_coverage={"Mañana": 1, "Tarde": 1, "Noche": 0, "Partido": 0},
        max_coverage={"Mañana": 3, "Tarde": 3, "Noche": 0, "Partido": 0},  # Partido bloqueado
        no_night_roles={"normal"},  # bloquea Noche para todos (Noche también en 0)
    )
    staff = [{"name": n, "role": "normal"} for n in
             ("P_manana", "Z_manana", "T_fijo", "V_reserva")]

    def _modelo_base():
        model, x, names, _ = _construir_modelo(staff, SEMANA, cfg)
        # Z_manana cubre Mañana TODOS los días (satisface el mínimo de
        # Mañana pase lo que pase). T_fijo cubre Tarde todos los días
        # EXCEPTO el martes (día 1) — ese día, y solo ese día, el mínimo
        # de Tarde depende de que alguien más lo cubra (V_reserva, o
        # P_manana si se mueve allí), para que la alternativa de P_manana
        # sea una elección real y no una necesidad estructural en ningún
        # otro día.
        for d in range(7):
            model.Add(x["Z_manana", d, "Mañana"] == 1)
            if d != 1:
                model.Add(x["T_fijo", d, "Tarde"] == 1)
        model.Add(x["T_fijo", 1, "Libre"] == 1)
        # Lunes: P_manana cubre Mañana (fija la premisa de continuidad).
        model.Add(x["P_manana", 0, "Mañana"] == 1)
        model.Add(x["V_reserva", 0, "Libre"] == 1)
        # Martes: P_manana debe trabajar (Libre y Partido bloqueados por
        # cfg). V_reserva queda LIBRE de decidir: Tarde (si hace falta) o
        # Libre (si no hace falta).
        model.Add(x["P_manana", 1, "Libre"] == 0)
        # Resto de la semana: P_manana y V_reserva Libre (aísla el día 1
        # de cualquier interacción de continuidad con días posteriores).
        for name in ("P_manana", "V_reserva"):
            for d in range(2, 7):
                model.Add(x[name, d, "Libre"] == 1)
        return model, x, names

    # Opción real: el solver decide martes para P_manana y V_reserva.
    model, x, names = _modelo_base()
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 30.0
    status = solver.Solve(model)
    assert status == cp_model.OPTIMAL
    turno_p_manana_martes = next(s for s in ("Mañana", "Tarde") if solver.Value(x["P_manana", 1, s]) == 1)
    assert turno_p_manana_martes == "Mañana", (
        f"Se esperaba que P_manana preservara su continuidad de Mañana el "
        f"martes (aceptando sobre-cobertura, I={PESO_SOBRECOBERTURA_DIURNA}) "
        f"en vez de romperla para evitarla (B={PESO_ROTURAS_CONTINUIDAD}) — "
        f"terminó en {turno_p_manana_martes!r}"
    )
    assert solver.Value(x["V_reserva", 1, "Tarde"]) == 1, (
        "Con P_manana en Mañana, V_reserva debía cubrir Tarde (único modo "
        "de satisfacer min_coverage['Tarde']=1 ese día)"
    )
    obj_continuidad = solver.ObjectiveValue()

    # Contrafactual: forzar la rotura (P_manana a Tarde el martes) —
    # V_reserva ya no hace falta en Tarde, el solver lo deja Libre (gratis).
    model2, x2, names2 = _modelo_base()
    model2.Add(x2["P_manana", 1, "Tarde"] == 1)
    obj_rotura = _obj(model2, x2, names2)

    assert obj_rotura > obj_continuidad, (
        f"Romper la continuidad (obj={obj_rotura}) debía costar más que "
        f"preservarla y aceptar la sobre-cobertura (obj={obj_continuidad})"
    )
    delta = obj_rotura - obj_continuidad
    assert delta == PESO_ROTURAS_CONTINUIDAD - PESO_SOBRECOBERTURA_DIURNA, (
        f"El delta debía ser exactamente B-I = "
        f"{PESO_ROTURAS_CONTINUIDAD - PESO_SOBRECOBERTURA_DIURNA} "
        f"(1 rotura de más en la opción rota, 0 sobre-cobertura en ninguna "
        f"de las dos — el break es la ÚNICA diferencia) — fue {delta}"
    )


# ─── Escenario 3 (Paso 2): max_coverage al límite — I no tiene nada que penalizar ─

def test_max_coverage_al_limite_no_necesita_logica_especial():
    """Si Mañana/Tarde ya están en su máximo (== mínimo aquí, sin holgura
    alguna), la restricción dura de fase 1 ya impide sobre-cubrir — el
    término I no encuentra nada que penalizar, sin necesitar ningún caso
    especial en el código."""
    cfg = _cfg(
        min_coverage={"Mañana": 2, "Tarde": 2, "Noche": 1, "Partido": 0},
        max_coverage={"Mañana": 2, "Tarde": 2, "Noche": 1, "Partido": 1},  # sin holgura en Mañana/Tarde
    )
    staff = [{"name": n, "role": "normal"} for n in
             ("M1", "M2", "T1", "T2", "N1", "P6")]

    model, x, names, _ = _construir_modelo(staff, SEMANA, cfg)
    for name, turno in (("M1", "Mañana"), ("M2", "Mañana"),
                         ("T1", "Tarde"), ("T2", "Tarde"), ("N1", "Noche")):
        for d in range(7):
            model.Add(x[name, d, turno] == 1)
    model.Add(x["P6", 0, "Libre"] == 0)
    for d in range(1, 7):
        model.Add(x["P6", d, "Libre"] == 1)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 30.0
    status = solver.Solve(model)
    assert status == cp_model.OPTIMAL
    turno_p6 = next(s for s in ("Mañana", "Tarde", "Noche", "Partido")
                     if solver.Value(x["P6", 0, s]) == 1)
    assert turno_p6 == "Partido", (
        f"Con Mañana/Tarde ya en su máximo (sin holgura), P6 solo puede ir "
        f"a Partido (Noche también al máximo) — terminó en {turno_p6!r}"
    )
    # El término I no debería contribuir nada al objetivo ese día: Mañana y
    # Tarde están exactamente en su mínimo (que aquí coincide con el
    # máximo), 0 unidades de sobre-cobertura posibles.
    assert solver.Value(sum(x[n, 0, "Mañana"] for n in ("M1", "M2"))) == cfg.min_coverage["Mañana"]
    assert solver.Value(sum(x[n, 0, "Tarde"] for n in ("T1", "T2"))) == cfg.min_coverage["Tarde"]
