"""
tests/test_hallazgo_minimo_libres_produccion.py — Valida la restricción dura
NUEVA de motor_cpsat.py (DIAS_LIBRES_SEGUIDOS_POR_SEMANA, ver su docstring)
que cierra el hallazgo BLOQUEANTE de producción: con la plantilla real de
Recepción, el motor CP-SAT podía generar semanas donde alguien trabajaba los
7 días sin ningún libre mientras otra persona libraba los 7 sin trabajar
ninguno — la cobertura mínima diaria se cumplía siempre (no era un bug de
esa restricción), pero el reparto INDIVIDUAL de libres no estaba acotado por
nada.

Un primer diseño de esta restricción (un simple PISO de 2 libres/semana)
resultó insuficiente: probado contra esta misma plantilla real, dejaba a
"Igor" con 7 libres/0 días trabajados esa semana — el otro lado exacto del
hallazgo, sin cerrarlo. La regla de negocio confirmada después con el
usuario es más precisa: EXACTAMENTE 2 días libres, CONSECUTIVOS, ni más ni
menos, salvo la excepción ya existente en el modelo para cuando ningún par
consecutivo es estructuralmente posible (mismo caso que activa el término
blando F, "Domingo suelto del sustituto de cobertura nocturna").

Deliberadamente NO usa el fixture _data_dir_aislado que el resto de la
suite usa: los dos primeros tests reproducen el escenario EXACTO que reveló
el problema, así que leen la plantilla y la configuración REALES de
producción (data/plantilla.json, data/roles.json, data/coverage_config.json)
tal como quedaron en el repositorio en el momento de este cierre — vía
app._get_dept_config / herramientas.cargar_plantilla, las mismas funciones
que usa app.api_generar, apuntando a herramientas.DATA_DIR="data" (su valor
por defecto, sin monkeypatch). No hay ningún riesgo de escritura accidental
sobre esos datos: se llama directamente a
motor_cpsat.generar_semana_cpsat_con_cobertura_nocturna (puro cálculo, sin
tocar disco) en vez de a app.api_generar/_generar_con_motor_cpsat (que sí
persisten historial.json/cpsat_estado_recepcion.json) — confirmado leyendo
ambas funciones antes de escribir este archivo.

NOTA sobre "10 personas" vs "9 personas": el hallazgo reportado describe la
plantilla real de Recepción como de 10 personas; en el momento de escribir
este test, data/plantilla.json tiene 9 (sin nadie con role="night_fixed" —
el departamento opera hoy en modo respaldo de cobertura nocturna, rotando
la Noche entre los "normal" existentes, ver cobertura_nocturna.
modo_respaldo_activo). Es precisamente ese modo respaldo, con pocos
candidatos elegibles para Noche, el que hacía más fácil que el CP-SAT sin
esta restricción concentrase los libres de forma desigual — el test usa la
plantilla real tal como está hoy en el repositorio, no una reconstrucción a
mano de "10 personas".

No existe hoy un data/cpsat_estado_recepcion.json persistido (el motor
CP-SAT nunca llegó a activarse en producción con el interruptor
USAR_MOTOR_CPSAT, precisamente por este hallazgo) — se genera la semana
desde estado_rotacion "en frío" (mismo criterio que
app._estado_rotacion_inicial_cpsat para el primer uso del motor en un
departamento) y noches_ultimos_5_dias vacío, exactamente lo que ocurriría
la primera vez que alguien active el interruptor de verdad.
"""

import copy
from datetime import date

import pytest

from modelo_base import DAYS
from motor_cpsat import (
    DIAS_LIBRES_SEGUIDOS_POR_SEMANA,
    _construir_modelo,
    _resolver_y_extraer,
    _diagnosticar_infactibilidad,
    generar_semana_cpsat_con_cobertura_nocturna,
)

SEMANA_HALLAZGO = date(2026, 8, 10)  # la semana real que reveló el problema


def _cfg_real_recepcion():
    """cfg tal como la construye app._get_dept_config("recepcion") hoy,
    leyendo directamente data/roles.json y data/coverage_config.json (sin
    aislar DATA_DIR — ver docstring del módulo)."""
    from app import _get_dept_config
    return _get_dept_config("recepcion")


def _staff_real_recepcion():
    """Plantilla real de Recepción (data/plantilla.json), vía la misma
    función que usa app.api_generar. deepcopy: nada de lo que hace este
    test debe poder mutar el fichero real en memoria de otro test."""
    from herramientas import cargar_plantilla
    return copy.deepcopy(cargar_plantilla("recepcion"))


