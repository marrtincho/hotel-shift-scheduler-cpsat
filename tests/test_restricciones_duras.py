"""
tests/test_restricciones_duras.py — Verifica que los límites min/max de cobertura
y la regla no-Mañana-tras-Tarde/Noche son restricciones duras, no advertencias.

Todas las pruebas usan semanas en futuro lejano (año 2099) y directorios temporales.
"""

import copy
from datetime import date, timedelta

import pytest

SEMANA_INICIO = date(2099, 2, 3)   # Lunes en el futuro lejano


# ─── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Redirige DATA_DIR y BACKUP_DIR al directorio temporal de la prueba."""
    backup = tmp_path / "backups"
    backup.mkdir()

    import herramientas
    monkeypatch.setattr(herramientas, "DATA_DIR",     str(tmp_path))
    monkeypatch.setattr(herramientas, "BACKUP_DIR",   str(backup))
    monkeypatch.setattr(herramientas, "FESTIVOS_FILE",
                        str(tmp_path / "festivos.json"))
    return tmp_path


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _plantilla_recepcion():
    import herramientas
    return copy.deepcopy(herramientas.PLANTILLA_DEFAULT["recepcion"])


def _generar_semana(staff, semana, cfg, prev_covers, prev_sunday, max_intentos=50):
    from modelo_base import generate_schedule_valido
    return generate_schedule_valido(
        staff_list=copy.deepcopy(staff),
        week_start=semana,
        cfg=cfg,
        prev_week_covers=prev_covers,
        prev_sunday_shifts=prev_sunday,
        max_intentos=max_intentos,
    )


# ─── Tests unitarios del look-ahead ──────────────────────────────────────────

def test_look_ahead_no_bloquea_persona_libre_dia_siguiente():
    """Bug del eligible -= 1 incondicional: si la persona a evaluar libra el día
    siguiente, no estaba contada en eligible y restarle 1 produce un conteo
    artificialmente bajo que bloquea Tarde de forma incorrecta.

    Escenario mínimo (Viernes → Sábado):
      - E1 libra Sábado → no afecta la cobertura de Mañana el sábado.
      - E2 y E3 ya tienen Tarde en Viernes (trabajan Sábado) → no elegibles para Mañana Sáb.
      - NF y E4 trabajan Sábado y no tienen Tarde/Noche en Viernes → los 2 elegibles.
      - min_coverage["Mañana"] = 2.
      - eligible real = 2 (NF + E4): ≥ 2 → Tarde debe estar PERMITIDA para E1.
      - Con el bug: eligible -= 1 → 1 < 2 → Tarde bloqueada (incorrecto).
      - Con el fix: eligible sin cambio → 2 ≥ 2 → Tarde permitida (correcto).
    """
    from modelo_base import _morning_coverage_ok
    from modelo_recepcion import RECEPCION_CONFIG

    cfg = copy.deepcopy(RECEPCION_CONFIG)  # min_coverage["Mañana"] = 2

    staff = [
        {"name": "NF",   "role": "night_fixed"},
        {"name": "Jefe", "role": "jefe"},
        {"name": "E1",   "role": "normal"},    # libra Sábado (K_person)
        {"name": "E2",   "role": "normal"},    # Tarde en Viernes, trabaja Sábado
        {"name": "E3",   "role": "normal"},    # Tarde en Viernes, trabaja Sábado
        {"name": "E4",   "role": "normal"},    # trabaja Vie y Sáb (no Tarde/Noche en Vie)
    ]

    # Estado del horario al momento de evaluar a E1 en Viernes (day_idx=4):
    #   NF:   Libre en Vie (días libres del night_fixed = Jue+Vie), trabaja Sáb
    #   Jefe: Mañana en Vie, Libre en Sáb+Dom
    #   E1:   pendiente (=''), Libre en Sáb
    #   E2:   Tarde en Vie, trabaja Sáb
    #   E3:   Tarde en Vie, trabaja Sáb
    #   E4:   pendiente (=''), trabaja Sáb
    schedule = {
        "NF":   ["", "", "", "Libre", "Libre", "", ""],   # libra Jue+Vie
        "Jefe": ["", "", "", "", "Mañana", "Libre", "Libre"],
        "E1":   ["", "", "", "", "",        "Libre", "Libre"],  # libra Sáb+Dom
        "E2":   ["", "", "", "", "Tarde",   "",      ""],
        "E3":   ["", "", "", "", "Tarde",   "",      ""],
        "E4":   ["", "", "", "", "",        "",      ""],
    }

    # eligible real para Mañana Sábado (trabajan Sáb Y no tienen Tarde/Noche en Vie):
    #   NF: trabaja Sáb ✓, Libre en Vie (no Tarde/Noche) ✓  → cuenta
    #   Jefe: Libre Sáb → NO cuenta
    #   E1: Libre Sáb → NO cuenta (es la persona que se evalúa)
    #   E2: trabaja Sáb ✓, Tarde Vie → NO cuenta
    #   E3: trabaja Sáb ✓, Tarde Vie → NO cuenta
    #   E4: trabaja Sáb ✓, "" Vie (no Tarde/Noche) ✓ → cuenta
    # eligible = 2 (NF + E4) ≥ min_man=2 → Tarde debe estar PERMITIDA para E1.
    result = _morning_coverage_ok("Tarde", 4, "E1", schedule, staff, cfg)
    assert result is True, (
        "E1 libra el sábado y no afecta la cobertura de Mañana: "
        "Tarde debe estar permitida en Viernes (bug del eligible -= 1 incondicional)"
    )


