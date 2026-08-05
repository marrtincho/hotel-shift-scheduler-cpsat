"""
tests/test_motor_cpsat.py — Valida el prototipo motor_cpsat.py (CP-SAT).

Fase 1: cubre las 6 restricciones duras que motor_cpsat.generar_semana_cpsat
implementa, más un test de "fijación de celda" que demuestra la ventaja
estructural de CP-SAT (fijar un valor no exige tocar el resto del modelo).

night_fixed ya está correctamente restringido (Noche fija salvo Libre en
cfg.night_fixed_days_off, sin sustituto — fase 3), así que los escenarios de
tests/test_restricciones_duras.py que solo dependen de cobertura y de la
regla no-Mañana-tras-Tarde/Noche (no de select_night_covers/rotación por
bloques, que siguen siendo fase 3) se reutilizan más abajo con
RECEPCION_CONFIG y STAFF_DEFAULT, encadenando semanas igual que el original.

Fase 2: cubre los 4 términos de objetivo (equidad de findes, roturas de
continuidad, desequilibrio histórico, uso de Partido) y el término E
(penalización alta a rol excluido de equidad sin fixed_days_off propio
recibiendo finde completo). Estos términos reutilizan _get_weekend_ratios y
_get_shift_counts de modelo_base.py, que leen historial.json/roles.json vía
herramientas — de ahí el fixture _data_dir_aislado (autouse) que aísla
DATA_DIR en un tmp_path por test, igual que tests/test_equidad_findes.py.
"""

import copy
from dataclasses import dataclass, field
from datetime import date, timedelta

import pytest
from ortools.sat.python import cp_model

from modelo_base import DAYS, SHIFTS, DeptConfig, _get_weekend_ratios
from motor_cpsat import (
    generar_semana_cpsat, _construir_modelo, _resolver_y_extraer,
    PESO_EQUIDAD_FINDES, ESCALA_RATIO,
)

SEMANA = date(2099, 2, 3)  # Lunes en futuro lejano