def _estado_rotacion_inicial(staff_list, cfg):
    """Mismo criterio que app._estado_rotacion_inicial_cpsat: semilla de
    estado_rotacion para el primer uso del motor CP-SAT en un departamento
    (sin esto, la selección de candidato de cobertura nocturna revienta con
    "orden está vacío"). Reimplementado aquí en vez de importado de app.py
    porque es una función privada (_estado_rotacion_inicial_cpsat) sin
    garantía de estabilidad de nombre fuera de app.py; la lógica es
    trivial y está documentada 1:1 con el original."""
    elegibles = [p["name"] for p in staff_list
                 if p.get("role") != "night_fixed" and p.get("role") not in cfg.no_night_roles]
    return {
        "orden": elegibles,
        "siguiente_turno": elegibles[0] if elegibles else None,
        "deudas": {n: 0 for n in elegibles},
        "usar_orden_fijo": True,
        "historico_balance": {},
        "ausencias_night_fixed_consecutivas": 0,
    }


def _dias_forzados_libre(person: dict, cfg) -> set:
    """Replica el cálculo de forced_libre de _construir_modelo para UNA
    persona: vacaciones/peticiones/LA (forced_days_off + extra_days_off) +
    cfg.fixed_days_off del rol + cfg.night_fixed_days_off si aplica."""
    role = person.get("role")
    dias = set()
    for day_name in list(person.get("forced_days_off", [])) + list(person.get("extra_days_off", [])):
        if day_name in DAYS:
            dias.add(DAYS.index(day_name))
    dias |= set(cfg.fixed_days_off.get(role, []))
    if role == "night_fixed":
        dias |= set(cfg.night_fixed_days_off)
    return dias


def _rol_excluido_del_patron(role: str, cfg) -> bool:
    return role == "night_fixed" or bool(cfg.fixed_days_off.get(role)) or bool(cfg.fixed_roles.get(role))


def _verificar_patron_libres(nombre: str, turnos: list, dias_forzados: set):
    """Confirma, para UNA persona no excluida, que su patrón de libres
    cumple la restricción #7: entre los días no forzados, exactamente
    DIAS_LIBRES_SEGUIDOS_POR_SEMANA(2) son "Libre", y esos 2 son
    consecutivos salvo que ningún par consecutivo fuera estructuralmente
    posible entre sus días no forzados (en cuyo caso 2 sueltos es
    correcto)."""
    dias_no_forzados = [d for d in range(7) if d not in dias_forzados]
    if len(dias_no_forzados) < DIAS_LIBRES_SEGUIDOS_POR_SEMANA:
        return  # restricción omitida para esta persona esta semana (ver docstring)

    libres_no_forzados = [d for d in dias_no_forzados if turnos[d] == "Libre"]
    assert len(libres_no_forzados) == DIAS_LIBRES_SEGUIDOS_POR_SEMANA, (
        f"{nombre}: se esperaban exactamente {DIAS_LIBRES_SEGUIDOS_POR_SEMANA} "
        f"libres entre los días no forzados {dias_no_forzados}, hubo "
        f"{len(libres_no_forzados)} ({libres_no_forzados}): {turnos}"
    )

    no_forzados_set = set(dias_no_forzados)
    pares_disponibles = [
        (d, d + 1) for d in range(6)
        if d in no_forzados_set and d + 1 in no_forzados_set
    ]
    if pares_disponibles:
        assert libres_no_forzados[1] - libres_no_forzados[0] == 1, (
            f"{nombre}: había al menos un par consecutivo disponible "
            f"({pares_disponibles}), pero sus 2 libres no forzados "
            f"({libres_no_forzados}) no son consecutivos: {turnos}"
        )


# ═══════════════════════════════════════════════════════════════════════════
# 1-2. Escenario real: patrón de 2 libres seguidos, y el modelo sigue factible
# ═══════════════════════════════════════════════════════════════════════════