def test_look_ahead_si_bloquea_cuando_quedan_pocos_elegibles():
    """Contraejemplo: si E1 TRABAJA el sábado y solo quedan 2 elegibles
    (de los cuales E1 es uno), asignarle Tarde dejaría solo 1 → debe bloquearse."""
    from modelo_base import _morning_coverage_ok
    from modelo_recepcion import RECEPCION_CONFIG

    cfg = copy.deepcopy(RECEPCION_CONFIG)  # min_coverage["Mañana"] = 2

    staff = [
        {"name": "NF",   "role": "night_fixed"},
        {"name": "Jefe", "role": "jefe"},
        {"name": "E1",   "role": "normal"},    # TRABAJA Sábado (no está libre)
        {"name": "E2",   "role": "normal"},    # Tarde en Viernes, trabaja Sábado
        {"name": "E3",   "role": "normal"},    # Tarde en Viernes, trabaja Sábado
    ]

    # E1 trabaja Sábado (no tiene Libre en Sáb).
    # eligible real = NF (Libre Vie, trabaja Sáb) + E1 (trabaja Sáb, "" Vie) = 2.
    # Si E1 hace Tarde → le restamos 1 correctamente → 1 < 2 → BLOQUEADO (correcto).
    schedule = {
        "NF":   ["", "", "", "Libre", "Libre", "", ""],
        "Jefe": ["", "", "", "", "Mañana", "Libre", "Libre"],
        "E1":   ["", "", "", "", "",      "",      ""],   # trabaja Sáb
        "E2":   ["", "", "", "", "Tarde", "",      ""],
        "E3":   ["", "", "", "", "Tarde", "",      ""],
    }

    result = _morning_coverage_ok("Tarde", 4, "E1", schedule, staff, cfg)
    assert result is False, (
        "E1 trabaja el sábado y es uno de solo 2 elegibles para Mañana: "
        "Tarde debe estar BLOQUEADA (correcto)"
    )


# ─── Tests de integración: 30 semanas consecutivas ───────────────────────────

def test_30_semanas_sin_violaciones(data_dir):
    """30 semanas consecutivas con la plantilla por defecto de recepción no deben
    producir ninguna violación de cobertura ni de la regla no-Mañana-tras-Tarde/Noche."""
    import herramientas  # noqa: F401 — garantiza que DATA_DIR está parcheado
    from modelo_recepcion import RECEPCION_CONFIG
    from modelo_base import DAYS

    cfg          = copy.deepcopy(RECEPCION_CONFIG)
    staff        = _plantilla_recepcion()
    prev_covers  = {}
    prev_sunday  = {}

    for i in range(30):
        semana = SEMANA_INICIO + timedelta(weeks=i)
        sched, staff_final, _, _, this_covers, violaciones = _generar_semana(
            staff, semana, cfg, prev_covers, prev_sunday
        )

        # Sin violaciones de ningún tipo (incluye SIN_ASIGNAR)
        assert not violaciones, (
            f"Semana {i + 1} ({semana}): violaciones inesperadas:\n"
            + "\n".join(f"  · {v}" for v in violaciones)
        )

        # Cobertura Mañana y Tarde entre 2 y 3 cada día
        for day_idx, day in enumerate(DAYS):
            for shift in ("Mañana", "Tarde"):
                count = sum(
                    1 for p in staff_final
                    if sched[p["name"]][day_idx] == shift
                )
                assert cfg.min_coverage[shift] <= count <= cfg.max_coverage[shift], (
                    f"Semana {i+1} ({semana}) — {day} {shift}: {count} personas "
                    f"(esperado {cfg.min_coverage[shift]}–{cfg.max_coverage[shift]})"
                )

        # Mañana del Sábado: al menos 2 personas que NO hicieron Tarde/Noche el Viernes
        viable_sab_man = sum(
            1 for p in staff_final
            if sched[p["name"]][5] == "Mañana"
            and sched[p["name"]][4] not in ("Tarde", "Noche")
        )
        assert viable_sab_man >= cfg.min_coverage["Mañana"], (
            f"Semana {i+1} ({semana}): solo {viable_sab_man} persona(s) para Mañana Sábado "
            f"sin haber hecho Tarde/Noche el Viernes (mínimo {cfg.min_coverage['Mañana']})"
        )

        prev_sunday = {p["name"]: sched[p["name"]][6] for p in staff_final}
        prev_covers = this_covers



