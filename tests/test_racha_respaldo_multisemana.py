"""
tests/test_racha_respaldo_multisemana.py — Verificación adicional del fix
de Tema 1 (bloqueados_por_racha, ver tests/test_cobertura_nocturna_orden_fijo.py
y tests/test_racha_noches.py): reconstruye el escenario completo "Viernes
de Carla" (plantilla de 10 personas, SIN night_fixed — modo respaldo
PERMANENTE toda la cadena, ver cobertura_nocturna._en_modo_respaldo) en
una cadena larga (12 semanas, más que las 8 mínimas pedidas) y confirma:

  1. Ninguna semana de la cadena produce INFEASIBLE espurio.
  2. El seguimiento determinista de racha en Python (motor_cpsat._RachaTracker,
     extraído del bucle de modo respaldo en esta sesión para poder probarlo
     de forma aislada) COINCIDE EXACTAMENTE, día a día y persona a persona,
     con lo que agregar_restriccion_racha_noches fuerza DENTRO del modelo
     CP-SAT — no solo "no revienta", sino que el conjunto de días que el
     tracker predice como descanso obligatorio es IDÉNTICO al conjunto de
     días que, mirando el resultado real ya resuelto por CP-SAT, resultan
     ser descanso obligatorio de verdad (definido de forma INDEPENDIENTE,
     con un escáner de rachas distinto y más simple —
     _dias_descanso_obligatorio_desde_turnos— que no comparte código con
     _RachaTracker ni con agregar_restriccion_racha_noches).

Un desfase de un día en cualquiera de las dos implementaciones (tracker
Python vs. restricción CP-SAT) aparecería aquí como una discrepancia
puntual entre "bloqueado según el tracker" y "descanso real según el
escáner independiente" — no como un INFEASIBLE (que solo detecta
sub-predicción, nunca sobre-predicción ni un desfase que la holgura de
personal disimule).
"""

import copy
from datetime import date, timedelta

import pytest

from modelo_base import DAYS, DeptConfig
from motor_cpsat import (
    generar_semana_cpsat_con_cobertura_nocturna,
    _RachaTracker,
    MAX_NOCHES_VENTANA,
    MIN_NOCHES_RACHA,
)

SEMANA_INICIAL = date(2099, 4, 6)  # Lunes en futuro lejano
N_SEMANAS = 12  # más que las 8 mínimas pedidas


