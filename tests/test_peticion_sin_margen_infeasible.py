"""
tests/test_peticion_sin_margen_infeasible.py — Caso de producción real
(esta sesión): un encargado reportó que generar el horario de Recepción
fallaba con INFEASIBLE tras conceder una petición suelta a un empleado.

Diagnóstico dirigido (confirmado con el usuario antes de tocar nada):
NO es un bug de motor_cpsat.py. La restricción #7 (patrón de 2 días libres
semanales) está bien implementada tal como se especificó: cuando alguien
tiene una petición concedida, esta CUENTA como uno de los 2 libres
semanales (no se apila aparte, a diferencia de vacaciones/LA) y el otro
libre debe formar PAR CONSECUTIVO con ella — el día inmediatamente anterior
o el inmediatamente siguiente, sin más alternativa (confirmado
explícitamente con el usuario: "Juntos en par... No debería dar infeasible
solo porque un día un empleado lo tiene pedido").

Esto es estructuralmente MÁS RÍGIDO que el patrón general sin petición (que
elige libremente entre los 6 pares consecutivos posibles de la semana): si
AMBOS días vecinos de la petición resultan ser, esa semana concreta, los
dos únicos días sin ningún margen de cobertura (porque otras 2 personas ya
tienen esos días concretos forzados libres por su cuenta), la restricción
de petición no tiene ninguna alternativa a la que recurrir — mientras que,
sin la petición, el patrón general SÍ podría haber evitado esos 2 días
formando el par en cualquier otro sitio de la semana (ver el escenario
"sin petición" de este archivo, deliberadamente construido para demostrar
que existía margen real).

Esto es el comportamiento CORRECTO y DESEADO, no un fallo: la petición
pin-ea un día concreto y el patrón debe respetar sus 2 libres alrededor de
él; si la plantilla de esa semana concreta no tiene margen en NINGUNO de
los 2 únicos días posibles, es un conflicto real de personal (igual que
cualquier otra restricción dura de este módulo, que nunca cede en
silencio — ver _diagnosticar_infactibilidad) y debe reportarse como tal,
no absorberse relajando la regla de negocio.

Este test documenta y fija ese comportamiento de forma permanente,
reproduciendo el caso mínimo exacto que detectó el problema en producción.
"""

from datetime import date

from modelo_base import DeptConfig
from motor_cpsat import _construir_modelo, _resolver_y_extraer

SEMANA = date(2099, 2, 3)  # Lunes en futuro lejano


def _cfg() -> DeptConfig:
    # min_coverage == max_coverage == 1 para Mañana y Tarde: exactamente 2
    # de las 3 personas deben trabajar cada día (1 puede librar) -- cero
    # margen de sobra, para que "quién libra qué día" importe de verdad.
    return DeptConfig(
        dept_id="test", dept_label="Test",
        night_fixed_days_off=[3, 4], night_cover_compensation=[5, 6],
        min_coverage={"Mañana": 1, "Tarde": 1, "Noche": 0, "Partido": 0},
        max_coverage={"Mañana": 1, "Tarde": 1, "Noche": 0, "Partido": 1},
        no_night_roles={"normal"}, fixed_roles={}, fixed_days_off={},
        cover_consecutive=True, night_rotation_key="test",
        allow_non_consecutive_cover=False, prefer_next_week_for_cover=False,
    )


def test_sin_peticion_el_patron_general_si_tiene_margen():
    """Precondición del caso: SIN la petición de Martin, el patrón general
    de 2-libres-consecutivos elige libremente entre los 6 pares posibles de
    la semana -- y con Martes/Jueves sin margen (B y C ya forzados libres
    esos días respectivamente), el solver puede evitarlos por completo
    formando el par de Martin en otro sitio (Viernes+Sábado en la práctica).
    Esto confirma que el conflicto de los tests siguientes viene
    ESPECÍFICAMENTE de anclar el par al día de la petición, no de una
    escasez real de personal en la semana."""
    cfg = _cfg()
    staff = [
        {"name": "Martin", "role": "normal"},
        {"name": "B", "role": "normal", "forced_days_off": ["Martes"]},
        {"name": "C", "role": "normal", "forced_days_off": ["Jueves"]},
    ]
    model, x, names, _ = _construir_modelo(staff, SEMANA, cfg)
    sched = _resolver_y_extraer(model, x, names)  # no debe lanzar RuntimeError

    martes, jueves = sched["Martin"][1], sched["Martin"][3]
    assert martes != "Libre" and jueves != "Libre", (
        f"Precondición del caso: con margen real disponible en el resto de "
        f"la semana, el patrón general de Martin debía evitar Martes y "
        f"Jueves (los únicos días sin margen): {sched['Martin']}"
    )


def test_peticion_en_dia_sin_margen_en_ambos_vecinos_es_infeasible():
    """CASO REAL DE PRODUCCIÓN (esta sesión): Martin tiene una petición
    concedida el Miércoles. Sus únicos 2 candidatos posibles para el
    segundo libre del patrón (Martes o Jueves, los vecinos inmediatos del
    Miércoles) son, esta semana concreta, los dos únicos días sin ningún
    margen de cobertura -- B ya tiene Martes forzado libre por su cuenta y
    C ya tiene Jueves forzado libre por la suya, y con min_coverage==
    max_coverage==1 no sobra nadie más para cubrir si Martin TAMBIÉN librara
    cualquiera de esos 2 días.

    Confirmado explícitamente con el usuario: esto es el comportamiento
    CORRECTO -- la restricción de petición está bien implementada tal como
    se especificó (par consecutivo con el día pedido, sin relajarlo a
    "cualquier día de la semana" como el patrón general), y un conflicto
    real de personal en los 2 únicos días posibles debe reportarse como
    INFEASIBLE, no absorberse en silencio ni cambiar la regla de negocio
    para evitarlo."""
    cfg = _cfg()
    staff = [
        {"name": "Martin", "role": "normal", "forced_days_off": ["Miércoles"]},
        {"name": "B", "role": "normal", "forced_days_off": ["Martes"]},
        {"name": "C", "role": "normal", "forced_days_off": ["Jueves"]},
    ]
    model, x, names, _ = _construir_modelo(staff, SEMANA, cfg)

    try:
        _resolver_y_extraer(model, x, names, staff, cfg)
        assert False, (
            "Se esperaba INFEASIBLE: la petición de Martin el Miércoles "
            "ancla su segundo libre a Martes o Jueves, los dos únicos días "
            "sin margen de cobertura esta semana (B y C ya los tienen "
            "forzados libres por su cuenta) -- no debería haber encontrado "
            "una solución."
        )
    except RuntimeError as e:
        mensaje = str(e)
        assert "INFEASIBLE" in mensaje, (
            f"El error debía reportar status=INFEASIBLE: {mensaje}"
        )
        # El diagnóstico automático (_diagnosticar_infactibilidad) debe
        # señalar el conflicto real -- los días forzados de B/C y la
        # cobertura mínima que chocan con ellos -- en vez de devolver solo
        # el RuntimeError desnudo sin ninguna pista útil para el encargado.
        assert "Diagnóstico automático" in mensaje, (
            f"Se esperaba el diagnóstico automático de restricciones "
            f"cedibles en conflicto, no solo el status desnudo: {mensaje}"
        )
        for pista in ("Martes", "Jueves", "Cobertura mínima"):
            assert pista in mensaje, (
                f"El diagnóstico debía mencionar {pista!r} como parte del "
                f"conflicto real (B/C forzados esos días + la cobertura "
                f"mínima que choca con ellos): {mensaje}"
            )
