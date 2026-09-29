"""
tests/test_presencia_cpsat.py — Presencia parcial en el motor CP-SAT (paso 2).

Contrato (ver REGLAS.md §1.8 y herramientas.py, MÓDULO 6):
  - Una persona cuyo rol tiene dias_presencia trabaja SIEMPRE en sus días de
    presencia — solo una vacación/petición concedida (forced_days_off /
    extra_days_off) puede librarlos.
  - El resto de la semana queda en el estado "Fuera", que no computa en
    nada (ni libres, ni equidad, ni cobertura, ni festivos trabajados).
  - Es la excepción a la restricción #7 (exactamente 2 "Libre" por semana):
    la persona con presencia puede acabar la semana con 0 libres.
  - Sin ningún rol con presencia (presencia_roles vacío), el modelo
    construido es EXACTAMENTE el de antes: cero variables "Fuera", cero
    restricciones nuevas (requisito de no-regresión, verificado además con
    la captura comparativa byte a byte de 6 semanas encadenadas).

Todos los tests construyen la cfg a mano (mismo patrón que
test_max_coverage_blando.py) y usan un DATA_DIR temporal.
"""

from datetime import date, timedelta

import pytest

from modelo_base import DeptConfig, SHIFTS, FUERA
from motor_cpsat import _construir_modelo, _resolver_y_extraer

SEMANA = date(2026, 1, 5)  # lunes

# Turnos de trabajo reales (todo SHIFTS menos Libre) — el contrato de
# presencia exige que los días de presencia caigan aquí, nunca en Libre ni
# en Fuera.
TURNOS_TRABAJO = [s for s in SHIFTS if s != "Libre"]