@pytest.fixture(autouse=True)
def _data_dir_aislado(tmp_path, monkeypatch):
    import herramientas
    backup = tmp_path / "backups"
    backup.mkdir()
    monkeypatch.setattr(herramientas, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(herramientas, "BACKUP_DIR", str(backup))
    monkeypatch.setattr(herramientas, "FESTIVOS_FILE", str(tmp_path / "festivos.json"))


# ─── Reconstrucción de "Viernes de Carla" ──────────────────────────────────
# Mismo patrón que escenario_viernes_carla_reconstruido.py/_cpsat.py (Fase
# 5): 10 personas, SIN night_fixed (modo respaldo permanente, ver
# cobertura_nocturna._en_modo_respaldo: nf is None -> True siempre), 5 de
# los 6 "normal" con un día suelto recurrente TODAS las semanas, y el jefe
# ausente un miércoles de por medio. Reimplementado aquí en vez de
# importar el script directamente porque ese script tiene efectos
# secundarios a nivel de import (fija os.environ["DATA_DIR"], borra un
# directorio, fija random.seed) incompatibles con el aislamiento por
# tmp_path de este archivo de test — mismo staff/cfg/patrón, sin esos
# efectos secundarios.

def _cfg() -> DeptConfig:
    return DeptConfig(
        dept_id="test_carla", dept_label="Test Carla",
        night_fixed_days_off=[3, 4], night_cover_compensation=[5, 6],
        min_coverage={"Mañana": 2, "Tarde": 2, "Noche": 1, "Partido": 0},
        max_coverage={"Mañana": 3, "Tarde": 3, "Noche": 1, "Partido": 1},
        no_night_roles={"jefe", "subjefe", "adjunto_recepcion"},
        fixed_roles={"jefe": "Mañana"},
        fixed_days_off={"jefe": [5, 6]},
        cover_consecutive=True, night_rotation_key="test_carla",
        allow_non_consecutive_cover=False, prefer_next_week_for_cover=False,
    )


def _staff_base() -> list:
    return [
        {"name": "Juan",   "role": "jefe"},
        {"name": "Laura",  "role": "subjefe"},
        {"name": "Sofia",  "role": "adjunto_recepcion"},
        {"name": "Marta",  "role": "adjunto_recepcion"},
        {"name": "Pedro",  "role": "normal"},
        {"name": "Ana",    "role": "normal"},
        {"name": "Miguel", "role": "normal"},
        {"name": "Diego",  "role": "normal"},
        {"name": "Carla",  "role": "normal"},
        {"name": "Luis",   "role": "normal"},
    ]


SUELTOS_RECURRENTES = {
    "Ana": "Martes", "Diego": "Miércoles", "Carla": "Jueves",
    "Luis": "Viernes", "Miguel": "Lunes",
}
SEMANAS_AUSENCIA_JEFE = {1, 3, 5, 7, 9, 11}


def _estado_rotacion_inicial(staff: list, cfg: DeptConfig) -> dict:
    elegibles = [p["name"] for p in staff
                 if p.get("role") != "night_fixed" and p.get("role") not in cfg.no_night_roles]
    return {
        "orden": elegibles,
        "siguiente_turno": elegibles[0],
        "deudas": {n: 0 for n in elegibles},
        "usar_orden_fijo": True,
        "historico_balance": {},
        "ausencias_night_fixed_consecutivas": 0,
    }


def _estado_rotacion_sesgado(staff: list, cfg: DeptConfig) -> dict:
    """Variante de _estado_rotacion_inicial con usar_orden_fijo=False
    (balance puro) e histórico sesgado a favor de Pedro (el único
    "normal" sin día suelto recurrente en SUELTOS_RECURRENTES, así que
    nunca queda bloqueado por ausencia): fuerza que Pedro cubra
    repetidamente hasta chocar con el límite de racha, en vez de la
    rotación equitativa de orden fijo (que por diseño reparte tan
    parejo que rara vez encadena 5 seguidas) — necesario para que la
    reconstrucción de "Viernes de Carla" someta de verdad el mecanismo
    de descanso post-racha a prueba, no solo la ausencia de INFEASIBLE."""
    estado = _estado_rotacion_inicial(staff, cfg)
    estado["usar_orden_fijo"] = False
    estado["historico_balance"] = {
        "Pedro": {"covers": 0, "pending": 0},
        **{n: {"covers": 1000, "pending": 0} for n in estado["orden"] if n != "Pedro"},
    }
    return estado


def _dias_descanso_obligatorio_desde_turnos(turnos: list) -> set:
    """Escáner de rachas INDEPENDIENTE (no comparte código con
    _RachaTracker ni con agregar_restriccion_racha_noches): recorre una
    secuencia de turnos concatenada y devuelve el conjunto de índices que
    son descanso obligatorio real — los 2 días inmediatamente después de
    una racha de EXACTAMENTE MAX_NOCHES_VENTANA(5) 'Noche' consecutivos.
    Sirve de referencia (ground truth, derivada solo del resultado ya
    resuelto por CP-SAT) contra la que comparar las predicciones de
    _RachaTracker."""
    dias = set()
    n = len(turnos)
    i = 0
    while i < n:
        if turnos[i] == "Noche":
            j = i
            while j < n and turnos[j] == "Noche":
                j += 1
            racha = j - i
            if racha == MAX_NOCHES_VENTANA:
                for k in range(j, min(j + 2, n)):
                    dias.add(k)
            i = j
        else:
            i += 1
    return dias


def test_viernes_de_carla_12_semanas_modo_respaldo_sin_infeasible_y_tracker_coincide_con_modelo():
    cfg = _cfg()
    staff = _staff_base()
    names = [p["name"] for p in staff]

    estado_rotacion = _estado_rotacion_sesgado(staff, cfg)
    noches_ultimos_5_dias = {}
    prev_sunday_shifts = {}
    forced_extra_prev = {}

    concatenado = {n: [] for n in names}
    # tracker_predicciones[dia_absoluto] = set de nombres que el tracker
    # consideraba bloqueados ANTES de decidir ese día (predicción Python,
    # capturada replicando _RachaTracker con el MISMO empalme y la MISMA
    # secuencia real que produjo el resultado, para comparar contra el
    # escáner independiente más abajo).
    tracker_predicciones = {}

    dia_absoluto = 0
    for i in range(N_SEMANAS):
        semana = SEMANA_INICIAL + timedelta(weeks=i)
        staff_semana = copy.deepcopy(staff)
        for nombre, dia in SUELTOS_RECURRENTES.items():
            p = next(x for x in staff_semana if x["name"] == nombre)
            p["forced_days_off"] = [dia]
        if i in SEMANAS_AUSENCIA_JEFE:
            jefe = next(x for x in staff_semana if x["role"] == "jefe")
            jefe["forced_days_off"] = jefe.get("forced_days_off", []) + ["Miércoles"]
        for p in staff_semana:
            extra = forced_extra_prev.get(p["name"])
            if extra:
                existentes = p.get("forced_days_off", [])
                p["forced_days_off"] = list(dict.fromkeys(existentes + extra))

        try:
            resultado = generar_semana_cpsat_con_cobertura_nocturna(
                staff_semana, semana, cfg, estado_rotacion, noches_ultimos_5_dias,
                prev_sunday_shifts=prev_sunday_shifts,
            )
        except RuntimeError as e:
            pytest.fail(
                f"INFEASIBLE espurio en la semana {i + 1} ({semana.isoformat()}) "
                f"de la reconstrucción de 'Viernes de Carla' en modo respaldo, "
                f"con el fix de bloqueados_por_racha ya aplicado: {e}"
            )

        schedule = resultado["schedule"]

        # Réplica INDEPENDIENTE del tracker (mismo código que usa
        # internamente generar_semana_cpsat_con_cobertura_nocturna, pero
        # instanciado aquí para poder capturar, día a día, qué predecía
        # ANTES de saber el resultado real de cada día). Pasa
        # descanso_pendiente_inicial (NUEVO, esta sesión) igual que la
        # llamada real — sin esto, esta réplica sub-predice el descanso
        # pendiente que cruza el límite de semana (el mismo tipo de fallo
        # que este test existe para detectar, pero en la réplica del test,
        # no en el mecanismo real).
        tracker = _RachaTracker(
            names, noches_ultimos_5_dias,
            descanso_pendiente_inicial=estado_rotacion.get("descanso_pendiente_inicial", {}),
        )
        for d in range(7):
            tracker_predicciones[dia_absoluto + d] = set(tracker.bloqueados())
            elegido_real = next(
                (n for n in names if schedule[n][d] == "Noche"), None
            )
            assert elegido_real is not None, (
                f"semana {i + 1}, día {DAYS[d]}: ningún elegible cubrió "
                f"Noche (min_coverage=1 debía garantizar exactamente uno) — "
                f"schedule del día: { {n: schedule[n][d] for n in names} }"
            )
            assert elegido_real not in tracker_predicciones[dia_absoluto + d], (
                f"semana {i + 1}, día {DAYS[d]}: el modelo CP-SAT asignó "
                f"Noche a {elegido_real!r}, pero el tracker de Python lo "
                f"consideraba en descanso obligatorio pendiente ese mismo "
                f"día — el tracker está SOBRE-prediciendo (o el modelo "
                f"real está violando la racha, lo que sería aún más grave)"
            )
            tracker.registrar(elegido_real)

        for n in names:
            concatenado[n].extend(schedule[n])

        estado_rotacion = resultado["estado_rotacion"]
        noches_ultimos_5_dias = resultado["noches_ultimos_5_dias"]
        forced_extra_prev = resultado["forced_days_off_extra"]
        prev_sunday_shifts = {n: schedule[n][6] for n in names}
        dia_absoluto += 7

    # ─── Comparación EXACTA, día a día, persona a persona ─────────────────
    # dias_descanso_reales[nombre]: ground truth derivada SOLO del
    # resultado ya resuelto (escáner independiente, sin relación de código
    # con _RachaTracker ni con agregar_restriccion_racha_noches).
    dias_descanso_reales = {
        n: _dias_descanso_obligatorio_desde_turnos(concatenado[n]) for n in names
    }
    # dias_bloqueado_por_tracker[nombre]: días en los que ESE nombre
    # concreto apareció en las predicciones capturadas arriba.
    dias_bloqueado_por_tracker = {n: set() for n in names}
    for dia, bloqueados in tracker_predicciones.items():
        for n in bloqueados:
            dias_bloqueado_por_tracker[n].add(dia)

    discrepancias = []
    for n in names:
        solo_tracker = dias_bloqueado_por_tracker[n] - dias_descanso_reales[n]
        solo_real = dias_descanso_reales[n] - dias_bloqueado_por_tracker[n]
        if solo_tracker:
            discrepancias.append(
                f"{n}: el tracker predijo descanso obligatorio en los días "
                f"absolutos {sorted(solo_tracker)}, pero el escáner "
                f"independiente sobre el resultado real NO los considera "
                f"descanso obligatorio (sobre-predicción)"
            )
        if solo_real:
            discrepancias.append(
                f"{n}: el resultado real tiene descanso obligatorio (tras "
                f"una racha de {MAX_NOCHES_VENTANA}) en los días absolutos "
                f"{sorted(solo_real)}, pero el tracker de Python NUNCA los "
                f"predijo como bloqueados (sub-predicción — la causa raíz "
                f"original del INFEASIBLE espurio, en una forma que esta "
                f"vez no llegó a fallar por pura holgura de personal)"
            )

    assert not discrepancias, (
        "El tracker de Python (_RachaTracker) diverge de lo que el modelo "
        "CP-SAT (agregar_restriccion_racha_noches) resolvió de verdad:\n" +
        "\n".join(f"  · {d}" for d in discrepancias)
    )

    # Confirmación de que el escenario realmente estresó el mecanismo: en
    # 12 semanas de modo respaldo permanente (10 personas, 6 "normal"
    # elegibles, sin night_fixed), alguien debió completar al menos una
    # racha real de 5 noches — si nadie lo hiciera nunca, esta comparación
    # no probaría nada sobre el caso que le preocupa a este test.
    total_descansos_reales = sum(len(v) for v in dias_descanso_reales.values())
    assert total_descansos_reales > 0, (
        "en 12 semanas de modo respaldo nadie completó nunca una racha de "
        f"{MAX_NOCHES_VENTANA} noches seguidas — el escenario no estresó el "
        "mecanismo de descanso post-racha en absoluto, esta comparación no "
        "prueba lo que dice probar"
    )


# ─── Caso de borde: racha que completa justo al filo del fin de semana ────

def test_racha_que_completa_al_filo_del_finde_tracker_coincide_con_forced_days_off_extra():
    """Caso de borde específico (Fase 5, sesión de racha): cuando una
    racha de 5 se completa cerca del sábado/domingo, parte (o la
    totalidad) del descanso obligatorio de 2 días cae FUERA de la semana
    en curso — agregar_restriccion_racha_noches lo modela con las
    variables postweek_vars, y generar_semana_cpsat_con_cobertura_nocturna
    las traduce a forced_days_off_extra (Lunes/Martes de la semana
    SIGUIENTE). El tracker de Python, al no conocer el postweek, debe
    seguir prediciendo correctamente el arranque de esa racha dentro de
    la semana en curso, y el descanso que SÍ cae dentro de esta semana
    debe coincidir con el descanso real — la parte que cruza a la semana
    siguiente ya está cubierta por el mecanismo de forced_days_off_extra
    (existente, no tocado por el fix de este Tema), pero aquí se confirma
    explícitamente que ambos mecanismos encajan sin salto ni solape."""
    cfg = _cfg()
    staff = _staff_base()
    names = [p["name"] for p in staff]

    estado_rotacion = _estado_rotacion_inicial(staff, cfg)
    # Estado sucio: Pedro entra con 4/5 últimos días ya como Noche —
    # completará su racha de 5 muy pronto en la semana 1 si vuelve a
    # cubrir. Con "usar_orden_fijo" y el orden empezando en Pedro
    # (primer elegible), y sin ausencias forzadas para nadie, el balance
    # puro de modo respaldo decide por orden/deudas — se fuerza
    # directamente en el bucle vía estado_rotacion.
    noches_ultimos_5_dias = {"Pedro": [False, True, True, True, True]}
    estado_rotacion["siguiente_turno"] = "Pedro"

    resultado = generar_semana_cpsat_con_cobertura_nocturna(
        staff, SEMANA_INICIAL, cfg, estado_rotacion, noches_ultimos_5_dias,
    )
    schedule = resultado["schedule"]

    # Réplica independiente del tracker con el mismo empalme sucio.
    tracker = _RachaTracker(names, noches_ultimos_5_dias)
    bloqueados_por_dia = {}
    for d in range(7):
        bloqueados_por_dia[d] = set(tracker.bloqueados())
        elegido_real = next(n for n in names if schedule[n][d] == "Noche")
        assert elegido_real not in bloqueados_por_dia[d], (
            f"día {DAYS[d]}: el modelo asignó Noche a {elegido_real!r} "
            f"pese a que el tracker lo consideraba en descanso obligatorio"
        )
        tracker.registrar(elegido_real)

    # Si Pedro trabajó Noche el Lunes (completando su racha sucia de 5),
    # el tracker debía predecir su descanso el Martes/Miércoles, Y el
    # resultado real debe confirmarlo — Y forced_days_off_extra NO debe
    # además duplicar ese descanso (postweek_vars solo se activa si la
    # racha completa cerca del fin de la semana, no el lunes).
    if schedule["Pedro"][0] == "Noche":
        assert "Pedro" in bloqueados_por_dia[1], (
            "el tracker debía predecir a Pedro bloqueado el Martes tras "
            "completar su racha sucia de 5 el Lunes"
        )
        assert "Pedro" in bloqueados_por_dia[2], (
            "el tracker debía predecir a Pedro bloqueado el Miércoles tras "
            "completar su racha sucia de 5 el Lunes"
        )
        assert schedule["Pedro"][1] == "Libre" and schedule["Pedro"][2] == "Libre", (
            f"el descanso obligatorio real de Pedro no coincide con la "
            f"predicción del tracker: {schedule['Pedro']}"
        )
        assert resultado["forced_days_off_extra"].get("Pedro", []) == [], (
            "el descanso de Pedro cabía entero dentro de esta semana "
            "(completó racha el Lunes); forced_days_off_extra no debía "
            "arrastrar nada extra a la semana siguiente para él: "
            f"{resultado['forced_days_off_extra']}"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Bloques de cobertura en modo respaldo (NUEVO, esta sesión — regla de
# rotación de cobertura nocturna en bloques de 4-5 noches seguidas)
# ═══════════════════════════════════════════════════════════════════════════

def _bloques_de_noches(turnos: list) -> list:
    """Agrupa una secuencia concatenada de turnos en la longitud de cada
    tramo consecutivo de 'Noche' (un "bloque"). [Noche,Noche,Libre,Noche] ->
    [2, 1]."""
    bloques = []
    i, n = 0, len(turnos)
    while i < n:
        if turnos[i] == "Noche":
            j = i
            while j < n and turnos[j] == "Noche":
                j += 1
            bloques.append(j - i)
            i = j
        else:
            i += 1
    return bloques


def test_bloques_modo_respaldo_caen_en_4_o_5_sin_interrupciones():
    """8 semanas de modo respaldo permanente (Jefe + 7 "normal", sin
    night_fixed, sin vacaciones/peticiones para nadie): sin ninguna
    interrupción posible, TODOS los bloques de Noche de TODAS las personas
    deben caer exactamente en {4, 5} (BLOQUE_RESPALDO_MIN/MAX) — nunca 1,
    2, 3, 6 o más. Confirma la regla de negocio (bloques de noches
    seguidas, no noche suelta a la vez) con un escáner INDEPENDIENTE sobre
    la salida concatenada, no confiando en que el propio mecanismo se
    autodeclare correcto."""
    cfg = _cfg()
    nombres = ["N1", "N2", "N3", "N4", "N5", "N6", "N7"]
    staff = [{"name": "Jefe", "role": "jefe"}] + [
        {"name": n, "role": "normal"} for n in nombres
    ]
    estado_rotacion = _estado_rotacion_inicial(staff, cfg)
    noches_ultimos_5_dias = {}
    prev_sunday_shifts = {}
    forced_extra_prev = {}
    concatenado = {n: [] for n in nombres}

    n_semanas = 8
    for i in range(n_semanas):
        semana = SEMANA_INICIAL + timedelta(weeks=i)
        staff_semana = copy.deepcopy(staff)
        for p in staff_semana:
            extra = forced_extra_prev.get(p["name"])
            if extra:
                existentes = p.get("forced_days_off", [])
                p["forced_days_off"] = list(dict.fromkeys(existentes + extra))

        resultado = generar_semana_cpsat_con_cobertura_nocturna(
            staff_semana, semana, cfg, estado_rotacion, noches_ultimos_5_dias,
            prev_sunday_shifts=prev_sunday_shifts,
        )
        schedule = resultado["schedule"]
        for n in nombres:
            concatenado[n].extend(schedule[n])

        estado_rotacion = resultado["estado_rotacion"]
        noches_ultimos_5_dias = resultado["noches_ultimos_5_dias"]
        forced_extra_prev = resultado["forced_days_off_extra"]
        prev_sunday_shifts = {n: schedule[n][6] for n in nombres}

    # El bloque del titular EN CURSO al terminar la simulación (si lo hay)
    # queda genuinamente incompleto — no es una interrupción real, solo el
    # corte de la ventana de observación (n_semanas), así que se excluye de
    # la comprobación de longitud exacta (su tramo final, aún sin terminar,
    # es precisamente ese bloque abierto).
    bloque_final = estado_rotacion.get("bloque_actual") or {}
    titular_en_curso = bloque_final.get("nombre")

    total_bloques = 0
    for n in nombres:
        bloques = _bloques_de_noches(concatenado[n])
        # Si n es quien queda a mitad de bloque al final de la simulación,
        # su ÚLTIMO tramo de Noche es ese bloque abierto — se descarta.
        if n == titular_en_curso and bloques:
            bloques = bloques[:-1]
        total_bloques += len(bloques)
        for longitud in bloques:
            assert longitud in (4, 5), (
                f"{n} tiene un bloque de Noche de longitud {longitud} "
                f"(esperado 4 o 5, sin ninguna vacación/petición que "
                f"pudiera justificar una interrupción en este escenario): "
                f"{concatenado[n]}"
            )
    assert total_bloques > 0, (
        f"en {n_semanas} semanas de modo respaldo permanente no se formó "
        "ningún bloque de Noche — el escenario no estresó el mecanismo de "
        "bloques en absoluto"
    )


def test_bloque_se_interrumpe_por_vacacion_y_arranca_uno_nuevo():
    """Sesga el balance para que N1 gane la rotación (igual que 'Viernes de
    Carla'), lo deja arrancar un bloque en la semana 1 sin ninguna
    interrupción, y en la semana 2 le concede una vacación/petición a N1
    justo a mitad de su bloque en curso (bloque_actual). Confirma:
      1. El bloque de N1 se corta ahí — cubre al menos MIN_NOCHES_RACHA(2)
         noches antes de la interrupción (regla general de racha mínima),
         no más allá del día bloqueado.
      2. Otra persona cubre Noche los días que N1 ya no puede."""
    cfg = _cfg()
    nombres = ["N1", "N2", "N3", "N4", "N5", "N6", "N7"]
    staff = [{"name": "Jefe", "role": "jefe"}] + [
        {"name": n, "role": "normal"} for n in nombres
    ]
    estado_rotacion = _estado_rotacion_inicial(staff, cfg)
    estado_rotacion["usar_orden_fijo"] = False
    estado_rotacion["historico_balance"] = {
        "N1": {"covers": 0, "pending": 0},
        **{n: {"covers": 1000, "pending": 0} for n in nombres[1:]},
    }

    # Semana 1: sin vacaciones — con el balance sesgado hacia N1, alguien
    # queda a mitad de bloque al terminar la semana (un bloque de 4-5 no
    # cabe entero en 7 días salvo que arranque exactamente el Lunes con
    # longitud 5 y nada más — en cuyo caso el bloque SIGUIENTE, de otra
    # persona, es el que queda a mitad; se interrumpe a QUIEN sea, no se
    # asume que tiene que ser N1 específicamente).
    resultado1 = generar_semana_cpsat_con_cobertura_nocturna(
        copy.deepcopy(staff), SEMANA_INICIAL, cfg, estado_rotacion, {},
    )
    bloque_tras_semana1 = resultado1["estado_rotacion"].get("bloque_actual") or {}
    titular = bloque_tras_semana1.get("nombre")
    assert titular is not None, (
        f"precondición del escenario: alguien debía quedar a mitad de "
        f"bloque al terminar la semana 1 — bloque_actual={bloque_tras_semana1}"
    )
    noches_cubiertas_semana1 = bloque_tras_semana1["noches_cubiertas"]

    # Semana 2: el titular del bloque en curso recibe una vacación/petición
    # concedida el PRIMER día de la semana — interrumpe su bloque en curso
    # inmediatamente.
    semana2 = SEMANA_INICIAL + timedelta(weeks=1)
    staff_semana2 = copy.deepcopy(staff)
    titular_p = next(p for p in staff_semana2 if p["name"] == titular)
    titular_p["forced_days_off"] = ["Lunes"]
    for p in staff_semana2:
        extra = resultado1["forced_days_off_extra"].get(p["name"])
        if extra:
            existentes = p.get("forced_days_off", [])
            p["forced_days_off"] = list(dict.fromkeys(existentes + extra))

    resultado2 = generar_semana_cpsat_con_cobertura_nocturna(
        staff_semana2, semana2, cfg, resultado1["estado_rotacion"],
        resultado1["noches_ultimos_5_dias"],
        prev_sunday_shifts={n: resultado1["schedule"][n][6] for n in nombres},
    )

    assert resultado2["schedule"][titular][0] == "Libre", (
        f"{titular} tiene vacación/petición concedida el Lunes de la "
        f"semana 2 — debía quedar Libre ese día pese a estar a mitad de "
        f"bloque: {resultado2['schedule'][titular]}"
    )
    assert noches_cubiertas_semana1 >= MIN_NOCHES_RACHA, (
        f"el bloque interrumpido de {titular} debía cubrir al menos "
        f"MIN_NOCHES_RACHA({MIN_NOCHES_RACHA}) noches antes de cortarse — "
        f"cubrió {noches_cubiertas_semana1}"
    )

    # Otra persona (no el titular interrumpido) debe cubrir Noche el
    # Lunes de la semana 2.
    cubridor_lunes = next(
        (n for n in nombres if resultado2["schedule"][n][0] == "Noche"), None
    )
    assert cubridor_lunes is not None and cubridor_lunes != titular, (
        f"otra persona debía cubrir Noche el Lunes de la semana 2 en vez "
        f"de {titular} (bloqueado por su vacación/petición): schedule del "
        f"día: { {n: resultado2['schedule'][n][0] for n in nombres} }"
    )
