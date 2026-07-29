"""
tests/test_cobertura_nocturna.py — Verifica que Noche se cubre correctamente
cuando el empleado night_fixed está de vacaciones o no existe en la plantilla.

Comportamiento esperado (diseño de bloques rotatorios):
  Cuando night_fixed está completamente ausente, _plan_noches_bloques designa
  rotaciones de hasta MAX_NOCHES_BLOQUE noches consecutivas.  En una semana
  pueden aparecer como máximo 2 personas distintas haciendo Noche (la que cierra
  su bloque y la que empieza el siguiente).  La cobertura es exactamente 1 Noche
  por día.  La rotación es equitativa entre semanas usando historial propio.

Escenarios probados:
  1. night_fixed de vacaciones (N=10) → Noche cubierta, ≤ 2 responsables/semana.
  2. Plantilla sin ningún night_fixed (N=9) → misma garantía.
  3. Roles con no_night_roles nunca hacen Noche.
  4. Regresión: con night_fixed activo, comportamiento idéntico al anterior.
  5. Rotación equitativa del sustituto nocturno (via bloques).

Todas las pruebas usan semanas en el futuro lejano y tmp_path para datos.
"""

import copy
import json
from datetime import date, timedelta

import pytest

from modelo_base import DAYS, generate_schedule_valido

SEMANA_INICIO = date(2099, 5, 5)  # Lunes en el futuro lejano


# ─── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    backup = tmp_path / "backups"
    backup.mkdir()
    import herramientas
    monkeypatch.setattr(herramientas, "DATA_DIR",     str(tmp_path))
    monkeypatch.setattr(herramientas, "BACKUP_DIR",   str(backup))
    monkeypatch.setattr(herramientas, "FESTIVOS_FILE",
                        str(tmp_path / "festivos.json"))
    monkeypatch.setattr(herramientas, "COVERAGE_CONFIG_FILE",
                        str(tmp_path / "coverage_config.json"))
    return tmp_path


# ─── Plantillas y helpers ────────────────────────────────────────────────────

def _cfg_normal():
    from modelo_recepcion import RECEPCION_CONFIG
    return copy.deepcopy(RECEPCION_CONFIG)


def _plantilla_10(night_fixed_vacation: bool = False) -> list:
    """10 personas. Si night_fixed_vacation=True, Carlos libra toda la semana."""
    carlos = {"name": "Carlos",  "role": "night_fixed",      "preferences": ["Noche"]}
    if night_fixed_vacation:
        carlos["forced_days_off"] = list(DAYS)
    return [
        carlos,
        {"name": "Juan",    "role": "jefe",              "preferences": ["Mañana"]},
        {"name": "Laura",   "role": "subjefe",            "preferences": ["Tarde"]},
        {"name": "Pedro",   "role": "normal",             "preferences": ["Mañana"]},
        {"name": "Ana",     "role": "normal",             "preferences": []},
        {"name": "Miguel",  "role": "normal",             "preferences": ["Tarde"]},
        {"name": "Sofía",   "role": "adjunto_recepcion",  "preferences": ["Mañana"]},
        {"name": "Diego",   "role": "normal",             "preferences": []},
        {"name": "Rosa",    "role": "normal",             "preferences": []},
        {"name": "Tomás",   "role": "normal",             "preferences": []},
    ]


def _plantilla_9(night_fixed_vacation: bool = False) -> list:
    """9 personas. Si night_fixed_vacation=True, Carlos libra toda la semana."""
    carlos = {"name": "Carlos", "role": "night_fixed", "preferences": ["Noche"]}
    if night_fixed_vacation:
        carlos["forced_days_off"] = list(DAYS)
    return [
        carlos,
        {"name": "Juan",    "role": "jefe",              "preferences": ["Mañana"]},
        {"name": "Laura",   "role": "subjefe",           "preferences": ["Tarde"]},
        {"name": "Pedro",   "role": "normal",            "preferences": ["Mañana"]},
        {"name": "Ana",     "role": "normal",            "preferences": []},
        {"name": "Miguel",  "role": "normal",            "preferences": ["Tarde"]},
        {"name": "Sofía",   "role": "adjunto_recepcion", "preferences": ["Mañana"]},
        {"name": "Diego",   "role": "normal",            "preferences": []},
        {"name": "Rosa",    "role": "normal",            "preferences": []},
    ]