@pytest.fixture(autouse=True)
def _data_dir_aislado(tmp_path, monkeypatch):
    import herramientas
    backup = tmp_path / "backups"
    backup.mkdir()
    monkeypatch.setattr(herramientas, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(herramientas, "BACKUP_DIR", str(backup))
    monkeypatch.setattr(herramientas, "FESTIVOS_FILE", str(tmp_path / "festivos.json"))
    return tmp_path


def _cfg(**overrides) -> DeptConfig:
    base = dict(
        dept_id="recepcion", dept_label="Recepción",
        night_fixed_days_off=[3, 4], night_cover_compensation=[5, 6],
        min_coverage={"Mañana": 1, "Tarde": 1, "Noche": 0, "Partido": 0},
        max_coverage={"Mañana": 4, "Tarde": 4, "Noche": 1, "Partido": 1},
        no_night_roles={"jefe"},
        fixed_roles={"jefe": "Mañana"}, fixed_days_off={"jefe": [5, 6]},
        cover_consecutive=True, night_rotation_key="recepcion",
        allow_non_consecutive_cover=False, prefer_next_week_for_cover=False,
        roles_excluidos_equidad=set(),
    )
    base.update(overrides)
    return DeptConfig(**base)


def _staff(ref_extra: dict = None) -> list:
    """Jefe + 2 normales + Ref (rol "refuerzo", configurado por cada test).
    Volumen deliberadamente holgado: los días con gente de sobra se absorben
    con el exceso blando de Mañana/Tarde (PESO_EXCESO_COBERTURA), así que el
    escenario nunca depende de que la aritmética cuadre al día."""
    return [
        {"name": "Jefe", "role": "jefe"},
        {"name": "Ana", "role": "normal"},
        {"name": "Luis", "role": "normal"},
        {"name": "Ref", "role": "refuerzo", **(ref_extra or {})},
    ]


def _resolver(cfg: DeptConfig, staff: list) -> dict:
    model, x, names, _ = _construir_modelo(staff, SEMANA, cfg)
    return model, x, names


# ═══════════════════════════════════════════════════════════════════════════
# 1. Días fuera fijados a "Fuera" + trabajo garantizado en los de presencia
# ═══════════════════════════════════════════════════════════════════════════

def test_dias_fuera_fijados_y_trabajo_garantizado_en_presencia():
    cfg = _cfg(presencia_roles={"refuerzo": {5, 6}})
    model, x, names = _resolver(cfg, _staff())
    schedule = _resolver_y_extraer(model, x, names)

    # Sábado (5) y Domingo (6) son días de presencia: turno de trabajo real,
    # jamás Libre ni Fuera.
    for d in (5, 6):
        assert schedule["Ref"][d] in TURNOS_TRABAJO, (
            f"Ref debía trabajar obligatoriamente el día {d}: {schedule['Ref']}"
        )
    # Lunes-Viernes son días fuera: "Fuera" en los 5.
    for d in range(5):
        assert schedule["Ref"][d] == FUERA, (
            f"Ref debía quedar Fuera el día {d}: {schedule['Ref']}"
        )

    # Estructura del modelo: en días fuera solo existe la variable FUERA
    # (fijada a 1), no hay variables de turno; en días de presencia al revés.
    assert (x["Ref", 0, FUERA] is not None)
    assert ("Ref", 0, "Mañana") not in x
    assert ("Ref", 5, FUERA) not in x
    assert ("Ref", 5, "Mañana") in x
    # Los demás (sin presencia) conservan su semana completa normal.
    for d in range(7):
        assert ("Jefe", d, "Mañana") in x
        assert ("Jefe", d, FUERA) not in x


# ═══════════════════════════════════════════════════════════════════════════
# 2. Excepción a la restricción #7: sin patrón de 2 libres semanales
# ═══════════════════════════════════════════════════════════════════════════

def test_sin_patron_de_dos_libres_cuando_presencia_cubre_la_semana_util():
    """Presencia de lunes a viernes: 5 días trabajados, 0 libres, finde
    Fuera — exactamente lo que la restricción #7 prohíbe para cualquier
    otro rol (2 libres), y el motivo de ser de esta categoría. Ana, del
    mismo equipo y sin presencia, mantiene intacto su patrón de 2."""
    cfg = _cfg(presencia_roles={"refuerzo": {0, 1, 2, 3, 4}})
    model, x, names = _resolver(cfg, _staff())
    schedule = _resolver_y_extraer(model, x, names)

    turnos_ref = schedule["Ref"]
    for d in range(5):
        assert turnos_ref[d] in TURNOS_TRABAJO, (
            f"Ref debía trabajar el día {d} (presencia): {turnos_ref}"
        )
    assert turnos_ref[5] == FUERA and turnos_ref[6] == FUERA
    assert turnos_ref.count("Libre") == 0, (
        f"Ref no debe recibir libres: {turnos_ref}"
    )
    # Contraste: el patrón de 2 libres sigue aplicándose a quien no tiene
    # presencia.
    assert schedule["Ana"].count("Libre") == 2, (
        f"Ana debía conservar sus 2 libres: {schedule['Ana']}"
    )


# ═══════════════════════════════════════════════════════════════════════════
# 3-4. Solo una concesión (vacación/petición) libra un día de presencia
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("campo, dia_nombre, idx", [
    ("forced_days_off", "Sábado", 5),
    ("extra_days_off", "Domingo", 6),
])
def test_vacacion_o_peticion_concedida_libra_dia_de_presencia(campo, dia_nombre, idx):
    cfg = _cfg(presencia_roles={"refuerzo": {5, 6}})
    model, x, names = _resolver(cfg, _staff({campo: [dia_nombre]}))
    schedule = _resolver_y_extraer(model, x, names)

    assert schedule["Ref"][idx] == "Libre", (
        f"La concesión ({campo}={dia_nombre}) debía librar ese día de "
        f"presencia: {schedule['Ref']}"
    )
    # El OTRO día de presencia sigue siendo trabajo obligatorio.
    otro = 6 if idx == 5 else 5
    assert schedule["Ref"][otro] in TURNOS_TRABAJO, (
        f"El día de presencia no concedido debía trabajarse: {schedule['Ref']}"
    )


# ═══════════════════════════════════════════════════════════════════════════
# 5. Exclusión de equidad: estructural, no solo por el flag
# ═══════════════════════════════════════════════════════════════════════════

def _coef_objetivo(model, nombre_var: str) -> int:
    """Coeficiente de una variable en el objetivo del modelo (0 si no
    aparece). Se lee del proto — así se comprueba el término A en sí, que
    de otro modo es invisible en el schedule (weekend_libre de la persona
    con presencia es 0 constante)."""
    proto = model.Proto()
    indice = next(
        i for i, v in enumerate(proto.variables) if v.name == nombre_var
    )
    for var, coef in zip(proto.objective.vars, proto.objective.coeffs):
        if var == indice:
            return coef
    return 0


def test_equidad_excluye_estructuralmente_a_roles_con_presencia(monkeypatch):
    """Con roles_excluidos_equidad VACÍO (flag sin usar), el término A no
    debe incluir a la persona con presencia en la media de ratios: su coef
    debe salir de la media de los demás (0.5), no de la media contaminada
    por su ratio 0.0 (0.333...). Se verifica contra el modelo de CONTROL
    sin presencia, donde el ratio sí contamina (567)."""
    ratios = {"Ana": {"ratio": 0.9}, "Luis": {"ratio": 0.1},
              "Ref": {"ratio": 0.0}, "Jefe": {"ratio": 0.5}}
    monkeypatch.setattr(
        "motor_cpsat._get_weekend_ratios", lambda dept, staff: dict(ratios)
    )

    # Con presencia y sin flag: avg = (0.9+0.1)/2 = 0.5 -> coef (0.9-0.5)*1000 = 400
    cfg_con = _cfg(presencia_roles={"refuerzo": {5, 6}})
    model_con, _, _, _ = _construir_modelo(_staff(), SEMANA, cfg_con)
    coef_con = _coef_objetivo(model_con, "weekend_libre_Ana")

    # Control sin presencia: avg = (0.9+0.1+0.0)/3 = 0.3333 -> coef 567
    cfg_sin = _cfg()  # presencia_roles vacío
    model_sin, _, _, _ = _construir_modelo(_staff(), SEMANA, cfg_sin)
    coef_sin = _coef_objetivo(model_sin, "weekend_libre_Ana")

    # PESO_EQUIDAD_FINDES multiplica al coef: 100 * 400 vs 100 * 567.
    from motor_cpsat import PESO_EQUIDAD_FINDES
    assert coef_con == PESO_EQUIDAD_FINDES * 400, (
        f"El ratio de Ref (0.0) no debía entrar en la media con presencia: coef={coef_con}"
    )
    assert coef_sin == PESO_EQUIDAD_FINDES * 567, (
        f"El modelo de control (sin presencia) debía mantener la media de 3: coef={coef_sin}"
    )


# ═══════════════════════════════════════════════════════════════════════════
# 6. Festivos trabajados: "Fuera" no se registra
# ═══════════════════════════════════════════════════════════════════════════

def test_festivo_no_se_registra_para_quien_estaba_fuera(monkeypatch):
    import herramientas

    # Festivo un miércoles: Ref está Fuera ese día (presencia solo finde),
    # Ana trabaja.
    schedule = {
        "Ana": ["Mañana"] * 7,
        "Ref": [FUERA] * 5 + ["Mañana", "Mañana"],
    }
    staff = [{"name": "Ana", "role": "normal"}, {"name": "Ref", "role": "refuerzo"}]

    festivo = {
        "date": SEMANA + timedelta(days=2), "dia_semana": "Miércoles",
        "nombre": "Festivo Test", "tipo": "local",
    }
    monkeypatch.setattr(herramientas, "festivos_en_semana", lambda ws: [festivo])
    herramientas.registrar_festivos_semana(SEMANA, schedule, staff)

    datos = herramientas.cargar_festivos_trabajados("recepcion")
    dia = datos[(SEMANA + timedelta(days=2)).isoformat()]["trabajadores"]
    assert dia.get("Ana") == "Mañana"
    assert "Ref" not in dia, (
        f"Quien estaba Fuera no debe registrarse como festivo trabajado: {dia}"
    )


# ═══════════════════════════════════════════════════════════════════════════
# 7. Sin presencia: modelo y schedule idénticos a los de siempre
# ═══════════════════════════════════════════════════════════════════════════

def test_sin_roles_con_presencia_cero_celdas_fuera_y_modelo_intacto():
    """Claves de x exactamente las de siempre (n × 7 × len(SHIFTS)), ningún
    "Fuera" en el schedule y el patrón #7 intacto para todos. Es la
    comprobación estructural de no-regresión que complementa la captura
    comparativa byte a byte (6 semanas encadenadas, Recepción y Mozos)."""
    cfg = _cfg()  # sin presencia_roles
    staff = _staff()
    model, x, names = _resolver(cfg, staff)

    esperadas = {
        (p["name"], d, s)
        for p in staff for d in range(7) for s in SHIFTS
    }
    assert set(x.keys()) == esperadas, (
        "Sin presencia el diccionario x debe tener exactamente las celdas de siempre"
    )

    schedule = _resolver_y_extraer(model, x, names)
    for p in staff:
        assert FUERA not in schedule[p["name"]], schedule[p["name"]]
        assert all(t in SHIFTS for t in schedule[p["name"]])
        if p["role"] == "normal":
            assert schedule[p["name"]].count("Libre") == 2


# ═══════════════════════════════════════════════════════════════════════════
# 8. Término G: un rol con presencia cubre la ausencia de otro rol, pero
#    solo en sus días de presencia
# ═══════════════════════════════════════════════════════════════════════════

def test_rol_con_presencia_cubre_ausencia_solo_en_sus_dias():
    """Ref cubre al Jefe (cobertura_roles) con presencia Lun-Mié: en esos
    días el peso 1000 del término G lo empuja al turno fijo del jefe
    (Mañana); en sus días fuera la celda es Fuera y no interviene."""
    cfg = _cfg(
        presencia_roles={"refuerzo": {0, 1, 2}},
        cobertura_roles={"refuerzo": {
            "cubre": "jefe", "dias_libres_fijos": False,
            "semana_completa": False,
        }},
    )
    staff = [
        {"name": "Jefe", "role": "jefe",
         "forced_days_off": ["Lunes", "Martes", "Miércoles"]},
        {"name": "Ana", "role": "normal"},
        {"name": "Luis", "role": "normal"},
        {"name": "Ref", "role": "refuerzo"},
    ]
    model, x, names = _resolver(cfg, staff)
    schedule = _resolver_y_extraer(model, x, names)

    for d in (0, 1, 2):
        assert schedule["Ref"][d] == "Mañana", (
            f"Ref debía cubrir la ausencia del jefe el día {d}: {schedule['Ref']}"
        )
    for d in (3, 4, 5, 6):
        assert schedule["Ref"][d] == FUERA, (
            f"Fuera de sus días de presencia Ref no interviene el día {d}: {schedule['Ref']}"
        )
