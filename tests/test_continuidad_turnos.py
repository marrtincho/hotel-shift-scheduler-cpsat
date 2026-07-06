"""
tests/test_continuidad_turnos.py — Pruebas de continuidad de turno dentro de
tiradas y rotación de turno al volver de librar.

Reglas probadas (preferencias blandas):
  · Dentro de una tirada (sin Libre en medio): mantener el mismo turno.
  · Al volver de librar: rotar al turno distinto.

Las restricciones duras (min/max coverage, no-Mañana-tras-Tarde/Noche) siempre
mandan sobre las preferencias blandas.

Todas las pruebas usan semanas en futuro lejano (2099) y DATA_DIR temporal.
"""

import copy
from datetime import date, timedelta

import pytest

from modelo_base import (
    DAYS,
    _contar_penalizaciones,
    generate_schedule,
    generate_schedule_valido,
    validar_schedule,
)

SEMANA_INICIO = date(2099, 3, 3)  # Lunes en futuro lejano


# ─── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    backup = tmp_path / "backups"
    backup.mkdir()
    import herramientas
    monkeypatch.setattr(herramientas, "DATA_DIR",      str(tmp_path))
    monkeypatch.setattr(herramientas, "BACKUP_DIR",    str(backup))
    monkeypatch.setattr(herramientas, "FESTIVOS_FILE", str(tmp_path / "festivos.json"))
    return tmp_path


# ─── Plantillas ───────────────────────────────────────────────────────────────

def _plantilla_recepcion():
    """Plantilla mínima de recepción: 8 personas."""
    return [
        {"name": "Carlos",  "role": "night_fixed",      "preferences": ["Noche"]},
        {"name": "Juan",    "role": "jefe",              "preferences": ["Mañana"]},
        {"name": "Laura",   "role": "subjefe",           "preferences": ["Tarde"]},
        {"name": "Pedro",   "role": "normal",            "preferences": ["Mañana"]},
        {"name": "Ana",     "role": "normal",            "preferences": []},
        {"name": "Miguel",  "role": "normal",            "preferences": ["Tarde"]},
        {"name": "Sofía",   "role": "adjunto_recepcion", "preferences": ["Mañana"]},
        {"name": "Diego",   "role": "normal",            "preferences": []},
    ]


def _plantilla_mozos():
    """Plantilla de mozos: 5 personas."""
    return [
        {"name": "Fran",  "role": "night_fixed", "preferences": ["Noche"]},
        {"name": "Rubén", "role": "normal",       "preferences": ["Mañana"]},
        {"name": "Gloria","role": "normal",       "preferences": ["Tarde"]},
        {"name": "Tomás", "role": "normal",       "preferences": []},
        {"name": "Inés",  "role": "normal",       "preferences": ["Mañana"]},
    ]


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _build_prev_last(schedule, staff_final):
    """Construye prev_last_shifts desde el schedule de la semana anterior."""
    resultado = {}
    for p in staff_final:
        turnos = schedule.get(p["name"], [])
        ult, tras_libre = None, False
        for t in reversed(turnos):
            if t == "Libre":
                tras_libre = True
                continue
            if t and t not in ("SIN_ASIGNAR", ""):
                ult = t
                break
        if ult is not None:
            resultado[p["name"]] = (ult, tras_libre)
    return resultado


def _roles_fijos(cfg):
    return cfg.no_night_roles | {"night_fixed"} | set(cfg.fixed_roles)


def _simular_semanas(n, plantilla_fn, cfg, max_intentos=50):
    """Simula n semanas encadenadas; devuelve lista de (sched, staff, viols)."""
    prev_covers    = {}
    prev_sunday    = {}
    prev_last      = {}
    resultados     = []

    for i in range(n):
        semana = SEMANA_INICIO + timedelta(weeks=i)
        staff  = copy.deepcopy(plantilla_fn())

        sched, staff_final, _, _, this_covers, viols = generate_schedule_valido(
            staff_list        = staff,
            week_start        = semana,
            cfg               = cfg,
            prev_week_covers  = prev_covers,
            prev_sunday_shifts= prev_sunday,
            prev_last_shifts  = prev_last,
            max_intentos      = max_intentos,
        )
        resultados.append((sched, staff_final, viols))
        prev_covers = this_covers
        prev_sunday = {p["name"]: sched[p["name"]][6] for p in staff_final}
        prev_last   = _build_prev_last(sched, staff_final)

    return resultados


# ─── Test 1 — Continuidad dentro de tirada (recepción) ───────────────────────

def test_continuidad_tirada_recepcion(data_dir):
    """Una sola semana. Cada empleado no fijo tiene como máximo 2 penalizaciones
    de continuidad (cambios dentro de tirada sin Libre en medio).
    Las restricciones duras pueden forzar algún cruce puntual."""
    from modelo_recepcion import RECEPCION_CONFIG
    cfg = copy.deepcopy(RECEPCION_CONFIG)

    staff  = copy.deepcopy(_plantilla_recepcion())
    sched, staff_final, viols = _simular_semanas(1, _plantilla_recepcion, cfg)[0]

    roles_fijos = _roles_fijos(cfg)
    infractores = []
    for p in staff_final:
        if p["role"] in roles_fijos:
            continue
        pen_persona = 0
        turnos = sched[p["name"]]
        ult, tras_libre = None, False
        for t in turnos:
            if t in ("", "SIN_ASIGNAR", "Noche"):
                continue
            if t == "Libre":
                tras_libre = True
                continue
            if ult is not None and not tras_libre and t != ult:
                pen_persona += 1
            ult, tras_libre = t, False
        if pen_persona > 2:
            infractores.append(
                f"{p['name']}: {pen_persona} cambios en tirada "
                f"(turnos={turnos})"
            )

    assert not infractores, (
        "Empleados con demasiados cambios de turno dentro de tirada:\n" +
        "\n".join(f"  · {i}" for i in infractores)
    )