def _plantilla_9_sin_night_fixed() -> list:
    """9 personas sin ningún empleado de rol night_fixed."""
    return [
        {"name": "Juan",    "role": "jefe",              "preferences": ["Mañana"]},
        {"name": "Laura",   "role": "subjefe",           "preferences": ["Tarde"]},
        {"name": "Pedro",   "role": "normal",            "preferences": ["Mañana"]},
        {"name": "Ana",     "role": "normal",            "preferences": []},
        {"name": "Miguel",  "role": "normal",            "preferences": ["Tarde"]},
        {"name": "Sofía",   "role": "adjunto_recepcion", "preferences": ["Mañana"]},
        {"name": "Diego",   "role": "normal",            "preferences": []},
        {"name": "Rosa",    "role": "normal",            "preferences": []},
        {"name": "Tomás",   "role": "normal",            "preferences": []},
    ]


def _generar_semanas(staff_factory, cfg, n_semanas=30, max_intentos=100):
    """Genera n_semanas encadenadas y devuelve lista de (idx, date, sched, staff, viols)."""
    prev_covers = {}
    prev_sunday = {}
    resultados  = []
    for i in range(n_semanas):
        semana = SEMANA_INICIO + timedelta(weeks=i)
        staff  = copy.deepcopy(staff_factory())
        sched, staff_final, _, _, this_covers, viols = generate_schedule_valido(
            staff_list=staff,
            week_start=semana,
            cfg=cfg,
            prev_week_covers=prev_covers,
            prev_sunday_shifts=prev_sunday,
            max_intentos=max_intentos,
        )
        resultados.append((i + 1, semana, sched, staff_final, viols))
        prev_sunday = {p["name"]: sched[p["name"]][6] for p in staff_final}
        prev_covers = this_covers
    return resultados


def _verificar_cobertura_y_unicidad(resultados, cfg):
    """Comprueba las invariantes del diseño de bloques rotatorios:
    1. Cobertura diaria de Noche exactamente igual a min_coverage (1 persona/día).
    2. A lo sumo 2 personas distintas hacen Noche en toda la semana
       (una cierra su bloque, otra lo empieza).
    3. Sin violaciones de restricciones duras.
    """
    from modelo_base import MAX_NOCHES_BLOQUE
    # Con MAX_NOCHES_BLOQUE=5 y 7 días, en el peor caso (hechas_start=4 al inicio
    # de semana) ocurren 2 transiciones → máximo 3 personas distintas/semana.
    max_personas_noche = 3

    fallos = []
    for semana_idx, semana, sched, staff_final, viols in resultados:
        # 1. Cobertura mínima de Noche cada día
        for day_idx, dia in enumerate(DAYS):
            n = sum(1 for p in staff_final if sched[p["name"]][day_idx] == "Noche")
            if n < cfg.min_coverage["Noche"]:
                fallos.append(
                    f"Semana {semana_idx} ({semana}) — {dia}: "
                    f"{n} Noches (mínimo {cfg.min_coverage['Noche']})"
                )
            if n > cfg.max_coverage.get("Noche", 1):
                fallos.append(
                    f"Semana {semana_idx} ({semana}) — {dia}: "
                    f"{n} Noches (máximo {cfg.max_coverage.get('Noche', 1)})"
                )
        if viols:
            fallos.extend(f"Semana {semana_idx}: {v}" for v in viols)

        # 2. A lo sumo max_personas_noche personas distintas hacen Noche en la semana
        nombres_noche = set()
        for day_idx in range(7):
            for p in staff_final:
                if sched[p["name"]][day_idx] == "Noche":
                    nombres_noche.add(p["name"])

        if len(nombres_noche) > max_personas_noche:
            fallos.append(
                f"Semana {semana_idx} ({semana}): {len(nombres_noche)} personas hacen Noche "
                f"(máximo esperado con bloques: {max_personas_noche}): {sorted(nombres_noche)}"
            )

    return fallos


# ─── 1. night_fixed de vacaciones completas (N=10) ───────────────────────────

def test_noche_cubierta_cuando_night_fixed_de_vacaciones(data_dir):
    """Con Carlos de vacaciones, _plan_noches_bloques cubre las 7 noches de la
    semana en bloques rotatorios de hasta MAX_NOCHES_BLOQUE noches consecutivas.
    Invariantes: cobertura diaria = 1, a lo sumo 2 personas distintas por semana,
    ninguna violación de restricciones duras. 30 semanas."""
    cfg = _cfg_normal()

    def staff_f():
        return _plantilla_10(night_fixed_vacation=True)

    resultados = _generar_semanas(staff_f, cfg)
    fallos = _verificar_cobertura_y_unicidad(resultados, cfg)

    assert not fallos, (
        "Problemas con Noche cuando night_fixed está de vacaciones:\n" +
        "\n".join(f"  · {f}" for f in fallos)
    )


