"""
tests/test_cobertura_nocturna_orden_fijo.py — Valida cobertura_nocturna.py:
selección de quién cubre las noches por orden fijo, con balance histórico
como respaldo.

No confundir con tests/test_cobertura_nocturna.py, que prueba el mecanismo
de bloques rotatorios del motor heurístico (modelo_base._plan_noches_bloques).

No usa herramientas/DATA_DIR: todo el estado vive en el dict estado_rotacion
que pasa el propio test, no hay lectura de disco aquí.
"""

import pytest

from modelo_base import DeptConfig, DAYS
from cobertura_nocturna import (
    seleccionar_candidato_cobertura_nocturna,
    avanzar_semana_ausencia_night_fixed,
    incorporar_empleado_orden,
    dar_de_baja_empleado_orden,
)


def _cfg(**overrides) -> DeptConfig:
    base = dict(
        dept_id="test", dept_label="Test",
        night_fixed_days_off=[3, 4],          # Jueves, Viernes
        night_cover_compensation=[5, 6],
        min_coverage={}, max_coverage={},
        no_night_roles={"jefe", "subjefe"},
        fixed_roles={}, fixed_days_off={},
        cover_consecutive=True, night_rotation_key="test",
        allow_non_consecutive_cover=False, prefer_next_week_for_cover=False,
    )
    base.update(overrides)
    return DeptConfig(**base)


def _staff(nombres, night_fixed="NocheFijo", **forced_por_nombre):
    staff = [{"name": "NocheFijo", "role": "night_fixed"}] if night_fixed else []
    for n in nombres:
        p = {"name": n, "role": "normal"}
        if n in forced_por_nombre:
            p["forced_days_off"] = forced_por_nombre[n]
        staff.append(p)
    return staff


def _estado_base(orden, siguiente=None, deudas=None, usar_orden_fijo=True, balance=None):
    return {
        "orden": list(orden),
        "siguiente_turno": siguiente if siguiente is not None else orden[0],
        "deudas": dict(deudas or {}),
        "usar_orden_fijo": usar_orden_fijo,
        "historico_balance": dict(balance or {}),
        "ausencias_night_fixed_consecutivas": 0,
    }


# ─── Orden fijo, sin bloqueos ────────────────────────────────────────────

def test_orden_fijo_rotacion_determinista_sin_bloqueos():
    orden = ["N1", "N2", "N3", "N4", "N5"]
    cfg = _cfg()
    staff = _staff(orden)
    estado = _estado_base(orden)

    elegidos = []
    for _ in range(7):  # más de una vuelta completa (5) al orden
        elegido, estado = seleccionar_candidato_cobertura_nocturna(staff, cfg, estado)
        elegidos.append(elegido)

    assert elegidos == ["N1", "N2", "N3", "N4", "N5", "N1", "N2"]


# ─── Bloqueo puntual (petición/vacación) ─────────────────────────────────

def test_bloqueado_una_semana_se_salta_y_queda_con_deuda():
    orden = ["N1", "N2", "N3", "N4", "N5"]
    cfg = _cfg()
    # N2 (siguiente_turno) tiene vacación que cubre Jueves+Viernes esta semana.
    staff = _staff(orden, N2=["Jueves", "Viernes"])
    estado = _estado_base(orden, siguiente="N2")

    elegido, estado2 = seleccionar_candidato_cobertura_nocturna(staff, cfg, estado)

    assert elegido == "N3", "N2 estaba bloqueado; debe saltarse a N3"
    assert estado2["deudas"].get("N2") == 1
    assert estado2["deudas"].get("N3", 0) == 0
    assert estado2["siguiente_turno"] == "N4", (
        "siguiente_turno debe pasar a quien sigue al ELEGIDO (N3) en el orden, "
        "no a quien sigue al bloqueado"
    )


# ─── Deuda pendiente se cobra antes que el turno normal ──────────────────

def test_semana_siguiente_a_deuda_cobra_antes_que_siguiente_turno_normal():
    orden = ["N1", "N2", "N3", "N4", "N5"]
    cfg = _cfg()
    # N2 ya no está bloqueado esta semana, pero arrastra deuda=1 de la anterior.
    staff = _staff(orden)
    estado = _estado_base(orden, siguiente="N4", deudas={"N2": 1})

    elegido, estado2 = seleccionar_candidato_cobertura_nocturna(staff, cfg, estado)

    assert elegido == "N2", (
        "N2 tiene deuda pendiente: debe cubrir esta semana aunque 'siguiente_turno' "
        "normal fuera N4"
    )
    assert estado2["deudas"]["N2"] == 0
    assert estado2["siguiente_turno"] == "N3", "el siguiente tras el ELEGIDO (N2) es N3"


