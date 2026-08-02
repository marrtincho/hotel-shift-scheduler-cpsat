"""
tests/test_domingo_suelto_sustituto.py — Valida el término F de motor_cpsat.py
(preferencia blanda: el sustituto de cobertura nocturna en departamentos con
cfg.prefer_next_week_for_cover=True — mozos hoy — prefiere Domingo como día
suelto, sin que sea una restricción dura) Y su gating estructural (corrección
posterior: el término solo se añade al objetivo cuando NO existe ningún par
de días consecutivos disponible para el sustituto — igual que la rama de
"días sueltos" en modelo_base.py).

IMPORTANTE sobre los escenarios: con cfg.night_fixed_days_off=[4,5] (mozos),
sin ningún día bloqueado el sustituto SIEMPRE tiene al menos un par
consecutivo disponible (Lunes+Martes, Martes+Miércoles o Miércoles+Jueves) —
así que el término F, correctamente gated, casi nunca se activa en un
escenario "limpio". Para ejercitarlo de verdad, los tests que necesitan el
término ACTIVO fuerzan dos días no adyacentes bloqueados para el sustituto
(p.ej. Martes+Jueves — un vertex-cover de los 3 pares candidatos (0,1),(1,2),
(2,3), confirmado con _tiene_par_consecutivo_disponible) — no es un capricho
del test, es la condición real que dispara el mecanismo.

No usa DATA_DIR/herramientas más allá de lo que motor_cpsat.py ya necesita
internamente para _get_weekend_ratios/_get_shift_counts (fase 2) — de ahí
el fixture _data_dir_aislado (autouse), igual que el resto de tests de este
prototipo.
"""

from datetime import date, timedelta

import pytest
from ortools.sat.python import cp_model

from modelo_base import DAYS, SHIFTS, DeptConfig
from motor_cpsat import (
    _construir_modelo,
    _resolver_y_extraer,
    _tiene_par_consecutivo_disponible,
    generar_semana_cpsat_con_cobertura_nocturna,
    PESO_DOMINGO_SUSTITUTO,
)

SEMANA = date(2099, 2, 3)  # Lunes en futuro lejano