def test_plantilla_real_recepcion_patron_de_libres_correcto_y_sigue_factible():
    """Reproduce la semana real (2026-08-10) con la plantilla y cfg reales
    de Recepción, Fases 1-5 completas (generar_semana_cpsat_con_cobertura_
    nocturna, no _construir_modelo aislado). Debe:
      1. Resolver sin lanzar RuntimeError (seguir siendo FACTIBLE con la
         restricción nueva sumada a todas las demás).
      2. Ninguna persona termina con 0 días libres (el bug original: alguien
         trabajando los 7 días).
      3. Ninguna persona termina con 7 días libres (el otro lado del bug:
         alguien sin trabajar ninguno).
      4. Cada persona cuyo rol no tenga ya una garantía estructural de
         libres cumple el patrón exacto: 2 libres, consecutivos salvo la
         excepción de negocio confirmada (sin par consecutivo disponible)."""
    cfg = _cfg_real_recepcion()
    staff = _staff_real_recepcion()
    assert len(staff) >= 2, "la plantilla real de Recepción está vacía o casi — revisar data/plantilla.json"

    estado_rotacion = _estado_rotacion_inicial(staff, cfg)

    resultado = generar_semana_cpsat_con_cobertura_nocturna(
        staff_list=staff,
        week_start=SEMANA_HALLAZGO,
        cfg=cfg,
        estado_rotacion=estado_rotacion,
        noches_ultimos_5_dias={},
    )  # no debe lanzar RuntimeError

    schedule = resultado["schedule"]
    by_name = {p["name"]: p for p in staff}

    detalle = []
    for name, turnos in schedule.items():
        assert len(turnos) == 7
        libres = sum(1 for t in turnos if t == "Libre")
        person = by_name[name]
        detalle.append((name, libres, turnos))

        assert libres > 0, (
            f"{name} tiene 0 días libres en la semana real — exactamente el "
            f"hallazgo bloqueante que esta restricción existe para cerrar: {turnos}"
        )
        assert libres < 7, (
            f"{name} tiene 7 días libres/0 trabajados en la semana real — el "
            f"otro lado exacto del hallazgo original (el caso de 'Igor' que "
            f"reveló que un simple piso de libres no bastaba): {turnos}"
        )

        role = person.get("role")
        if not _rol_excluido_del_patron(role, cfg):
            dias_forzados = _dias_forzados_libre(person, cfg)
            _verificar_patron_libres(name, turnos, dias_forzados)

    # Pegado explícito del schedule completo para revisión humana (ver
    # criterio de cierre pedido) — visible con `pytest -s` o en un fallo.
    resumen = "\n".join(
        f"  {name}: libres={libres}  {turnos}" for name, libres, turnos in sorted(detalle)
    )
    print(f"\nSchedule real Recepción, semana {SEMANA_HALLAZGO.isoformat()}:\n{resumen}")


# ═══════════════════════════════════════════════════════════════════════════
# 3. Escenario de estrés: plantilla reducida donde el patrón de libres,
#    junto con la cobertura mínima, es genuinamente imposible
# ═══════════════════════════════════════════════════════════════════════════

def test_estres_diagnostico_senala_el_patron_de_libres_como_parte_del_conflicto():
    """Reduce la plantilla real a un tamaño donde cumplir el patrón de
    libres (restricción #7) para todos, junto con la cobertura mínima de
    todos los turnos, es genuinamente imposible (mismo patrón de estrés ya
    usado varias veces en esta migración: cobertura real, plantilla
    insuficiente). Con min_coverage real (Mañana2+Tarde2+Noche1=5) y solo 5
    "normal" (sin jefe, para no restar personal disponible a la cobertura
    de Noche vía no_night_roles), los 5 deben cubrir el mínimo TODOS los
    días sin excepción — 0 holgura estructural, imposible que además cada
    uno tenga su patrón de 2 libres.

    Confirma que el diagnóstico automático (Tema 3, Fase 5) señala esta
    restricción nueva como parte del conflicto — "Patrón de N días libres
    semanales de <nombre>" — en vez de devolver un INFEASIBLE sin explicar
    ni caer en el mensaje genérico de "conflicto estructural no cubierto"."""
    cfg = _cfg_real_recepcion()
    cfg = copy.deepcopy(cfg)
    cfg.no_night_roles = set()  # los 5 "normal" deben poder cubrir Noche también

    staff = [{"name": n, "role": "normal"} for n in ("N1", "N2", "N3", "N4", "N5")]

    model, x, names, by_name = _construir_modelo(staff, SEMANA_HALLAZGO, cfg)
    with pytest.raises(RuntimeError) as excinfo:
        _resolver_y_extraer(model, x, names, staff, cfg)

    mensaje = str(excinfo.value)
    assert "status=INFEASIBLE" in mensaje, f"debía reportar INFEASIBLE: {mensaje}"
    assert "Diagnóstico automático" in mensaje, (
        f"debía activarse el diagnóstico automático (staff_list/cfg "
        f"disponibles): {mensaje}"
    )
    assert f"Patrón de {DIAS_LIBRES_SEGUIDOS_POR_SEMANA} días libres semanales de" in mensaje, (
        f"el diagnóstico debía señalar la restricción nueva de patrón de "
        f"libres semanales como parte del conflicto mínimo, no devolver un "
        f"mensaje genérico: {mensaje}"
    )

    # Confirmación directa y adicional, llamando al diagnóstico por su
    # cuenta (fuera de la rama INFEASIBLE, como haría alguien investigando
    # manualmente) sobre el mismo staff/cfg.
    mensaje_directo = _diagnosticar_infactibilidad(staff, cfg)
    assert f"Patrón de {DIAS_LIBRES_SEGUIDOS_POR_SEMANA} días libres semanales de" in mensaje_directo, (
        f"_diagnosticar_infactibilidad debía señalar lo mismo, invocada "
        f"directamente: {mensaje_directo}"
    )