def test_dos_personas_con_deuda_desempata_por_orden_de_aparicion():
    orden = ["N1", "N2", "N3", "N4", "N5"]
    cfg = _cfg()
    staff = _staff(orden)
    # N2 y N4 tienen la MISMA deuda máxima (2); N2 aparece antes en "orden".
    estado = _estado_base(orden, siguiente="N1", deudas={"N2": 2, "N4": 2})

    elegido, estado2 = seleccionar_candidato_cobertura_nocturna(staff, cfg, estado)

    assert elegido == "N2", "empate de deuda máxima: gana quien aparece antes en 'orden'"
    assert estado2["deudas"]["N2"] == 1
    assert estado2["deudas"]["N4"] == 2, "la deuda de N4 no se toca esta semana"


# ─── usar_orden_fijo=False: balance puro, orden/siguiente/deudas ignorados ──

@pytest.mark.parametrize("balance", [
    {"N1": {"covers": 3, "pending": 0}, "N2": {"covers": 1, "pending": 0}, "N3": {"covers": 2, "pending": 0}},
    {"N1": {"covers": 0, "pending": 0}, "N2": {"covers": 0, "pending": 1}, "N3": {"covers": 0, "pending": 0}},
    {},  # sin histórico: todos empatan en 0, desempate por nombre
])
def test_usar_orden_fijo_false_coincide_con_balance_puro(balance):
    orden = ["N3", "N2", "N1"]  # orden deliberadamente distinto al que ganaría por balance
    cfg = _cfg()
    staff = _staff(["N1", "N2", "N3"])
    # deudas/siguiente_turno apuntan a alguien que NO debería ganar si el modo
    # de balance puro está realmente ignorándolos.
    estado = _estado_base(
        orden, siguiente="N3", deudas={"N3": 99}, usar_orden_fijo=False, balance=balance
    )

    elegido, _ = seleccionar_candidato_cobertura_nocturna(staff, cfg, estado)

    # Réplica manual del criterio de balance puro (covers - pending, nombre).
    def sort_key(nombre):
        info = balance.get(nombre, {})
        return (info.get("covers", 0) - info.get("pending", 0), nombre)

    esperado = min(["N1", "N2", "N3"], key=sort_key)
    assert elegido == esperado


# ─── Modo respaldo: ausencia larga (>=2 semanas consecutivas) ───────────

def test_modo_respaldo_por_ausencia_larga_preserva_orden_y_siguiente_turno():
    orden = ["N1", "N2", "N3", "N4", "N5"]
    cfg = _cfg()
    estado = _estado_base(
        orden, siguiente="N3",
        balance={"N1": {"covers": 5, "pending": 0}, "N2": {"covers": 0, "pending": 0}},
    )

    staff_ausente = [
        {"name": "NocheFijo", "role": "night_fixed", "forced_days_off": list(DAYS)}
    ] + [{"name": n, "role": "normal"} for n in orden]

    # Semana 1: night_fixed de vacaciones toda la semana (1ª semana consecutiva).
    estado = avanzar_semana_ausencia_night_fixed(staff_ausente, estado)
    assert estado["ausencias_night_fixed_consecutivas"] == 1
    # Con 1 sola semana de ausencia (todavía no dispara el modo respaldo),
    # el modo normal sigue activo: cubre según orden fijo.
    elegido1, estado = seleccionar_candidato_cobertura_nocturna(staff_ausente, cfg, estado)
    assert elegido1 == "N3"  # siguiente_turno normal
    assert estado["siguiente_turno"] == "N4"

    # Semana 2: night_fixed sigue de vacaciones -> 2ª semana consecutiva -> respaldo.
    estado_antes_respaldo = dict(estado)
    estado = avanzar_semana_ausencia_night_fixed(staff_ausente, estado)
    assert estado["ausencias_night_fixed_consecutivas"] == 2

    # (requisito de negocio: el orden fijo
    # se aplica también en modo respaldo, "más allá de si existe el rol de
    # noche fijo o no". Antes, el modo respaldo caía en balance puro y aquí
    # se esperaba N2, el de menor covers-pending; ahora el ciclo NO se
    # interrumpe y sigue por siguiente_turno.)
    elegido2, estado = seleccionar_candidato_cobertura_nocturna(staff_ausente, cfg, estado)
    assert elegido2 == "N4", (
        "en modo respaldo el ciclo fijo continúa por siguiente_turno (N4), "
        "no se cambia a balance puro"
    )
    assert estado["orden"] == orden, "el orden fijo nunca se reordena solo"
    assert estado["siguiente_turno"] == "N5", (
        "el ciclo avanza también en modo respaldo — antes se congelaba"
    )
    assert estado_antes_respaldo["siguiente_turno"] == "N4"

    # Semana 3: night_fixed vuelve. El ciclo sigue exactamente donde estaba,
    # sin saltos ni repeticiones por el cambio de modo.
    staff_presente = [{"name": "NocheFijo", "role": "night_fixed"}] + \
                      [{"name": n, "role": "normal"} for n in orden]
    estado = avanzar_semana_ausencia_night_fixed(staff_presente, estado)
    assert estado["ausencias_night_fixed_consecutivas"] == 0

    elegido3, estado = seleccionar_candidato_cobertura_nocturna(staff_presente, cfg, estado)
    assert elegido3 == "N5", (
        "el ciclo es continuo a través del cambio de modo: N3 -> N4 -> N5"
    )


