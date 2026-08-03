"""
tests/test_libre_debido_vacaciones.py — Valida el término J (NUEVO, esta
sesión — Tema 2, cierre de pendientes Fase 5) de motor_cpsat.py: libre(s)
debido(s) antes de que empiecen las vacaciones.

Cubre los 5 escenarios pedidos:
  1. Vacaciones empiezan Domingo, 6 días trabajados esa semana -> 2 libres
     debidos, colocados justo antes.
  2. Vacaciones empiezan Miércoles, 2 días trabajados antes -> 1 libre
     debido, colocado justo antes.
  3. Vacaciones empiezan Martes, 1 día trabajado antes -> ningún libre
     debido (por debajo del umbral).
  4. Escenario de estrés: colocar el libre debido justo antes de vacaciones
     rompería cobertura mínima -> el término cede (sigue siendo factible,
     sin el libre en esa posición).
  5. Compatibilidad con el término F (Domingo suelto del sustituto de
     cobertura nocturna) cuando ambos pudieran aplicar a la misma persona
     la misma semana.

DISEÑO DE LOS ESCENARIOS 1/2/3/5 — nota importante: una primera versión de
estos tests usaba una plantilla con MUCHA holgura de cobertura ("sin
presión"), asumiendo que el solver dejaría trabajados por defecto todos
los días previos salvo los que el término J pidiera librar. Resultó
FALSO: con holgura de sobra, el término I (sobre-cobertura diurna) crea un
incentivo — ajeno a J — para librar días adicionales en CUALQUIER
posición, y además, dentro del propio término J, una vez que el conteo de
días trabajados cae a un tramo inferior, la posición exacta del/de los
día(s) de más que se libran deja de estar determinada por J (el óptimo de
coste 0 admite varias colocaciones, no solo la pegada a las vacaciones) —
verificado empíricamente antes de escribir la versión actual, no asumido.
Para obtener un óptimo ÚNICO y deterministic, cada test usa una plantilla
de 4 personas (Vero + N1 + N2 + Aux) con cobertura SIN holgura
(min_coverage == max_coverage == 3 = headcount-1) y un rol "aux_rol" con
fixed_days_off puestos exactamente en los días que deben quedar SIN
holgura para Vero (fuerza su trabajo ahí) — dejando holgura de exactamente
1 persona/día solo en las posiciones donde el término J debe decidir.
Confirmado analíticamente y con ejecución real que, bajo este diseño, la
única solución de coste 0 es la que coloca el/los libre(s) exactamente en
las posiciones esperadas (ver comentarios en cada test).

Usa _construir_modelo + _resolver_y_extraer directamente (fase 1+2 del
prototipo), igual que tests/test_motor_cpsat.py — de ahí el fixture
_data_dir_aislado (autouse), necesario porque los términos A/C de fase 2
leen histórico vía herramientas/DATA_DIR.
"""

from datetime import date

import pytest
from ortools.sat.python import cp_model