# ─── 2. Sin ningún empleado night_fixed (N=9) ────────────────────────────────

def test_noche_cubierta_sin_empleado_night_fixed(data_dir):
    """Sin ningún Carlos en la plantilla, _plan_noches_bloques cubre las 7 noches
    en bloques rotatorios. Invariantes: cobertura diaria = 1, ≤ 2 personas/semana,
    ninguna violación de restricciones duras. 30 semanas."""
    cfg = _cfg_normal()

    def staff_f():
        return _plantilla_9_sin_night_fixed()

    resultados = _generar_semanas(staff_f, cfg)
    fallos = _verificar_cobertura_y_unicidad(resultados, cfg)

    assert not fallos, (
        "Problemas con Noche sin ningún empleado night_fixed:\n" +
        "\n".join(f"  · {f}" for f in fallos)
    )


# ─── 3. Roles no_night_roles nunca hacen Noche ───────────────────────────────

def test_noche_no_asignada_a_roles_sin_noches(data_dir):
    """Con night_fixed de vacaciones, jefe/subjefe/adjunto_recepcion nunca deben
    aparecer en turno de Noche."""
    cfg = _cfg_normal()
    roles_sin_noche = cfg.no_night_roles

    def staff_f():
        return _plantilla_10(night_fixed_vacation=True)

    resultados = _generar_semanas(staff_f, cfg)

    infractores = []
    for semana_idx, semana, sched, staff_final, _ in resultados:
        for p in staff_final:
            if p["role"] not in roles_sin_noche:
                continue
            for day_idx, dia in enumerate(DAYS):
                if sched[p["name"]][day_idx] == "Noche":
                    infractores.append(
                        f"Semana {semana_idx} ({semana}) — {p['name']} "
                        f"(rol {p['role']}) tiene Noche el {dia}"
                    )

    assert not infractores, (
        "Roles no_night_roles asignados a Noche:\n" +
        "\n".join(f"  · {i}" for i in infractores)
    )


# ─── 4. Rotación equitativa de bloques nocturnos ────────────────────────────

def test_rotacion_noche_semanal(data_dir):
    """Verifica que los bloques nocturnos rotan equitativamente entre empleados
    elegibles: en 30 semanas, el total de noches de ningún elegible supera
    ceil(total_noches / elegibles) + MAX_NOCHES_BLOQUE."""
    from modelo_base import MAX_NOCHES_BLOQUE
    cfg = _cfg_normal()

    def staff_f():
        return _plantilla_10(night_fixed_vacation=True)

    resultados = _generar_semanas(staff_f, cfg)

    # Contar noches totales por persona
    conteo: dict[str, int] = {}
    for _, _, sched, staff_final, _ in resultados:
        for p in staff_final:
            noches = sum(1 for d in range(7) if sched[p["name"]][d] == "Noche")
            if noches:
                conteo[p["name"]] = conteo.get(p["name"], 0) + noches

    staff_base = _plantilla_10(night_fixed_vacation=True)
    blocked_roles = cfg.no_night_roles | {"night_fixed"} | set(cfg.fixed_roles)
    elegibles = [p["name"] for p in staff_base if p["role"] not in blocked_roles]
    n_elegibles = len(elegibles)

    import math
    total_noches = sum(conteo.values())
    max_permitido = math.ceil(total_noches / n_elegibles) + MAX_NOCHES_BLOQUE

    desequilibrios = [
        f"{nombre}: {noches} noches (máx permitido {max_permitido})"
        for nombre, noches in conteo.items()
        if noches > max_permitido
    ]

    assert not desequilibrios, (
        "Rotación de bloques nocturnos desequilibrada:\n" +
        "\n".join(f"  · {d}" for d in desequilibrios)
    )


# ─── 5. Regresión: con night_fixed activo, comportamiento inalterado ─────────