# ─── Modo respaldo: rol night_fixed inexistente ──────────────────────────

def test_modo_respaldo_por_rol_inexistente_es_permanente():
    orden = ["N1", "N2", "N3"]
    cfg = _cfg()
    staff_sin_night_fixed = [{"name": n, "role": "normal"} for n in orden]
    estado = _estado_base(
        orden, siguiente="N1",
        balance={"N1": {"covers": 0, "pending": 0}, "N2": {"covers": 5, "pending": 0},
                 "N3": {"covers": 9, "pending": 0}},
    )

    # Sin llamar a avanzar_semana_ausencia_night_fixed en absoluto (no hace
    # falta: el disparador es "no existe el rol", no un contador de semanas).
    #
    # (requisito de negocio.) Este es el
    # caso de la plantilla REAL de Recepción: no hay night_fixed, así que el
    # modo respaldo está permanentemente activo. Antes eso significaba balance
    # puro siempre y el orden fijo era código muerto en producción; ahora el
    # ciclo fijo rota N1 -> N2 -> N3 -> N1 y es lo que se ve en el horario.
    elegidos = []
    for _ in range(4):
        elegido, estado = seleccionar_candidato_cobertura_nocturna(staff_sin_night_fixed, cfg, estado)
        elegidos.append(elegido)
        assert estado["orden"] == orden, "el orden fijo nunca se reordena solo"

    assert elegidos == ["N1", "N2", "N3", "N1"], (
        f"sin night_fixed el ciclo fijo debe rotar y volver a empezar, no "
        f"repetir siempre al de menor covers: {elegidos}"
    )


# ─── Incorporación y baja de empleados ───────────────────────────────────

def test_incorporar_empleado_se_anade_al_final_sin_tocar_siguiente_turno():
    estado = _estado_base(["N1", "N2", "N3"], siguiente="N2")
    estado2 = incorporar_empleado_orden(estado, "N4")

    assert estado2["orden"] == ["N1", "N2", "N3", "N4"]
    assert estado2["siguiente_turno"] == "N2"


def test_baja_de_empleado_que_no_era_siguiente_turno():
    estado = _estado_base(["N1", "N2", "N3", "N4"], siguiente="N2", deudas={"N3": 2})
    estado2 = dar_de_baja_empleado_orden(estado, "N3")

    assert estado2["orden"] == ["N1", "N2", "N4"]
    assert estado2["siguiente_turno"] == "N2", "no era el que se fue: no cambia"
    assert "N3" not in estado2["deudas"]


def test_baja_del_siguiente_turno_reasigna_al_inmediato_siguiente():
    estado = _estado_base(["N1", "N2", "N3", "N4"], siguiente="N2")
    estado2 = dar_de_baja_empleado_orden(estado, "N2")

    assert estado2["orden"] == ["N1", "N3", "N4"]
    assert estado2["siguiente_turno"] == "N3", "quien estaba justo después de N2"


def test_baja_del_siguiente_turno_cuando_era_el_ultimo_pasa_al_primero():
    estado = _estado_base(["N1", "N2", "N3", "N4"], siguiente="N4")
    estado2 = dar_de_baja_empleado_orden(estado, "N4")

    assert estado2["orden"] == ["N1", "N2", "N3"]
    assert estado2["siguiente_turno"] == "N1", "N4 era el último: el nuevo siguiente es el primero"