@pytest.fixture(autouse=True)
def _data_dir_aislado(tmp_path, monkeypatch):
    import herramientas
    backup = tmp_path / "backups"
    backup.mkdir()
    monkeypatch.setattr(herramientas, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(herramientas, "BACKUP_DIR", str(backup))
    monkeypatch.setattr(herramientas, "FESTIVOS_FILE", str(tmp_path / "festivos.json"))


def _cfg_mozos(**overrides) -> DeptConfig:
    base = dict(
        dept_id="mozos", dept_label="Mozos",
        night_fixed_days_off=[4, 5],          # Viernes, Sábado
        night_cover_compensation=[0, 1],       # Lunes+Martes semana siguiente
        min_coverage={"Mañana": 1, "Tarde": 1, "Noche": 1, "Partido": 0},
        max_coverage={"Mañana": 1, "Tarde": 1, "Noche": 1, "Partido": 2},
        no_night_roles=set(), fixed_roles={}, fixed_days_off={},
        cover_consecutive=False, night_rotation_key="mozos",
        allow_non_consecutive_cover=True, prefer_next_week_for_cover=True,
    )
    base.update(overrides)
    return DeptConfig(**base)


def _cfg_recepcion(**overrides) -> DeptConfig:
    base = dict(
        dept_id="recepcion", dept_label="Recepción",
        night_fixed_days_off=[3, 4],          # Jueves, Viernes
        night_cover_compensation=[5, 6],
        min_coverage={"Mañana": 2, "Tarde": 2, "Noche": 1, "Partido": 0},
        max_coverage={"Mañana": 3, "Tarde": 3, "Noche": 1, "Partido": 1},
        no_night_roles={"jefe", "subjefe"},
        fixed_roles={"jefe": "Mañana"}, fixed_days_off={"jefe": [5, 6]},
        cover_consecutive=True, night_rotation_key="recepcion",
        allow_non_consecutive_cover=False, prefer_next_week_for_cover=False,
    )
    base.update(overrides)
    return DeptConfig(**base)


def _staff_mozos(forced_por_nombre=None):
    forced_por_nombre = forced_por_nombre or {}
    staff = [{"name": "NightFixed", "role": "night_fixed"}]
    for n in ("N1", "N2", "N3", "N4"):
        p = {"name": n, "role": "normal"}
        if n in forced_por_nombre:
            p["forced_days_off"] = forced_por_nombre[n]
        staff.append(p)
    return staff


def _solve_con_objetivo(model, x, names):
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 30.0
    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise RuntimeError(f"CP-SAT infactible (status={solver.StatusName(status)}).")
    schedule = {name: ["" for _ in range(7)] for name in names}
    for name in names:
        for d in range(7):
            for s in SHIFTS:
                if solver.Value(x[name, d, s]) == 1:
                    schedule[name][d] = s
                    break
    return schedule, solver.ObjectiveValue()


# ─── Helper de gating: unidad directa ───────────────────────────────────────

def test_tiene_par_consecutivo_disponible_helper():
    cfg = _cfg_mozos()
    # Sin ningún día bloqueado: quedan (0,1),(1,2),(2,3) libres de must_work={4,5}.
    assert _tiene_par_consecutivo_disponible(cfg, set()) is True
    # Martes(1)+Jueves(3): vertex-cover de los 3 pares candidatos -> ninguno queda.
    assert _tiene_par_consecutivo_disponible(cfg, {1, 3}) is False
    # Un solo día bloqueado nunca basta para tapar los 3 pares.
    assert _tiene_par_consecutivo_disponible(cfg, {2}) is True


# ─── Domingo disponible (gating activo): se prefiere como suelto ───────────

def test_domingo_disponible_se_elige_como_suelto():
    """N1 tiene Martes+Jueves ya bloqueados (motivo ajeno, p.ej. una petición
    parcial) -> sin par consecutivo disponible -> término F activo. Domingo
    es viable sin romper cobertura -> debe elegirse."""
    cfg = _cfg_mozos()
    staff = _staff_mozos({"N1": ["Martes", "Jueves"]})
    assert not _tiene_par_consecutivo_disponible(cfg, {1, 3})  # confirma el gating

    model, x, names, _ = _construir_modelo(staff, SEMANA, cfg, nombre_sustituto_cobertura="N1")
    model.Add(x["N1", 4, "Noche"] == 1)  # Viernes
    model.Add(x["N1", 5, "Noche"] == 1)  # Sábado

    sched = _resolver_y_extraer(model, x, names)

    assert sched["N1"][6] == "Libre", (
        f"Se esperaba Domingo libre para el sustituto (sin par consecutivo "
        f"disponible, preferencia activa y viable): {sched['N1']}"
    )


# ─── Encadenamiento REAL: dos llamadas reales al orquestador ────────────────

def test_encadenamiento_produce_tres_dias_libres_consecutivos():
    """Llama a generar_semana_cpsat_con_cobertura_nocturna DOS veces en
    secuencia real. El staff de la semana 2 recibe como forced_days_off de
    N1 EXACTAMENTE lo que devolvió resultado1["forced_days_off_extra"] en
    la llamada de la semana 1 — no un valor escrito a mano."""
    cfg = _cfg_mozos()
    estado_rotacion = {
        "orden": ["N1", "N2", "N3", "N4"],
        "siguiente_turno": "N1",
        "deudas": {},
        "usar_orden_fijo": True,
        "historico_balance": {},
        "ausencias_night_fixed_consecutivas": 0,
    }
    noches_ultimos_5_dias = {}

    staff1 = _staff_mozos({"N1": ["Martes", "Jueves"]})  # sin par consecutivo -> F activo
    resultado1 = generar_semana_cpsat_con_cobertura_nocturna(
        staff1, SEMANA, cfg, estado_rotacion, noches_ultimos_5_dias
    )
    assert resultado1["schedule"]["N1"][6] == "Libre", (
        f"Se esperaba domingo libre en semana 1: {resultado1['schedule']['N1']}"
    )
    assert resultado1["forced_days_off_extra"].get("N1") == ["Lunes", "Martes"], (
        f"Se esperaba compensación Lunes+Martes calculada por el punto 1: "
        f"{resultado1['forced_days_off_extra']}"
    )

    # ── Semana 2: staff construido a partir del resultado REAL de la 1 ──
    staff2 = _staff_mozos()
    for p in staff2:
        extra = resultado1["forced_days_off_extra"].get(p["name"])
        if extra:
            existentes = p.get("forced_days_off", [])
            p["forced_days_off"] = list(dict.fromkeys(existentes + extra))

    semana2 = SEMANA + timedelta(weeks=1)
    resultado2 = generar_semana_cpsat_con_cobertura_nocturna(
        staff2, semana2, cfg,
        resultado1["estado_rotacion"], resultado1["noches_ultimos_5_dias"],
    )

    tramo = [
        resultado1["schedule"]["N1"][6],
        resultado2["schedule"]["N1"][0],
        resultado2["schedule"]["N1"][1],
    ]
    assert tramo == ["Libre", "Libre", "Libre"], (
        f"Se esperaban 3 días libres consecutivos (Domingo sem.1 + "
        f"Lunes+Martes sem.2, encadenados de verdad): {tramo}"
    )


# ─── Estrés: Domingo rompería cobertura → el modelo cede ───────────────────

def test_cede_cuando_domingo_rompe_cobertura_minima():
    cfg = _cfg_mozos()
    # N1 sin par consecutivo disponible (término F activo) Y N2+N3 de
    # vacaciones el domingo: si N1 también librara ese día, solo quedaría
    # N4 para cubrir Mañana+Tarde (mínimo 1 cada uno) — imposible con una
    # sola persona.
    staff = _staff_mozos({
        "N1": ["Martes", "Jueves"],
        "N2": ["Domingo"],
        "N3": ["Domingo"],
    })

    model, x, names, _ = _construir_modelo(staff, SEMANA, cfg, nombre_sustituto_cobertura="N1")
    model.Add(x["N1", 4, "Noche"] == 1)
    model.Add(x["N1", 5, "Noche"] == 1)

    sched = _resolver_y_extraer(model, x, names)  # no debe lanzar RuntimeError

    assert sched["N1"][6] != "Libre", (
        f"Se esperaba que N1 NO librara el domingo (cobertura mínima lo "
        f"exige, N2/N3 de vacaciones ese día): {sched['N1']}"
    )


# ─── No afecta a quien no es el sustituto ───────────────────────────────────

def test_no_afecta_a_quien_no_es_el_sustituto():
    """Con el término F activo para N1 (sin par consecutivo disponible),
    forzar que N2 (NO es el sustituto) trabaje el Domingo no debe costar
    nada en el objetivo. Forzar lo mismo para N1 sí debe encarecerlo en
    exactamente PESO_DOMINGO_SUSTITUTO."""
    cfg = _cfg_mozos()
    staff = _staff_mozos({"N1": ["Martes", "Jueves"]})

    def _resolver(forzar_domingo_trabajado_de):
        model, x, names, _ = _construir_modelo(staff, SEMANA, cfg, nombre_sustituto_cobertura="N1")
        model.Add(x["N1", 4, "Noche"] == 1)
        model.Add(x["N1", 5, "Noche"] == 1)
        model.Add(x[forzar_domingo_trabajado_de, 6, "Libre"] == 0)
        _, objetivo = _solve_con_objetivo(model, x, names)
        return objetivo

    model_base, x_base, names_base, _ = _construir_modelo(
        staff, SEMANA, cfg, nombre_sustituto_cobertura="N1"
    )
    model_base.Add(x_base["N1", 4, "Noche"] == 1)
    model_base.Add(x_base["N1", 5, "Noche"] == 1)
    _, objetivo_base = _solve_con_objetivo(model_base, x_base, names_base)

    objetivo_n2_trabaja_domingo = _resolver("N2")
    objetivo_n1_trabaja_domingo = _resolver("N1")

    assert objetivo_n2_trabaja_domingo == objetivo_base, (
        f"Forzar que N2 (no es el sustituto) trabaje el domingo no debería "
        f"cambiar el objetivo (término F no debe incluirlo): base="
        f"{objetivo_base}, con N2 forzado={objetivo_n2_trabaja_domingo}"
    )
    assert objetivo_n1_trabaja_domingo == objetivo_base + PESO_DOMINGO_SUSTITUTO, (
        f"Forzar que N1 (el sustituto) trabaje el domingo debería encarecer "
        f"el objetivo en exactamente PESO_DOMINGO_SUSTITUTO "
        f"({PESO_DOMINGO_SUSTITUTO}): base={objetivo_base}, con N1 forzado="
        f"{objetivo_n1_trabaja_domingo}"
    )


# ─── No se activa en recepción (prefer_next_week_for_cover=False) ──────────

def test_no_se_activa_en_recepcion():
    cfg = _cfg_recepcion()
    assert cfg.prefer_next_week_for_cover is False

    staff = [
        {"name": "Jefe", "role": "jefe"},
        {"name": "N1", "role": "normal"},
        {"name": "N2", "role": "normal"},
        {"name": "N3", "role": "normal"},
        {"name": "N4", "role": "normal"},
        {"name": "N5", "role": "normal"},
    ]

    model_sin, x_sin, names_sin, _ = _construir_modelo(staff, SEMANA, cfg)
    _, objetivo_sin = _solve_con_objetivo(model_sin, x_sin, names_sin)

    # Mismo escenario, pero indicando explícitamente un "sustituto" — no
    # debería tener ningún efecto porque cfg.prefer_next_week_for_cover
    # es False en recepción.
    model_con, x_con, names_con, _ = _construir_modelo(
        staff, SEMANA, cfg, nombre_sustituto_cobertura="N1"
    )
    _, objetivo_con = _solve_con_objetivo(model_con, x_con, names_con)

    assert objetivo_con == objetivo_sin, (
        f"En recepción (prefer_next_week_for_cover=False) el término F no "
        f"debería activarse ni con nombre_sustituto_cobertura indicado: "
        f"sin={objetivo_sin}, con={objetivo_con}"
    )


# ─── Solapamiento: descanso post-racha + compensación de sustituto ─────────

def test_solapamiento_racha_5_noches_y_sustituto_misma_persona_misma_semana():
    """Escenario mínimo donde AMBOS mecanismos de forced_days_off_extra
    coinciden en la misma persona la misma semana:
      1. Descanso post-racha (motor_cpsat.agregar_restriccion_racha_noches):
         Objetivo encadena 5 noches (Martes-Sábado) y la racha termina en
         sábado, así que 1 de los 2 días de descanso obligatorio se sale de
         la semana -> por sí solo forzaría solo "Lunes" la semana siguiente
         (el domingo de descanso cae dentro de esta semana).
      2. Compensación incondicional de sustituto (cfg.prefer_next_week_for_
         cover): Objetivo es también quien cubre cfg.night_fixed_days_off
         (Viernes+Sábado) esta semana -> por sí solo forzaría "Lunes"+
         "Martes" la semana siguiente, sin condición.

    Se fuerza el escenario con SOLO entradas legítimas del orquestador real
    (generar_semana_cpsat_con_cobertura_nocturna, sin tocar _construir_
    modelo): NocheFijo tiene una ausencia PARCIAL (forced_days_off =
    Martes+Miércoles+Jueves, p.ej. una baja de 3 días) que dentro de esa
    misma semana se solapa con sus días fijos Viernes+Sábado -> deja
    Martes-Sábado sin cobertura de Noche. Dando a todo el resto de la
    plantilla un rol en cfg.no_night_roles, Objetivo queda como la ÚNICA
    persona elegible para Noche, así que min_coverage Noche=1 la fuerza
    estructuralmente a cubrir esos 5 días -- sin necesidad de forzar
    directamente ninguna celda del modelo. La ausencia es solo de 3 días
    (no toda la semana), así que no activa el modo respaldo (el contador de
    ausencias consecutivas de night_fixed sigue en 0) y la selección de
    sustituto sigue el camino normal de una sola llamada.

    Verifica que forced_days_off_extra["Objetivo"] sea EXACTAMENTE la unión
    ["Lunes", "Martes"] -- sin duplicados y sin que un mecanismo pise al
    otro (mecanismo 1 solo pide "Lunes"; mecanismo 2 pide "Lunes"+"Martes"
    incondicional; la unión no debe perder "Martes" ni duplicar "Lunes")."""
    cfg = _cfg_mozos(
        min_coverage={"Mañana": 2, "Tarde": 2, "Noche": 1, "Partido": 0},
        max_coverage={"Mañana": 4, "Tarde": 4, "Noche": 1, "Partido": 1},
        no_night_roles={"diurno"},
    )
    staff = [
        {"name": "NocheFijo", "role": "night_fixed",
         "forced_days_off": ["Martes", "Miércoles", "Jueves"]},
        {"name": "Objetivo", "role": "normal"},
        {"name": "D1", "role": "diurno"},
        {"name": "D2", "role": "diurno"},
        {"name": "D3", "role": "diurno"},
        {"name": "D4", "role": "diurno"},
    ]
    estado_rotacion = {
        "orden": ["Objetivo"],
        "siguiente_turno": "Objetivo",
        "deudas": {},
        "usar_orden_fijo": True,
        "historico_balance": {},
        "ausencias_night_fixed_consecutivas": 0,
    }

    resultado = generar_semana_cpsat_con_cobertura_nocturna(
        staff, SEMANA, cfg, estado_rotacion, noches_ultimos_5_dias={},
    )

    turnos = resultado["schedule"]["Objetivo"]
    assert turnos[1:6] == ["Noche"] * 5, (
        f"Precondición del escenario: se esperaba Objetivo con Noche "
        f"Martes-Sábado (única elegible, cobertura mínima la fuerza), "
        f"fue {turnos}"
    )
    assert turnos[6] == "Libre", (
        f"Domingo de esta semana debía ser 'Libre' (mitad del descanso "
        f"post-racha que SÍ cabe dentro de la semana), fue {turnos[6]!r}"
    )
    assert resultado["estado_rotacion"]["ausencias_night_fixed_consecutivas"] == 0, (
        "una ausencia parcial (3 días, no toda la semana) no debe activar "
        "el contador de ausencias consecutivas de night_fixed"
    )

    extra = resultado["forced_days_off_extra"].get("Objetivo")
    assert extra == ["Lunes", "Martes"], (
        f"Se esperaba la unión exacta de ambos mecanismos (['Lunes', "
        f"'Martes']), sin que uno pisara al otro: {extra}"
    )
    assert len(extra) == len(set(extra)), (
        f"forced_days_off_extra no debe contener días duplicados: {extra}"
    )


# ─── Gating: con par consecutivo disponible, gana la continuidad ───────────

def test_gating_prefiere_par_consecutivo_sobre_domingo_suelto():
    """Reproduce el escenario de estrés que detectó el bug original: N1 SIN
    ningún día bloqueado (par consecutivo estructuralmente disponible), y
    una trampa de escasez real que fuerza, SI N1 trabaja tanto Lunes como
    Martes, turnos distintos entre ambos días (rotura de continuidad
    inevitable — Partido bloqueado como escapatoria):

      - N2 agota el único hueco de Tarde el Lunes.
      - N3 agota el único hueco de Mañana el Martes.
      - N1 forzado a librar Miércoles (común a ambas alternativas) y a
        trabajar Jueves; exactamente uno de {Martes, Domingo} es su
        segundo libre (opción real, no una tercera vía).

    Opción A (par consecutivo Martes+Miércoles, sin romper continuidad,
    sin domingo): antes del fix costaba 15 (término F sin gating). Con el
    gating, el término F ni se añade (hay par consecutivo disponible) ->
    coste 0.
    Opción B (Miércoles+Domingo sueltos, Lunes+Martes trabajados con
    turnos distintos): coste 5 (rotura), término F ausente igualmente.

    Antes del fix el solver elegía B (5 < 15). Con el gating, el término F
    no compite en absoluto para N1 (hay par consecutivo disponible) — el
    objetivo se reduce a minimizar solo B, así que ahora debe elegir A.
    """
    cfg = _cfg_mozos(
        max_coverage={"Mañana": 1, "Tarde": 1, "Noche": 1, "Partido": 0},  # Partido bloqueado
    )
    staff = _staff_mozos()  # N1 sin días bloqueados -> par consecutivo SÍ disponible
    assert _tiene_par_consecutivo_disponible(cfg, set())  # confirma la precondición del gating

    model, x, names, _ = _construir_modelo(staff, SEMANA, cfg, nombre_sustituto_cobertura="N1")
    model.Add(x["N1", 4, "Noche"] == 1)   # Viernes: cubre
    model.Add(x["N1", 5, "Noche"] == 1)   # Sábado: cubre
    model.Add(x["N1", 2, "Libre"] == 1)   # Miércoles: libre siempre (común)
    model.Add(x["N1", 0, "Libre"] == 0)   # Lunes: debe trabajar
    model.Add(x["N1", 3, "Libre"] == 0)   # Jueves: debe trabajar
    model.Add(x["N1", 1, "Libre"] + x["N1", 6, "Libre"] == 1)  # exactamente uno: Martes o Domingo
    model.Add(x["N2", 0, "Tarde"] == 1)   # agota el único slot de Tarde el Lunes
    model.Add(x["N3", 1, "Mañana"] == 1)  # agota el único slot de Mañana el Martes

    sched = _resolver_y_extraer(model, x, names)

    lunes, martes, domingo = sched["N1"][0], sched["N1"][1], sched["N1"][6]
    assert martes == "Libre", (
        f"Se esperaba que el gating hiciera preferir el par consecutivo "
        f"Martes+Miércoles (sin romper continuidad) en vez de sacrificarlo "
        f"por el domingo suelto: horario N1={sched['N1']}"
    )
    assert domingo != "Libre"
    assert lunes == "Mañana", f"Lunes forzado por escasez a Mañana, fue {lunes}"