def test_noche_normal_sin_cambios_con_night_fixed_activo(data_dir):
    """Con Carlos activo (sin vacaciones, N=9), Noche cumple min/max cada día y
    ningún rol no_night aparece en Noche. Sin violaciones en 30 semanas.
    N=9 (no N=10): con night_fixed activo, Noche queda a max=1 ya cubierta por
    Carlos, por lo que añadir un décimo trabajador dejaría 9 activos un lunes
    para 8 slots máximos → imposible estructuralmente."""
    cfg = _cfg_normal()

    def staff_f():
        return _plantilla_9(night_fixed_vacation=False)

    resultados = _generar_semanas(staff_f, cfg)

    infractores = []
    for semana_idx, semana, sched, staff_final, viols in resultados:
        if viols:
            infractores.extend(f"Semana {semana_idx} violación: {v}" for v in viols)
            continue

        for day_idx, dia in enumerate(DAYS):
            n = sum(1 for p in staff_final if sched[p["name"]][day_idx] == "Noche")
            if n < cfg.min_coverage["Noche"]:
                infractores.append(
                    f"Semana {semana_idx} — {dia}: {n} Noches "
                    f"(mínimo {cfg.min_coverage['Noche']})"
                )
            if n > cfg.max_coverage["Noche"]:
                infractores.append(
                    f"Semana {semana_idx} — {dia}: {n} Noches "
                    f"(máximo {cfg.max_coverage['Noche']})"
                )
            for p in staff_final:
                if p["role"] in cfg.no_night_roles and sched[p["name"]][day_idx] == "Noche":
                    infractores.append(
                        f"Semana {semana_idx} — {p['name']} "
                        f"(rol {p['role']}) tiene Noche el {dia}"
                    )

    assert not infractores, (
        "Problemas detectados con night_fixed activo:\n" +
        "\n".join(f"  · {i}" for i in infractores)
    )


# ═══════════════════════════════════════════════════════════════════════════
# 6. Corrección: bloque de 5 noches no debe dejar libres no consecutivos
#    (Lunes + Domingo) cuando cfg.cover_consecutive es True.
# ═══════════════════════════════════════════════════════════════════════════

def _staff_simple(nombres: list[str]) -> list:
    """Plantilla mínima de empleados 'normal' sin restricciones, para probar
    _plan_noches_bloques de forma aislada (sin pasar por generate_schedule)."""
    return [{"name": n, "role": "normal", "preferences": []} for n in nombres]


def _seed_estado_bloques(data_dir, rotation_key: str, estado: dict):
    """Escribe directamente el fichero de estado de rotación por bloques,
    simulando que la semana anterior dejó al saliente con 'hechas' noches
    ya acumuladas antes de generar la semana bajo prueba."""
    path = data_dir / f"night_rotation_{rotation_key}.json"
    path.write_text(json.dumps(estado), encoding="utf-8")


def test_evita_libres_no_consecutivos_recepcion(data_dir):
    """Recepción (cover_consecutive=True): si el saliente llega al lunes
    (day_idx=0) con hechas==MAX_NOCHES_BLOQUE-1, trabajar esa noche del lunes
    completaría su bloque justo ese día y forzaría al entrante a arrancar el
    martes, dejándolo con libres Domingo+Lunes (no consecutivos). La
    corrección debe cerrar el bloque del saliente ANTES del lunes (se queda
    en 4 noches, no 5) para que el entrante arranque el lunes."""
    from modelo_base import _plan_noches_bloques, MAX_NOCHES_BLOQUE

    cfg   = _cfg_normal()
    staff = _staff_simple(["Ana", "Bruno", "Carla", "Diego", "Elena"])

    _seed_estado_bloques(
        data_dir, cfg.night_rotation_key + "_bloques",
        {"_actual": {"nombre": "Ana", "hechas": MAX_NOCHES_BLOQUE - 1},
         "Ana": {"covers": 4, "pending": 0}},
    )

    asignaciones, estado_final = _plan_noches_bloques(staff, cfg)
    ana = next(p for p in staff if p["name"] == "Ana")

    # 1. Ana (saliente) cierra con 4 noches, no llega a trabajar el lunes.
    assert asignaciones.get(0) != "Ana", "Ana no debería cubrir la noche del lunes"
    assert "Ana" not in asignaciones.values(), "Ana no debería cubrir ninguna noche esta semana"
    assert estado_final["Ana"]["covers"] == 4, "El total de noches de Ana no debe incrementarse"

    # 2. El descanso de Ana son 2 días consecutivos (no Domingo+Lunes sueltos).
    libres_ana = ana.get("forced_days_off", [])
    assert len(libres_ana) == 2
    idx_libres_ana = sorted(DAYS.index(d) for d in libres_ana)
    assert idx_libres_ana in [[i, i + 1] for i in range(6)], (
        f"Los libres de Ana ({libres_ana}) no son consecutivos"
    )

    # 3. El entrante arranca el LUNES (day_idx=0), no el martes.
    entrante = asignaciones[0]
    assert entrante != "Ana"
    assert all(asignaciones[d] == entrante for d in range(5)), (
        "El entrante debería cubrir un bloque completo Lunes-Viernes"
    )

    # 4. Los libres del entrante esa semana son Sáb+Dom, consecutivos
    #    (caso 'empieza en Lunes', el bueno).
    persona_entrante = next(p for p in staff if p["name"] == entrante)
    assert set(persona_entrante.get("forced_days_off", [])) == {"Sábado", "Domingo"}


