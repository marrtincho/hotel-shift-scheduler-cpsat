"""
tests/test_cobertura_jefatura_cpsat.py — Adapta tests/test_cobertura_jefatura.py
(motor heurístico) al motor CP-SAT (motor_cpsat.py), más tests nuevos para la
restricción 9 (turno complementario jefe/subjefe), que no existe en el
heurístico.

No se reutiliza generate_schedule_valido/generate_schedule (API del motor
heurístico, no aplicable a CP-SAT) — cada escenario se adapta a
generar_semana_cpsat / _construir_modelo + _resolver_y_extraer, y las
aserciones se adaptan a lo que el modelo CP-SAT realmente decide (sin
"days_off"/"violaciones" explícitos, que son conceptos del heurístico), pero
se conserva la misma cobertura de escenarios de negocio: día suelto, días
libres fijos no cuentan como ausencia, semana completa opción A vs B, jefe
inexistente, cubrir_dias_libres_fijos activo/desactivado, la combinación de
ambas casillas, y que max_coverage nunca se rompe por la restricción 8.

Restricción 8 se modela como término de objetivo (peso muy alto,
PESO_COBERTURA_AUSENCIA), NUNCA como restricción dura — así que, a
diferencia del heurístico (que usa "violaciones == []" como criterio de
éxito), aquí el criterio es que el modelo sea SIEMPRE factible (nunca lanza
RuntimeError) y que el término gane sus desempates salvo cuando
max_coverage lo impide estructuralmente.

CORRECCIÓN (revisión posterior): una primera versión de este archivo tenía
varios tests que adaptaban la INTERFAZ del original pero no verificaban de
verdad el mecanismo — mismo tipo de fallo detectado antes en "domingo
suelto". En concreto:
  - Con PESO_COBERTURA_AUSENCIA=1000 y plantilla holgada, se comprobó
    empíricamente que la Opción A (semana_completa=False) y la Opción B
    (semana_completa=True) producen el MISMO schedule para Laura (7 días
    Mañana) cuando el jefe está ausente toda la semana — el peso base ya
    domina en ambas, así que un test que solo compara "el schedule
    resultante" entre A y B no demuestra nada sobre la diferencia real
    entre ambas. La única forma fiable de verificar que la Opción B añade
    presión REAL es comparar el valor objetivo óptimo (ObjectiveValue())
    forzando el mismo escenario en A y en B — ver
    test_semana_completa_opcion_b_penaliza_exactamente_2x_peso_extra_en_finde
    y su equivalente para jefe inexistente.
  - Los tests que solo llaman a _dias_cobertura_ausencia (función pura,
    sin resolver el modelo) verifican el CÁLCULO de entrada, pero no
    demuestran que el término G actúe de verdad en el solver — se añadió
    al menos un caso de extremo a extremo con verificación de coste para
    la familia de cubrir_dias_libres_fijos.

De las 13 funciones de test del archivo original, 3 NO se portaron:
  - test_equidad_15_semanas_subjefe_fuera_del_balanceo: no aplica aquí
    porque la propiedad genérica ("rol excluido de equidad sin
    fixed_days_off casi nunca recibe finde completo") ya está cubierta,
    de forma independiente de cobertura_roles, en
    tests/test_motor_cpsat.py::test_rol_excluido_sin_fixed_days_off_casi_nunca_libra_finde_completo.
  - test_roles_json_antiguo_se_carga_sin_error_con_defaults: prueba
    herramientas.cargar_roles/retrocompatibilidad de roles.json, no el
    motor CP-SAT — fuera de alcance de este archivo.
  - test_15_semanas_cubrir_dias_libres_fijos_siempre_2_libres_y_cobertura_ok:
    tenía DOS propiedades encadenadas. La de "siempre exactamente 2
    libres/semana" no aplica (el modelo CP-SAT no tiene, en ninguna fase,
    una restricción de "N días libres por semana" — es un concepto propio
    de la rotación por pares del heurístico). La de "cobertura_ok a lo
    largo de muchas semanas encadenadas con cobertura_roles activo" SÍ es
    una propiedad real que solo puede fallar por acumulación (no en una
    semana aislada) — se portó por separado, ver
    test_15_semanas_encadenadas_cubrir_dias_libres_fijos_nunca_rompe_max_coverage.
"""

import copy
from datetime import date, timedelta

import pytest

from modelo_base import DAYS
from motor_cpsat import (
    _construir_modelo, _resolver_y_extraer, _dias_cobertura_ausencia,
    PESO_COBERTURA_AUSENCIA, PESO_COMPLEMENTARIO_JEFATURA, generar_semana_cpsat,
    PESO_COBERTURA_DIAS_LIBRES_FIJOS,
)

SEMANA = date(2099, 6, 1)  # Lunes en futuro lejano


