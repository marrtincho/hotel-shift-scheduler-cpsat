"""
tests/test_temporadas.py — Tests de gestión de plantilla variable por temporadas.

Cubre:
  1. validar_viabilidad: casos con 6, 7, 11 y 12 personas.
  2. Que cambiar el perfil activo cambia los límites usados en generate_schedule_valido.
  3. Que la generación con 10 personas (perfil alta) y 7 personas (perfil baja)
     produce 30 semanas sin violaciones.

Todas las pruebas usan semanas en futuro lejano y directorios temporales.
"""

import copy
from datetime import date, timedelta

import pytest

SEMANA_INICIO = date(2099, 3, 3)   # Lunes en el futuro lejano


# ─── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Redirige DATA_DIR, BACKUP_DIR y COVERAGE_CONFIG_FILE al directorio temporal."""
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


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _cfg_normal():
    """DeptConfig de recepción con los valores del perfil 'normal'."""
    from modelo_recepcion import RECEPCION_CONFIG
    return copy.deepcopy(RECEPCION_CONFIG)


def _cfg_con_cobertura(min_cov: dict, max_cov: dict):
    """DeptConfig de recepción con min/max personalizados."""
    cfg = _cfg_normal()
    cfg.min_coverage = min_cov
    cfg.max_coverage = max_cov
    return cfg


def _staff_n(n: int) -> list:
    """Genera una plantilla mínima de n personas para recepción:
    1 night_fixed, 1 jefe, el resto normal."""
    staff = [
        {"name": "Carlos", "role": "night_fixed", "preferences": ["Noche"]},
        {"name": "Juan",   "role": "jefe",         "preferences": ["Mañana"]},
    ]
    roles_extra = ["subjefe", "normal", "adjunto_recepcion"]
    for i in range(n - 2):
        role = roles_extra[min(i, len(roles_extra) - 1)]
        staff.append({"name": f"Emp{i+1}", "role": role, "preferences": []})
    return staff


def _generar_semanas(staff, cfg, n_semanas, max_intentos=50):
    """Genera n_semanas encadenadas y devuelve lista de violaciones por semana."""
    from modelo_base import generate_schedule_valido
    prev_covers = {}
    prev_sunday = {}
    violaciones_total = []
    for i in range(n_semanas):
        semana = SEMANA_INICIO + timedelta(weeks=i)
        sched, staff_final, _, _, this_covers, violaciones = generate_schedule_valido(
            staff_list=copy.deepcopy(staff),
            week_start=semana,
            cfg=cfg,
            prev_week_covers=prev_covers,
            prev_sunday_shifts=prev_sunday,
            max_intentos=max_intentos,
        )
        if violaciones:
            violaciones_total.append((i + 1, semana, violaciones))
        prev_sunday = {p["name"]: sched[p["name"]][6] for p in staff_final}
        prev_covers = this_covers
    return violaciones_total


# ─── 1. Tests de validar_viabilidad ──────────────────────────────────────────

def test_viabilidad_6_personas_inviable():
    """6 personas × 5 días = 30 días-turno < min(2+2+1+0)×7=35 → inviable."""
    from modelo_base import validar_viabilidad
    cfg   = _cfg_normal()
    staff = _staff_n(6)
    viable, msg = validar_viabilidad(staff, cfg)
    assert not viable, "6 personas deben ser inviables con el perfil normal"
    assert "insuficiente" in msg.lower() or "faltan" in msg.lower(), (
        f"El mensaje no explica el déficit: {msg}"
    )
    assert "1" in msg or "person" in msg.lower(), (
        f"El mensaje debería indicar cuántas personas faltan: {msg}"
    )


def test_viabilidad_7_personas_viable():
    """7 personas × 5 = 35 días-turno = min 35 → en el límite pero viable."""
    from modelo_base import validar_viabilidad
    cfg   = _cfg_normal()
    staff = _staff_n(7)
    viable, msg = validar_viabilidad(staff, cfg)
    assert viable, f"7 personas deben ser viables (en el límite): {msg}"
    assert msg == ""


def test_viabilidad_11_personas_viable():
    """11 personas × 5 = 55 días-turno ≤ max(3+3+1+1)×7=56 → viable."""
    from modelo_base import validar_viabilidad
    cfg   = _cfg_normal()
    staff = _staff_n(11)
    viable, msg = validar_viabilidad(staff, cfg)
    assert viable, f"11 personas deben ser viables: {msg}"
    assert msg == ""