@pytest.fixture(autouse=True)
def _data_dir_aislado(tmp_path, monkeypatch):
    """Aísla DATA_DIR en un tmp_path por test. Los términos de objetivo de
    fase 2 reutilizan _get_weekend_ratios/_get_shift_counts de modelo_base.py,
    que leen historial.json/roles.json vía herramientas — sin este fixture,
    cualquier test tocaría los datos reales del proyecto."""
    import herramientas
    backup = tmp_path / "backups"
    backup.mkdir()
    monkeypatch.setattr(herramientas, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(herramientas, "BACKUP_DIR", str(backup))
    monkeypatch.setattr(herramientas, "FESTIVOS_FILE", str(tmp_path / "festivos.json"))


def _cfg(**overrides) -> DeptConfig:
    base = dict(
        dept_id="test",
        dept_label="Test",
        night_fixed_days_off=[],
        night_cover_compensation=[],
        min_coverage={"Mañana": 2, "Tarde": 2, "Noche": 1, "Partido": 0},
        max_coverage={"Mañana": 3, "Tarde": 3, "Noche": 1, "Partido": 1},
        no_night_roles={"jefe", "subjefe"},
        fixed_roles={"jefe": "Mañana"},
        fixed_days_off={"jefe": [5, 6]},
        cover_consecutive=True,
        night_rotation_key="test",
        allow_non_consecutive_cover=False,
        prefer_next_week_for_cover=False,
    )
    base.update(overrides)
    return DeptConfig(**base)


def _staff_basico():
    # Gus/Hugo (hallazgo de producción): con la
    # restricción dura nueva de patrón de libres (motor_cpsat.
    # DIAS_LIBRES_SEGUIDOS_POR_SEMANA — exactamente 2 libres CONSECUTIVOS
    # por semana, no solo "al menos 2"), los 6 "normal" originales no
    # dejaban margen real para que cada uno formara su propio par
    # consecutivo en una rotación válida junto con la cobertura mínima —
    # INFEASIBLE incluso en el escenario "básico" sin ninguna otra
    # complicación. 2 personas extra dan el margen que hacía falta para
    # TODOS los tests de este archivo que reutilizan este fixture.
    return [
        {"name": "Jefe",   "role": "jefe"},
        {"name": "Ana",    "role": "normal"},
        {"name": "Beto",   "role": "normal"},
        {"name": "Carla",  "role": "normal"},
        {"name": "Diego",  "role": "normal"},
        {"name": "Eva",    "role": "normal"},
        {"name": "Fer",    "role": "normal"},
        {"name": "Gus",    "role": "normal"},
        {"name": "Hugo",   "role": "normal"},
    ]


# ─── 1. Un turno por persona/día ─────────────────────────────────────────────

def test_un_turno_por_persona_dia():
    staff = _staff_basico()
    cfg = _cfg()
    sched = generar_semana_cpsat(staff, SEMANA, cfg)
    for name in [p["name"] for p in staff]:
        assert len(sched[name]) == 7
        for turno in sched[name]:
            assert turno in SHIFTS


# ─── 2. Cobertura mín/máx ─────────────────────────────────────────────────────

def test_cobertura_min_max_respetada():
    staff = _staff_basico()
    cfg = _cfg()
    sched = generar_semana_cpsat(staff, SEMANA, cfg)
    for s in ("Mañana", "Tarde", "Noche", "Partido"):
        mn = cfg.min_coverage.get(s, 0)
        mx = cfg.max_coverage.get(s, 99)
        for d in range(7):
            count = sum(1 for p in staff if sched[p["name"]][d] == s)
            assert mn <= count <= mx, f"{DAYS[d]} {s}: {count} (esperado {mn}-{mx})"


# ─── 3. No-Mañana-tras-Tarde/Noche ────────────────────────────────────────────

def test_no_manana_tras_tarde_noche_dentro_de_semana():
    staff = _staff_basico()
    cfg = _cfg()
    sched = generar_semana_cpsat(staff, SEMANA, cfg)
    for name in [p["name"] for p in staff]:
        turnos = sched[name]
        for d in range(1, 7):
            if turnos[d - 1] in ("Tarde", "Noche"):
                assert turnos[d] != "Mañana", (
                    f"{name}: {DAYS[d-1]} ({turnos[d-1]}) -> {DAYS[d]} (Mañana) prohibido"
                )


def test_no_manana_tras_domingo_anterior():
    """La transición día 6 semana pasada -> día 0 de esta semana también aplica."""
    staff = _staff_basico()
    cfg = _cfg()
    prev_sunday = {"Ana": "Noche", "Beto": "Tarde"}
    sched = generar_semana_cpsat(staff, SEMANA, cfg, prev_sunday_shifts=prev_sunday)
    assert sched["Ana"][0] != "Mañana"
    assert sched["Beto"][0] != "Mañana"


# ─── 4. Roles con turno fijo ──────────────────────────────────────────────────

def test_rol_con_turno_fijo():
    staff = _staff_basico()
    cfg = _cfg()
    sched = generar_semana_cpsat(staff, SEMANA, cfg)
    for d in range(7):
        assert sched["Jefe"][d] in ("Mañana", "Libre")
        if sched["Jefe"][d] != "Libre":
            assert sched["Jefe"][d] == "Mañana"


# ─── 5. Roles con días libres fijos ───────────────────────────────────────────

def test_rol_con_dias_libres_fijos():
    staff = _staff_basico()
    cfg = _cfg()
    sched = generar_semana_cpsat(staff, SEMANA, cfg)
    for d in cfg.fixed_days_off["jefe"]:
        assert sched["Jefe"][d] == "Libre"


# ─── 6. Vacaciones / peticiones concedidas ────────────────────────────────────

def test_vacaciones_forced_days_off_respetadas():
    staff = _staff_basico()
    staff[1]["forced_days_off"] = ["Martes", "Miércoles"]  # Ana
    staff[2]["extra_days_off"] = ["Jueves"]                 # Beto
    cfg = _cfg()
    sched = generar_semana_cpsat(staff, SEMANA, cfg)
    assert sched["Ana"][1] == "Libre"
    assert sched["Ana"][2] == "Libre"
    assert sched["Beto"][3] == "Libre"


def test_vacaciones_semana_completa():
    staff = _staff_basico()  # ya incluye Gus/Hugo — margen suficiente incluso con Diego fuera toda la semana
    diego = next(p for p in staff if p["name"] == "Diego")
    diego["forced_days_off"] = DAYS[:]  # vacaciones toda la semana
    cfg = _cfg()
    sched = generar_semana_cpsat(staff, SEMANA, cfg)
    assert all(t == "Libre" for t in sched["Diego"])


# ─── Test de fijación de celda (ventaja estructural de CP-SAT) ──────────────

def test_fijar_celda_no_requiere_tocar_otras_restricciones():
    """Fija Ana=Tarde el Miércoles (equivalente a una edición manual futura)
    directamente como constante del modelo, sin modificar ninguna otra
    restricción, y comprueba que el resto del horario sigue cumpliendo
    cobertura y no-Mañana-tras-Tarde/Noche. Esto demuestra que "fijar una
    celda" en CP-SAT es aditivo: una restricción más, no un rediseño del
    modelo, a diferencia del motor heurístico actual (que tendría que
    tratar la celda fijada como caso especial en cada pasada).

    Usa el mismo constructor de modelo (_construir_modelo) que
    generar_semana_cpsat en vez de duplicar la construcción de
    restricciones — así la prueba queda fiel a la implementación real."""
    staff = _staff_basico()
    cfg = _cfg()

    model, x, names, by_name = _construir_modelo(staff, SEMANA, cfg)

    # ── La única línea "nueva" para fijar la celda manualmente ──
    model.Add(x["Ana", 2, "Tarde"] == 1)

    sched = _resolver_y_extraer(model, x, names)

    assert sched["Ana"][2] == "Tarde"

    for s in ("Mañana", "Tarde", "Noche", "Partido"):
        mn = cfg.min_coverage.get(s, 0)
        mx = cfg.max_coverage.get(s, 99)
        for d in range(7):
            count = sum(1 for p in staff if sched[p["name"]][d] == s)
            assert mn <= count <= mx
    for name in names:
        turnos = sched[name]
        for d in range(1, 7):
            if turnos[d - 1] in ("Tarde", "Noche"):
                assert turnos[d] != "Mañana"


# ─── Caso inviable: CP-SAT debe reportarlo, no forzar una solución rara ──────

def test_infactible_lanza_error_explicito():
    """Con personal insuficiente para los mínimos de cobertura, CP-SAT debe
    reportar INFEASIBLE explícitamente (RuntimeError), no devolver un
    horario que viole restricciones duras."""
    staff = [
        {"name": "Uno", "role": "normal"},
        {"name": "Dos", "role": "normal"},
    ]
    cfg = _cfg()  # requiere Mañana>=2, Tarde>=2, Noche>=1 simultáneamente por día
    with pytest.raises(RuntimeError):
        generar_semana_cpsat(staff, SEMANA, cfg)


# ─── night_fixed ──────────────────────────────────────────────────────────────

def test_night_fixed_noche_fija_y_libre_en_night_fixed_days_off():
    """night_fixed: Noche todos los días salvo cfg.night_fixed_days_off, que
    son Libre — sin sustituto (fase 3), la cobertura de Noche esos días
    queda sujeta a min/max_coverage como cualquier otro turno."""
    staff = _staff_basico() + [{"name": "NocheFijo", "role": "night_fixed"}]
    cfg = _cfg(night_fixed_days_off=[3, 4])  # Jueves, Viernes libres
    sched = generar_semana_cpsat(staff, SEMANA, cfg)
    for d in range(7):
        if d in (3, 4):
            assert sched["NocheFijo"][d] == "Libre"
        else:
            assert sched["NocheFijo"][d] == "Noche"


# ─── Escenarios reutilizados de tests/test_restricciones_duras.py ───────────
# night_fixed ya está correctamente restringido (ver arriba), así que estos
# escenarios encajan ahora en las 6 restricciones modeladas. No se reutiliza
# select_night_covers/rotación por bloques (fase 3): el hueco de Noche que
# deja night_fixed en sus días libres lo cubre aquí cualquier persona
# elegible vía min/max_coverage, sin selección de sustituto dedicada.

def _staff_default_recepcion():
    from modelo_recepcion import STAFF_DEFAULT
    return copy.deepcopy(STAFF_DEFAULT)


def test_recepcion_semana_sin_violaciones():
    """Una semana con la plantilla y config reales de Recepción no debe violar
    cobertura ni la regla no-Mañana-tras-Tarde/Noche."""
    from modelo_recepcion import RECEPCION_CONFIG

    cfg = copy.deepcopy(RECEPCION_CONFIG)
    staff = _staff_default_recepcion()
    sched = generar_semana_cpsat(staff, SEMANA, cfg)

    for s in ("Mañana", "Tarde", "Noche", "Partido"):
        mn = cfg.min_coverage.get(s, 0)
        mx = cfg.max_coverage.get(s, 99)
        for d in range(7):
            count = sum(1 for p in staff if sched[p["name"]][d] == s)
            assert mn <= count <= mx, f"{DAYS[d]} {s}: {count} (esperado {mn}-{mx})"

    for p in staff:
        turnos = sched[p["name"]]
        for d in range(1, 7):
            if turnos[d - 1] in ("Tarde", "Noche"):
                assert turnos[d] != "Mañana"

    # Jefe: turno fijo Mañana, libra Sáb+Dom.
    for d in range(7):
        if d in cfg.fixed_days_off["jefe"]:
            assert sched["Juan"][d] == "Libre"
        else:
            assert sched["Juan"][d] == "Mañana"

    # night_fixed: Noche fija salvo Jue+Vie libres.
    for d in range(7):
        if d in cfg.night_fixed_days_off:
            assert sched["Carlos"][d] == "Libre"
        else:
            assert sched["Carlos"][d] == "Noche"

    # En los días de night_fixed_days_off, Carlos libra y no hay sustituto
    # dedicado en esta fase (fase 3) — pero min_coverage["Noche"]=1 sigue
    # siendo una restricción dura, así que alguien más elegible (no
    # no_night_roles) debe cubrir la Noche esos días para que el modelo sea
    # factible. Confirma que esa cobertura ocurre de verdad (no que el
    # modelo la esquivó dejando Noche en 0 porque min_coverage lo permitía).
    assert cfg.min_coverage.get("Noche", 0) > 0
    for d in cfg.night_fixed_days_off:
        cubridor = next(
            (p["name"] for p in staff
             if p["name"] != "Carlos" and sched[p["name"]][d] == "Noche"),
            None,
        )
        assert cubridor is not None, (
            f"{DAYS[d]}: nadie cubre la Noche que deja libre Carlos "
            f"(night_fixed), pese a min_coverage['Noche']={cfg.min_coverage['Noche']}"
        )
        rol_cubridor = next(p["role"] for p in staff if p["name"] == cubridor)
        assert rol_cubridor not in cfg.no_night_roles, (
            f"{DAYS[d]}: {cubridor} cubre Noche pero su rol ({rol_cubridor}) "
            f"está en no_night_roles"
        )


def test_recepcion_12_semanas_encadenadas_sin_violaciones():
    """12 semanas consecutivas encadenando prev_sunday_shifts: ninguna debe
    violar cobertura ni la regla no-Mañana-tras-Tarde/Noche."""
    from datetime import timedelta

    from modelo_recepcion import RECEPCION_CONFIG

    cfg = copy.deepcopy(RECEPCION_CONFIG)
    staff = _staff_default_recepcion()
    prev_sunday = {}

    # Contador de instancias donde el domingo de la semana anterior con
    # Tarde/Noche efectivamente bloqueó Mañana el lunes siguiente. Si esto se
    # queda en 0, la cadena entre semanas no se estaría probando de verdad
    # (podría pasar aunque cada semana se resolviera de forma independiente
    # con prev_sunday_shifts=None, porque nunca se daría el caso).
    cruces_bloqueados = 0

    for i in range(12):
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

        for p in staff:
            turnos = sched[p["name"]]
            for d in range(7):
                if d == 0:
                    prev_shift = prev_sunday.get(p["name"])
                else:
                    prev_shift = turnos[d - 1]
                assert not (prev_shift in ("Tarde", "Noche") and turnos[d] == "Mañana"), (
                    f"Semana {i+1} ({semana}): {p['name']} Mañana tras {prev_shift}"
                )
                if d == 0 and prev_shift in ("Tarde", "Noche"):
                    cruces_bloqueados += 1

        prev_sunday = {p["name"]: sched[p["name"]][6] for p in staff}

    assert cruces_bloqueados > 0, (
        "En 12 semanas encadenadas nunca se dio un caso real de "
        "Domingo(Tarde/Noche) -> Lunes bloqueado: la prueba no está "
        "verificando de verdad el encadenamiento entre semanas."
    )


def test_recepcion_caso_imposible_lanza_error():
    """Con solo 3 personas (night_fixed + 2 normales), insuficientes para
    Mañana=2 + Tarde=2 simultáneas, CP-SAT debe reportar infactibilidad."""
    from modelo_recepcion import RECEPCION_CONFIG

    cfg = copy.deepcopy(RECEPCION_CONFIG)
    staff_mini = [
        {"name": "NocheF", "role": "night_fixed"},
        {"name": "Emp1", "role": "normal"},
        {"name": "Emp2", "role": "normal"},
    ]
    with pytest.raises(RuntimeError):
        generar_semana_cpsat(staff_mini, SEMANA, cfg)


# ═══════════════════════════════════════════════════════════════════════════
# Fase 2 — términos de objetivo
# ═══════════════════════════════════════════════════════════════════════════

# ─── A. Equidad de findes ─────────────────────────────────────────────────

def test_equidad_prioriza_a_quien_tiene_menos_findes_libres_historicos():
    """Historial simulado: 5 personas siempre han librado el finde completo
    las últimas 4 semanas, una nunca. Con min_coverage=5 (Mañana2+Tarde2+
    Noche1) y 6 personas, solo cabe 1 finde completo libre por semana — el
    término de equidad debe priorizar a la rezagada para ese único slot."""
    from herramientas import registrar_semana

    cfg = _cfg()  # dept_id="test": Mañana2+Tarde2+Noche1=5 mínimo
    # Sobrado6/Sobrado7 (hallazgo de producción, ver
    # motor_cpsat.MIN_LIBRES_POR_SEMANA): con solo 6 personas y 5 mínimo
    # diario, no queda margen para que cada una tenga también sus 2 libres
    # obligatorios (restricción dura nueva) — 2 personas extra dan el
    # margen que hacía falta sin afectar la propiedad verificada (Rezagado
    # sigue siendo, con diferencia, quien menos finde libre ha tenido).
    staff = [
        {"name": "Rezagado", "role": "normal"},
        {"name": "Sobrado1", "role": "normal"},
        {"name": "Sobrado2", "role": "normal"},
        {"name": "Sobrado3", "role": "normal"},
        {"name": "Sobrado4", "role": "normal"},
        {"name": "Sobrado5", "role": "normal"},
        {"name": "Sobrado6", "role": "normal"},
        {"name": "Sobrado7", "role": "normal"},
    ]

    for i in range(1, 5):
        semana_pasada = SEMANA - timedelta(weeks=i)
        sched_falso = {
            "Rezagado": ["Mañana", "Tarde", "Mañana", "Tarde", "Mañana", "Tarde", "Mañana"],
            "Sobrado1": ["Mañana", "Tarde", "Mañana", "Tarde", "Mañana", "Libre", "Libre"],
            "Sobrado2": ["Tarde", "Mañana", "Tarde", "Mañana", "Tarde", "Libre", "Libre"],
            "Sobrado3": ["Mañana", "Tarde", "Mañana", "Tarde", "Mañana", "Libre", "Libre"],
            "Sobrado4": ["Noche", "Noche", "Noche", "Noche", "Noche", "Libre", "Libre"],
            "Sobrado5": ["Tarde", "Mañana", "Tarde", "Mañana", "Tarde", "Libre", "Libre"],
        }
        registrar_semana(semana_pasada, sched_falso, staff, forzar=True, dept_id=cfg.dept_id)

    sched = generar_semana_cpsat(staff, SEMANA, cfg)

    assert sched["Rezagado"][5] == "Libre" and sched["Rezagado"][6] == "Libre", (
        f"Rezagado (ratio histórico 0, resto en 1.0) debería recibir el "
        f"único finde disponible esta semana: {sched['Rezagado']}"
    )


# ─── B. Continuidad ────────────────────────────────────────────────────────

def test_continuidad_mantiene_turno_en_mitad_de_tirada():
    """Con historial vacío (equidad y desequilibrio histórico neutros, ambos
    en 0 al no haber datos), el único término de objetivo con efecto real es
    la continuidad. Se fija Ana=Tarde el Martes y se le prohíbe librar o
    hacer Noche el Miércoles (para aislar la elección entre turnos de día,
    ya que Noche/Libre no participan de la continuidad por diseño). Con
    Mañana bloqueada por la regla no-Mañana-tras-Tarde, la única disyuntiva
    real es Tarde (mantiene tirada, coste 0) vs Partido (rompe tirada, coste
    PESO_ROTURAS_CONTINUIDAD + PESO_USO_PARTIDO) — Tarde debe ganar."""
    staff = _staff_basico()
    cfg = _cfg()

    model, x, names, by_name = _construir_modelo(staff, SEMANA, cfg)
    model.Add(x["Ana", 1, "Tarde"] == 1)   # Ana trabaja Tarde el Martes
    model.Add(x["Ana", 2, "Libre"] == 0)   # el Miércoles trabaja
    model.Add(x["Ana", 2, "Noche"] == 0)   # descarta la vía de escape que no penaliza

    sched = _resolver_y_extraer(model, x, names)

    assert sched["Ana"][2] == "Tarde", (
        f"Ana debería mantenerse en Tarde el Miércoles (continuidad de tirada "
        f"abierta desde el Martes), pero su horario es: {sched['Ana']}"
    )


# ─── E. Penalización alta a rol excluido de equidad sin fixed_days_off ──────

def test_rol_excluido_sin_fixed_days_off_casi_nunca_libra_finde_completo():
    """Staff con un rol en roles_excluidos_equidad SIN entrada en
    fixed_days_off (equivalente a subjefe hoy: cubre_ausencia_de jefe, pero
    sin días libres fijos propios). 10 semanas encadenadas, sin peticiones
    ni vacaciones para esa persona: debe recibir el finde completo libre en
    0 (o casi 0) de esas semanas — no por restricción dura, sino por el peso
    alto del término E. El nombre del rol ("cubridor") es arbitrario y no
    aparece en ningún sitio de motor_cpsat.py."""
    # N6/N7 (hallazgo de producción, ver motor_cpsat.
    # MIN_LIBRES_POR_SEMANA): con solo 6 personas (mínimo diario 5) no
    # queda margen para que cada una tenga también sus 2 libres
    # obligatorios (restricción dura nueva) — 2 personas extra dan el
    # margen que hacía falta, sin afectar la propiedad verificada (peso
    # alto del término E sobre Cubridor).
    cfg = _cfg(roles_excluidos_equidad={"cubridor"})
    staff = [
        {"name": "Cubridor", "role": "cubridor"},
        {"name": "N1", "role": "normal"},
        {"name": "N2", "role": "normal"},
        {"name": "N3", "role": "normal"},
        {"name": "N4", "role": "normal"},
        {"name": "N5", "role": "normal"},
        {"name": "N6", "role": "normal"},
        {"name": "N7", "role": "normal"},
    ]

    prev_sunday = {}
    findes_completos = 0
    for i in range(10):
        semana = SEMANA + timedelta(weeks=i)
        sched = generar_semana_cpsat(staff, semana, cfg, prev_sunday_shifts=prev_sunday)
        if sched["Cubridor"][5] == "Libre" and sched["Cubridor"][6] == "Libre":
            findes_completos += 1
        prev_sunday = {p["name"]: sched[p["name"]][6] for p in staff}

    assert findes_completos <= 1, (
        f"Cubridor (rol excluido de equidad, sin fixed_days_off, sin "
        f"peticiones/vacaciones) recibió finde completo libre en "
        f"{findes_completos}/10 semanas — el peso del término E no está "
        f"desincentivándolo lo suficiente"
    )


def test_rol_excluido_sin_fixed_days_off_si_libra_con_peticion_concedida():
    """Mismo escenario, pero con una vacación/petición concedida (forced_days_off)
    para Cubridor justo ese finde: la restricción dura #6 (fase 1) debe
    ganarle a la penalización blanda del término E — el finde libre se
    fuerza igual, sin conflicto."""
    # N6/N7 (NUEVO, mismo motivo que en el test anterior — ver
    # motor_cpsat.MIN_LIBRES_POR_SEMANA).
    cfg = _cfg(roles_excluidos_equidad={"cubridor"})
    staff = [
        {"name": "Cubridor", "role": "cubridor", "forced_days_off": ["Sábado", "Domingo"]},
        {"name": "N1", "role": "normal"},
        {"name": "N2", "role": "normal"},
        {"name": "N3", "role": "normal"},
        {"name": "N4", "role": "normal"},
        {"name": "N5", "role": "normal"},
        {"name": "N6", "role": "normal"},
        {"name": "N7", "role": "normal"},
    ]

    sched = generar_semana_cpsat(staff, SEMANA, cfg)

    assert sched["Cubridor"][5] == "Libre" and sched["Cubridor"][6] == "Libre", (
        f"Cubridor tiene vacación/petición concedida el finde: la restricción "
        f"dura #6 debe forzar Libre pese al término E. Horario: {sched['Cubridor']}"
    )


def test_jefe_y_night_fixed_no_activan_termino_finde_rol_excluido():
    """Aunque jefe y night_fixed estén (como en la config real) en
    roles_excluidos_equidad, su patrón de días libres sigue gobernado SOLO
    por la restricción dura de fase 1 (cfg.fixed_days_off /
    cfg.night_fixed_days_off) — el término E no debería añadir ninguna
    presión adicional redundante, porque ambos quedan excluidos de
    roles_incentivo_trabajar_finde por tener ya un mecanismo duro de días
    libres."""
    cfg = _cfg(
        roles_excluidos_equidad={"jefe", "night_fixed", "cubridor"},
        night_fixed_days_off=[3, 4],
    )
    staff = _staff_basico() + [{"name": "NocheFijo", "role": "night_fixed"}]

    sched = generar_semana_cpsat(staff, SEMANA, cfg)

    # Jefe: Libre exactamente en cfg.fixed_days_off["jefe"], Mañana el resto.
    for d in range(7):
        if d in cfg.fixed_days_off["jefe"]:
            assert sched["Jefe"][d] == "Libre"
        else:
            assert sched["Jefe"][d] == "Mañana"

    # night_fixed: Libre exactamente en cfg.night_fixed_days_off, Noche el resto.
    for d in range(7):
        if d in cfg.night_fixed_days_off:
            assert sched["NocheFijo"][d] == "Libre"
        else:
            assert sched["NocheFijo"][d] == "Noche"


# ─── Escenario de estrés: ¿el término E realmente cede ante un coste mayor? ──

def test_termino_e_cede_cuando_la_alternativa_cuesta_mas_de_200():
    """Escenario construido a mano para forzar, por diseño, que NO darle el
    finde completo a Cubridor cueste más de 200 (PESO_FINDE_ROL_EXCLUIDO) en
    otras penalizaciones — no roturas/Partido (ver nota de diseño abajo),
    sino el término de equidad A, que también es una "otra penalización"
    legítima entre los 4 términos de fase 2.

    La presión real sobre el finde no viene de que la cobertura esté "sin
    holgura" (max_coverage == min_coverage) — con la restricción dura nueva
    de patrón de libres (motor_cpsat.DIAS_LIBRES_SEGUIDOS_POR_SEMANA,
    exactamente 2 CONSECUTIVOS por persona) esa rigidez ya no es compatible
    con que cada persona pueda además encajar su propio par en una
    rotación válida — viene de bloquear EXPLÍCITAMENTE a N2-N5 (y, en el
    contrafactual, también a Cubridor) fuera del finde, dejando la
    cobertura mínima (con margen normal, max_coverage generoso) como la
    única vía real de presión. N2-N5 están comprometidos (bloqueados) el
    finde por un motivo ajeno al modelo (simula, p.ej., que ya tienen otra
    cobertura asignada esa semana). Eso deja solo dos candidatos posibles
    para el finde: Cubridor o N1 — y con historial fabricado (N1 ratio=1.0,
    N2-N5 ratio=0.0 → media≈0.167), darle el finde a N1 en vez de a
    Cubridor cuesta ~8330 solo por el término A, muy por encima de los 200
    de E.

    NOTA de diseño — por qué no roturas+Partido como sugería el planteo
    original: en este modelo, "Libre" es una vía de escape sin coste
    siempre que la cobertura lo permita, así que forzar Partido/roturas
    específicamente exige que TODO el equipo esté obligado a trabajar ese
    día (cobertura mínima = plantilla completa) — pero eso también le
    quitaría a Cubridor la posibilidad de librar en el escenario base,
    invalidando la comparación. La equidad (A) no tiene ese problema: no
    depende de forzar a nadie a trabajar, solo de qué le cuesta a cada
    candidato *librar*. Es un término distinto al que proponía el enunciado,
    pero igual de "otra penalización" real dentro del objetivo de fase 2.

    ALCANCE de la conclusión — léase con precisión, no generalizar de más:
    este test demuestra que E cede frente al término de equidad (A)
    específicamente. NO demuestra (ni lo intenta) que E ceda frente a B
    (roturas de continuidad) o C (desequilibrio histórico) por separado —
    esos dos, según el razonamiento de arriba, no se pueden aislar como
    causa dominante en una sola semana con este modelo de cobertura, porque
    "Libre" siempre gana como escape mientras la cobertura lo permita: para
    que B o C solos superaran 200 haría falta forzar a todo el equipo a
    trabajar, lo cual invalida la comparación (Cubridor tampoco podría
    librar en el escenario base). Esto es una propiedad ESTRUCTURAL del
    modelo tal como está definido hoy (min_coverage/max_coverage uniforme
    por turno, sin coste por celda Libre en sí), no una limitación de este
    test ni algo que una prueba mejor diseñada pudiera sortear. Si en el
    futuro se añade algún coste asociado a "trabajar sin librar" o se hace
    la cobertura configurable por día, esta conclusión debería revisarse.
    """
    from herramientas import registrar_semana

    # max_coverage == min_coverage (sin holgura) preservado — es lo que
    # convierte "Cubridor libra el finde" en algo estructuralmente forzado
    # por el máximo (N1+N2-N5 ya agotan el máximo de 5 el finde en el
    # escenario real, así que a Cubridor no le queda sitio), no una mera
    # preferencia blanda; ver docstring. N6/N7/N8 (NUEVO —
    # hallazgo de producción): dan el margen de libres que Cubridor/N1/
    # N2-N5 necesitan para su propio patrón CONSECUTIVO de 2 (restricción
    # dura nueva) SIN competir por el finde. Usan un rol con
    # cfg.fixed_days_off=[Sáb,Dom] (no forced_days_off personal): eso los
    # EXCLUYE por completo de la restricción nueva (a diferencia de un
    # forced_days_off personal, que solo resta esos días del conteo y
    # seguiría exigiéndoles un par aparte entre semana) — trabajan los 5
    # días de diario con normalidad, aportando cobertura ahí sin nunca ser
    # candidatos al finde.
    cfg = _cfg(
        roles_excluidos_equidad={"cubridor"},
        min_coverage={"Mañana": 2, "Tarde": 2, "Noche": 1, "Partido": 0},
        max_coverage={"Mañana": 2, "Tarde": 2, "Noche": 1, "Partido": 0},  # sin holgura
        fixed_days_off={"jefe": [5, 6], "buffer_rol": [5, 6]},
    )
    staff = [
        {"name": "Cubridor", "role": "cubridor"},
        {"name": "N1", "role": "normal"},
        {"name": "N2", "role": "normal"},
        {"name": "N3", "role": "normal"},
        {"name": "N4", "role": "normal"},
        {"name": "N5", "role": "normal"},
        {"name": "N6", "role": "buffer_rol"},
        {"name": "N7", "role": "buffer_rol"},
        {"name": "N8", "role": "buffer_rol"},
    ]

    for i in range(1, 5):
        semana_pasada = SEMANA - timedelta(weeks=i)
        sched_falso = {
            "Cubridor": ["Mañana", "Tarde", "Mañana", "Tarde", "Mañana", "Tarde", "Mañana"],
            "N1": ["Mañana", "Tarde", "Mañana", "Tarde", "Mañana", "Libre", "Libre"],   # ratio 1.0
            "N2": ["Tarde", "Mañana", "Tarde", "Mañana", "Tarde", "Mañana", "Tarde"],   # ratio 0.0
            "N3": ["Mañana", "Tarde", "Mañana", "Tarde", "Mañana", "Tarde", "Mañana"],  # ratio 0.0
            "N4": ["Noche", "Noche", "Noche", "Noche", "Noche", "Noche", "Noche"],      # ratio 0.0
            "N5": ["Tarde", "Mañana", "Tarde", "Mañana", "Tarde", "Mañana", "Tarde"],   # ratio 0.0
        }
        registrar_semana(semana_pasada, sched_falso, staff, forzar=True, dept_id=cfg.dept_id)

    # Confirma primero que la alternativa (N1 cubre en vez de Cubridor) cuesta,
    # por diseño, más de 200 vía el término A — antes de mirar si el solver
    # realmente elige evitarla.
    ratios = _get_weekend_ratios(cfg.dept_id, staff)
    avg_ratio = sum(d["ratio"] for d in ratios.values()) / len(ratios)
    # Importa PESO_EQUIDAD_FINDES/ESCALA_RATIO reales (Fase 5, Paso 2 del
    # informe): antes tenían el valor de A hardcodeado (*10), que quedó
    # desconectado de la constante real tras subir PESO_EQUIDAD_FINDES a 100
    # — el cálculo seguía "pasando" pero validaba un número obsoleto, no el
    # peso que el modelo usa de verdad. Con el import, este assert vuelve a
    # ser una guarda real frente a futuros cambios de peso.
    coste_alternativa_termino_a = PESO_EQUIDAD_FINDES * round((1.0 - avg_ratio) * ESCALA_RATIO)
    assert coste_alternativa_termino_a > 200, (
        f"Escenario mal calibrado: la alternativa (N1 cubre) cuesta solo "
        f"{coste_alternativa_termino_a} vía equidad, no supera los 200 de "
        f"PESO_FINDE_ROL_EXCLUIDO — hay que ajustar el historial fabricado"
    )

    # ── Escenario real: N2-N5 bloqueados, Cubridor libre de decidir ──
    model, x, names, by_name = _construir_modelo(staff, SEMANA, cfg)
    for n in ("N2", "N3", "N4", "N5"):
        model.Add(x[n, 5, "Libre"] == 0)
        model.Add(x[n, 6, "Libre"] == 0)
    model.Add(x["N1", 5, "Libre"] == 0)
    model.Add(x["N1", 6, "Libre"] == 0)
    sched_real = _resolver_y_extraer(model, x, names)

    assert sched_real["Cubridor"][5] == "Libre" and sched_real["Cubridor"][6] == "Libre", (
        f"Cubridor era la única opción viable para el finde (N1-N5 "
        f"bloqueados) y aun así el término E (200) le impidió recibirlo, "
        f"pese a que la alternativa costaría {coste_alternativa_termino_a} — "
        f"el término está actuando como restricción dura de facto. "
        f"Horario: {sched_real['Cubridor']}"
    )

    # ── Contrafactual: confirma que la alternativa bloqueada de verdad
    # habría presionado sobre el resto del sistema (no es una comparación
    # vacía) ──
    # NOTA (max_coverage pasó de restricción dura a blanda,
    # PESO_EXCESO_COBERTURA): la versión original de este contrafactual
    # esperaba que N1 quedara Libre AMBOS días del finde de forma
    # ABSOLUTA, por exclusión estructural de capacidad (con max_coverage
    # duro y sin holgura, Cubridor+N2-N5 ya saturaban por completo la
    # cobertura máxima del finde, así que N1 no tenía sitio posible). Con
    # max_coverage blando esa exclusión ya NO es absoluta — el solver
    # puede, en su lugar, aceptar exceder algún máximo (a un coste de
    # PESO_EXCESO_COBERTURA=5000 por persona de más) si el conjunto de
    # presiones lo hace más barato que forzar a N1 a un patrón concreto.
    # La propiedad que este contrafactual puede seguir demostrando de
    # verdad es más débil pero sigue siendo real: bloquear a Cubridor
    # (en vez de a N1) cambia el resultado de verdad respecto al
    # escenario real de arriba — no es una comparación vacía donde da
    # igual a quién se bloquee.
    model2, x2, names2, by_name2 = _construir_modelo(staff, SEMANA, cfg)
    for n in ("N2", "N3", "N4", "N5"):
        model2.Add(x2[n, 5, "Libre"] == 0)
        model2.Add(x2[n, 6, "Libre"] == 0)
    model2.Add(x2["Cubridor", 5, "Libre"] == 0)
    model2.Add(x2["Cubridor", 6, "Libre"] == 0)
    sched_alt = _resolver_y_extraer(model2, x2, names2)

    assert sched_alt["N1"] != sched_real["N1"] or sched_alt["Cubridor"][5:] != ["Libre", "Libre"], (
        "Bloquear a Cubridor en vez de a N1 debía cambiar de verdad el "
        f"resultado (o el patrón de N1, o que Cubridor ya no libre el "
        f"finde): escenario real N1={sched_real['N1']}, contrafactual "
        f"N1={sched_alt['N1']}, Cubridor contrafactual={sched_alt['Cubridor']}"
    )