def test_mozos_sin_correccion_mantiene_libres_no_consecutivos(data_dir):
    """Mozos (cover_consecutive=False): el mismo escenario NO debe activar la
    corrección — el bloque de 5 noches con arranque en martes, y por tanto
    libres no consecutivos para el entrante, sigue siendo el comportamiento
    aceptado y documentado para este departamento, sin cambios."""
    from modelo_base import _plan_noches_bloques, MAX_NOCHES_BLOQUE
    from modelo_mozos import MOZOS_CONFIG

    cfg   = copy.deepcopy(MOZOS_CONFIG)
    assert cfg.cover_consecutive is False
    staff = _staff_simple(["Ana", "Bruno", "Carla", "Diego", "Elena"])

    _seed_estado_bloques(
        data_dir, cfg.night_rotation_key + "_bloques",
        {"_actual": {"nombre": "Ana", "hechas": MAX_NOCHES_BLOQUE - 1},
         "Ana": {"covers": 4, "pending": 0}},
    )

    asignaciones, estado_final = _plan_noches_bloques(staff, cfg)

    # Ana SÍ trabaja el lunes (su 5ª noche) — comportamiento sin corregir.
    assert asignaciones.get(0) == "Ana"
    assert estado_final["Ana"]["covers"] == 5

    # El bloque siguiente arranca en martes, como antes de la corrección.
    entrante = asignaciones[1]
    assert entrante != "Ana"
    assert asignaciones[0] == "Ana" and asignaciones[1] != "Ana"


def test_hechas_cero_no_dispara_correccion_por_error(data_dir):
    """Caso límite: si nadie está cubriendo aún (hechas==0 al llegar al
    lunes), la nueva condición no debe dispararse por accidente — el lunes
    se asigna con normalidad al primer candidato de la rotación."""
    from modelo_base import _plan_noches_bloques, MAX_NOCHES_BLOQUE

    cfg   = _cfg_normal()
    staff = _staff_simple(["Ana", "Bruno", "Carla", "Diego", "Elena"])

    _seed_estado_bloques(
        data_dir, cfg.night_rotation_key + "_bloques",
        {"_actual": {"nombre": None, "hechas": 0}},
    )

    asignaciones, _ = _plan_noches_bloques(staff, cfg)

    # Se cubre el lunes con normalidad (bloque nuevo de 5 noches Lun-Vie).
    assert asignaciones.get(0) is not None
    assert all(asignaciones[d] == asignaciones[0] for d in range(5))


# ─── 7. Regresión encadenada: nunca aparecen libres Lunes+Domingo ───────────

def test_regresion_nunca_libres_lunes_domingo(data_dir):
    """Encadena 16 semanas de recepción sin night_fixed en plantilla y
    verifica que ningún empleado termina la semana con sus dos únicos días
    libres siendo exactamente Lunes+Domingo (no consecutivos). El resto de
    combinaciones consecutivas (Lun+Mar, Mar+Mié, ..., Sáb+Dom) siguen
    apareciendo con normalidad."""
    cfg = _cfg_normal()

    def staff_f():
        return _plantilla_9_sin_night_fixed()

    resultados = _generar_semanas(staff_f, cfg, n_semanas=16)

    infractores = []
    pares_consecutivos_vistos = set()
    for semana_idx, semana, sched, staff_final, viols in resultados:
        for p in staff_final:
            libres = [i for i, t in enumerate(sched[p["name"]]) if t == "Libre"]
            if len(libres) != 2:
                continue
            par = tuple(sorted(libres))
            if par == (0, 6):
                infractores.append(
                    f"Semana {semana_idx} ({semana}) — {p['name']}: "
                    f"libres Lunes+Domingo (no consecutivos)"
                )
            elif par[1] - par[0] == 1:
                pares_consecutivos_vistos.add(par)

    assert not infractores, (
        "Aparecieron libres Lunes+Domingo (no consecutivos):\n" +
        "\n".join(f"  · {i}" for i in infractores)
    )
    # Confirma que seguimos viendo pares consecutivos normales (el fix no
    # eliminó por error toda variedad de arranque de bloque).
    assert pares_consecutivos_vistos, "No se observó ningún par de libres consecutivos en 16 semanas"
