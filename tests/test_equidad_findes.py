"""
tests/test_equidad_findes.py — Tests de equidad en la distribución de fines de
semana libres a largo plazo.

Todas las pruebas usan semanas en futuro lejano (año 2099) y un DATA_DIR
temporal, nunca tocan datos reales de producción.
"""

import copy
from datetime import date, timedelta

import pytest

# Lunes en futuro lejano para no interferir con el historial real
SEMANA_INICIO = date(2099, 1, 7)


# ─── Fixture ─────────────────────────────────────────────────────────────────

@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Redirige DATA_DIR y archivos auxiliares a un directorio temporal."""
    backup = tmp_path / "backups"
    backup.mkdir()

    import herramientas
    monkeypatch.setattr(herramientas, "DATA_DIR",      str(tmp_path))
    monkeypatch.setattr(herramientas, "BACKUP_DIR",    str(backup))
    monkeypatch.setattr(herramientas, "FESTIVOS_FILE", str(tmp_path / "festivos.json"))
    return tmp_path


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _plantilla_base():
    from herramientas import PLANTILLA_DEFAULT
    return copy.deepcopy(PLANTILLA_DEFAULT["recepcion"])


def _simular(n_semanas, semana_extra_staff=None):
    """
    Simula n_semanas semanas consecutivas con la plantilla por defecto de
    recepción. Registra cada semana en el historial temporal.

    semana_extra_staff: {indice_semana (0-based): dict_persona} para incorporar
    un empleado nuevo a partir de esa semana.
    """
    from herramientas import registrar_semana
    from app import _get_dept_config
    from modelo_base import generate_schedule

    # cfg construido igual que en producción (app._get_dept_config), no la
    # DeptConfig estática de modelo_recepcion.py: incorpora el parche de
    # roles_excluidos_equidad (y fixed_roles/fixed_days_off/cobertura_roles)
    # desde herramientas.cargar_roles, que es lo que todo camino real usa.
    cfg = _get_dept_config("recepcion")

    plantilla    = _plantilla_base()
    prev_covers  = {}
    prev_sunday  = {}

    for i in range(n_semanas):
        week_start = SEMANA_INICIO + timedelta(weeks=i)

        # Incorporar nuevo empleado si toca esta semana
        if semana_extra_staff and i in semana_extra_staff:
            nuevo = copy.deepcopy(semana_extra_staff[i])
            plantilla.append(nuevo)

        staff = copy.deepcopy(plantilla)

        sch, staff_final, _, _, next_covers, _ = generate_schedule(
            staff_list        = staff,
            week_start        = week_start,
            cfg               = cfg,
            prev_week_covers  = prev_covers,
            prev_sunday_shifts= prev_sunday,
        )

        registrar_semana(week_start, sch, staff_final, forzar=True, dept_id="recepcion")

        prev_covers = next_covers
        prev_sunday = {p["name"]: sch[p["name"]][6] for p in staff_final}


def _contar_finds(historial, nombres):
    """
    Cuenta fines de semana libres no compensados por cobertura nocturna.
    Un finde es compensado si la persona trabajó Noche en los días que el
    night_fixed tenía Libre esa semana.
    """
    finds = {n: 0 for n in nombres}

    for entrada in historial.values():
        sch       = entrada.get("schedule", {})
        staff_sem = entrada.get("staff", [])

        # Días de cobertura nocturna = días libres del night_fixed esa semana
        nf = next((p for p in staff_sem if p.get("role") == "night_fixed"), None)
        dias_cob: set = set()
        if nf:
            nf_t     = sch.get(nf["name"], [])
            dias_cob = {i for i, t in enumerate(nf_t) if t == "Libre"}

        for nombre in nombres:
            t = sch.get(nombre, [])
            if len(t) < 7:
                continue
            if t[5] == "Libre" and t[6] == "Libre":
                fue_comp = bool(dias_cob and any(t[d] == "Noche" for d in dias_cob))
                if not fue_comp:
                    finds[nombre] += 1

    return finds


# ─── Tests ───────────────────────────────────────────────────────────────────

def test_equidad_12_semanas(data_dir):
    """
    Tras 12 semanas sin peticiones ni vacaciones, la diferencia entre el
    empleado con más fines de semana libres (no compensados) y el que menos
    tiene debe ser <= 2.
    """
    from herramientas import cargar_historial
    from app import _get_dept_config

    _simular(12)

    historial = cargar_historial("recepcion")
    # Roles excluidos de equidad según la config real de producción (p.ej.
    # jefe/night_fixed/subjefe): sincronizado con lo que assign_days_off
    # realmente aplica, en vez de mantener una lista aparte a mano que puede
    # desincronizarse (ver diagnóstico previo con Laura/subjefe).
    cfg       = _get_dept_config("recepcion")
    fixed     = set(cfg.roles_excluidos_equidad)
    nombres   = [p["name"] for p in _plantilla_base() if p["role"] not in fixed]

    finds = _contar_finds(historial, nombres)
    diff  = max(finds.values()) - min(finds.values())

    assert diff <= 2, (
        f"Desequilibrio de fines de semana > 2: {finds}\n"
        f"(máx={max(finds.values())}, mín={min(finds.values())})"
    )


def test_dias_fijos_siempre_respetados(data_dir):
    """
    Jefe y night_fixed mantienen sus días fijos en las 12 semanas:
    - Jefe: libre en Sáb(5) y Dom(6) siempre.
    - Night_fixed: libre en Jue(3) y Vie(4), Noche el resto de días.
    """
    from herramientas import cargar_historial
    from modelo_recepcion import RECEPCION_CONFIG

    _simular(12)

    historial = cargar_historial("recepcion")
    plantilla = _plantilla_base()
    jefe      = next(p["name"] for p in plantilla if p["role"] == "jefe")
    nf        = next(p["name"] for p in plantilla if p["role"] == "night_fixed")
    jefe_lib  = set(RECEPCION_CONFIG.fixed_days_off["jefe"])
    nf_lib    = set(RECEPCION_CONFIG.night_fixed_days_off)

    for semana_str, entrada in historial.items():
        sch = entrada.get("schedule", {})

        t_jefe = sch.get(jefe, [])
        for d in jefe_lib:
            assert t_jefe[d] == "Libre", (
                f"Jefe no libre en día {d} la semana {semana_str}: {t_jefe}"
            )

        t_nf = sch.get(nf, [])
        for d in range(7):
            if d in nf_lib:
                assert t_nf[d] == "Libre", (
                    f"Night_fixed no libre en día {d} semana {semana_str}: {t_nf}"
                )
            else:
                assert t_nf[d] == "Noche", (
                    f"Night_fixed no hace Noche en día {d} semana {semana_str}: {t_nf}"
                )


def test_cobertura_minima_finde(data_dir):
    """
    En Sábado y Domingo nunca hay menos personas disponibles (no libres) de las
    que requiere el mínimo total de cobertura. Esto verifica que assign_days_off
    no libra a demasiadas personas simultáneamente en el finde.
    (La distribución interna Mañana/Tarde es best-effort en assign_shifts y
    puede verse limitada por la regla no_morning_after; por eso se comprueba
    el total de disponibles, no cada turno por separado.)
    """
    from herramientas import cargar_historial
    from modelo_recepcion import RECEPCION_CONFIG

    _simular(12)

    historial      = cargar_historial("recepcion")
    total_minimo   = sum(RECEPCION_CONFIG.min_coverage.values())

    for semana_str, entrada in historial.items():
        sch = entrada.get("schedule", {})
        for day_idx in [5, 6]:  # Sábado y Domingo
            disponibles = sum(
                1 for t in sch.values()
                if len(t) > day_idx and t[day_idx] != "Libre"
            )
            assert disponibles >= total_minimo, (
                f"Personas disponibles insuficientes en día {day_idx} "
                f"semana {semana_str}: {disponibles} < {total_minimo}\n"
                f"Horario del día: {[(n, t[day_idx]) for n, t in sch.items() if len(t) > day_idx]}"
            )


def test_incorporacion_tardia(data_dir):
    """
    Un empleado que se incorpora en la semana 10 (de 20) debe terminar con un
    ratio de fines de semana libre dentro de ±0.15 del ratio medio del resto de
    empleados sin rol fijo (equidad proporcional, no absoluta).
    """
    from herramientas import cargar_historial

    nuevo = {"name": "Nuevo", "role": "normal", "preferences": []}
    _simular(20, semana_extra_staff={9: nuevo})

    historial = cargar_historial("recepcion")
    fixed     = {"jefe", "night_fixed"}
    nombres_orig = [p["name"] for p in _plantilla_base() if p["role"] not in fixed]

    def _ratio(nombre, desde_iso=None):
        """Ratio de fines de semana no compensados / semanas trabajadas."""
        n_finds   = 0
        n_semanas = 0
        for semana_str, entrada in historial.items():
            if desde_iso and semana_str < desde_iso:
                continue
            staff_sem = {p["name"] for p in entrada.get("staff", [])}
            if nombre not in staff_sem:
                continue
            sch = entrada.get("schedule", {})
            t   = sch.get(nombre, [])
            if not t or all(x == "Libre" for x in t):
                continue
            n_semanas += 1

            nf = next(
                (p for p in entrada.get("staff", []) if p.get("role") == "night_fixed"),
                None,
            )
            dias_cob: set = set()
            if nf:
                nf_t     = sch.get(nf["name"], [])
                dias_cob = {i for i, x in enumerate(nf_t) if x == "Libre"}

            if len(t) >= 7 and t[5] == "Libre" and t[6] == "Libre":
                if not (dias_cob and any(t[d] == "Noche" for d in dias_cob)):
                    n_finds += 1

        return n_finds / max(n_semanas, 1)

    # El nuevo entra en la semana de índice 9 (la 10ª semana)
    semana_nuevo_iso = (SEMANA_INICIO + timedelta(weeks=9)).strftime("%Y-%m-%d")
    ratio_nuevo  = _ratio("Nuevo", desde_iso=semana_nuevo_iso)
    ratios_orig  = [_ratio(n) for n in nombres_orig]
    avg_orig     = sum(ratios_orig) / len(ratios_orig)

    assert abs(ratio_nuevo - avg_orig) <= 0.15, (
        f"Ratio del nuevo ({ratio_nuevo:.3f}) demasiado lejos del promedio "
        f"({avg_orig:.3f}). Ratios originales: "
        f"{dict(zip(nombres_orig, [round(r, 3) for r in ratios_orig]))}"
    )
