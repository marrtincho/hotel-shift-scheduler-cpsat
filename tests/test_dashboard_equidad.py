"""
tests/test_dashboard_equidad.py — Bloque "Mi equidad" del dashboard del
empleado (GET /api/dashboard → campo "equidad").

Reutiliza _get_weekend_ratios (modelo_base.py) vía estadisticas.equidad_de;
estos tests no comprueban esa función en sí (ya cubierta en
test_equidad_findes.py) sino que la normalización por semanas presentes,
la exclusión de roles con excluir_de_equidad=True, el umbral de 4 semanas,
el margen del 15% y —sobre todo— la privacidad de la respuesta (nunca
nombres ni valores individuales de otros empleados) funcionan como se pide.

Usa semanas en futuro lejano (2099) como el resto de la suite: el filtro
de ventana de _get_weekend_ratios solo excluye semanas más ANTIGUAS que
"hoy - 52 semanas", nunca semanas futuras, así que 2099 sigue entrando en
la ventana sin tocar datos reales.
"""

from datetime import date, timedelta

import pytest

SEMANA_INICIO = date(2099, 2, 2)  # Lunes en futuro lejano


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    backup = tmp_path / "backups"
    backup.mkdir()

    import herramientas
    import cupos
    import app as app_module

    monkeypatch.setattr(herramientas, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(herramientas, "BACKUP_DIR", str(backup))
    monkeypatch.setattr(herramientas, "INTERVENCIONES_FILE", str(tmp_path / "intervenciones.json"))
    monkeypatch.setattr(cupos, "CUPOS_EXTRA_FILE", str(tmp_path / "cupos_extra.json"))
    monkeypatch.setattr(app_module, "DATA_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def client(data_dir):
    import app as app_module
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def _sesion(client, *, uid, username, rol, nombre, vinculado):
    with client.session_transaction() as sess:
        sess["uid"] = uid
        sess["username"] = username
        sess["rol"] = rol
        sess["nombre"] = nombre
        sess["vinculado"] = vinculado


def _seed_plantilla(roles: dict, dept_id="recepcion"):
    """roles: {nombre: role}."""
    from herramientas import guardar_plantilla
    guardar_plantilla(
        [{"name": n, "role": r, "preferences": []} for n, r in roles.items()],
        dept_id,
    )


def _seed_semana(lunes, personas_libre_finde, todas_las_personas, roles=None, dept_id="recepcion"):
    """Crea/actualiza una semana de historial: quienes están en
    'personas_libre_finde' libran Sáb+Dom (resto de días Mañana); el resto
    trabaja Mañana toda la semana (nunca finde libre). Sin night_fixed en
    el staff, así que ningún finde libre se exime como compensación
    nocturna — cada finde libre cuenta siempre."""
    from herramientas import cargar_historial, guardar_historial, _semana_str
    roles = roles or {}
    historial = cargar_historial(dept_id)
    schedule = {}
    for nombre in todas_las_personas:
        if nombre in personas_libre_finde:
            schedule[nombre] = ["Mañana"] * 5 + ["Libre", "Libre"]
        else:
            schedule[nombre] = ["Mañana"] * 7
    historial[_semana_str(lunes)] = {
        "schedule": schedule,
        "staff": [{"name": n, "role": roles.get(n, "normal"), "preferences": []}
                  for n in todas_las_personas],
    }
    guardar_historial(historial, dept_id)


def _seed_n_semanas(n, personas_libre_cada, todas_las_personas, roles=None, dept_id="recepcion",
                    inicio=SEMANA_INICIO):
    """personas_libre_cada: dict {nombre: set(indices de semana 0-based en
    que libra el finde)}. Genera n semanas consecutivas."""
    for i in range(n):
        lunes = inicio + timedelta(weeks=i)
        libran = {n_ for n_, semanas in personas_libre_cada.items() if i in semanas}
        _seed_semana(lunes, libran, todas_las_personas, roles=roles, dept_id=dept_id)


def _dashboard_equidad(client, nombre):
    _sesion(client, uid="1", username=nombre.lower(), rol="empleado", nombre=nombre, vinculado=nombre)
    r = client.get("/api/dashboard")
    assert r.status_code == 200
    return r.get_json()["data"]


# ─── Privacidad ──────────────────────────────────────────────────────────────

def test_no_incluye_nombres_ni_datos_de_otros_empleados(client, data_dir):
    roles = {"Ana": "normal", "Luis": "normal", "Marta": "normal"}
    _seed_plantilla(roles)
    personas_libre = {"Ana": {0, 2, 4, 6}, "Luis": {1, 3}, "Marta": {5}}
    _seed_n_semanas(8, personas_libre, list(roles), roles=roles)

    import json
    data = _dashboard_equidad(client, "Ana")
    full_json = json.dumps(data, ensure_ascii=False)

    for otro in ("Luis", "Marta"):
        assert otro not in full_json, f"'{otro}' no debería aparecer en la respuesta de Ana"


# ─── Exclusión de la media del equipo ────────────────────────────────────────

def test_media_equipo_excluye_excluidos_de_equidad_y_poca_antiguedad(client, data_dir):
    roles = {"Ana": "normal", "Luis": "normal", "Jefa": "jefe", "Nuevo": "normal"}
    _seed_plantilla(roles)
    # Ana y Luis: 4 semanas de finde libre en 20 -> ratio 0.2 cada uno.
    # Jefa: excluida por rol (excluir_de_equidad=True por defecto).
    # Nuevo: solo 2 semanas de presencia (< 4) -> fuera de la media también.
    personas_libre = {
        "Ana":  {0, 5, 10, 15},
        "Luis": {2, 7, 12, 17},
        "Jefa": {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19},  # siempre libre
        "Nuevo": set(),
    }
    todas = list(roles)
    for i in range(20):
        lunes = SEMANA_INICIO + timedelta(weeks=i)
        libran = {n for n, semanas in personas_libre.items() if i in semanas}
        presentes = todas if i >= 18 else [n for n in todas if n != "Nuevo"]  # Nuevo solo últimas 2
        _seed_semana(lunes, libran, presentes, roles=roles)

    data = _dashboard_equidad(client, "Ana")
    media = data["equidad"]["findes"]["media_equipo"]
    # Con solo Ana y Luis elegibles (ambos ratio 0.2), la media debe ser 0.2,
    # sin que Jefa (excluida) ni Nuevo (<4 semanas) la desplacen.
    assert media == pytest.approx(0.2, abs=1e-6)


# ─── Normalización: misma posición pese a distinta antigüedad ──────────────

def test_mismo_ratio_distinta_antiguedad_misma_posicion(client, data_dir):
    roles = {"Veterana": "normal", "Reciente": "normal", "Tercera": "normal"}
    _seed_plantilla(roles)
    todas = list(roles)
    # Veterana: 4 de 20 (0.2). Reciente: 2 de 10 (0.2, incorporada semana 10).
    # Tercera: constante en las 20 semanas para dar cuerpo al cálculo de media.
    for i in range(20):
        lunes = SEMANA_INICIO + timedelta(weeks=i)
        libran = set()
        presentes = ["Veterana", "Tercera"]
        if i in (0, 5, 10, 15):
            libran.add("Veterana")
        if i in (2, 8):
            libran.add("Tercera")
        if i >= 10:
            presentes = presentes + ["Reciente"]
            if i in (12, 17):
                libran.add("Reciente")
        _seed_semana(lunes, libran, presentes, roles=roles)

    data_vet = _dashboard_equidad(client, "Veterana")
    data_rec = _dashboard_equidad(client, "Reciente")

    eq_vet = data_vet["equidad"]["findes"]
    eq_rec = data_rec["equidad"]["findes"]
    assert eq_vet["ratio"] == pytest.approx(0.2, abs=1e-6)
    assert eq_rec["ratio"] == pytest.approx(0.2, abs=1e-6)
    assert eq_vet["semanas"] != eq_rec["semanas"]  # distinta antigüedad real
    assert eq_vet["posicion"] == eq_rec["posicion"]  # misma posición pese a ello


# ─── Margen del 15% ──────────────────────────────────────────────────────────

def test_diferencia_menor_15_por_ciento_es_en_media(client, data_dir):
    roles = {"Ana": "normal", "Luis": "normal"}
    _seed_plantilla(roles)
    todas = list(roles)
    # Ana: 4/20 = 0.20. Luis: 4/18 ≈ 0.222 (diferencia relativa ~11% < 15%).
    for i in range(20):
        lunes = SEMANA_INICIO + timedelta(weeks=i)
        libran = set()
        if i in (0, 5, 10, 15):
            libran.add("Ana")
        if i < 18 and i in (1, 6, 11, 16):
            libran.add("Luis")
        presentes = todas if i < 18 else ["Ana"]
        _seed_semana(lunes, libran, presentes, roles=roles)

    data = _dashboard_equidad(client, "Ana")
    eq = data["equidad"]["findes"]
    assert eq["posicion"] == "en_media"


def test_diferencia_mayor_15_por_ciento_no_es_en_media(client, data_dir):
    roles = {"Ana": "normal", "Luis": "normal"}
    _seed_plantilla(roles)
    todas = list(roles)
    # Ana: 6/20 = 0.30. Luis: 2/20 = 0.10. Media de ambos = 0.20.
    # Diferencia relativa de Ana respecto a la media: 50% > 15%.
    for i in range(20):
        lunes = SEMANA_INICIO + timedelta(weeks=i)
        libran = set()
        if i in (0, 3, 6, 9, 12, 15):
            libran.add("Ana")
        if i in (1, 11):
            libran.add("Luis")
        _seed_semana(lunes, libran, todas, roles=roles)

    data = _dashboard_equidad(client, "Ana")
    eq = data["equidad"]["findes"]
    assert eq["posicion"] == "por_encima"


# ─── Menos de 4 semanas: sin comparación ─────────────────────────────────────

def test_menos_de_4_semanas_no_incluye_comparacion(client, data_dir):
    roles = {"Ana": "normal", "Nuevo": "normal"}
    _seed_plantilla(roles)
    _seed_n_semanas(20, {"Ana": {0, 5, 10, 15}}, ["Ana"], roles=roles)
    # "Nuevo" solo aparece en las últimas 2 semanas.
    for i in range(18, 20):
        lunes = SEMANA_INICIO + timedelta(weeks=i)
        _seed_semana(lunes, set(), ["Ana", "Nuevo"], roles=roles)

    data = _dashboard_equidad(client, "Nuevo")
    eq = data["equidad"]
    assert eq["datos_insuficientes"] is True
    assert "media_equipo" not in eq["findes"]
    assert "posicion" not in eq["findes"]
    assert eq["findes"]["semanas"] < 4


# ─── Rol excluido de equidad (subjefe) ───────────────────────────────────────

def test_rol_excluido_de_equidad_sin_comparacion(client, data_dir):
    roles = {"Ana": "normal", "SubJefa": "subjefe"}
    _seed_plantilla(roles)
    _seed_n_semanas(10, {"Ana": {0, 5}, "SubJefa": {1, 2}}, list(roles), roles=roles)

    data = _dashboard_equidad(client, "SubJefa")
    eq = data["equidad"]
    assert eq["excluido_de_balanceo"] is True
    assert "media_equipo" not in eq["findes"]
    assert "posicion" not in eq["findes"]
    assert eq["findes"]["propios"] == 2  # sus propios datos SÍ se muestran


# ─── Invalidación de caché ───────────────────────────────────────────────────

def test_cache_se_invalida_al_registrar_semana_nueva(client, data_dir):
    from herramientas import registrar_semana, _semana_str
    import estadisticas as est

    roles = {"Ana": "normal", "Luis": "normal"}
    _seed_plantilla(roles)
    _seed_n_semanas(5, {"Ana": {0}, "Luis": {1}}, list(roles), roles=roles)

    data1 = _dashboard_equidad(client, "Ana")
    findes_antes = data1["equidad"]["findes"]["propios"]
    semanas_antes = data1["equidad"]["findes"]["semanas"]

    # Registrar una semana nueva con Ana librando el finde otra vez, vía el
    # mismo helper que usan los endpoints reales, invalidando la caché como
    # ellos hacen.
    nueva_semana = SEMANA_INICIO + timedelta(weeks=5)
    schedule = {"Ana": ["Mañana"] * 5 + ["Libre", "Libre"], "Luis": ["Mañana"] * 7}
    staff_final = [{"name": n, "role": r, "preferences": []} for n, r in roles.items()]
    registrar_semana(nueva_semana, schedule, staff_final, forzar=True, dept_id="recepcion")
    est.invalidar_cache("recepcion")

    data2 = _dashboard_equidad(client, "Ana")
    findes_despues = data2["equidad"]["findes"]["propios"]
    semanas_despues = data2["equidad"]["findes"]["semanas"]

    assert findes_despues == findes_antes + 1
    assert semanas_despues == semanas_antes + 1