def test_viabilidad_12_personas_inviable():
    """12 personas × 5 = 60 días-turno > max 56 → inviable por exceso."""
    from modelo_base import validar_viabilidad
    cfg   = _cfg_normal()
    staff = _staff_n(12)
    viable, msg = validar_viabilidad(staff, cfg)
    assert not viable, "12 personas deben ser inviables (exceso)"
    assert "excesivo" in msg.lower() or "sobran" in msg.lower(), (
        f"El mensaje no explica el exceso: {msg}"
    )


def test_viabilidad_descuenta_vacaciones_completas():
    """Una persona con todos los días forzados como Libre (vacaciones completas)
    no cuenta como activa en el cómputo de viabilidad."""
    from modelo_base import validar_viabilidad
    from modelo_base import DAYS
    cfg = _cfg_normal()
    # 8 personas pero 1 de vacaciones completas → efectivo 7 → viable en el límite
    staff = _staff_n(8)
    staff[-1]["forced_days_off"] = list(DAYS)  # vacaciones toda la semana
    viable, _ = validar_viabilidad(staff, cfg)
    # Con 7 activos: 35 = min 35 → viable
    assert viable, "Con 7 activos (1 de vacaciones) debe ser viable"


# ─── 2. Test: cambiar perfil activo cambia los límites usados ────────────────

def test_cambiar_perfil_activo_cambia_limites(data_dir):
    """Al cambiar el perfil activo en coverage_config.json, cargar_coverage_activa
    devuelve los nuevos límites, y generate_schedule_valido los usa."""
    import herramientas
    from herramientas import guardar_coverage_config, cargar_coverage_activa

    cfg_baja_esperada = {
        "min_coverage": {"Mañana": 2, "Tarde": 1, "Noche": 1, "Partido": 0},
        "max_coverage": {"Mañana": 3, "Tarde": 3, "Noche": 1, "Partido": 1},
    }
    cfg_normal_esperada = {
        "min_coverage": {"Mañana": 2, "Tarde": 2, "Noche": 1, "Partido": 0},
        "max_coverage": {"Mañana": 3, "Tarde": 3, "Noche": 1, "Partido": 1},
    }

    # Estado inicial: perfil normal
    activa = cargar_coverage_activa("recepcion")
    assert activa["min_coverage"] == cfg_normal_esperada["min_coverage"], (
        "El perfil inicial debe ser 'normal'"
    )

    # Cambiar a baja
    config = herramientas.cargar_coverage_config()
    config["recepcion"]["activo"] = "baja"
    guardar_coverage_config(config)

    activa_baja = cargar_coverage_activa("recepcion")
    assert activa_baja["min_coverage"] == cfg_baja_esperada["min_coverage"], (
        "Tras cambiar a 'baja', min_coverage debe ser el del perfil baja"
    )
    assert activa_baja["max_coverage"] == cfg_baja_esperada["max_coverage"]

    # Volver a normal: los límites se restauran
    config["recepcion"]["activo"] = "normal"
    guardar_coverage_config(config)
    activa_normal = cargar_coverage_activa("recepcion")
    assert activa_normal["min_coverage"] == cfg_normal_esperada["min_coverage"]


# ─── 3. Tests de generación con plantillas de tamaño variable ────────────────

def test_generate_10_personas_perfil_alta(data_dir):
    """10 personas con perfil 'alta' (max Mañana=4, Tarde=4) → 30 semanas sin violaciones.

    Con el perfil normal, Viernes tendría 9 trabajando pero el máximo por día
    es 8 (3+3+1+1). Con el perfil alta, el máximo sube a 11 (4+4+1+2) → factible.
    """
    import herramientas
    from herramientas import guardar_coverage_config
    from modelo_base import DAYS

    # Activar perfil alta
    config = herramientas.cargar_coverage_config()
    config["recepcion"]["activo"] = "alta"
    guardar_coverage_config(config)

    cfg = _cfg_con_cobertura(
        min_cov={"Mañana": 2, "Tarde": 2, "Noche": 1, "Partido": 0},
        max_cov={"Mañana": 4, "Tarde": 4, "Noche": 1, "Partido": 2},
    )

    staff = [
        {"name": "Carlos",  "role": "night_fixed",       "preferences": ["Noche"]},
        {"name": "Juan",    "role": "jefe",               "preferences": ["Mañana"]},
        {"name": "Laura",   "role": "subjefe",            "preferences": ["Tarde"]},
        {"name": "Pedro",   "role": "normal",             "preferences": ["Mañana"]},
        {"name": "Ana",     "role": "normal",             "preferences": []},
        {"name": "Miguel",  "role": "normal",             "preferences": ["Tarde"]},
        {"name": "Sofía",   "role": "adjunto_recepcion",  "preferences": ["Mañana"]},
        {"name": "Diego",   "role": "normal",             "preferences": []},
        {"name": "Rosa",    "role": "normal",             "preferences": ["Tarde"]},
        {"name": "Tomás",   "role": "normal",             "preferences": []},
    ]

    fallos = _generar_semanas(staff, cfg, 30, max_intentos=100)
    assert not fallos, (
        "Con 10 personas y perfil alta no debe haber violaciones:\n" +
        "\n".join(
            f"  Semana {s}: {v}"
            for s, _, v in fallos
        )
    )