# ─── Bug real detectado en Fase 5 (ver docs/INFORME_FASE_5.md, Paso 3) ─────
#
# En modo respaldo (sin night_fixed en plantilla, o >=2 semanas ausente),
# quien orquesta la generación (motor_cpsat.generar_semana_cpsat_con_
# cobertura_nocturna) llama a seleccionar_candidato_cobertura_nocturna UNA
# VEZ POR CADA DÍA de la semana, para decidir quién cubre la Noche de ESE
# día concreto. Pero _elegibles/_bloqueados_por_peticion_o_vacacion solo
# excluyen a quien tiene forced_days_off en cfg.night_fixed_days_off (un
# par FIJO, p.ej. Jueves+Viernes) — NUNCA comprueban si la persona tiene
# libre el día ESPECÍFICO que se está cubriendo en esa llamada, porque ni
# esta función ni _elegibles reciben ese día como parámetro.
#
# Consecuencia verificada en motor_cpsat.py: puede seleccionar a alguien
# para cubrir la Noche de un día en el que esa misma persona ya tiene
# forced_days_off (vacación/petición concedida) — produciendo, dentro de
# _construir_modelo, una contradicción directa entre la restricción dura
# #6 (Libre forzado ese día) y la celda de cobertura externa fijada a
# Noche == 1 por el llamador. Motor_cpsat.py responde con
# RuntimeError("... status=INFEASIBLE"), no por falta real de personal,
# sino por este bug de elegibilidad.
#
# CORREGIDO (Fase 5, cierre del bug bloqueante): seleccionar_candidato_
# cobertura_nocturna ahora acepta dias_cubrir={d} (ver su docstring y el
# docstring del módulo) — el llamador de modo respaldo (motor_cpsat.py)
# ya lo pasa en cada una de sus 7 llamadas. Este test deja de ser xfail:
# ya no basta con "no lanza excepción", se confirma con aserciones
# explícitas que (a) Ana NO es elegida el día que tiene bloqueado, y (b)
# alguien más SÍ lo es (la cobertura de esa noche no queda sin cubrir).
def test_modo_respaldo_no_debe_asignar_a_quien_tiene_ese_dia_forzado_libre():
    """Modo respaldo (sin night_fixed): Ana tiene Lunes forzado libre
    (vacación/petición concedida). Al pedir cobertura día a día para toda
    la semana, pasando dias_cubrir={d} en cada llamada (como hace
    motor_cpsat.py), la llamada del Lunes NO debe devolver a Ana — y debe
    devolver a otra persona elegible de verdad (Diego o Miguel), no
    limitarse a "no fallar"."""
    orden = ["Ana", "Diego", "Miguel"]
    # Ana tiene el LUNES forzado libre. Con este orden/estado (verificado
    # empíricamente antes de escribir el test, no asumido): la rotación en
    # modo respaldo (balance puro, covers=0 para todos al principio,
    # desempate alfabético) asignaba a Ana el Lunes, Jueves y Domingo antes
    # del arreglo — el Lunes es justo el día que Ana tiene bloqueado, así
    # que el escenario SÍ fuerza el conflicto (con otro día se corre el
    # riesgo de que "pase" por simple coincidencia de fase de rotación, no
    # porque el bug no exista — el mismo patrón de falso negativo que ya
    # se detectó una vez con los tests de "domingo suelto").
    staff = _staff(orden, night_fixed=None, Ana=["Lunes"])
    cfg = _cfg()
    estado = _estado_base(orden)

    dia_indice_lunes = DAYS.index("Lunes")
    for d in range(7):
        elegido, estado = seleccionar_candidato_cobertura_nocturna(
            staff, cfg, estado, dias_cubrir={d}
        )
        if d == dia_indice_lunes:
            assert elegido != "Ana", (
                f"seleccionar_candidato_cobertura_nocturna asignó a Ana la "
                f"cobertura nocturna del Lunes, pese a que Ana tiene ese "
                f"día forzado Libre (forced_days_off=['Lunes']) y a que se "
                f"pasó dias_cubrir={{d}} explícitamente. Elegido: {elegido!r}"
            )
            assert elegido in ("Diego", "Miguel"), (
                f"El Lunes debía cubrirlo alguien REALMENTE elegible ese "
                f"día (Diego o Miguel, ninguno bloqueado) — no basta con "
                f"que no sea Ana, tiene que ser una asignación real. "
                f"Elegido: {elegido!r}"
            )


