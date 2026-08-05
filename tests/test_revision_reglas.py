"""
tests/test_revision_reglas.py — Tests añadidos en la revisión de código de
esta sesión. Cada bloque ancla una de las reglas de negocio que la revisión
encontró mal cubiertas o mal implementadas:

  1. Noche fantasma: con max_coverage blando SOLO en Mañana/Tarde, un
     excedente de plantilla nunca puede convertirse en noches que el solver
     se inventa (quién cubre cada noche lo decide cobertura_nocturna.py).
  2. Descanso mínimo de 9h: la tabla de transiciones prohibidas derivada de
     los horarios configurados coincide EXACTAMENTE con lo que bloquea la
     restricción #3 del modelo, y app._calcular_advertencias avisa de las
     cuatro (antes solo de las dos que terminan en Mañana).
  3. Continuidad entre días libres: decisión de negocio confirmada — un
     cambio de turno a través del bloque de libres NO se penaliza; el mismo
     cambio entre dos días trabajados SÍ.
  4. Patrón de libres: nadie acaba con la semana entera libre por tener
     más peticiones concedidas que el tamaño del patrón.
"""

import copy
from datetime import date

import pytest

from modelo_base import DeptConfig, DAYS
from motor_cpsat import (
    _construir_modelo, _resolver_y_extraer,
    generar_semana_cpsat_con_cobertura_nocturna,
    TURNOS_MAX_COBERTURA_BLANDA,
    PESO_ROTURAS_CONTINUIDAD,
)

SEMANA = date(2026, 8, 10)  # Lunes real — la semana del hallazgo de producción


