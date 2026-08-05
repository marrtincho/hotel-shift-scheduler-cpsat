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
    # N5/N6 (regla de rotación de cobertura nocturna):
    # con el Domingo del sustituto ahora forzado Libre de forma DURA (ver
    # docstring del módulo, sección actualizada), N1 acumula más días
    # forzados que antes (Fri+Sat de cobertura + Domingo + su propio
    # patrón) — solo 4 "normal" no siempre dejan margen para que N2-N4
    # también cumplan su propio patrón de 2 libres junto con la cobertura
    # mínima diaria, en escenarios donde N1 ADEMÁS tiene vacaciones
    # propias. 2 personas extra dan el margen que hacía falta sin
    # participar de ningún bloqueo/aserción de los tests.
    for n in ("N1", "N2", "N3", "N4", "N5", "N6"):
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

    model, x, names, _ = _construir_modelo(staff, SEMANA, cfg, nombre_sustituto_cobertura="N1", dias_bloqueados_externos={"N1": {4, 5}})
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


# ─── Estrés: Domingo es regla dura → NUNCA cede, ni bajo presión de cobertura ─

def test_no_cede_domingo_es_regla_dura_incluso_si_rompe_cobertura_minima():
    """NOTA (regla de negocio confirmada por el usuario): el
    Domingo del sustituto que cubre Viernes+Sábado ya NO es una preferencia
    blanda (término F) que pueda ceder bajo presión de cobertura — es una
    restricción DURA (ver restricción #7, caso especial). Este test antes
    verificaba que el modelo "cedía" (N1 trabajaba el domingo) cuando no
    quedaba nadie más para cubrir Mañana+Tarde; ahora, con la regla dura,
    ese mismo conflicto debe reportarse como INFEASIBLE genuino — el hueco
    de cobertura no puede resolverse a costa de la regla de descanso del
    sustituto. Staff mínimo (N1-N4, SIN el buffer N5/N6 de otros tests de
    este archivo) para preservar la escasez real que crea el conflicto."""
    cfg = _cfg_mozos()
    staff = [
        {"name": "NightFixed", "role": "night_fixed"},
        {"name": "N1", "role": "normal", "forced_days_off": ["Martes", "Jueves"]},
        {"name": "N2", "role": "normal", "forced_days_off": ["Domingo"]},
        {"name": "N3", "role": "normal", "forced_days_off": ["Domingo"]},
        {"name": "N4", "role": "normal"},
    ]

    model, x, names, _ = _construir_modelo(staff, SEMANA, cfg, nombre_sustituto_cobertura="N1", dias_bloqueados_externos={"N1": {4, 5}})
    model.Add(x["N1", 4, "Noche"] == 1)
    model.Add(x["N1", 5, "Noche"] == 1)

    with pytest.raises(RuntimeError, match="INFEASIBLE"):
        _resolver_y_extraer(model, x, names)


# ─── No afecta a quien no es el sustituto ───────────────────────────────────

def test_no_afecta_a_quien_no_es_el_sustituto():
    """NOTA (regla dura): forzar que N2 (NO es el sustituto)
    trabaje el Domingo no afecta en nada la regla del sustituto (N1 sigue
    forzado a librar el domingo, sin cambio de objetivo). Forzar que N1
    (el sustituto real) trabaje el domingo ahora contradice directamente la
    restricción dura #7 (x[N1,6,"Libre"]==1) — ya no es una cuestión de
    coste (antes PESO_DOMINGO_SUSTITUTO), es un conflicto duro genuino:
    debe reportarse INFEASIBLE."""
    cfg = _cfg_mozos()
    staff = _staff_mozos({"N1": ["Martes", "Jueves"]})

    def _resolver(forzar_domingo_trabajado_de):
        model, x, names, _ = _construir_modelo(staff, SEMANA, cfg, nombre_sustituto_cobertura="N1", dias_bloqueados_externos={"N1": {4, 5}})
        model.Add(x["N1", 4, "Noche"] == 1)
        model.Add(x["N1", 5, "Noche"] == 1)
        model.Add(x[forzar_domingo_trabajado_de, 6, "Libre"] == 0)
        _, objetivo = _solve_con_objetivo(model, x, names)
        return objetivo

    model_base, x_base, names_base, _ = _construir_modelo(
        staff, SEMANA, cfg, nombre_sustituto_cobertura="N1",
        dias_bloqueados_externos={"N1": {4, 5}},
    )
    model_base.Add(x_base["N1", 4, "Noche"] == 1)
    model_base.Add(x_base["N1", 5, "Noche"] == 1)
    _, objetivo_base = _solve_con_objetivo(model_base, x_base, names_base)

    objetivo_n2_trabaja_domingo = _resolver("N2")
    assert objetivo_n2_trabaja_domingo == objetivo_base, (
        f"Forzar que N2 (no es el sustituto) trabaje el domingo no debería "
        f"cambiar el objetivo (la regla dura de N1 es independiente de N2): "
        f"base={objetivo_base}, con N2 forzado={objetivo_n2_trabaja_domingo}"
    )

    with pytest.raises(RuntimeError, match="INFEASIBLE"):
        _resolver("N1")