def test_modo_respaldo_infactibilidad_real_cuando_todos_bloqueados_ese_dia():
    """Distingue el caso corregido (INFEASIBLE espurio, ver test anterior)
    del caso de infactibilidad REAL: si TODOS los candidatos elegibles
    tienen el mismo día concreto forzado Libre, no hay nadie que pueda
    cubrir esa noche — seleccionar_candidato_cobertura_nocturna debe
    seguir lanzando RuntimeError, y debe hacerlo por el motivo correcto
    ("nadie elegible", no una excepción distinta o un valor inventado).

    Tras el arreglo, un RuntimeError de esta función SIEMPRE significa
    "no hay solución real" — antes del arreglo, el INFEASIBLE de
    motor_cpsat.py para el escenario del test anterior también parecía
    "sin solución real" pero no lo era (bug de elegibilidad, no falta de
    personal). Este test confirma que ese otro caso, el genuinamente sin
    solución, se sigue reportando explícitamente."""
    orden = ["Ana", "Diego", "Miguel"]
    dia_indice_lunes = DAYS.index("Lunes")
    # Los 3 candidatos elegibles tienen el LUNES forzado libre (vacación
    # concedida a los 3 a la vez, o coincidencia de peticiones) — nadie
    # puede cubrir la noche de ese día.
    staff = _staff(orden, night_fixed=None, Ana=["Lunes"], Diego=["Lunes"], Miguel=["Lunes"])
    cfg = _cfg()
    estado = _estado_base(orden)

    with pytest.raises(RuntimeError, match="[Nn]adie.*elegible"):
        seleccionar_candidato_cobertura_nocturna(
            staff, cfg, estado, dias_cubrir={dia_indice_lunes}
        )


# ─── Tema 1 (sesión Fase 5, cierre de pendientes): mismo tipo de bug que ───
# ─── dias_cubrir, pero para el descanso obligatorio post-racha de noches ───
#
# Verificación: la elegibilidad de seleccionar_candidato_cobertura_nocturna
# no sabía nada, por sí sola, de que alguien pudiera estar en su descanso
# obligatorio de 2 días tras agotar una racha de 5 noches (restricción de
# Fase 3, agregar_restriccion_racha_noches en motor_cpsat.py) — esa
# restricción se añade al modelo DESPUÉS de que este módulo ya fijó
# cobertura_externa (Noche=1) para los 7 días de la semana en modo
# respaldo. Arreglo con el MISMO patrón que dias_cubrir: nuevo parámetro
# bloqueados_por_racha, un conjunto de nombres a excluir para el día que
# decide esta llamada concreta — quien orquesta (motor_cpsat.py) es quien
# calcula, día a día y de forma determinista en Python, quién está en ese
# descanso (ver _racha_inicial_por_nombre y el bucle de modo respaldo en
# generar_semana_cpsat_con_cobertura_nocturna).

def test_bloqueados_por_racha_se_salta_en_modo_respaldo():
    """Con Ana pasada explícitamente en bloqueados_por_racha (simulando que
    el llamador ya detectó que Ana está en su descanso obligatorio
    post-racha ese día), la función debe saltarla igual que hace con
    quien tiene el día forzado libre por vacación/petición, y devolver a
    otra persona realmente elegible."""
    orden = ["Ana", "Diego", "Miguel"]
    staff = _staff(orden, night_fixed=None)
    cfg = _cfg()
    estado = _estado_base(orden)

    elegido, estado = seleccionar_candidato_cobertura_nocturna(
        staff, cfg, estado, dias_cubrir={0}, bloqueados_por_racha={"Ana"}
    )

    assert elegido != "Ana", (
        f"Ana estaba en bloqueados_por_racha (descanso obligatorio "
        f"post-racha); no debía ser elegida. Elegido: {elegido!r}"
    )
    assert elegido in ("Diego", "Miguel"), (
        f"El día debía cubrirlo alguien realmente elegible (Diego o "
        f"Miguel), no basta con que no sea Ana. Elegido: {elegido!r}"
    )