# ─── Test 2 — Rotación tras librar (recepción, 10 semanas) ───────────────────

def test_rotacion_tras_librar_recepcion(data_dir):
    """En 10 semanas encadenadas, para cada empleado no fijo, el número de veces
    que rotó el turno al volver de librar debe superar las veces que repitió
    turno (la rotación debe ser mayoritaria)."""
    from modelo_recepcion import RECEPCION_CONFIG
    cfg = copy.deepcopy(RECEPCION_CONFIG)

    resultados  = _simular_semanas(10, _plantilla_recepcion, cfg)
    roles_fijos = _roles_fijos(cfg)

    # Reconstruir la cadena de prev_last para cada semana
    prev_last = {}
    mismo_turno  = {}  # nombre → veces volvió al mismo turno
    rotacion     = {}  # nombre → veces rotó

    for sched, staff_final, _ in resultados:
        for p in staff_final:
            if p["role"] in roles_fijos:
                continue
            nombre = p["name"]
            mismo_turno.setdefault(nombre, 0)
            rotacion.setdefault(nombre, 0)

            prev = prev_last.get(nombre)
            ult_prev = prev[0] if isinstance(prev, (tuple, list)) else prev
            tras_prev = prev[1] if isinstance(prev, (tuple, list)) else False

            ult, tras_libre = ult_prev, tras_prev
            for t in sched[nombre]:
                if t in ("", "SIN_ASIGNAR", "Noche"):
                    continue
                if t == "Libre":
                    tras_libre = True
                    continue
                if ult is not None and tras_libre:
                    if t == ult:
                        mismo_turno[nombre] += 1
                    else:
                        rotacion[nombre] += 1
                ult, tras_libre = t, False

        prev_last = _build_prev_last(sched, staff_final)

    infractores = []
    for nombre in rotacion:
        m = mismo_turno.get(nombre, 0)
        r = rotacion.get(nombre, 0)
        if (m + r) > 0 and m >= r:
            infractores.append(f"{nombre}: volvió al mismo={m}, rotó={r}")

    assert not infractores, (
        "Empleados donde la repetición de turno supera la rotación:\n" +
        "\n".join(f"  · {i}" for i in infractores)
    )


# ─── Test 4 — Restricciones duras intactas (recepción, 10 semanas) ───────────

def test_restricciones_duras_recepcion(data_dir):
    """Las 10 semanas encadenadas no producen violaciones de cobertura ni de
    no-Mañana-tras-Tarde/Noche."""
    from modelo_recepcion import RECEPCION_CONFIG
    cfg = copy.deepcopy(RECEPCION_CONFIG)

    resultados = _simular_semanas(10, _plantilla_recepcion, cfg)

    prev_sunday = {}
    infractores = []
    for idx, (sched, staff_final, viols) in enumerate(resultados, 1):
        if viols:
            infractores.extend(f"Semana {idx}: {v}" for v in viols)
        fallos_val = validar_schedule(sched, staff_final, cfg, prev_sunday)
        infractores.extend(f"Semana {idx} (validar): {f}" for f in fallos_val)
        prev_sunday = {p["name"]: sched[p["name"]][6] for p in staff_final}

    assert not infractores, (
        "Violaciones de restricciones duras:\n" +
        "\n".join(f"  · {i}" for i in infractores)
    )


# ─── Test 5 — Mozos: continuidad y restricciones duras ───────────────────────

def test_continuidad_tirada_mozos(data_dir):
    """Test 1 aplicado a la plantilla de mozos."""
    from modelo_mozos import MOZOS_CONFIG
    cfg = copy.deepcopy(MOZOS_CONFIG)

    sched, staff_final, _ = _simular_semanas(1, _plantilla_mozos, cfg)[0]

    roles_fijos = _roles_fijos(cfg)
    infractores = []
    for p in staff_final:
        if p["role"] in roles_fijos:
            continue
        pen_persona = 0
        turnos = sched[p["name"]]
        ult, tras_libre = None, False
        for t in turnos:
            if t in ("", "SIN_ASIGNAR", "Noche"):
                continue
            if t == "Libre":
                tras_libre = True
                continue
            if ult is not None and not tras_libre and t != ult:
                pen_persona += 1
            ult, tras_libre = t, False
        if pen_persona > 2:
            infractores.append(
                f"{p['name']}: {pen_persona} cambios en tirada "
                f"(turnos={turnos})"
            )

    assert not infractores, (
        "Mozos con demasiados cambios de turno dentro de tirada:\n" +
        "\n".join(f"  · {i}" for i in infractores)
    )


def test_restricciones_duras_mozos(data_dir):
    """Test 4 aplicado a la plantilla de mozos (10 semanas)."""
    from modelo_mozos import MOZOS_CONFIG
    cfg = copy.deepcopy(MOZOS_CONFIG)

    resultados = _simular_semanas(10, _plantilla_mozos, cfg)

    prev_sunday = {}
    infractores = []
    for idx, (sched, staff_final, viols) in enumerate(resultados, 1):
        if viols:
            infractores.extend(f"Semana {idx}: {v}" for v in viols)
        fallos_val = validar_schedule(sched, staff_final, cfg, prev_sunday)
        infractores.extend(f"Semana {idx} (validar): {f}" for f in fallos_val)
        prev_sunday = {p["name"]: sched[p["name"]][6] for p in staff_final}

    assert not infractores, (
        "Violaciones de restricciones duras en mozos:\n" +
        "\n".join(f"  · {i}" for i in infractores)
    )