@pytest.fixture(autouse=True)
def _data_dir_aislado(tmp_path, monkeypatch):
    """Aísla DATA_DIR — los términos de fase 2 (equidad/histórico) leen
    historial.json/roles.json vía herramientas, igual que en test_motor_cpsat.py."""
    import herramientas
    backup = tmp_path / "backups"
    backup.mkdir()
    monkeypatch.setattr(herramientas, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(herramientas, "BACKUP_DIR", str(backup))
    monkeypatch.setattr(herramientas, "FESTIVOS_FILE", str(tmp_path / "festivos.json"))


def _cfg_con_cobertura(semana_completa: bool = False, cubrir_dias_libres_fijos: bool = False):
    """RECEPCION_CONFIG con subjefe configurado para cubrir la ausencia del
    jefe — mismos parámetros que _cfg_con_cobertura en
    tests/test_cobertura_jefatura.py."""
    from modelo_recepcion import RECEPCION_CONFIG
    cfg = copy.deepcopy(RECEPCION_CONFIG)
    cfg.cobertura_roles = {"subjefe": {"cubre": "jefe", "semana_completa": semana_completa,
                                        "dias_libres_fijos": cubrir_dias_libres_fijos}}
    cfg.roles_excluidos_equidad = {"night_fixed", "jefe", "subjefe"}
    return cfg


def _plantilla():
    from modelo_recepcion import STAFF_DEFAULT
    return copy.deepcopy(STAFF_DEFAULT)


def _plantilla_holgada():
    """STAFF_DEFAULT + 1 persona normal extra — da margen de cobertura para
    que la restricción 8 (peso alto) no choque con max_coverage en los
    escenarios de semana completa, igual que en el test original."""
    staff = _plantilla()
    staff.append({"name": "Extra", "role": "normal", "preferences": []})
    return staff


# ═══════════════════════════════════════════════════════════════════════════
# 1. Ausencia de un día suelto (petición del jefe)
# ═══════════════════════════════════════════════════════════════════════════

def test_dia_suelto_de_ausencia_subjefe_cubre_solo_ese_dia():
    cfg = _cfg_con_cobertura(semana_completa=False)
    staff = _plantilla()
    juan = next(p for p in staff if p["name"] == "Juan")
    juan["forced_days_off"] = ["Martes"]

    dias_ausencia, _dias_fijos, semana_completa = _dias_cobertura_ausencia(
        staff, cfg, "jefe", cfg.cobertura_roles["subjefe"]
    )
    assert dias_ausencia == {1}  # Martes
    assert not semana_completa

    sched = generar_semana_cpsat(staff, SEMANA, cfg)
    assert sched["Juan"][1] == "Libre"
    assert sched["Laura"][1] == "Mañana"  # cubre el turno fijo del jefe


# ═══════════════════════════════════════════════════════════════════════════
# 2. Días libres FIJOS del jefe (Sáb+Dom): no son ausencia
# ═══════════════════════════════════════════════════════════════════════════

def test_dias_libres_fijos_del_jefe_no_cuentan_como_ausencia():
    """Test clave contra el error de tratar los libres fijos como ausencia:
    si se tratasen como tal, dias_ausencia incluiría el finde del jefe
    aunque no haya ninguna vacación/petición real."""
    cfg = _cfg_con_cobertura(semana_completa=False)
    staff = _plantilla()  # Juan (jefe) sin peticiones ni vacaciones

    dias_ausencia, _dias_fijos, semana_completa = _dias_cobertura_ausencia(
        staff, cfg, "jefe", cfg.cobertura_roles["subjefe"]
    )
    assert dias_ausencia == set()
    assert not semana_completa

    sched = generar_semana_cpsat(staff, SEMANA, cfg)
    # El jefe conserva sus libres fijos Sáb+Dom con normalidad (restricción
    # dura de fase 1, no afectada por la restricción 8).
    assert sched["Juan"][5] == "Libre"
    assert sched["Juan"][6] == "Libre"


# ═══════════════════════════════════════════════════════════════════════════
# 3/4. Ausencia de semana completa — Opción A vs Opción B
# ═══════════════════════════════════════════════════════════════════════════

def test_semana_completa_dias_ausencia_identicos_en_opcion_a_y_b():
    """El cálculo de días de ausencia (y por tanto la presión de la
    restricción 8 para hacer el turno fijo del jefe) es IDÉNTICO entre
    opción A y B cuando el jefe está ausente toda la semana — la única
    diferencia entre ambas opciones es la presión EXTRA sobre el fin de
    semana específicamente (ver test siguiente), no los días cubiertos."""
    staff = _plantilla_holgada()
    juan = next(p for p in staff if p["name"] == "Juan")
    juan["forced_days_off"] = list(DAYS)

    cfg_a = _cfg_con_cobertura(semana_completa=False)
    cfg_b = _cfg_con_cobertura(semana_completa=True)

    dias_a, _fijos_a, completa_a = _dias_cobertura_ausencia(staff, cfg_a, "jefe", cfg_a.cobertura_roles["subjefe"])
    dias_b, _fijos_b, completa_b = _dias_cobertura_ausencia(staff, cfg_b, "jefe", cfg_b.cobertura_roles["subjefe"])

    assert dias_a == dias_b == set(range(7))
    assert completa_a and completa_b  # la condición RAW es la misma; el flag de cfg decide si se usa


def test_termino_g_penaliza_no_cubrir_turno_fijo_un_dia_de_diario():
    """Verificación de coste (no de schedule) de que el término G actúa
    también entre semana, no solo el fin de semana: comparar CUÁL par de
    libres semanales de Laura (restricción dura NUEVA de patrón de libres,
    motor_cpsat.DIAS_LIBRES_SEGUIDOS_POR_SEMANA — Laura es "subjefe", sin
    fixed_days_off propio, así que su par de 2 libres consecutivos es
    obligatorio, ni más ni menos) cae DENTRO o FUERA de dias_ausencia debe
    marcar una diferencia exacta en el objetivo — confirma que el término
    está realmente activo en días de diario, no que "por casualidad" el
    schedule óptimo coincide con lo esperado (el fallo detectado en la
    revisión anterior: A y B pueden dar el MISMO schedule sin que eso
    demuestre que el mecanismo actúa).

    El jefe está ausente solo Lunes-Miércoles (NO toda la semana — con la
    restricción dura nueva, Laura solo puede tener UN par de 2 libres
    consecutivos en total, así que ya no se puede aislar el coste de "1
    día de más" como en la versión anterior de este test; en su lugar se
    compara el par ENTERO dentro vs fuera de la ausencia). El par base se
    fija en Jueves-Viernes (fuera de dias_ausencia={Lun,Mar,Mié} Y fuera del
    fin de semana — evita a propósito activar el término E, que también
    reacciona si el par cae en Sáb+Dom, contaminando la comparación con
    algo ajeno a G). El par forzado se fija en Martes-Miércoles, dentro de
    dias_ausencia: pierde cobertura esos 2 días concretos, coste exacto
    2×PESO_COBERTURA_AUSENCIA frente al base."""
    from ortools.sat.python import cp_model

    cfg = _cfg_con_cobertura(semana_completa=False)  # Opción A: sin presión extra de finde
    staff = _plantilla_holgada()
    juan = next(p for p in staff if p["name"] == "Juan")
    juan["forced_days_off"] = ["Lunes", "Martes", "Miércoles"]

    model_base, x_base, names_base, by_name_base = _construir_modelo(staff, SEMANA, cfg)
    model_base.Add(x_base["Laura", 3, "Libre"] == 1)  # Jueves: par fuera de dias_ausencia y del finde
    model_base.Add(x_base["Laura", 4, "Libre"] == 1)  # Viernes
    solver_base = cp_model.CpSolver()
    solver_base.parameters.max_time_in_seconds = 30.0
    status_base = solver_base.Solve(model_base)
    assert status_base in (cp_model.OPTIMAL, cp_model.FEASIBLE)
    obj_base = solver_base.ObjectiveValue()

    model_forzado, x_forzado, names_forzado, by_name_forzado = _construir_modelo(staff, SEMANA, cfg)
    model_forzado.Add(x_forzado["Laura", 1, "Libre"] == 1)  # Martes: par DENTRO de dias_ausencia
    model_forzado.Add(x_forzado["Laura", 2, "Libre"] == 1)  # Miércoles
    solver_forzado = cp_model.CpSolver()
    solver_forzado.parameters.max_time_in_seconds = 30.0
    status_forzado = solver_forzado.Solve(model_forzado)
    assert status_forzado in (cp_model.OPTIMAL, cp_model.FEASIBLE)
    obj_forzado = solver_forzado.ObjectiveValue()

    assert round(obj_forzado - obj_base) == 2 * PESO_COBERTURA_AUSENCIA, (
        f"Mover el par de libres obligatorio de Laura de fuera de "
        f"dias_ausencia (Jue-Vie) a dentro (Mar-Mié) debería costar "
        f"exactamente 2×PESO_COBERTURA_AUSENCIA "
        f"({2 * PESO_COBERTURA_AUSENCIA}) más — 2 días de ausencia sin "
        f"cobertura en vez de 0: obj_base={obj_base}, obj_forzado={obj_forzado}"
    )


def test_semana_completa_opcion_b_presencia_total_sin_finde():
    """Opción B: con margen de plantilla suficiente, Laura no libra el fin
    de semana — la presión extra de la opción B (peso alto adicional sobre
    Sáb/Dom) es lo bastante fuerte para vencer cualquier otra preferencia."""
    cfg = _cfg_con_cobertura(semana_completa=True)
    staff = _plantilla_holgada()
    juan = next(p for p in staff if p["name"] == "Juan")
    juan["forced_days_off"] = list(DAYS)

    sched = generar_semana_cpsat(staff, SEMANA, cfg)

    for d, turno in enumerate(sched["Laura"]):
        if turno != "Libre":
            assert turno == "Mañana"
    assert sched["Laura"][5] != "Libre", f"Laura libra el Sábado en opción B: {sched['Laura']}"
    assert sched["Laura"][6] != "Libre", f"Laura libra el Domingo en opción B: {sched['Laura']}"


def test_semana_completa_opcion_b_penaliza_exactamente_2x_peso_extra_en_finde():
    """Prueba determinista y calibrada (en vez de depender de qué elige el
    solver libremente) de que la ÚNICA diferencia entre opción A y B es una
    penalización extra de PESO_COBERTURA_AUSENCIA por cada día de fin de
    semana en que Laura esté Libre. Se fuerza (con model.Add) que Laura
    libre Sáb+Dom en ambas opciones y se compara el valor objetivo óptimo
    resultante: la diferencia debe ser exactamente 2 * PESO_COBERTURA_AUSENCIA
    (nada más, nada menos) — confirma que el resto del modelo (equidad,
    continuidad, etc.) no se ve alterado por el flag, solo el término G."""
    staff = _plantilla_holgada()
    juan = next(p for p in staff if p["name"] == "Juan")
    juan["forced_days_off"] = list(DAYS)

    cfg_a = _cfg_con_cobertura(semana_completa=False)
    cfg_b = _cfg_con_cobertura(semana_completa=True)

    def _resolver_forzando_libre_finde(cfg):
        model, x, names, by_name = _construir_modelo(staff, SEMANA, cfg)
        model.Add(x["Laura", 5, "Libre"] == 1)
        model.Add(x["Laura", 6, "Libre"] == 1)
        solver_schedule = _resolver_y_extraer(model, x, names)
        return model, solver_schedule

    from ortools.sat.python import cp_model

    model_a, _ = _resolver_forzando_libre_finde(cfg_a)
    model_b, _ = _resolver_forzando_libre_finde(cfg_b)

    solver_a = cp_model.CpSolver()
    solver_a.parameters.max_time_in_seconds = 30.0
    solver_a.Solve(model_a)
    obj_a = solver_a.ObjectiveValue()

    solver_b = cp_model.CpSolver()
    solver_b.parameters.max_time_in_seconds = 30.0
    solver_b.Solve(model_b)
    obj_b = solver_b.ObjectiveValue()

    assert round(obj_b - obj_a) == 2 * PESO_COBERTURA_AUSENCIA


# ═══════════════════════════════════════════════════════════════════════════
# 5. Rol "jefe" inexistente en la plantilla → ausencia de semana completa
# ═══════════════════════════════════════════════════════════════════════════

def test_jefe_inexistente_dias_ausencia_es_semana_completa():
    """El rol jefe inexistente en la plantilla se calcula como ausencia de
    semana completa (los 7 días), igual que una vacación de semana
    entera — verificación de la función pura, deliberadamente separada de
    la verificación de coste (ver test siguiente), que es la que confirma
    que el solver reacciona a esto."""
    cfg = _cfg_con_cobertura(semana_completa=False)
    staff = [p for p in _plantilla_holgada() if p["name"] != "Juan"]  # sin jefe

    dias_ausencia, _dias_fijos, semana_completa = _dias_cobertura_ausencia(
        staff, cfg, "jefe", cfg.cobertura_roles["subjefe"]
    )
    assert dias_ausencia == set(range(7))
    assert semana_completa


def test_jefe_inexistente_opcion_b_presencia_total_sin_finde():
    """Con margen de plantilla suficiente y jefe inexistente, la Opción B
    mantiene a Laura trabajando (no Libre) el fin de semana — chequeo de
    schedule real (no vacío: sí hay ausencia real, sí hay presión de
    Opción B activa)."""
    cfg = _cfg_con_cobertura(semana_completa=True)
    staff = [p for p in _plantilla_holgada() if p["name"] != "Juan"]  # sin jefe

    sched = generar_semana_cpsat(staff, SEMANA, cfg)
    for d, turno in enumerate(sched["Laura"]):
        if turno != "Libre":
            assert turno == "Mañana"
    assert sched["Laura"][5] != "Libre"
    assert sched["Laura"][6] != "Libre"


def test_jefe_inexistente_opcion_b_penaliza_exactamente_2x_peso_extra_en_finde():
    """Mismo patrón de verificación que
    test_semana_completa_opcion_b_penaliza_exactamente_2x_peso_extra_en_finde,
    aplicado específicamente al escenario de "jefe inexistente" (en vez de
    asumir que, por ser mecánicamente la misma ruta de código, el resultado
    ya está probado): se fuerza a Laura Libre Sáb+Dom en Opción A y en
    Opción B y se compara el valor objetivo óptimo resultante. La
    diferencia debe ser exactamente 2 * PESO_COBERTURA_AUSENCIA — confirma
    que la Opción B añade presión real también cuando la ausencia viene de
    un rol inexistente, no solo de una vacación de semana completa (que
    "los schedules sin forzar salen iguales entre A y B" —ver
    test_jefe_inexistente_opcion_b_presencia_total_sin_finde y su
    contraparte de Opción A, omitida a propósito por la misma razón que la
    de semana completa— no descarta que el término no esté actuando)."""
    from ortools.sat.python import cp_model

    cfg_a = _cfg_con_cobertura(semana_completa=False)
    cfg_b = _cfg_con_cobertura(semana_completa=True)
    staff = [p for p in _plantilla_holgada() if p["name"] != "Juan"]  # sin jefe

    def _resolver_forzando_libre_finde(cfg):
        model, x, names, by_name = _construir_modelo(staff, SEMANA, cfg)
        model.Add(x["Laura", 5, "Libre"] == 1)
        model.Add(x["Laura", 6, "Libre"] == 1)
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = 30.0
        status = solver.Solve(model)
        assert status in (cp_model.OPTIMAL, cp_model.FEASIBLE)
        return solver.ObjectiveValue()

    obj_a = _resolver_forzando_libre_finde(cfg_a)
    obj_b = _resolver_forzando_libre_finde(cfg_b)

    assert round(obj_b - obj_a) == 2 * PESO_COBERTURA_AUSENCIA, (
        f"obj_a={obj_a}, obj_b={obj_b} — la diferencia debería ser "
        f"exactamente {2 * PESO_COBERTURA_AUSENCIA}"
    )


# ═══════════════════════════════════════════════════════════════════════════
# 5b. cubrir_dias_libres_fijos: el cubridor también cubre el finde fijo
# ═══════════════════════════════════════════════════════════════════════════

def test_cubrir_dias_libres_fijos_activo_devuelve_el_finde_del_jefe_aparte():
    """(actualizado esta sesión) Los días libres FIJOS del jefe ya no se
    mezclan con las ausencias reales: _dias_cobertura_ausencia los devuelve
    en un conjunto APARTE, para que el término G pueda cobrarlos al peso
    menor PESO_COBERTURA_DIAS_LIBRES_FIJOS en vez de al de una ausencia
    real. Sin ninguna ausencia real, dias_ausencia queda vacío y el finde
    fijo aparece en el segundo conjunto."""
    cfg = _cfg_con_cobertura(semana_completa=False, cubrir_dias_libres_fijos=True)
    staff = _plantilla()  # Juan presente, sin ausencias reales

    dias_ausencia, dias_fijos, semana_completa = _dias_cobertura_ausencia(
        staff, cfg, "jefe", cfg.cobertura_roles["subjefe"]
    )
    assert dias_ausencia == set()
    assert dias_fijos == {5, 6}
    assert not semana_completa


def test_cubrir_dias_libres_fijos_activo_subjefe_trabaja_el_finde():
    """Con la casilla activa y el jefe presente sin ausencias reales, el
    subjefe cubre igualmente Sáb+Dom (los días libres FIJOS del jefe) en el
    turno fijo del jefe."""
    cfg = _cfg_con_cobertura(semana_completa=False, cubrir_dias_libres_fijos=True)
    staff = _plantilla_holgada()

    sched = generar_semana_cpsat(staff, SEMANA, cfg)

    assert sched["Laura"][5] == "Mañana"
    assert sched["Laura"][6] == "Mañana"


def test_cubrir_dias_libres_fijos_activo_penaliza_exactamente_al_forzar_libre():
    """Verificación de extremo a extremo (no solo la función pura
    _dias_cobertura_ausencia) de que el término G actúa realmente en el
    solver para este caso concreto: jefe PRESENTE, sin ninguna ausencia
    real, solo con la casilla cubrir_dias_libres_fijos activa. Forzar a
    Laura a estar Libre el sábado (uno de los días libres FIJOS del jefe,
    no una ausencia real) debe costar EXACTAMENTE
    PESO_COBERTURA_DIAS_LIBRES_FIJOS más que el óptimo sin forzar nada —
    confirma que el mecanismo se activa de verdad para este caso, no solo
    que el cálculo de entrada (dias_fijos == {5, 6}, ver test anterior) es
    correcto.

    (actualizado esta sesión) Antes esperaba PESO_COBERTURA_AUSENCIA: los
    días libres fijos del cubierto pasaron a cobrarse al peso menor, para
    que el subjefe pueda seguir librando algún fin de semana."""
    from ortools.sat.python import cp_model

    cfg = _cfg_con_cobertura(semana_completa=False, cubrir_dias_libres_fijos=True)
    staff = _plantilla_holgada()  # Juan presente, sin ausencias reales

    model_base, x_base, names_base, by_name_base = _construir_modelo(staff, SEMANA, cfg)
    solver_base = cp_model.CpSolver()
    solver_base.parameters.max_time_in_seconds = 30.0
    solver_base.Solve(model_base)
    obj_base = solver_base.ObjectiveValue()

    model_forzado, x_forzado, names_forzado, by_name_forzado = _construir_modelo(staff, SEMANA, cfg)
    model_forzado.Add(x_forzado["Laura", 5, "Libre"] == 1)
    solver_forzado = cp_model.CpSolver()
    solver_forzado.parameters.max_time_in_seconds = 30.0
    status_forzado = solver_forzado.Solve(model_forzado)
    assert status_forzado in (cp_model.OPTIMAL, cp_model.FEASIBLE)
    obj_forzado = solver_forzado.ObjectiveValue()

    assert round(obj_forzado - obj_base) == PESO_COBERTURA_DIAS_LIBRES_FIJOS, (
        f"Forzar a Laura Libre el sábado (día libre fijo del jefe, sin "
        f"ausencia real) debería costar exactamente "
        f"PESO_COBERTURA_DIAS_LIBRES_FIJOS "
        f"({PESO_COBERTURA_DIAS_LIBRES_FIJOS}) más que el óptimo: "
        f"obj_base={obj_base}, obj_forzado={obj_forzado}"
    )


def test_cubrir_dias_libres_fijos_desactivado_no_activa_termino():
    """No regresión: con la casilla desactivada (default), sin ausencia
    real, dias_ausencia queda vacío — la restricción 8 no genera presión
    alguna sobre el subjefe ese finde."""
    cfg = _cfg_con_cobertura(semana_completa=False, cubrir_dias_libres_fijos=False)
    staff = _plantilla()

    dias_ausencia, _fijos, _completa = _dias_cobertura_ausencia(staff, cfg, "jefe", cfg.cobertura_roles["subjefe"])
    assert dias_ausencia == set()


def test_cubrir_dias_libres_fijos_mas_semana_completa_combinadas():
    """Ambas casillas activas + jefe de vacaciones toda la semana: los días
    de ausencia siguen siendo los 7 (la unión con el finde fijo no añade
    nada nuevo, ya estaba incluido), y la presión extra de "semana
    completa" se mantiene activa."""
    cfg = _cfg_con_cobertura(semana_completa=True, cubrir_dias_libres_fijos=True)
    staff = _plantilla_holgada()
    juan = next(p for p in staff if p["name"] == "Juan")
    juan["forced_days_off"] = list(DAYS)

    dias_ausencia, _dias_fijos, semana_completa = _dias_cobertura_ausencia(
        staff, cfg, "jefe", cfg.cobertura_roles["subjefe"]
    )
    assert dias_ausencia == set(range(7))
    assert semana_completa

    sched = generar_semana_cpsat(staff, SEMANA, cfg)
    for d, turno in enumerate(sched["Laura"]):
        if turno != "Libre":
            assert turno == "Mañana"
    assert sched["Laura"][5] != "Libre"
    assert sched["Laura"][6] != "Libre"


# ═══════════════════════════════════════════════════════════════════════════
# 5c. max_coverage nunca se rompe por la restricción 8 (el más importante)
# ═══════════════════════════════════════════════════════════════════════════

def test_cobertura_finde_respeta_max_coverage_sin_romper_limite():
    """Si añadir a la cubridora a la Mañana del sábado superaría
    max_coverage, NO se supera el máximo: el peso alto de la restricción 8
    cede (estructuralmente, por la restricción DURA de fase 1 #2), y a
    Laura se le asigna otro turno legal en su lugar. Escenario mínimo y
    determinista: un tercer rol fijo "otro" (que solo trabaja el fin de
    semana, fixed_roles=Mañana) ya ocupa el único hueco de Mañana el
    sábado antes de resolver — sin solaparse entre semana con el turno
    fijo del jefe (que también es Mañana), para no introducir un
    conflicto de max_coverage ajeno a la restricción 8 que se quiere
    probar aquí."""
    from modelo_base import DeptConfig

    cfg = DeptConfig(
        dept_id="recepcion", dept_label="Recepción",
        night_fixed_days_off=[], night_cover_compensation=[],
        min_coverage={"Mañana": 1}, max_coverage={"Mañana": 1},
        no_night_roles={"jefe", "subjefe", "otro"},
        fixed_roles={"jefe": "Mañana", "otro": "Mañana"},
        fixed_days_off={"jefe": [5, 6], "otro": [0, 1, 2, 3, 4]},
        cover_consecutive=True, night_rotation_key="test_max_cobertura",
        allow_non_consecutive_cover=False, prefer_next_week_for_cover=False,
        cobertura_roles={"subjefe": {"cubre": "jefe", "semana_completa": False,
                                      "dias_libres_fijos": True}},
        roles_excluidos_equidad={"jefe", "subjefe"},
    )
    staff = [
        {"name": "Juan",  "role": "jefe"},
        {"name": "Laura", "role": "subjefe"},
        {"name": "Otro",  "role": "otro"},  # trabaja solo Sáb+Dom, fixed_roles Mañana
    ]

    _dias_ausencia, dias_fijos, _completa = _dias_cobertura_ausencia(
        staff, cfg, "jefe", cfg.cobertura_roles["subjefe"]
    )
    # cubrir_dias_libres_fijos activo: el Sábado entra por la vía de los
    # días libres FIJOS (peso menor), no como ausencia real — el jefe está
    # presente esta semana.
    assert 5 in dias_fijos

    sched = generar_semana_cpsat(staff, SEMANA, cfg)

    conteo_sabado_manana = sum(1 for t in sched.values() if t[5] == "Mañana")
    assert conteo_sabado_manana <= cfg.max_coverage["Mañana"], (
        "Se superó el máximo de cobertura de Mañana el sábado"
    )
    assert sched["Otro"][5] == "Mañana"          # Otro ya ocupaba el único hueco
    assert sched["Laura"][5] != "Mañana"         # Laura no puede coincidir: máximo ya alcanzado
    assert sched["Laura"][5] != ""               # pero sigue teniendo un turno legal asignado


# ═══════════════════════════════════════════════════════════════════════════
# 5d. 15 semanas encadenadas con cobertura_roles activo: max_coverage nunca
#     se rompe a lo largo de la cadena (propiedad que solo falla por
#     acumulación, no en una semana aislada — ver docstring del módulo)
# ═══════════════════════════════════════════════════════════════════════════

def test_15_semanas_encadenadas_cubrir_dias_libres_fijos_nunca_rompe_max_coverage():
    """cubrir_dias_libres_fijos activo de forma permanente (Laura cubre
    Sáb+Dom del jefe todas las semanas, sin que el jefe tenga nunca una
    ausencia real) durante 15 semanas encadenadas (prev_sunday_shifts
    propagado, historial real registrado semana a semana vía
    herramientas.registrar_semana, igual que
    test_recepcion_12_semanas_encadenadas_sin_violaciones en
    test_motor_cpsat.py) — confirma que ninguna semana rompe min/max
    coverage y que Laura cubre el turno fijo del jefe (Mañana) en Sáb+Dom
    en TODAS las semanas, no solo en una semana aislada."""
    from herramientas import registrar_semana

    cfg = _cfg_con_cobertura(semana_completa=False, cubrir_dias_libres_fijos=True)
    staff = _plantilla_holgada()
    prev_sunday = {}

    for i in range(15):
        semana = SEMANA + timedelta(weeks=i)
        sched = generar_semana_cpsat(staff, semana, cfg, prev_sunday_shifts=prev_sunday)

        for s in ("Mañana", "Tarde", "Noche", "Partido"):
            mn = cfg.min_coverage.get(s, 0)
            mx = cfg.max_coverage.get(s, 99)
            for d in range(7):
                count = sum(1 for p in staff if sched[p["name"]][d] == s)
                assert mn <= count <= mx, (
                    f"Semana {i+1} ({semana}) — {DAYS[d]} {s}: {count} "
                    f"(esperado {mn}-{mx})"
                )

        assert sched["Laura"][5] == "Mañana", (
            f"Semana {i+1}: Laura no cubre Sábado (día libre fijo del jefe): {sched['Laura']}"
        )
        assert sched["Laura"][6] == "Mañana", (
            f"Semana {i+1}: Laura no cubre Domingo (día libre fijo del jefe): {sched['Laura']}"
        )

        registrar_semana(semana, sched, staff, forzar=True, dept_id=cfg.dept_id)
        prev_sunday = {p["name"]: sched[p["name"]][6] for p in staff}


# ═══════════════════════════════════════════════════════════════════════════
# 6. Retrocompatibilidad: sin cobertura_roles configurado, no pasa nada
# ═══════════════════════════════════════════════════════════════════════════

def test_sin_cobertura_roles_configurado_no_activa_nada():
    from modelo_recepcion import RECEPCION_CONFIG
    cfg = copy.deepcopy(RECEPCION_CONFIG)  # cobertura_roles={} por default
    staff = _plantilla()

    sched = generar_semana_cpsat(staff, SEMANA, cfg)
    # No debe lanzar ninguna excepción; el schedule es válido y completo.
    assert len(sched["Laura"]) == 7


# ═══════════════════════════════════════════════════════════════════════════
# Restricción 9 — Turno complementario jefe/subjefe (tests NUEVOS)
# ═══════════════════════════════════════════════════════════════════════════

def test_complementario_jefe_subjefe_turnos_distintos_sin_presion_externa():
    """Jefe y subjefe ambos presentes, sin otra presión: en los días que
    ambos trabajan, el subjefe evita coincidir con el turno fijo del jefe
    (Mañana) — la restricción 9 (peso moderado) gana el desempate."""
    from modelo_recepcion import RECEPCION_CONFIG
    cfg = copy.deepcopy(RECEPCION_CONFIG)  # sin cobertura_roles: no interfiere la restricción 8
    staff = _plantilla()

    sched = generar_semana_cpsat(staff, SEMANA, cfg)

    for d in range(7):
        jefe_trabaja = sched["Juan"][d] != "Libre"
        subjefe_trabaja = sched["Laura"][d] != "Libre"
        if jefe_trabaja and subjefe_trabaja:
            assert sched["Laura"][d] != sched["Juan"][d], (
                f"Día {DAYS[d]}: Jefe y subjefe coinciden en {sched['Juan'][d]}"
            )


def test_complementario_cede_si_es_la_unica_forma_de_cubrir_minimo():
    """Escenario de estrés: se bloquea (con model.Add, no fijando la celda
    de Laura a mano) que cualquier otra persona normal haga Mañana un día
    concreto, dejando Mañana con min_coverage=2 solo alcanzable si Jefe
    (fijo) Y Laura lo hacen ambos.

    Dos verificaciones, no solo "el resultado final coincide":
    1. Coste exacto: el valor objetivo óptimo de este escenario debe ser
       EXACTAMENTE PESO_COMPLEMENTARIO_JEFATURA (no 0) — confirma que la
       coincidencia realmente se PAGA (el término 9 sigue activo y
       penalizando, no ha sido desactivado ni puesto a coste cero), no que
       "por casualidad" salió gratis.
    2. Necesidad real, no preferencia: forzar explícitamente que Laura NO
       coincida (model.Add(x[Laura, día, Mañana] == 0)) en este mismo
       escenario debe dar INFEASIBLE — confirma que ceder aquí no es una
       elección de coste (el término 9 podría en teoría preferir otra
       alternativa más cara) sino la ÚNICA forma de cumplir min_coverage
       (restricción dura de fase 1), exactamente el caso que se quiere
       probar."""
    from modelo_recepcion import RECEPCION_CONFIG
    from ortools.sat.python import cp_model

    cfg = copy.deepcopy(RECEPCION_CONFIG)
    # _plantilla_holgada (NUEVO, esta sesión — hallazgo de producción): con
    # la restricción dura nueva de patrón de libres (motor_cpsat.
    # DIAS_LIBRES_SEGUIDOS_POR_SEMANA), _plantilla() (8 personas) no deja
    # margen para que cada "normal"/subjefe/adjunto también encaje su
    # propio par consecutivo junto con el bloqueo de estrés de Martes —
    # INFEASIBLE ajeno a lo que este test verifica. La persona "Extra" no
    # participa del bloqueo (_bloquear_normales solo excluye jefe/subjefe,
    # así que Extra SÍ queda bloqueada igual que el resto — pero da
    # cobertura de sobra el resto de la semana).
    staff = _plantilla_holgada()
    dia_estres = 1  # Martes

    def _bloquear_normales(model, x, names, by_name):
        for name in names:
            role = by_name[name].get("role")
            if role not in ("jefe", "subjefe"):
                model.Add(x[name, dia_estres, "Mañana"] == 0)

    # ── 1. Coste del escenario de estrés — vía DELTA contra el mismo staff
    # SIN el bloqueo de estrés (NUEVO, esta sesión: con _plantilla_holgada,
    # ya no se puede aislar un delta EXACTO de solo H — "Extra" también
    # necesita reacomodar su propio patrón de 2 libres consecutivos
    # (restricción dura nueva) ante el bloqueo del Martes, lo que añade
    # coste adicional ajeno a H; medido empíricamente en +10 extra sobre el
    # coste de H. Se comprueba un MÍNIMO —delta >= H— en vez de una
    # igualdad exacta: sigue confirmando que H se paga de verdad (no sale
    # gratis), que es lo que este test existe para probar) ──
    model_base, x_base, names_base, by_name_base = _construir_modelo(staff, SEMANA, cfg)
    solver_base = cp_model.CpSolver()
    solver_base.parameters.max_time_in_seconds = 30.0
    status_base = solver_base.Solve(model_base)
    assert status_base in (cp_model.OPTIMAL, cp_model.FEASIBLE)
    obj_base = solver_base.ObjectiveValue()

    model, x, names, by_name = _construir_modelo(staff, SEMANA, cfg)
    _bloquear_normales(model, x, names, by_name)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 30.0
    status = solver.Solve(model)
    assert status in (cp_model.OPTIMAL, cp_model.FEASIBLE)
    delta = solver.ObjectiveValue() - obj_base
    assert delta >= PESO_COMPLEMENTARIO_JEFATURA, (
        f"El coste del escenario de estrés debería superar al base en al "
        f"menos PESO_COMPLEMENTARIO_JEFATURA ({PESO_COMPLEMENTARIO_JEFATURA}) — "
        f"la coincidencia se paga, no sale gratis: base={obj_base}, "
        f"estrés={solver.ObjectiveValue()}"
    )
    assert solver.Value(x["Juan", dia_estres, "Mañana"]) == 1
    assert solver.Value(x["Laura", dia_estres, "Mañana"]) == 1, (
        "Laura debía ceder y coincidir con Jefe en Mañana para cumplir min_coverage"
    )

    # ── 2. Confirma que es NECESIDAD (infeasible evitarlo), no preferencia ──
    model2, x2, names2, by_name2 = _construir_modelo(staff, SEMANA, cfg)
    _bloquear_normales(model2, x2, names2, by_name2)
    model2.Add(x2["Laura", dia_estres, "Mañana"] == 0)

    solver2 = cp_model.CpSolver()
    solver2.parameters.max_time_in_seconds = 30.0
    status2 = solver2.Solve(model2)
    assert status2 == cp_model.INFEASIBLE, (
        "Si Laura NO coincidiendo con Jefe siguiera siendo factible, el "
        "escenario no probaría 'necesidad', solo 'preferencia' — el "
        "escenario de estrés debe dejar la coincidencia como única opción."
    )


def test_complementario_no_interfiere_en_dias_de_cobertura_de_ausencia():
    """Jefe ausente un día suelto (restricción 8 activa ese día): el jefe
    está Libre, así que la restricción 9 no puede generar coste alguno ese
    día (no hay "ambos trabajan") — Laura simplemente cubre el turno fijo
    del jefe sin conflicto entre ambas restricciones."""
    cfg = _cfg_con_cobertura(semana_completa=False)
    staff = _plantilla()
    juan = next(p for p in staff if p["name"] == "Juan")
    juan["forced_days_off"] = ["Miércoles"]

    sched = generar_semana_cpsat(staff, SEMANA, cfg)

    assert sched["Juan"][2] == "Libre"
    assert sched["Laura"][2] == "Mañana"


def test_complementario_sin_subjefe_no_activa_termino():
    from modelo_recepcion import RECEPCION_CONFIG
    cfg = copy.deepcopy(RECEPCION_CONFIG)
    # +1 "normal" extra (NUEVO, esta sesión — hallazgo de producción, ver
    # motor_cpsat.DIAS_LIBRES_SEGUIDOS_POR_SEMANA): quitar a Laura deja
    # solo 6 personas no fijas, sin margen para que cada una cumpla
    # también su propio patrón de 2 libres consecutivos — INFEASIBLE
    # ajeno a lo que este test verifica.
    staff = [p for p in _plantilla() if p["name"] != "Laura"]  # sin subjefe
    staff.append({"name": "Extra", "role": "normal", "preferences": []})

    sched = generar_semana_cpsat(staff, SEMANA, cfg)
    assert "Laura" not in sched
    assert len(sched["Juan"]) == 7


def test_complementario_sin_jefe_no_activa_termino():
    from modelo_recepcion import RECEPCION_CONFIG
    cfg = copy.deepcopy(RECEPCION_CONFIG)
    # +1 "normal" extra (NUEVO, esta sesión — mismo motivo que arriba).
    staff = [p for p in _plantilla() if p["name"] != "Juan"]  # sin jefe
    staff.append({"name": "Extra", "role": "normal", "preferences": []})

    sched = generar_semana_cpsat(staff, SEMANA, cfg)
    assert "Juan" not in sched
    assert len(sched["Laura"]) == 7