from modelo_base import DAYS, SHIFTS, DeptConfig
from motor_cpsat import (
    _construir_modelo,
    _resolver_y_extraer,
    _inicio_bloque_vacaciones,
    _tiene_par_consecutivo_disponible,
    PESO_LIBRE_DEBIDO_VACACIONES,
    PESO_USO_PARTIDO,
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


def _cfg(dias_libres_aux, **overrides) -> DeptConfig:
    """cfg con cobertura SIN holgura (headcount-1, ver docstring del
    módulo): 4 personas (Vero+N1+N2+Aux), min==max=3. Aux libra siempre
    los días de `dias_libres_aux` (fixed_days_off de su rol) — esos son
    los días en que Vero queda SIN holgura (forzada a trabajar); el resto
    de días, con Aux de vuelta, hay exactamente 1 hueco de holgura."""
    base = dict(
        dept_id="test", dept_label="Test",
        night_fixed_days_off=[], night_cover_compensation=[],
        min_coverage={"Mañana": 2, "Tarde": 1, "Noche": 0, "Partido": 0},
        max_coverage={"Mañana": 2, "Tarde": 1, "Noche": 0, "Partido": 0},
        no_night_roles={"normal", "aux_rol"},
        fixed_roles={}, fixed_days_off={"aux_rol": list(dias_libres_aux)},
        cover_consecutive=True, night_rotation_key="test",
        allow_non_consecutive_cover=False, prefer_next_week_for_cover=False,
    )
    base.update(overrides)
    return DeptConfig(**base)


def _staff(dias_vac: list) -> list:
    """Buffer1/Buffer2 (NUEVO, esta sesión — hallazgo de producción, ver
    motor_cpsat.MIN_LIBRES_POR_SEMANA): con la restricción dura nueva de
    mínimo de libres semanales, N1/N2 (role="normal") también deben
    conseguir sus propios 2 libres — algo que el diseño original de 4
    personas (Vero+N1+N2+Aux, cobertura SIN holgura salvo 1 hueco/día) no
    dejaba sitio para hacer sin invalidar el escenario que estos tests
    existen para probar (0 holgura en días concretos, para que la posición
    del/de los libre(s) debido(s) de Vero quede determinada sin ambigüedad).
    Buffer1/Buffer2 comparten el rol "aux_rol" con Aux (mismos
    fixed_days_off) — dan holgura real los días en que Aux NO libra (más
    candidatos disponibles que el mínimo exige), sin tocar los días en que
    Aux SÍ libra (ahí Vero/N1/N2 siguen siendo, entre los tres, exactamente
    la cantidad exigida — 0 holgura, igual que antes). No están sujetos al
    mínimo de libres nuevo (mismo rol que Aux, con fixed_days_off), así que
    no compiten por los huecos que el término J reserva para Vero."""
    return [
        {"name": "Vero", "role": "normal", "forced_days_off": list(dias_vac),
         "dias_vacaciones": list(dias_vac)},
        {"name": "N1", "role": "normal"},
        {"name": "N2", "role": "normal"},
        {"name": "Aux", "role": "aux_rol"},
        {"name": "Buffer1", "role": "aux_rol"},
        {"name": "Buffer2", "role": "aux_rol"},
    ]


def _resolver(staff, cfg, **kwargs):
    model, x, names, by_name = _construir_modelo(staff, SEMANA_INICIO, cfg, **kwargs)
    return _resolver_y_extraer(model, x, names)


# ─── 1. Empieza Domingo, 6 días trabajados posibles ────────────────────────

def test_vacaciones_empiezan_domingo_activa_2_libres_debidos():
    """Aux libra Lunes-Jueves (0 holgura ahí: Vero forzada a trabajar).
    Viernes y Sábado, con Aux de vuelta, hay exactamente 1 hueco de
    holgura cada día. Único óptimo de coste 0 (verificado analíticamente):
    Vero se queda con AMBOS huecos (Viernes+Sábado) — cualquier reparto
    alternativo dista, o bien 15, o bien 30 puntos del óptimo (ver
    docstring del módulo)."""
    cfg = _cfg(dias_libres_aux=[0, 1, 2, 3])
    staff = _staff(["Domingo"])
    vero = staff[0]

    assert _inicio_bloque_vacaciones(vero) == 6, "precondición: bloque empieza el Domingo (día 6)"

    schedule = _resolver(staff, cfg)
    turnos_vero = schedule["Vero"]

    assert turnos_vero[0:4] == ["Tarde"] * 4 or all(t != "Libre" for t in turnos_vero[0:4]), (
        f"Lunes-Jueves debían quedar trabajados (sin holgura de cobertura "
        f"ahí): {turnos_vero}"
    )
    assert turnos_vero[4] == "Libre", f"Viernes (2º libre debido) debía ser Libre: {turnos_vero}"
    assert turnos_vero[5] == "Libre", f"Sábado (1er libre debido) debía ser Libre: {turnos_vero}"
    assert turnos_vero[6] == "Libre", "Domingo es el propio día de vacaciones"


# ─── 2. Empieza Miércoles, 2 días trabajados antes ─────────────────────────

def test_vacaciones_empiezan_miercoles_activa_1_libre_debido():
    """Aux libra solo el Lunes (0 holgura ahí). Martes, con Aux de vuelta,
    tiene exactamente 1 hueco de holgura — el único óptimo de coste 0 es
    que Vero se quede con él (resto trabajado el Lunes, forzado).

    dias_bloqueados_externos={"Vero": {0}} (NUEVO, esta sesión — hallazgo
    de producción): sin esto, la restricción dura nueva de patrón de
    libres (motor_cpsat.DIAS_LIBRES_SEGUIDOS_POR_SEMANA) no tiene forma de
    saber que el Lunes está estructuralmente bloqueado para Vero (0
    holgura por Aux librando ese día, no por una vacación/petición propia
    de Vero) — sin esa información intentaría formar un par consecutivo
    que incluyera el Lunes, chocando con la cobertura mínima real. Con
    solo el Martes como día no bloqueado (1 día, por debajo del mínimo del
    patrón), la restricción nueva se omite para Vero esta semana y el
    término J (blando) es quien decide el Martes, tal como pide el test."""
    cfg = _cfg(dias_libres_aux=[0])
    staff = _staff(["Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"])
    vero = staff[0]

    assert _inicio_bloque_vacaciones(vero) == 2, "precondición: bloque empieza el Miércoles (día 2)"

    schedule = _resolver(staff, cfg, dias_bloqueados_externos={"Vero": {0}})
    turnos_vero = schedule["Vero"]

    assert turnos_vero[0] != "Libre", f"Lunes debía quedar trabajado (sin holgura ahí): {turnos_vero}"
    assert turnos_vero[1] == "Libre", f"Martes (libre debido) debía ser Libre: {turnos_vero}"


# ─── 3. Empieza Martes, 1 día trabajado antes: NO se activa nada ───────────

def test_vacaciones_empiezan_martes_no_activa_libre_debido():
    """Mismo montaje que el escenario 2 (Aux libra solo el Lunes, 0
    holgura ahí para Vero), pero la vacación empieza un día antes
    (Martes): solo queda 1 día previo dentro de la semana (Lunes), por
    debajo del umbral (0-1 -> 0 libres debidos). El Lunes queda trabajado
    igualmente porque la cobertura no le deja otra opción — confirma que,
    con el término J estructuralmente inactivo (_inicio_bloque_vacaciones
    solo ve 1 día previo posible, nunca llega a pedir nada), no hace falta
    ningún libre adicional para que el modelo sea óptimo."""
    cfg = _cfg(dias_libres_aux=[0])
    staff = _staff(["Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"])
    vero = staff[0]

    assert _inicio_bloque_vacaciones(vero) == 1, "precondición: bloque empieza el Martes (día 1)"

    schedule = _resolver(staff, cfg)
    turnos_vero = schedule["Vero"]

    assert turnos_vero[0] != "Libre", (
        f"Lunes debía quedar trabajado (sin holgura de cobertura, y sin "
        f"ninguna presión del término J: con solo 1 día previo el tramo es "
        f"siempre 0-1 -> 0 libres debidos): {turnos_vero}"
    )


# ─── 4. Estrés: colocar el libre debido rompería cobertura mínima ─────────

def test_termino_cede_cuando_el_libre_debido_rompe_cobertura_minima():
    """Construido a mano para que el día que el término J preferiría dejar
    libre (Sábado, justo antes de una vacación que empieza el Domingo)
    coincida con el ÚNICO día en que la cobertura mínima no tiene ninguna
    holgura: N3 tiene el Sábado fijo libre (fixed_days_off de su rol), así
    que ese día solo quedan Vero/N1/N2 disponibles — exactamente el mínimo
    de cobertura (3). Si Vero también librara el Sábado por el término J,
    la cobertura de ese día bajaría a 2, por debajo del mínimo: una
    restricción DURA, así que el solver debe preferir pagar la penalización
    de PESO_LIBRE_DEBIDO_VACACIONES antes que romperla — el modelo debe
    seguir siendo factible, sin RuntimeError, y Vero debe seguir trabajando
    el Sábado."""
    cfg = DeptConfig(
        dept_id="test", dept_label="Test",
        night_fixed_days_off=[], night_cover_compensation=[],
        fixed_roles={}, fixed_days_off={"n3_rol": [5]},  # rol con Sábado fijo libre
        min_coverage={"Mañana": 2, "Tarde": 1, "Noche": 0, "Partido": 0},  # suma 3 = N-1
        max_coverage={"Mañana": 4, "Tarde": 4, "Noche": 0, "Partido": 1},
        no_night_roles={"normal", "n3_rol"},
        cover_consecutive=True, night_rotation_key="test",
        allow_non_consecutive_cover=False, prefer_next_week_for_cover=False,
    )
    # N4 (NUEVO, esta sesión — hallazgo de producción): con solo Vero/N1/N2
    # como "normal" y cobertura sin holgura (min=3=headcount-1), los 3 no
    # tenían margen para cumplir también su propio patrón de 2 libres
    # seguidos (restricción dura nueva) — INFEASIBLE ajeno a lo que este
    # test verifica. N4 comparte el rol de N3 ("n3_rol", también libre fijo
    # el Sábado) para no tocar la propiedad central del escenario (0
    # holgura de cobertura específicamente el Sábado, con Vero/N1/N2 como
    # únicos disponibles ese día) — solo da margen el resto de la semana.
    staff = [
        {"name": "Vero", "role": "normal", "forced_days_off": ["Domingo"], "dias_vacaciones": ["Domingo"]},
        {"name": "N1", "role": "normal"},
        {"name": "N2", "role": "normal"},
        {"name": "N3", "role": "n3_rol"},
        {"name": "N4", "role": "n3_rol"},
    ]

    schedule = _resolver(staff, cfg)  # no debe lanzar RuntimeError

    assert schedule["Vero"][5] != "Libre", (
        f"El Sábado no tenía holgura de cobertura (N3 ya libra ese día por "
        f"fixed_days_off); Vero debía seguir trabajando ese día en vez de "
        f"que el término J rompiera la cobertura mínima. Turnos de Vero: "
        f"{schedule['Vero']}"
    )
    assert schedule["Vero"][6] == "Libre", "Domingo es el propio día de vacaciones"

    # Confirmación de que el escenario realmente exigía ceder: sin la
    # restricción de cobertura del Sábado, el óptimo habría sido librar
    # ahí (como en el escenario 1) — aquí, en cambio, cubrirlo cuesta la
    # penalización del término J en vez de romper una restricción dura.
    cfg_sin_presion = DeptConfig(
        dept_id="test", dept_label="Test",
        night_fixed_days_off=[], night_cover_compensation=[],
        fixed_roles={}, fixed_days_off={},
        min_coverage={"Mañana": 1, "Tarde": 0, "Noche": 0, "Partido": 0},
        max_coverage={"Mañana": 4, "Tarde": 4, "Noche": 0, "Partido": 1},
        no_night_roles={"normal"},
        cover_consecutive=True, night_rotation_key="test",
        allow_non_consecutive_cover=False, prefer_next_week_for_cover=False,
    )
    staff_sin_presion = [
        {"name": "Vero", "role": "normal", "forced_days_off": ["Domingo"],
         "dias_vacaciones": ["Domingo"]},
        {"name": "N1", "role": "normal"},
    ]
    schedule_control = _resolver(staff_sin_presion, cfg_sin_presion)
    assert schedule_control["Vero"][5] == "Libre", (
        "control: sin la presión de cobertura del escenario de estrés, el "
        "mismo bloque de vacaciones SÍ debía producir el libre debido del "
        "Sábado — confirma que el escenario de estrés fuerza de verdad el "
        "choque que este test existe para probar, no que el término nunca "
        "actúa en absoluto"
    )


# ─── 5. Compatibilidad con el término F (Domingo suelto del sustituto) ────

def test_compatible_con_termino_f_domingo_suelto_sustituto():
    """Cuando la MISMA persona es a la vez el sustituto de cobertura
    nocturna (término F) y tiene un bloque de vacaciones que empieza a
    mitad de semana (término J), ambos términos pueden coexistir en la
    suma del objetivo sin ningún conflicto — ninguno de los dos fuerza una
    celda de forma dura (F penaliza blandamente no-Libre el Domingo; J
    penaliza blandamente no-Libre justo antes de vacaciones), así que
    sumarlos nunca puede producir una contradicción estructural.

    Caso concreto: vacación Viernes-Domingo (empieza Viernes, día 4) +
    cfg.night_fixed_days_off=[1,2] (Martes/Miércoles — solo relevante para
    el cálculo de gating de F, _tiene_par_consecutivo_disponible; no fuerza
    nada real sobre Vero en este modelo). Con esa combinación, los únicos
    días "libres para emparejar" que F considera son Lunes y Jueves (no
    consecutivos) -> el gate de F se activa. A la vez, J sigue activo
    porque el bloque de vacaciones empieza a mitad de semana. Aux libra
    Lunes-Miércoles (0 holgura ahí); Jueves, con Aux de vuelta, tiene
    exactamente 1 hueco de holgura — el óptimo de coste 0 de J exige que
    sea Vero quien se lo quede.

    Nota documentada (en vez de asumida): en este caso concreto, el
    término F termina siendo estructuralmente inerte — penaliza no-Libre
    el Domingo, pero el Domingo YA está forzado Libre por la propia
    vacación (restricción dura #6 de fase 1), así que su coste es siempre
    0 pase lo que pase con J. Esto no es casualidad: cualquier vez que el
    bloque de vacaciones de la persona alcance el Domingo (lo habitual
    cuando arranca a media semana y no termina antes del fin de semana),
    el objetivo de F queda automáticamente satisfecho por la vacación
    misma, antes de que compita con nada. El caso en que SÍ podrían
    competir de verdad por el mismo día es estructuralmente más estrecho
    (vacación que no llega al Domingo) — pero incluso ahí, al ser ambos
    términos blandos sin ninguna restricción dura compartida, solo se
    suman en el objetivo: en el peor caso uno pesa más que el otro para
    esa celda, nunca hay contradicción ni INFEASIBLE."""
    cfg = _cfg(
        dias_libres_aux=[0, 1, 2],
        night_fixed_days_off=[1, 2], prefer_next_week_for_cover=True,
    )
    staff = _staff(["Viernes", "Sábado", "Domingo"])
    vero = staff[0]

    # Confirma las precondiciones del escenario (ambos mecanismos deben
    # detectar que aplican, si no el test no prueba lo que dice probar).
    assert _inicio_bloque_vacaciones(vero) == 4, "el bloque debe empezar el Viernes (día 4)"
    dias_bloqueados_vero = {DAYS.index(d) for d in ["Viernes", "Sábado", "Domingo"]}
    assert _tiene_par_consecutivo_disponible(cfg, dias_bloqueados_vero) is False, (
        "precondición: no debe haber ningún par consecutivo disponible para "
        "Vero (gate de F activo) — si lo hubiera, este test no demostraría "
        "coexistencia real de ambos términos"
    )

    # dias_bloqueados_externos={"Vero": {0,1,2}} (NUEVO, esta sesión —
    # hallazgo de producción, mismo motivo que en el escenario 2 de
    # arriba): Lunes-Miércoles están estructuralmente bloqueados para Vero
    # (0 holgura por Aux librando esos días), no por una vacación/petición
    # propia — sin decírselo a la restricción dura nueva de patrón de
    # libres, esta intentaría formar un par consecutivo ahí, chocando con
    # la cobertura mínima real.
    schedule = _resolver(
        staff, cfg, nombre_sustituto_cobertura="Vero",
        dias_bloqueados_externos={"Vero": {0, 1, 2}},
    )  # no debe lanzar RuntimeError

    assert schedule["Vero"][0:3] != ["Libre"] * 3 and all(
        t != "Libre" for t in schedule["Vero"][0:3]
    ), f"Lunes-Miércoles debían quedar trabajados (sin holgura ahí): {schedule['Vero']}"
    assert schedule["Vero"][3] == "Libre", (
        f"El término J debía seguir colocando su libre debido (Jueves) sin "
        f"que la presencia simultánea del término F lo alterara: "
        f"{schedule['Vero']}"
    )
    assert schedule["Vero"][6] == "Libre", "Domingo es el propio día de vacaciones"


# ═══════════════════════════════════════════════════════════════════════════
# Confirmación explícita con ObjectiveValue() en los 4 valores frontera
# ═══════════════════════════════════════════════════════════════════════════
#
# Los tests 1-3 de arriba confirman el término J observando el SCHEDULE
# resultante (¿quedó Libre el día esperado?). Esta sección va un paso más
# allá: confirma con el VALOR NUMÉRICO del objetivo (solver.ObjectiveValue(),
# no solo el schedule) que, en cada uno de los 4 puntos frontera pedidos —
# 1 día trabajado (0 libres exigidos), 2 días (1 libre), 4 días (1 libre,
# el límite superior de ese tramo) y 5 días (2 libres, el salto al
# siguiente tramo) — el término aporta EXACTAMENTE
# PESO_LIBRE_DEBIDO_VACACIONES * (libres exigidos no satisfechos), ni más
# ni menos.
#
# Para poder leer esa cifra sin ambigüedad hace falta ELIMINAR el problema
# de fondo de los tests 1-3 (con holgura de cobertura, "cuántos días se
# trabajan antes" es una variable que el propio solver decide, así que no
# se puede fijar un valor exacto de antemano para comprobar la frontera).
# Aquí se rompe esa circularidad de raíz: una plantilla de UNA sola
# persona (Vero), con su rol fijado a turno "Partido" TODOS los días que
# no estén en forced_days_off (fixed_roles) y cobertura mínima/máxima
# totalmente permisiva (todo en 0/generoso) — así, forced_days_off por sí
# solo determina el schedule COMPLETO sin ninguna decisión libre del
# solver: "días trabajados antes de vacaciones" queda fijado a mano, con
# certeza, para cada caso.
#
# Por qué "Partido" y no otro turno: dos motivos, cada uno anula un
# término de fase 2 que si no contaminaría la lectura de ObjectiveValue():
#   - Término I (sobrecobertura diurna) solo penaliza Mañana/Tarde
#     (TURNOS_DIURNOS_SIN_PARTIDO) — Partido queda fuera por diseño (ver
#     docstring del módulo), así que trabajar no añade coste de I.
#   - Término B (continuidad) solo penaliza CAMBIAR de turno entre días
#     trabajados consecutivos — al ser SIEMPRE el mismo turno (Partido)
#     los días que trabaja, dos días trabajados consecutivos nunca rompen
#     continuidad, sea cual sea el patrón de libres entre medio.
# Los términos A/C (equidad/desequilibrio histórico) ya dan 0 porque el
# fixture _data_dir_aislado deja el histórico vacío (ratios y
# shift_counts en 0 para todo el mundo, ver tests/test_motor_cpsat.py).
# E/F/G/H no se activan (rol no excluido de equidad, sin
# nombre_sustituto_cobertura, sin cfg.cobertura_roles, sin roles
# jefe/subjefe en la plantilla).
#
# Lo único que queda en el objetivo, además de J, es D (uso de Partido):
# PESO_USO_PARTIDO(20) por cada día trabajado — un término CONOCIDO y
# CONSTANTE para un número de días trabajados dado (no depende de en qué
# día concreto caigan), así que se puede restar limpiamente para leer la
# contribución de J sola: ObjectiveValue() = PESO_USO_PARTIDO * K +
# PESO_LIBRE_DEBIDO_VACACIONES * (libres exigidos no satisfechos).

def _cfg_aislado_j() -> DeptConfig:
    return DeptConfig(
        dept_id="test_j_frontera", dept_label="Test J frontera",
        night_fixed_days_off=[], night_cover_compensation=[],
        min_coverage={"Mañana": 0, "Tarde": 0, "Noche": 0, "Partido": 0},
        max_coverage={"Mañana": 5, "Tarde": 5, "Noche": 0, "Partido": 5},
        no_night_roles={"vero_rol"}, fixed_roles={"vero_rol": "Partido"}, fixed_days_off={},
        cover_consecutive=True, night_rotation_key="test_j_frontera",
        allow_non_consecutive_cover=False, prefer_next_week_for_cover=False,
    )


def _resolver_con_objetivo(dias_libres_lunes_a_sabado: list) -> tuple:
    """Resuelve el modelo aislado de una sola persona (Vero, turno fijo
    Partido salvo los días de `dias_libres_lunes_a_sabado` + el propio
    Domingo de vacaciones) y devuelve (turnos_vero, ObjectiveValue()).
    Vacación SIEMPRE empieza el Domingo (día 6, pos1=Sábado, pos2=Viernes)
    en toda esta sección — el punto frontera bajo prueba se controla por
    completo con QUÉ días de Lunes-Sábado se fuerzan libres, no con el día
    de inicio."""
    cfg = _cfg_aislado_j()
    staff = [{
        "name": "Vero", "role": "vero_rol",
        "forced_days_off": list(dias_libres_lunes_a_sabado) + ["Domingo"],
        "dias_vacaciones": ["Domingo"],
    }]
    model, x, names, by_name = _construir_modelo(staff, SEMANA_INICIO, cfg)
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    status = solver.Solve(model)
    assert status in (cp_model.OPTIMAL, cp_model.FEASIBLE), solver.StatusName(status)
    turnos = ["" for _ in range(7)]
    for d in range(7):
        for s in SHIFTS:
            if solver.Value(x["Vero", d, s]) == 1:
                turnos[d] = s
                break
    return turnos, solver.ObjectiveValue()


def test_frontera_1_dia_trabajado_cero_libres_exigidos():
    """1 día trabajado (tramo 0-1): 0 libres exigidos — y, a diferencia de
    los demás tramos, esto debe cumplirse SIN IMPORTAR en qué posición
    caiga ese único día trabajado (dentro del tramo 0-1 el término ni
    siquiera mira la posición, step1 ya es 0 solo por el conteo). Se
    confirma con DOS colocaciones distintas del único día trabajado
    (Lunes vs. Sábado/pos1) dando el MISMO ObjectiveValue()."""
    turnos_a, obj_a = _resolver_con_objetivo(
        ["Martes", "Miércoles", "Jueves", "Viernes", "Sábado"]  # trabaja solo Lunes
    )
    turnos_b, obj_b = _resolver_con_objetivo(
        ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes"]  # trabaja solo Sábado (pos1)
    )

    esperado = PESO_USO_PARTIDO * 1  # + 0 de J
    assert obj_a == esperado, (
        f"1 día trabajado (Lunes), sin tocar pos1: ObjectiveValue()={obj_a}, "
        f"esperado {esperado} (solo el coste D de 1 día de Partido, J=0). "
        f"turnos={turnos_a}"
    )
    assert obj_b == esperado, (
        f"1 día trabajado, EN pos1 (Sábado): ObjectiveValue()={obj_b}, "
        f"esperado {esperado} (J sigue siendo 0 pese a que pos1 no está "
        f"libre — por debajo del umbral, la posición no importa). "
        f"turnos={turnos_b}"
    )
    assert obj_a == obj_b, (
        "con 1 solo día trabajado, el ObjectiveValue() no debía depender de "
        f"en qué día cayera ese día trabajado: {obj_a} vs {obj_b}"
    )


def test_frontera_2_dias_trabajados_un_libre_exigido():
    """2 días trabajados (tramo 2-4, límite inferior): 1 libre exigido, en
    pos1 (Sábado). Se confirma con el par compliant/violating: la
    diferencia EXACTA de ObjectiveValue() debe ser
    PESO_LIBRE_DEBIDO_VACACIONES * 1."""
    turnos_cumple, obj_cumple = _resolver_con_objetivo(
        ["Miércoles", "Jueves", "Viernes", "Sábado"]  # trabaja Lunes+Martes; pos1 libre
    )
    turnos_incumple, obj_incumple = _resolver_con_objetivo(
        ["Lunes", "Martes", "Miércoles", "Jueves"]  # trabaja Viernes+Sábado; pos1 NO libre
    )

    esperado_cumple = PESO_USO_PARTIDO * 2  # + 0 de J (pos1 satisfecho)
    esperado_incumple = PESO_USO_PARTIDO * 2 + PESO_LIBRE_DEBIDO_VACACIONES * 1

    assert obj_cumple == esperado_cumple, (
        f"2 días trabajados, pos1 libre: ObjectiveValue()={obj_cumple}, "
        f"esperado {esperado_cumple}. turnos={turnos_cumple}"
    )
    assert obj_incumple == esperado_incumple, (
        f"2 días trabajados, pos1 NO libre: ObjectiveValue()={obj_incumple}, "
        f"esperado {esperado_incumple}. turnos={turnos_incumple}"
    )
    assert obj_incumple - obj_cumple == PESO_LIBRE_DEBIDO_VACACIONES * 1, (
        f"la diferencia entre incumplir y cumplir con 2 días trabajados "
        f"debía ser exactamente PESO_LIBRE_DEBIDO_VACACIONES*1="
        f"{PESO_LIBRE_DEBIDO_VACACIONES}, fue {obj_incumple - obj_cumple}"
    )


def test_frontera_4_dias_trabajados_un_libre_exigido_limite_superior():
    """4 días trabajados (tramo 2-4, límite SUPERIOR): sigue siendo 1 solo
    libre exigido (no 2) — confirma que el tramo no cambia justo en su
    borde alto. Mismo patrón compliant/violating, misma diferencia
    esperada de 1*PESO que en el caso de 2 días (el tramo entero pide lo
    mismo, sea 2, 3 o 4 días trabajados)."""
    turnos_cumple, obj_cumple = _resolver_con_objetivo(
        ["Viernes", "Sábado"]  # trabaja Lunes-Jueves; pos1 (y pos2) libres
    )
    turnos_incumple, obj_incumple = _resolver_con_objetivo(
        ["Lunes", "Miércoles"]  # trabaja Martes,Jueves,Viernes,Sábado; pos1 NO libre
    )

    esperado_cumple = PESO_USO_PARTIDO * 4  # + 0 de J
    esperado_incumple = PESO_USO_PARTIDO * 4 + PESO_LIBRE_DEBIDO_VACACIONES * 1

    assert obj_cumple == esperado_cumple, (
        f"4 días trabajados, pos1 libre: ObjectiveValue()={obj_cumple}, "
        f"esperado {esperado_cumple}. turnos={turnos_cumple}"
    )
    assert obj_incumple == esperado_incumple, (
        f"4 días trabajados, pos1 NO libre: ObjectiveValue()={obj_incumple}, "
        f"esperado {esperado_incumple}. turnos={turnos_incumple}"
    )
    assert obj_incumple - obj_cumple == PESO_LIBRE_DEBIDO_VACACIONES * 1, (
        f"con 4 días trabajados (límite superior del tramo 2-4) la "
        f"diferencia de incumplir vs cumplir debía seguir siendo "
        f"exactamente 1*PESO={PESO_LIBRE_DEBIDO_VACACIONES}, no 2*PESO — "
        f"fue {obj_incumple - obj_cumple}"
    )


def test_frontera_5_dias_trabajados_dos_libres_exigidos_salto_de_tramo():
    """5 días trabajados (tramo 5-6): SALTA a 2 libres exigidos (pos1 Y
    pos2) — el doble del máximo posible en el tramo 2-4 (que solo llegaba
    a 1*PESO). Confirmado con el peor caso (ninguno de los 2 satisfecho,
    coste 2*PESO) frente al mejor caso ALCANZABLE con 5 días realmente
    trabajados (pos1 satisfecho, pos2 no — 1*PESO).

    Nota de diseño explícita (no un caso omitido): con exactamente 5 días
    trabajados fijados de antemano, NO existe ninguna colocación con coste
    0 — satisfacer el tramo por completo exige 2 libres en pos1+pos2, pero
    eso deja como máximo 4 días trabajados (no 5) entre los 6 previos, así
    que "5 trabajados y las 2 exigencias satisfechas" son mutuamente
    excluyentes por construcción (si se satisficieran ambas, el conteo
    real bajaría a 4, el tramo ya cubierto arriba). Por eso este test
    compara el peor caso (0 de 2) contra el mejor ALCANZABLE (1 de 2), no
    contra un "cumple del todo" que no puede existir a este conteo."""
    turnos_peor, obj_peor = _resolver_con_objetivo(
        ["Lunes"]  # trabaja Martes-Sábado; ni pos1 ni pos2 libres
    )
    turnos_mejor, obj_mejor = _resolver_con_objetivo(
        ["Sábado"]  # trabaja Lunes-Viernes; pos1 libre, pos2 (Viernes) no
    )

    esperado_peor = PESO_USO_PARTIDO * 5 + PESO_LIBRE_DEBIDO_VACACIONES * 2
    esperado_mejor = PESO_USO_PARTIDO * 5 + PESO_LIBRE_DEBIDO_VACACIONES * 1

    assert obj_peor == esperado_peor, (
        f"5 días trabajados, ni pos1 ni pos2 libres: "
        f"ObjectiveValue()={obj_peor}, esperado {esperado_peor} "
        f"(2*PESO — el doble del máximo del tramo 2-4). turnos={turnos_peor}"
    )
    assert obj_mejor == esperado_mejor, (
        f"5 días trabajados, solo pos1 libre: ObjectiveValue()={obj_mejor}, "
        f"esperado {esperado_mejor}. turnos={turnos_mejor}"
    )
    assert obj_peor - obj_mejor == PESO_LIBRE_DEBIDO_VACACIONES * 1, (
        f"satisfacer pos1 (de los 2 exigidos) debía ahorrar exactamente "
        f"1*PESO={PESO_LIBRE_DEBIDO_VACACIONES}: {obj_peor} vs {obj_mejor}"
    )

    # Confirmación cruzada del SALTO de tramo: el máximo coste alcanzable
    # en el tramo 2-4 (4 días trabajados, ningún libre — ver
    # test_frontera_4_dias_trabajados_un_libre_exigido_limite_superior,
    # caso "incumple": PESO_USO_PARTIDO*4 + PESO_LIBRE_DEBIDO_VACACIONES*1)
    # es 1*PESO; aquí, con 5 días trabajados y ningún libre, el máximo
    # coste es 2*PESO — exactamente el doble, el salto de tramo pedido.
    obj_4_max_penalizado = PESO_USO_PARTIDO * 4 + PESO_LIBRE_DEBIDO_VACACIONES * 1
    obj_5_max_penalizado = PESO_USO_PARTIDO * 5 + PESO_LIBRE_DEBIDO_VACACIONES * 2
    assert obj_peor == obj_5_max_penalizado
    assert (obj_5_max_penalizado - PESO_USO_PARTIDO * 5) == 2 * (
        obj_4_max_penalizado - PESO_USO_PARTIDO * 4
    ), "el coste máximo de J en el tramo 5-6 debía ser el doble que en el tramo 2-4"