def test_30_semanas_plantilla_ampliada(data_dir):
    """Mismo test con una plantilla de 9 personas (team más grande).

    Con 9 personas: K=2 (dos con Sáb+Dom libres) + jefe + sustituto = 4 libres en Sáb.
    En Viernes, el peor caso es 8 trabajando (Carlos + persona(Jue+Vie) libres = 2 libres)
    que es exactamente la capacidad máxima de slots. No excede los límites.
    """
    import herramientas  # noqa: F401
    from modelo_recepcion import RECEPCION_CONFIG
    from modelo_base import DAYS

    cfg = copy.deepcopy(RECEPCION_CONFIG)

    staff_ampliado = [
        {"name": "Carlos",  "role": "night_fixed",       "preferences": ["Noche"]},
        {"name": "Juan",    "role": "jefe",               "preferences": ["Mañana"]},
        {"name": "Laura",   "role": "subjefe",            "preferences": ["Tarde"]},
        {"name": "Pedro",   "role": "normal",             "preferences": ["Mañana"]},
        {"name": "Ana",     "role": "normal",             "preferences": []},
        {"name": "Miguel",  "role": "normal",             "preferences": ["Tarde"]},
        {"name": "Sofía",   "role": "adjunto_recepcion",  "preferences": ["Mañana"]},
        {"name": "Diego",   "role": "normal",             "preferences": []},
        {"name": "Rosa",    "role": "normal",             "preferences": ["Tarde"]},
    ]

    prev_covers = {}
    prev_sunday = {}

    for i in range(30):
        semana = SEMANA_INICIO + timedelta(weeks=i)
        sched, staff_final, _, _, this_covers, violaciones = _generar_semana(
            staff_ampliado, semana, cfg, prev_covers, prev_sunday
        )

        assert not violaciones, (
            f"Semana {i + 1} ({semana}) [plantilla ampliada]: violaciones:\n"
            + "\n".join(f"  · {v}" for v in violaciones)
        )

        for day_idx, day in enumerate(DAYS):
            for shift in ("Mañana", "Tarde"):
                count = sum(
                    1 for p in staff_final
                    if sched[p["name"]][day_idx] == shift
                )
                assert cfg.min_coverage[shift] <= count <= cfg.max_coverage[shift], (
                    f"Semana {i+1} ({semana}) [ampliada] — {day} {shift}: {count} personas"
                )

        viable_sab_man = sum(
            1 for p in staff_final
            if sched[p["name"]][5] == "Mañana"
            and sched[p["name"]][4] not in ("Tarde", "Noche")
        )
        assert viable_sab_man >= cfg.min_coverage["Mañana"], (
            f"Semana {i+1} ({semana}) [ampliada]: solo {viable_sab_man} "
            f"para Mañana Sábado sin haber hecho Tarde/Noche el Viernes"
        )

        prev_sunday = {p["name"]: sched[p["name"]][6] for p in staff_final}
        prev_covers = this_covers


# ─── Test del caso imposible (regresión) ─────────────────────────────────────

def test_caso_imposible_devuelve_violaciones(data_dir):
    """Con solo 3 personas (insuficientes para cubrir Mañana=2, Tarde=2, Noche=1),
    generate_schedule_valido debe devolver violaciones explicativas sin lanzar excepción."""
    import herramientas  # noqa: F401
    from modelo_recepcion import RECEPCION_CONFIG

    cfg = copy.deepcopy(RECEPCION_CONFIG)
    staff_mini = [
        {"name": "NocheF", "role": "night_fixed", "preferences": []},
        {"name": "Emp1",   "role": "normal",       "preferences": []},
        {"name": "Emp2",   "role": "normal",       "preferences": []},
    ]

    _, _, _, _, _, violaciones = _generar_semana(
        staff_mini, SEMANA_INICIO, cfg, {}, {}
    )

    assert violaciones, "Con 3 personas debe haber violaciones de cobertura"
    assert all(isinstance(v, str) and v for v in violaciones), (
        "Cada violación debe ser un string no vacío"
    )
    # Verificar que las violaciones son legibles (contienen al menos un día o nombre)
    from modelo_base import DAYS
    dias_o_nombres = set(DAYS) | {p["name"] for p in staff_mini}
    assert any(
        any(token in v for token in dias_o_nombres) for v in violaciones
    ), f"Las violaciones no parecen legibles: {violaciones}"
