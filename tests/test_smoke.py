"""
tests/test_smoke.py — Pruebas de humo del generador de horarios.

Todas las pruebas usan semanas en futuro lejano (año 2099) y datos en
memoria/directorios temporales: nunca tocan datos reales de producción.
"""

import copy
import json
import os
from datetime import date, timedelta

import pytest

# ─── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """
    Redirige DATA_DIR y BACKUP_DIR de herramientas al directorio temporal
    de la prueba. También ajusta FESTIVOS_FILE para que no lea datos reales.
    """
    backup = tmp_path / "backups"
    backup.mkdir()

    import herramientas
    monkeypatch.setattr(herramientas, "DATA_DIR",    str(tmp_path))
    monkeypatch.setattr(herramientas, "BACKUP_DIR",  str(backup))
    monkeypatch.setattr(herramientas, "FESTIVOS_FILE",
                        str(tmp_path / "festivos.json"))
    return tmp_path


# ─── Datos de prueba ──────────────────────────────────────────────────────────

SEMANA_PRUEBA = date(2099, 6, 7)   # Lunes de la semana de prueba (futuro lejano)

# Plantilla mínima con todos los roles que ejercen las reglas
PLANTILLA_TEST = [
    {"name": "NocheF",  "role": "night_fixed",      "preferences": ["Noche"]},
    {"name": "Jefe",    "role": "jefe",              "preferences": ["Mañana"]},
    {"name": "Subj",    "role": "subjefe",           "preferences": ["Tarde"]},
    {"name": "Emp1",    "role": "normal",            "preferences": []},
    {"name": "Emp2",    "role": "normal",            "preferences": ["Tarde"]},
    {"name": "Emp3",    "role": "normal",            "preferences": ["Mañana"]},
    {"name": "Emp4",    "role": "adjunto_recepcion", "preferences": ["Mañana"]},
    {"name": "Emp5",    "role": "normal",            "preferences": []},
]


# ─── Bloque A: generación de horario semanal ─────────────────────────────────

def _generar(data_dir):
    """Genera un horario de recepción con la plantilla de prueba y devuelve
    (full_schedule, staff_final, night_cover)."""
    import herramientas  # noqa: F401 — asegura que DATA_DIR ya está parcheado
    from modelo_recepcion import RECEPCION_CONFIG
    from modelo_base import generate_schedule

    cfg        = copy.deepcopy(RECEPCION_CONFIG)
    staff_list = copy.deepcopy(PLANTILLA_TEST)

    full_schedule, staff_final, _, night_cover, _ = generate_schedule(
        staff_list=staff_list,
        week_start=SEMANA_PRUEBA,
        cfg=cfg,
        prev_week_covers={},
        prev_sunday_shifts={},
    )
    return full_schedule, staff_final, night_cover


def test_cobertura_maxima_no_superada(data_dir):
    """El máximo de cobertura por turno nunca se supera en ningún día.
    El mínimo es un objetivo best-effort (puede fallar cuando Jefe + sustituto
    de noche libran el mismo día); el máximo sí es un límite duro."""
    from modelo_recepcion import RECEPCION_CONFIG
    from modelo_base import DAYS, SHIFTS

    cfg = RECEPCION_CONFIG
    full_schedule, staff_final, _ = _generar(data_dir)

    for dia_idx, dia in enumerate(DAYS):
        for shift, maximo in cfg.max_coverage.items():
            conteo = sum(
                1 for p in staff_final
                if full_schedule[p["name"]][dia_idx] == shift
            )
            assert conteo <= maximo, (
                f"{dia} – {shift}: cobertura {conteo} > máximo {maximo}"
            )

    # Todos los slots tienen un turno válido (sin celdas vacías)
    turnos_validos = set(SHIFTS)
    for p in staff_final:
        for dia_idx, turno in enumerate(full_schedule[p["name"]]):
            assert turno in turnos_validos, (
                f"{p['name']} tiene turno inválido '{turno}' el día {DAYS[dia_idx]}"
            )


def test_no_manana_tras_noche_o_tarde(data_dir):
    """Nadie hace Mañana inmediatamente después de Noche o Tarde,
    incluido el cruce Domingo→Lunes."""
    from modelo_base import DAYS, NO_MORNING_AFTER

    full_schedule, staff_final, _ = _generar(data_dir)

    for p in staff_final:
        turnos = full_schedule[p["name"]]
        for i in range(1, 7):
            turno_ant = turnos[i - 1]
            turno_act = turnos[i]
            assert not (turno_ant in NO_MORNING_AFTER and turno_act == "Mañana"), (
                f"{p['name']}: {DAYS[i-1]}({turno_ant}) → {DAYS[i]}(Mañana) — "
                "no se puede hacer Mañana tras Noche/Tarde"
            )


def test_dias_libres_por_empleado(data_dir):
    """Cada empleado tiene exactamente 2 días libres semanales."""
    full_schedule, staff_final, _ = _generar(data_dir)

    for p in staff_final:
        libres = full_schedule[p["name"]].count("Libre")
        assert libres == 2, (
            f"{p['name']} tiene {libres} días libres (se esperan 2)"
        )


def test_night_fixed_solo_hace_noches(data_dir):
    """El empleado con rol night_fixed solo trabaja turno Noche (o está libre)."""
    full_schedule, staff_final, _ = _generar(data_dir)

    for p in staff_final:
        if p["role"] != "night_fixed":
            continue
        turnos = full_schedule[p["name"]]
        for dia_idx, turno in enumerate(turnos):
            assert turno in ("Noche", "Libre"), (
                f"{p['name']} (night_fixed) tiene turno '{turno}' el día {dia_idx}"
            )


def test_cruce_domingo_lunes(data_dir):
    """La regla Domingo→Lunes se propaga correctamente entre semanas usando
    prev_sunday_shifts: si alguien hizo Noche el domingo previo, no puede
    hacer Mañana el lunes de la semana siguiente."""
    from modelo_recepcion import RECEPCION_CONFIG
    from modelo_base import generate_schedule, NO_MORNING_AFTER

    cfg = copy.deepcopy(RECEPCION_CONFIG)

    # Forzamos un prev_sunday_shifts donde Emp1 hizo Noche el domingo anterior
    # (el domingo de la semana anterior a SEMANA_PRUEBA)
    prev_sunday = {"Emp1": "Noche"}

    staff_list = copy.deepcopy(PLANTILLA_TEST)
    full_schedule, staff_final, _, _, _ = generate_schedule(
        staff_list=staff_list,
        week_start=SEMANA_PRUEBA,
        cfg=cfg,
        prev_week_covers={},
        prev_sunday_shifts=prev_sunday,
    )

    turno_lunes = full_schedule.get("Emp1", ["?"] * 7)[0]
    assert turno_lunes != "Mañana", (
        f"Emp1 hizo Noche el domingo previo y sin embargo el lunes tiene Mañana"
    )


# ─── Bloque B: helper atómico ─────────────────────────────────────────────────

def test_guardar_json_atomico_escribe_y_lee(tmp_path):
    """Escribe un dict con el helper atómico y lo vuelve a leer correctamente."""
    from herramientas import guardar_json_atomico

    ruta = str(tmp_path / "prueba.json")
    datos = {"clave": "valor", "num": 42, "lista": [1, 2, 3]}

    guardar_json_atomico(ruta, datos)

    with open(ruta, encoding="utf-8") as f:
        leido = json.load(f)

    assert leido == datos


def test_guardar_json_atomico_tmp_huerfano_no_rompe(tmp_path):
    """Un fichero .tmp huérfano preexistente no impide la escritura."""
    from herramientas import guardar_json_atomico

    ruta     = str(tmp_path / "dato.json")
    ruta_tmp = ruta + ".tmp"

    # Creamos un .tmp corrupto a propósito
    with open(ruta_tmp, "w") as f:
        f.write("CORRUPTO{{{")

    datos = {"ok": True}
    guardar_json_atomico(ruta, datos)

    with open(ruta, encoding="utf-8") as f:
        leido = json.load(f)

    assert leido == datos
    assert not os.path.exists(ruta_tmp), "El .tmp no debe quedar tras la escritura"


# ─── Bloque C: login ──────────────────────────────────────────────────────────

@pytest.fixture
def usuarios_dir(tmp_path, monkeypatch):
    """Aísla el fichero de usuarios de auth en el directorio temporal."""
    import auth
    import herramientas
    monkeypatch.setattr(herramientas, "DATA_DIR",   str(tmp_path))
    monkeypatch.setattr(herramientas, "BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setattr(auth, "USERS_FILE",
                        str(tmp_path / "usuarios.json"))
    monkeypatch.setattr(auth, "LOG_FILE",
                        str(tmp_path / "actividad.json"))
    (tmp_path / "backups").mkdir()
    return tmp_path


def test_crear_y_autenticar_usuario(usuarios_dir):
    """Crear un usuario y autenticarlo con contraseña correcta debe funcionar."""
    from auth import crear_usuario, autenticar

    ok, msg = crear_usuario(
        username="prueba",
        nombre="Usuario Prueba",
        rol="empleado",
        password="segura123",
    )
    assert ok, f"crear_usuario falló: {msg}"

    usuario = autenticar("prueba", "segura123")
    assert usuario is not None, "autenticar devolvió None con contraseña correcta"
    assert usuario["username"] == "prueba"


def test_autenticar_password_incorrecta(usuarios_dir):
    """Autenticar con contraseña incorrecta debe devolver None."""
    from auth import crear_usuario, autenticar

    crear_usuario("user2", "Otro", "empleado", "correcta456")
    resultado = autenticar("user2", "incorrecta")
    assert resultado is None, "autenticar debería devolver None con contraseña incorrecta"