# ─── No se activa en recepción (prefer_next_week_for_cover=False) ──────────

def test_no_se_activa_en_recepcion():
    cfg = _cfg_recepcion()
    assert cfg.prefer_next_week_for_cover is False

    # N1-N7 (no solo N1-N5): con la restricción dura de patrón de libres
    # (motor_cpsat.DIAS_LIBRES_SEGUIDOS_POR_SEMANA), 6 "normal" ya cubrían
    # la demanda diaria exactamente al límite (0 de margen: oferta ==
    # demanda persona-día) — factible en teoría pero sin ningún margen
    # para que el par CONSECUTIVO de cada uno encaje en una rotación real,
    # infactible en la práctica por un motivo ajeno a lo que este test
    # verifica (el gating de prefer_next_week_for_cover en recepción). Un
    # 7º "normal" da el margen real que hacía falta.
    staff = [
        {"name": "Jefe", "role": "jefe"},
        {"name": "N1", "role": "normal"},
        {"name": "N2", "role": "normal"},
        {"name": "N3", "role": "normal"},
        {"name": "N4", "role": "normal"},
        {"name": "N5", "role": "normal"},
        {"name": "N6", "role": "normal"},
        {"name": "N7", "role": "normal"},
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

    NOTA (hallazgo de producción, restricción dura NUEVA de
    patrón de libres, motor_cpsat.DIAS_LIBRES_SEGUIDOS_POR_SEMANA): la
    versión original de este test dejaba que la necesidad de Objetivo
    cubrir Martes-Sábado emergiera SOLO de la presión de cobertura mínima
    + cfg.no_night_roles (sin forzar directamente ninguna celda), tal como
    decidiría el orquestador real. Eso ya no es compatible con la
    restricción dura nueva: _construir_modelo solo sabe qué días están
    "no disponibles" para el patrón de libres de una persona a través de
    dias_bloqueados_externos (días YA fijados por una restricción añadida
    DESPUÉS de _construir_modelo, ver su docstring) — los días Martes-
    Jueves de este escenario NO se fijan así (emergen de la optimización,
    no de una asignación externa previa), así que la restricción nueva no
    podía saber que esos días eran estructuralmente obligatorios para
    Objetivo, y el patrón de 2 libres (que caería en Lunes+Domingo, no
    consecutivos entre sí) competía con la cobertura mínima de forma
    espuria. Se reconstruye el escenario con _construir_modelo +
    agregar_restriccion_racha_noches directamente (mismo patrón que otros
    tests de este archivo, p.ej. test_no_afecta_a_quien_no_es_el_
    sustituto), indicando explícitamente dias_bloqueados_externos para que
    la restricción nueva disponga de la misma información que tendría en
    el escenario real, y replicando a mano (no reimplementando, solo
    aplicando literalmente) las 5 líneas de unión incondicional que
    generar_semana_cpsat_con_cobertura_nocturna ya aplica sobre
    forced_days_off_extra — la lógica de unión en sí no cambia."""
    from motor_cpsat import agregar_restriccion_racha_noches, _resolver_y_extraer_con_extra

    cfg = _cfg_mozos(
        min_coverage={"Mañana": 2, "Tarde": 2, "Noche": 1, "Partido": 0},
        max_coverage={"Mañana": 4, "Tarde": 4, "Noche": 1, "Partido": 1},
        no_night_roles={"diurno"},
        night_fixed_days_off=[4, 5],
        prefer_next_week_for_cover=True,
    )
    staff = [
        # NocheFijo cubre Noche con normalidad Lunes y Domingo (turno fijo,
        # solo ausente Martes-Jueves por forced_days_off y Viernes+Sábado
        # por night_fixed_days_off) — sin esta persona, Objetivo sería el
        # ÚNICO candidato posible también Lunes/Domingo (nadie más está
        # fuera de cfg.no_night_roles), lo que chocaría de forma genuina
        # con su propio patrón de libres (necesita estar libre esos 2 días
        # concretos) — no es lo que este test existe para probar.
        {"name": "NocheFijo", "role": "night_fixed",
         "forced_days_off": ["Martes", "Miércoles", "Jueves"]},
        {"name": "Objetivo", "role": "normal"},
        {"name": "D1", "role": "diurno"},
        {"name": "D2", "role": "diurno"},
        {"name": "D3", "role": "diurno"},
        {"name": "D4", "role": "diurno"},
        {"name": "D5", "role": "diurno"},
        {"name": "D6", "role": "diurno"},
    ]

    model, x, names, by_name = _construir_modelo(
        staff, SEMANA, cfg, nombre_sustituto_cobertura="Objetivo",
        dias_bloqueados_externos={"Objetivo": {1, 2, 3, 4, 5}},
    )
    for d in range(1, 6):  # Martes-Sábado: Objetivo cubre Noche (único elegible)
        model.Add(x["Objetivo", d, "Noche"] == 1)

    # NOTA (regla de racha mínima de 2, MIN_NOCHES_RACHA):
    # NocheFijo trabaja Noche por turno fijo Lunes y Domingo (Martes-Jueves
    # forzado libre por vacación, Viernes+Sábado por night_fixed_days_off).
    # Un empalme VACÍO ({} -> [False]*5 para todos) es IRREAL para un
    # night_fixed en marcha: su patrón semanal recurrente SIEMPRE trabaja el
    # Domingo anterior (turno fijo), así que el Lunes de esta semana NUNCA
    # está genuinamente aislado en producción — el empalme correcto debe
    # reflejar ese Domingo pasado en Noche (índice 4), igual que pasaría con
    # el estado real. Sin este dato, el Lunes de NocheFijo parece una noche
    # suelta aislada y choca de forma espuria con MIN_NOCHES_RACHA(2).
    # personas_bloque_continua={"NocheFijo"}: réplica manual de la misma
    # exención que generar_semana_cpsat_con_cobertura_nocturna aplica siempre
    # a night_fixed (ver motor_cpsat.py) — su Domingo en Noche es turno fijo
    # estructural, no un arranque nuevo que necesite verificarse contra el
    # Sábado (que para NocheFijo es Libre por night_fixed_days_off, no por
    # elección). Sin esta exención, la regla conservadora de Domingo
    # (Domingo<=Sábado) chocaría de forma espuria con su propio turno fijo.
    postweek_vars = agregar_restriccion_racha_noches(
        model, x, names,
        noches_ultimos_5_dias={"NocheFijo": [False, False, False, False, True]},
        personas_bloque_continua={"NocheFijo"},
    )
    extra_vars = {}
    for n in names:
        extra_vars[f"{n}__lunes"], extra_vars[f"{n}__martes"] = postweek_vars[n]

    schedule, valores_extra = _resolver_y_extraer_con_extra(model, x, names, extra_vars)

    turnos = schedule["Objetivo"]
    assert turnos[1:6] == ["Noche"] * 5, (
        f"Precondición del escenario: se esperaba Objetivo con Noche "
        f"Martes-Sábado: {turnos}"
    )
    assert turnos[6] == "Libre", (
        f"Domingo de esta semana debía ser 'Libre' (mitad del descanso "
        f"post-racha que SÍ cabe dentro de la semana), fue {turnos[6]!r}"
    )
    assert valores_extra["Objetivo__lunes"] == 1, (
        "el mecanismo 1 (descanso post-racha) debía activar 'Lunes' para "
        "la semana siguiente (racha de 5 termina en Sábado, 1 de los 2 "
        "días de descanso cae fuera de esta semana)"
    )
    assert valores_extra["Objetivo__martes"] == 0, (
        "por sí solo, el mecanismo 1 NO debía pedir 'Martes' (el otro día "
        "de descanso, Domingo, sí cupo dentro de esta semana)"
    )

    # Unión de ambos mecanismos — misma lógica, literal, que
    # generar_semana_cpsat_con_cobertura_nocturna aplica sobre el
    # diccionario que ya construyó a partir de postweek_vars (mecanismo 1)
    # antes de fusionar con la compensación incondicional (mecanismo 2).
    forced_days_off_extra = {}
    dias = []
    if valores_extra["Objetivo__lunes"]:
        dias.append("Lunes")
    if valores_extra["Objetivo__martes"]:
        dias.append("Martes")
    if dias:
        forced_days_off_extra["Objetivo"] = dias
    if cfg.prefer_next_week_for_cover:
        existentes = forced_days_off_extra.get("Objetivo", [])
        forced_days_off_extra["Objetivo"] = list(dict.fromkeys(existentes + ["Lunes", "Martes"]))

    extra = forced_days_off_extra.get("Objetivo")
    assert extra == ["Lunes", "Martes"], (
        f"Se esperaba la unión exacta de ambos mecanismos (['Lunes', "
        f"'Martes']), sin que uno pisara al otro: {extra}"
    )
    assert len(extra) == len(set(extra)), (
        f"forced_days_off_extra no debe contener días duplicados: {extra}"
    )


# ─── Gating: con par consecutivo disponible, gana la continuidad ───────────

def test_domingo_no_exige_par_consecutivo_aunque_haya_uno_disponible():
    """NOTA (regla de negocio confirmada por el usuario): el
    mozo que cubre Viernes+Sábado libra SIEMPRE el Domingo (regla dura,
    restricción #7 caso especial) y el día libre restante puede colocarse
    donde haga falta, SIN exigir que forme un par consecutivo — ni siquiera
    cuando hay uno estructuralmente disponible (a diferencia de cualquier
    otra persona normal, para quien restricción #7 SÍ exige un par cuando
    existe). Se fuerza Miércoles Libre y Jueves trabajado: el Domingo YA
    cubre uno de los 2 libres, así que el patrón solo exige 1 Libre más
    entre los días no forzados — Miércoles ya lo satisface, y Martes queda
    libre para trabajar sin violar nada."""
    cfg = _cfg_mozos()
    staff = _staff_mozos()  # N1 sin días bloqueados -> par consecutivo SÍ disponible
    assert _tiene_par_consecutivo_disponible(cfg, set())  # confirma que SÍ había un par posible

    model, x, names, _ = _construir_modelo(staff, SEMANA, cfg, nombre_sustituto_cobertura="N1", dias_bloqueados_externos={"N1": {4, 5}})
    model.Add(x["N1", 4, "Noche"] == 1)   # Viernes: cubre
    model.Add(x["N1", 5, "Noche"] == 1)   # Sábado: cubre
    model.Add(x["N1", 2, "Libre"] == 1)   # Miércoles: libre siempre (común)
    model.Add(x["N1", 3, "Libre"] == 0)   # Jueves: debe trabajar (Miércoles no puede emparejarse con Jueves)

    sched = _resolver_y_extraer(model, x, names)

    domingo = sched["N1"][6]
    assert domingo == "Libre", (
        f"El Domingo del sustituto de Viernes+Sábado es SIEMPRE Libre "
        f"(regla dura), con o sin par consecutivo disponible: {sched['N1']}"
    )
    libres_lunes_a_jueves = sched["N1"][0:4].count("Libre")
    assert libres_lunes_a_jueves == 1, (
        f"Entre Lunes y Jueves debía haber exactamente 1 Libre más (el "
        f"segundo del patrón de 2, ya satisfecho por Miércoles, sin exigir "
        f"que forme par con nada): horario N1={sched['N1']}"
    )