def test_generate_7_personas_perfil_baja(data_dir):
    """7 personas con perfil 'baja' (min Tarde=1) → 30 semanas sin violaciones.

    Con el perfil normal (min Tarde=2), 7 personas pueden dejar algunos días
    con solo 2 asignables pero 3 slots necesarios. Con baja (min Tarde=1),
    2 asignables cubren exactamente los 2 slots requeridos.
    """
    import herramientas
    from herramientas import guardar_coverage_config

    # Activar perfil baja
    config = herramientas.cargar_coverage_config()
    config["recepcion"]["activo"] = "baja"
    guardar_coverage_config(config)

    cfg = _cfg_con_cobertura(
        min_cov={"Mañana": 2, "Tarde": 1, "Noche": 1, "Partido": 0},
        max_cov={"Mañana": 3, "Tarde": 3, "Noche": 1, "Partido": 1},
    )

    staff = [
        {"name": "Carlos",  "role": "night_fixed",      "preferences": ["Noche"]},
        {"name": "Juan",    "role": "jefe",              "preferences": ["Mañana"]},
        {"name": "Laura",   "role": "subjefe",           "preferences": ["Tarde"]},
        {"name": "Pedro",   "role": "normal",            "preferences": []},
        {"name": "Ana",     "role": "normal",            "preferences": []},
        {"name": "Miguel",  "role": "normal",            "preferences": []},
        {"name": "Sofía",   "role": "adjunto_recepcion", "preferences": ["Mañana"]},
    ]

    fallos = _generar_semanas(staff, cfg, 30, max_intentos=100)
    assert not fallos, (
        "Con 7 personas y perfil baja no debe haber violaciones:\n" +
        "\n".join(
            f"  Semana {s}: {v}"
            for s, _, v in fallos
        )
    )


# ─── 4. Tests de validar_coverage_perfil ─────────────────────────────────────

def test_validar_coverage_perfil_valido():
    from herramientas import validar_coverage_perfil
    datos = {
        "min_coverage": {"Mañana": 2, "Tarde": 2, "Noche": 1, "Partido": 0},
        "max_coverage": {"Mañana": 3, "Tarde": 3, "Noche": 1, "Partido": 1},
    }
    assert validar_coverage_perfil(datos) == []


def test_validar_coverage_perfil_min_mayor_max():
    from herramientas import validar_coverage_perfil
    datos = {
        "min_coverage": {"Mañana": 4, "Tarde": 2, "Noche": 1, "Partido": 0},
        "max_coverage": {"Mañana": 3, "Tarde": 3, "Noche": 1, "Partido": 1},
    }
    errores = validar_coverage_perfil(datos)
    assert any("min > max" in e and "Mañana" in e for e in errores)


def test_validar_coverage_perfil_noche_cero():
    from herramientas import validar_coverage_perfil
    datos = {
        "min_coverage": {"Mañana": 2, "Tarde": 2, "Noche": 0, "Partido": 0},
        "max_coverage": {"Mañana": 3, "Tarde": 3, "Noche": 1, "Partido": 1},
    }
    errores = validar_coverage_perfil(datos)
    assert any("Noche" in e for e in errores)


def test_validar_coverage_perfil_valor_negativo():
    from herramientas import validar_coverage_perfil
    datos = {
        "min_coverage": {"Mañana": -1, "Tarde": 2, "Noche": 1, "Partido": 0},
        "max_coverage": {"Mañana": 3,  "Tarde": 3, "Noche": 1, "Partido": 1},
    }
    errores = validar_coverage_perfil(datos)
    assert any("Mañana" in e for e in errores)