def test_bloqueados_por_racha_infactibilidad_real_cuando_todos_bloqueados():
    """Si TODOS los candidatos elegibles están en descanso obligatorio
    post-racha el mismo día (caso extremo pero posible con plantilla
    pequeña), sigue sin haber nadie que pueda cubrir esa noche —
    RuntimeError con el mismo mensaje que el resto de casos de
    infactibilidad real de esta función, no un valor inventado."""
    orden = ["Ana", "Diego", "Miguel"]
    staff = _staff(orden, night_fixed=None)
    cfg = _cfg()
    estado = _estado_base(orden)

    with pytest.raises(RuntimeError, match="[Nn]adie.*elegible"):
        seleccionar_candidato_cobertura_nocturna(
            staff, cfg, estado, dias_cubrir={0},
            bloqueados_por_racha={"Ana", "Diego", "Miguel"},
        )


# ─── Reconciliación de 'orden' con la plantilla ────────────

def test_reconciliar_orden_incorpora_nuevos_al_final_sin_recuperar_atrasos():
    """REGRESIÓN: incorporar_empleado_orden/dar_de_baja_empleado_orden
    existían desde el principio pero NADIE las llamaba fuera de sus tests, así
    que 'orden' se quedaba obsoleto en cuanto cambiaba la plantilla. Con el
    orden fijo activo también en modo respaldo eso importa: _elegir_orden_fijo
    solo mira dentro de 'orden', así que un empleado ausente de la lista nunca
    haría noches.

    El alta entra al FINAL del ciclo y sin recuperar atrasos (confirmado con
    el usuario): un fichaje nuevo con covers=0 no debe encadenar decenas de
    noches para 'ponerse al día' — que es justo lo que hacía el balance puro.
    """
    from cobertura_nocturna import reconciliar_orden

    cfg = _cfg()
    staff = [{"name": n, "role": "normal"} for n in ("N1", "N2", "N3", "Nuevo")]
    estado = _estado_base(
        ["N1", "N2", "N3"], siguiente="N1",
        balance={"N1": {"covers": 30, "pending": 0}, "N2": {"covers": 30, "pending": 0},
                 "N3": {"covers": 25, "pending": 0}},
    )

    estado = reconciliar_orden(estado, staff, cfg)
    assert estado["orden"] == ["N1", "N2", "N3", "Nuevo"]

    elegidos = []
    for _ in range(8):
        elegido, estado = seleccionar_candidato_cobertura_nocturna(staff, cfg, estado)
        elegidos.append(elegido)
    assert elegidos == ["N1", "N2", "N3", "Nuevo"] * 2, (
        f"el nuevo espera su turno como todos: {elegidos}"
    )


def test_reconciliar_orden_da_de_baja_a_quien_ya_no_esta_en_plantilla():
    from cobertura_nocturna import reconciliar_orden

    cfg = _cfg()
    staff = [{"name": n, "role": "normal"} for n in ("N1", "N3")]
    estado = _estado_base(["N1", "N2", "N3"], siguiente="N2")

    estado = reconciliar_orden(estado, staff, cfg)
    assert estado["orden"] == ["N1", "N3"]
    assert estado["siguiente_turno"] in ("N1", "N3"), (
        "siguiente_turno apuntaba al que se dio de baja; debe recolocarse"
    )


def test_reconciliar_orden_excluye_roles_sin_noches_y_night_fixed():
    """Mismo criterio de elegibilidad que _elegibles y que
    app._estado_rotacion_inicial_cpsat: 'empleados capaces de cubrir noches'."""
    from cobertura_nocturna import reconciliar_orden

    cfg = _cfg()  # no_night_roles incluye jefe/subjefe/adjunto_recepcion
    staff = [
        {"name": "NocheFijo", "role": "night_fixed"},
        {"name": "Jefe", "role": "jefe"},
        {"name": "Sub", "role": "subjefe"},
        {"name": "N1", "role": "normal"},
        {"name": "N2", "role": "normal"},
    ]
    # Estado virgen (primera generación del departamento): 'orden' vacío.
    estado_virgen = {
        "orden": [], "siguiente_turno": None, "deudas": {},
        "usar_orden_fijo": True, "historico_balance": {},
        "ausencias_night_fixed_consecutivas": 0,
    }
    estado = reconciliar_orden(estado_virgen, staff, cfg)
    assert estado["orden"] == ["N1", "N2"]
    assert estado["siguiente_turno"] == "N1", (
        "con 'orden' vacío, siguiente_turno debe quedar apuntando al ciclo "
        "recién construido — si no, _elegir_orden_fijo empezaría por el 0 "
        "igualmente pero el estado quedaría incoherente en disco"
    )