@pytest.fixture(autouse=True)
def _data_dir_aislado(tmp_path, monkeypatch):
    import herramientas
    backup = tmp_path / "backups"
    backup.mkdir()
    monkeypatch.setattr(herramientas, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(herramientas, "BACKUP_DIR", str(backup))
    monkeypatch.setattr(herramientas, "FESTIVOS_FILE", str(tmp_path / "festivos.json"))


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


def _staff_excedente() -> list:
    """Composición real reportada por el usuario para 2026-08-10: 12
    personas contra una capacidad diaria de 8 (3+3+1+1) — hay excedente
    estructural, que es justo lo que obliga a superar algún máximo."""
    return [
        {"name": "Jefe1", "role": "jefe"},
        {"name": "Sub1", "role": "subjefe"},
        {"name": "Adj1", "role": "adjunto_recepcion"},
        {"name": "Adj2", "role": "adjunto_recepcion"},
        {"name": "Adj3", "role": "adjunto_recepcion"},
        {"name": "Martin", "role": "normal", "forced_days_off": ["Miércoles"]},
    ] + [{"name": f"Emp{i}", "role": "normal"} for i in range(2, 8)]


def _estado_rotacion_inicial(staff: list, cfg: DeptConfig) -> dict:
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


# ═══════════════════════════════════════════════════════════════════════════
# 1. Noche fantasma — la regresión que encontró la revisión
# ═══════════════════════════════════════════════════════════════════════════

def test_excedente_de_plantilla_nunca_genera_noches_extra():
    """REGRESIÓN (revisión de esta sesión). Cuando max_coverage era blando en
    los CUATRO turnos, Noche era el destino MÁS BARATO para el personal
    sobrante: K=5000 pelado, frente a K+I=5030 en Mañana/Tarde y K+D=5020 en
    Partido. El solver "resolvía" el excedente inventándose noches — que es
    lo peor que puede pasar aquí, porque quién cubre cada noche NO lo decide
    el solver (lo decide cobertura_nocturna.py y se fija como restricción
    dura después), así que una noche de más rompe la rotación por bloques y
    además contamina noches_ultimos_5_dias de la semana siguiente.

    Con el máximo duro en Noche y Partido, el excedente solo puede ir a
    Mañana/Tarde."""
    cfg = _cfg_recepcion_estrecha()
    staff = _staff_excedente()

    resultado = generar_semana_cpsat_con_cobertura_nocturna(
        staff, SEMANA, cfg, _estado_rotacion_inicial(staff, cfg), {}
    )
    schedule = resultado["schedule"]

    for d in range(7):
        cuantos = sum(1 for turnos in schedule.values() if turnos[d] == "Noche")
        assert cuantos == cfg.max_coverage["Noche"], (
            f"{DAYS[d]}: {cuantos} personas de Noche, el máximo duro es "
            f"{cfg.max_coverage['Noche']} — el solver se ha inventado una "
            f"noche que la rotación nocturna no había planificado"
        )
        partido = sum(1 for turnos in schedule.values() if turnos[d] == "Partido")
        assert partido <= cfg.max_coverage["Partido"], (
            f"{DAYS[d]}: {partido} personas de Partido, máximo duro "
            f"{cfg.max_coverage['Partido']}"
        )

    # Y el escenario debe seguir siendo uno con excedente real — si no,
    # el test no estaría probando nada.
    excesos = [
        (DAYS[d], turno)
        for d in range(7) for turno in TURNOS_MAX_COBERTURA_BLANDA
        if sum(1 for t in schedule.values() if t[d] == turno) > cfg.max_coverage[turno]
    ]
    assert excesos, (
        "Se esperaba excedente real en Mañana/Tarde en esta composición; "
        "sin él, el test no demuestra que el excedente se desvía ahí"
    )


def test_max_coverage_blando_solo_en_manana_y_tarde():
    """Ancla la decisión de alcance: Noche y Partido NO están en el conjunto
    de turnos con máximo blando. Si alguien los añade, este test lo para."""
    assert set(TURNOS_MAX_COBERTURA_BLANDA) == {"Mañana", "Tarde"}


def test_el_exceso_respeta_el_orden_ambos_al_maximo_antes_de_exceder():
    """Regla de negocio (pedido explícito del usuario): no se supera el máximo
    de un turno mientras el OTRO turno de máximo blando todavía tenga sitio.
    "De nada sirve exceder el máximo de Tarde cuando Mañana aún no lo ha
    alcanzado."

    Se comprueba sobre la semana completa del escenario con excedente real: en
    cada día en que algún turno blando supera su máximo, el otro debe estar
    AL MENOS en su máximo."""
    cfg = _cfg_recepcion_estrecha()
    staff = _staff_excedente()

    resultado = generar_semana_cpsat_con_cobertura_nocturna(
        staff, SEMANA, cfg, _estado_rotacion_inicial(staff, cfg), {}
    )
    schedule = resultado["schedule"]

    hubo_exceso = False
    for d in range(7):
        cuenta = {
            turno: sum(1 for t in schedule.values() if t[d] == turno)
            for turno in TURNOS_MAX_COBERTURA_BLANDA
        }
        for turno, n in cuenta.items():
            if n <= cfg.max_coverage[turno]:
                continue
            hubo_exceso = True
            for otro, n_otro in cuenta.items():
                if otro == turno:
                    continue
                assert n_otro >= cfg.max_coverage[otro], (
                    f"{DAYS[d]}: {turno}={n} supera su máximo "
                    f"({cfg.max_coverage[turno]}) mientras {otro}={n_otro} "
                    f"aún tiene sitio (máximo {cfg.max_coverage[otro]}) — "
                    f"hay que llenar ambos antes de exceder ninguno"
                )
    assert hubo_exceso, "el escenario debía producir algún exceso real"


def test_orden_del_exceso_es_preferencia_dominante_no_restriccion_dura():
    """El término L pesa PESO_ORDEN_EXCESO(20000), muy por encima de
    PESO_EXCESO_COBERTURA(5000) y de PESO_COBERTURA_AUSENCIA(1000), así que
    gana siempre que exista una alternativa legal. Pero es BLANDO a propósito:
    el mal orden solo aparece cuando está estructuralmente forzado (p.ej. la
    regla de descanso de 9h impide llevar gente a Mañana), y ahí un modelo con
    esto como restricción dura devolvería INFEASIBLE en vez de un horario
    válido con advertencia.

    Este test fija esa jerarquía de pesos; el de arriba fija el efecto."""
    from motor_cpsat import (
        PESO_ORDEN_EXCESO, PESO_EXCESO_COBERTURA, PESO_COBERTURA_AUSENCIA,
    )
    assert PESO_ORDEN_EXCESO > PESO_EXCESO_COBERTURA > PESO_COBERTURA_AUSENCIA


def test_validar_viabilidad_no_bloquea_lo_que_el_motor_si_resuelve():
    """REGRESIÓN (revisión de esta sesión): app.api_generar llama a
    modelo_base.validar_viabilidad ANTES de elegir motor, y su fórmula de
    capacidad (personas x 5 <= sum(max_coverage) x 7) trataba max_coverage
    como techo duro de los CUATRO turnos. Con Mañana/Tarde blandas eso
    rechazaba con 422 "Personal excesivo" semanas que motor_cpsat sí genera —
    bloqueando justo el caso que el término K existe para desbloquear.

    Con turnos_sin_techo=TURNOS_MAX_COBERTURA_BLANDA la capacidad es ilimitada
    y la comprobación de "sobra gente" se omite. La de "falta gente"
    (min_coverage, siempre dura) NO se toca."""
    from modelo_base import validar_viabilidad

    cfg = _cfg_recepcion_estrecha()
    staff = _staff_excedente()  # 12 personas: 60 días-turno vs 56 de capacidad

    viable_duro, msg = validar_viabilidad(staff, cfg)
    assert not viable_duro and "Personal excesivo" in msg, (
        f"el comportamiento histórico (todos los máximos duros) debe "
        f"conservarse por defecto: {msg!r}"
    )

    viable_blando, msg_blando = validar_viabilidad(
        staff, cfg, turnos_sin_techo=TURNOS_MAX_COBERTURA_BLANDA
    )
    assert viable_blando, (
        f"con Mañana/Tarde sin techo no sobra nadie — el motor absorbe el "
        f"excedente superando el máximo y avisando: {msg_blando!r}"
    )

    # Y el motor efectivamente la resuelve, que es la razón de ser del cambio.
    generar_semana_cpsat_con_cobertura_nocturna(
        staff, SEMANA, cfg, _estado_rotacion_inicial(staff, cfg), {}
    )


def test_validar_viabilidad_sigue_detectando_falta_de_personal():
    """La otra mitad: min_coverage es dura en todos los motores, así que
    'Personal insuficiente' debe seguir saltando aunque los máximos no cuenten
    como techo."""
    from modelo_base import validar_viabilidad

    cfg = _cfg_recepcion_estrecha()  # min 2+2+1+0 = 5/día -> 35 días-turno
    staff = [{"name": f"P{i}", "role": "normal"} for i in range(4)]  # 4x5 = 20

    viable, msg = validar_viabilidad(
        staff, cfg, turnos_sin_techo=TURNOS_MAX_COBERTURA_BLANDA
    )
    assert not viable and "Personal insuficiente" in msg, msg


# ═══════════════════════════════════════════════════════════════════════════
# 2. Descanso mínimo de 9h
# ═══════════════════════════════════════════════════════════════════════════

def test_transiciones_prohibidas_derivadas_coinciden_con_el_modelo():
    """La tabla derivada de los horarios configurados debe coincidir
    EXACTAMENTE con lo que bloquea la restricción #3 de _construir_modelo,
    que está escrita a mano. Es el ancla que impide que las dos definiciones
    de la misma regla se separen en silencio."""
    from herramientas import transiciones_prohibidas

    # Lo que la restricción #3 de _construir_modelo bloquea, literalmente.
    bloqueadas_por_el_modelo = {
        ("Tarde", "Mañana"),
        ("Noche", "Mañana"),
        ("Noche", "Tarde"),
        ("Noche", "Partido"),
    }
    assert transiciones_prohibidas() == bloqueadas_por_el_modelo


@pytest.mark.parametrize("partido", [
    [{"inicio": "10:00", "fin": "18:00"}],                                    # el real
    [{"inicio": "12:00", "fin": "20:00"}],                                    # variante rara
    [{"inicio": "10:00", "fin": "13:00"}, {"inicio": "17:00", "fin": "20:00"}],  # el antiguo partido
])
def test_transiciones_prohibidas_estables_para_las_variantes_de_partido(partido):
    """Las tres formas de Partido que han estado sobre la mesa dan el mismo
    conjunto de transiciones prohibidas, así que cambiar el horario de
    Partido no altera las restricciones del modelo."""
    from herramientas import transiciones_prohibidas, HORARIOS_TURNOS_DEFAULT

    horarios = dict(HORARIOS_TURNOS_DEFAULT, Partido=partido)
    assert transiciones_prohibidas(horarios) == {
        ("Tarde", "Mañana"), ("Noche", "Mañana"),
        ("Noche", "Tarde"), ("Noche", "Partido"),
    }


def test_el_modelo_nunca_asigna_tarde_ni_partido_tras_noche():
    """La regla de 9h en el propio solver: tras una Noche solo puede venir
    Noche o Libre. Tarde el día siguiente serían 8h de descanso y Partido
    solo 3h."""
    cfg = _cfg_recepcion_estrecha()
    staff = _staff_excedente()
    model, x, names, _ = _construir_modelo(staff, SEMANA, cfg)
    schedule = _resolver_y_extraer(model, x, names, staff, cfg)

    for nombre, turnos in schedule.items():
        for d in range(6):
            if turnos[d] == "Noche":
                assert turnos[d + 1] in ("Noche", "Libre"), (
                    f"{nombre}: {DAYS[d]} (Noche) → {DAYS[d+1]} ({turnos[d+1]}) "
                    f"deja menos de 9h de descanso"
                )


@pytest.mark.parametrize("siguiente,horas", [("Tarde", 8), ("Partido", 3)])
def test_advertencias_detectan_descanso_corto_tras_noche(siguiente, horas):
    """REGRESIÓN (revisión de esta sesión): app._calcular_advertencias se
    apoyaba en modelo_base.NO_MORNING_AFTER, que solo conoce las
    transiciones que TERMINAN en Mañana — así que una edición manual podía
    dejar Noche→Partido (3h de descanso) sin ninguna advertencia."""
    import app as app_mod

    cfg = _cfg_recepcion_estrecha()
    staff = [{"name": "Ana", "role": "normal"}, {"name": "Luis", "role": "normal"}]
    schedule = {
        "Ana":  ["Noche", siguiente, "Libre", "Libre", "Mañana", "Mañana", "Mañana"],
        "Luis": ["Mañana"] * 5 + ["Libre", "Libre"],
    }

    advertencias = app_mod._calcular_advertencias(schedule, staff, cfg)
    descanso = [a for a in advertencias if a["tipo"] == "descanso"]

    assert len(descanso) == 1, f"esperada 1 advertencia de descanso: {descanso}"
    assert "Ana" in descanso[0]["mensaje"]
    assert siguiente in descanso[0]["mensaje"]
    assert "9h" in descanso[0]["mensaje"]
    assert descanso[0]["celdas"] == [
        {"nombre": "Ana", "dia_idx": 0}, {"nombre": "Ana", "dia_idx": 1}
    ]


def test_advertencias_no_marcan_transiciones_con_descanso_suficiente():
    """No falsos positivos: Noche→Noche (16h), Tarde→Partido (11h) y
    Partido→Mañana (13h) están todas por encima de las 9h."""
    import app as app_mod

    cfg = _cfg_recepcion_estrecha()
    staff = [{"name": "Ana", "role": "normal"}]
    schedule = {
        "Ana": ["Noche", "Noche", "Libre", "Libre", "Tarde", "Partido", "Mañana"],
    }
    advertencias = app_mod._calcular_advertencias(schedule, staff, cfg)
    assert [a for a in advertencias if a["tipo"] == "descanso"] == []


# ═══════════════════════════════════════════════════════════════════════════
# 3. Continuidad: gratis entre días libres, penalizada entre días trabajados
# ═══════════════════════════════════════════════════════════════════════════

def _cfg_continuidad() -> DeptConfig:
    """Escenario deliberadamente PEQUEÑO y holgado: estos dos tests comparan
    el valor ÓPTIMO de dos modelos, así que el solver tiene que PROBAR
    optimalidad — con la plantilla grande de _staff_excedente() eso agota el
    límite de tiempo y la comparación dejaría de ser fiable (además de
    tardar un minuto por test)."""
    return DeptConfig(
        dept_id="recepcion", dept_label="Recepción",
        night_fixed_days_off=[], night_cover_compensation=[],
        min_coverage={"Mañana": 1, "Tarde": 1, "Noche": 0, "Partido": 0},
        max_coverage={"Mañana": 3, "Tarde": 3, "Noche": 1, "Partido": 1},
        no_night_roles=set(), fixed_roles={}, fixed_days_off={},
        cover_consecutive=True, night_rotation_key="recepcion",
        allow_non_consecutive_cover=False, prefer_next_week_for_cover=False,
    )


def _staff_continuidad() -> list:
    return [{"name": f"P{i}", "role": "normal"} for i in range(4)]


def _coste(model, x, fijaciones) -> float:
    """Valor óptimo del modelo tras fijar unas celdas concretas. Exige
    OPTIMAL (no FEASIBLE): comparar dos valores donde alguno fuera solo
    'la mejor solución encontrada hasta agotar el tiempo' no demostraría
    nada sobre el coste real de una preferencia."""
    from ortools.sat.python import cp_model
    for (nombre, dia, turno) in fijaciones:
        model.Add(x[nombre, dia, turno] == 1)
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    solver.parameters.max_time_in_seconds = 30.0
    status = solver.Solve(model)
    assert status == cp_model.OPTIMAL, (
        f"el escenario debe resolverse a optimalidad para que la "
        f"comparación de costes signifique algo (status={solver.StatusName(status)})"
    )
    return solver.ObjectiveValue()


def test_cambio_de_turno_a_traves_de_los_libres_no_se_penaliza():
    """DECISIÓN DE NEGOCIO confirmada con el usuario en la revisión: el
    bloque de días libres es exactamente donde SÍ se puede rotar de turno
    sin coste; lo que se penaliza es cambiar a mitad de semana. Este test
    fija esa decisión para que no se rompa sin querer.

    Se comparan dos horarios de la MISMA persona que solo difieren en el
    turno posterior a sus dos libres: mantenerlo o cambiarlo debe costar
    exactamente lo mismo."""
    cfg = _cfg_continuidad()
    staff = _staff_continuidad()

    # P0 libra Miércoles+Jueves. Antes: Mañana. Después: Mañana vs Tarde.
    comunes = [("P0", 0, "Mañana"), ("P0", 1, "Mañana"),
               ("P0", 2, "Libre"), ("P0", 3, "Libre")]

    m1, x1, _n1, _ = _construir_modelo(staff, SEMANA, cfg)
    igual = _coste(m1, x1, comunes + [("P0", 4, "Mañana")])

    m2, x2, _n2, _ = _construir_modelo(staff, SEMANA, cfg)
    distinto = _coste(m2, x2, comunes + [("P0", 4, "Tarde")])

    assert igual == distinto, (
        f"Cambiar de turno a través del bloque de libres debe ser gratis "
        f"(decisión de negocio): mantener Mañana={igual}, cambiar a "
        f"Tarde={distinto}"
    )


def test_cambio_de_turno_entre_dias_trabajados_si_se_penaliza():
    """La otra mitad de la misma decisión: entre dos días trabajados
    consecutivos, cambiar de turno cuesta AL MENOS
    PESO_ROTURAS_CONTINUIDAD.

    "Al menos" y no "exactamente": forzar el turno de un día concreto
    también reordena el resto del reparto, así que el óptimo alternativo
    arrastra otros términos además de B (aquí sale 80 = B(50) más 30 de
    efectos secundarios del reparto). Lo que este test tiene que demostrar
    es que la rotura de continuidad se cobra de verdad, no descomponer el
    objetivo entero."""
    cfg = _cfg_continuidad()
    staff = _staff_continuidad()

    m1, x1, _n1, _ = _construir_modelo(staff, SEMANA, cfg)
    igual = _coste(m1, x1, [("P0", 0, "Mañana"), ("P0", 1, "Mañana")])

    m2, x2, _n2, _ = _construir_modelo(staff, SEMANA, cfg)
    distinto = _coste(m2, x2, [("P0", 0, "Mañana"), ("P0", 1, "Tarde")])

    assert round(distinto - igual) >= PESO_ROTURAS_CONTINUIDAD, (
        f"Cambiar de turno entre dos días trabajados debe costar al menos "
        f"PESO_ROTURAS_CONTINUIDAD ({PESO_ROTURAS_CONTINUIDAD}): "
        f"mantener={igual}, cambiar={distinto}"
    )


def _delta_cambiar_el_lunes(prev_sunday_shifts):
    """Coste de cambiar de turno el lunes (Mañana -> Tarde) bajo un
    prev_sunday_shifts dado. Devuelve la diferencia cambiar - mantener."""
    cfg = _cfg_continuidad()
    staff = _staff_continuidad()
    m1, x1, _n, _ = _construir_modelo(staff, SEMANA, cfg,
                                      prev_sunday_shifts=prev_sunday_shifts)
    mantener = _coste(m1, x1, [("P0", 0, "Mañana")])
    m2, x2, _n2, _ = _construir_modelo(staff, SEMANA, cfg,
                                       prev_sunday_shifts=prev_sunday_shifts)
    cambiar = _coste(m2, x2, [("P0", 0, "Tarde")])
    return cambiar - mantener


def test_continuidad_cruza_la_frontera_de_semana():
    """REGRESIÓN (revisión de esta sesión): el bucle del término B empieza en
    d=1, así que comparaba 0-1, 1-2 … 5-6 pero NUNCA el domingo de la semana
    anterior con el lunes de esta. Medido sobre la plantilla real antes del
    arreglo: 100% de continuidad dentro de la semana frente a 42% en el cruce.

    Se aísla el efecto comparando el MISMO cambio (lunes Mañana -> Tarde) con
    y sin domingo previo trabajado: la diferencia entre ambas diferencias es
    exactamente el término de cruce."""
    delta_con_domingo = _delta_cambiar_el_lunes({"P0": "Mañana"})
    delta_sin_domingo = _delta_cambiar_el_lunes({})

    assert round(delta_con_domingo - delta_sin_domingo) == PESO_ROTURAS_CONTINUIDAD, (
        f"Cambiar de turno respecto al domingo anterior debe costar "
        f"exactamente PESO_ROTURAS_CONTINUIDAD ({PESO_ROTURAS_CONTINUIDAD}) "
        f"más: con domingo trabajado delta={delta_con_domingo}, "
        f"sin domingo delta={delta_sin_domingo}"
    )


def test_el_cruce_de_semana_es_gratis_si_el_domingo_anterior_fue_libre():
    """La otra mitad de la decisión de negocio: volver de los días libres con
    un turno distinto sigue siendo GRATIS, también cuando esos libres caen al
    final de la semana anterior. Si el domingo previo fue Libre, el término de
    cruce ni se añade al objetivo.

    Noche no se prueba aquí porque no tiene elección que medir: tras una Noche
    el lunes solo puede ser Noche o Libre (regla de descanso de 9h,
    restricción dura), así que el cambio a Tarde que usa este test no existe
    como alternativa. Noche tampoco participa nunca en continuidad."""
    delta_sin_domingo = _delta_cambiar_el_lunes({})
    delta_libre = _delta_cambiar_el_lunes({"P0": "Libre"})
    assert round(delta_libre - delta_sin_domingo) == 0, (
        f"con Domingo=Libre el cruce no debe penalizarse: "
        f"delta={delta_libre} vs sin domingo={delta_sin_domingo}"
    )


def test_racha_de_dias_trabajados_no_cruza_de_6_entre_semanas():
    """REGRESIÓN (revisión de esta sesión): el patrón de 2 libres es POR
    SEMANA, así que librar Lun+Mar una semana y Sáb+Dom la siguiente encadena
    10 días trabajados sin romper ninguna regla dura. Medido sobre la
    plantilla real en 4 semanas encadenadas: Maira 9 días seguidos, Carmen 8.

    Se aísla el término comparando el coste de trabajar el lunes con y sin
    arrastre de 6 días de la semana anterior."""
    from motor_cpsat import PESO_RACHA_TRABAJO, MAX_DIAS_TRABAJADOS_SEGUIDOS

    cfg = _cfg_continuidad()
    staff = _staff_continuidad()

    def delta_trabajar_el_lunes(previos):
        m1, x1, _n, _ = _construir_modelo(staff, SEMANA, cfg,
                                          dias_trabajados_previos=previos)
        trabajando = _coste(m1, x1, [("P0", 0, "Mañana")])
        m2, x2, _n2, _ = _construir_modelo(staff, SEMANA, cfg,
                                           dias_trabajados_previos=previos)
        librando = _coste(m2, x2, [("P0", 0, "Libre")])
        return trabajando - librando

    con_arrastre = delta_trabajar_el_lunes({"P0": MAX_DIAS_TRABAJADOS_SEGUIDOS})
    sin_arrastre = delta_trabajar_el_lunes({})

    # "Al menos" y no "exactamente": forzar el lunes de una persona reordena
    # el resto del reparto, así que el óptimo alternativo arrastra otros
    # términos además de M (aquí sale 330 = M(300) + 30 de efectos
    # secundarios). Lo que este test demuestra es que la racha se cobra de
    # verdad y con la magnitud esperada, no descomponer el objetivo entero.
    assert round(con_arrastre - sin_arrastre) >= PESO_RACHA_TRABAJO, (
        f"Trabajar el día {MAX_DIAS_TRABAJADOS_SEGUIDOS + 1} seguido debe "
        f"costar al menos PESO_RACHA_TRABAJO ({PESO_RACHA_TRABAJO}) más: "
        f"con arrastre={con_arrastre}, sin arrastre={sin_arrastre}"
    )


def test_racha_de_trabajo_es_blanda_no_vuelve_infactible():
    """El término M es una preferencia, no una restricción dura: con un
    arrastre que ya excede el tope (7 días trabajados al cierre de la semana
    anterior, algo que el motor no puede deshacer) el modelo debe seguir
    generando en vez de reportar INFEASIBLE."""
    cfg = _cfg_continuidad()
    staff = _staff_continuidad()
    previos = {p["name"]: 7 for p in staff}

    model, x, names, _ = _construir_modelo(staff, SEMANA, cfg,
                                           dias_trabajados_previos=previos)
    schedule = _resolver_y_extraer(model, x, names, staff, cfg)
    assert all(len(t) == 7 for t in schedule.values())


# ═══════════════════════════════════════════════════════════════════════════
# 4. Patrón de libres — nadie acaba con la semana entera libre
# ═══════════════════════════════════════════════════════════════════════════

def test_mas_peticiones_que_el_patron_no_deja_los_libres_sin_tope():
    """REGRESIÓN (revisión de esta sesión): con MÁS peticiones concedidas que
    DIAS_LIBRES_SEGUIDOS_POR_SEMANA, la restricción #7 hacía `continue` sin
    imponer ninguna cota, así que el solver podía dar Libre también al resto
    de la semana — el mismo fallo silencioso ("7 libres / 0 días
    trabajados") que esa restricción existe para evitar."""
    cfg = _cfg_recepcion_estrecha()
    staff = _staff_excedente()
    # Emp3 tiene 3 peticiones concedidas: una más que el patrón admite.
    for p in staff:
        if p["name"] == "Emp3":
            p["forced_days_off"] = ["Lunes", "Miércoles", "Viernes"]

    model, x, names, _ = _construir_modelo(staff, SEMANA, cfg)
    schedule = _resolver_y_extraer(model, x, names, staff, cfg)

    turnos = schedule["Emp3"]
    libres = [d for d, t in enumerate(turnos) if t == "Libre"]
    assert libres == [0, 2, 4], (
        f"Emp3 debía librar exactamente sus 3 días de petición y trabajar "
        f"los otros 4, sin libres adicionales: {turnos}"
    )
