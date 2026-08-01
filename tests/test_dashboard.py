"""
tests/test_dashboard.py — Panel personal del empleado (/api/dashboard y
/api/dashboard/ics).

Todas las pruebas usan un DATA_DIR temporal (nunca tocan datos reales) y
sesiones forjadas directamente en el test client de Flask (sin login real),
igual que se hace en el resto de la suite para endpoints autenticados.
"""

from datetime import date, timedelta
from unittest.mock import patch

import pytest


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Redirige DATA_DIR/BACKUP_DIR de herramientas, el fichero de cupos
    extra y el DATA_DIR de app.py al directorio temporal de la prueba."""
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


def _lunes_actual() -> date:
    hoy = date.today()
    return hoy - timedelta(days=hoy.weekday())


def _seed_plantilla(nombres, dept_id="recepcion"):
    from herramientas import guardar_plantilla
    guardar_plantilla(
        [{"name": n, "role": "normal", "preferences": []} for n in nombres],
        dept_id,
    )


def _seed_historial_semana(lunes, schedule, dept_id="recepcion", staff_extra=None):
    """Crea/actualiza una entrada de historial para la semana 'lunes'.
    schedule: {"nombre": [7 turnos]}. staff_extra permite añadir campos como
    night_cover_this_week a la instantánea de staff guardada."""
    from herramientas import cargar_historial, guardar_historial, _semana_str
    historial = cargar_historial(dept_id)
    staff_extra = staff_extra or {}
    historial[_semana_str(lunes)] = {
        "schedule": schedule,
        "staff": [
            {"name": nombre, "role": "normal", "preferences": [], **staff_extra.get(nombre, {})}
            for nombre in schedule
        ],
    }
    guardar_historial(historial, dept_id)


def _seed_peticion(nombre, semana, dias, dept_id="recepcion", origen="empleado"):
    from herramientas import cargar_peticiones, guardar_peticiones, _semana_str
    pet = cargar_peticiones(dept_id)
    key = _semana_str(semana)
    pet.setdefault(key, {})
    pet[key][nombre] = {
        "dias": dias, "la": [], "origen": origen, "estado": "activa",
        "creada_en": date.today().isoformat(),
    }
    guardar_peticiones(pet, dept_id)


# ─── Aislamiento: un empleado solo ve lo suyo ────────────────────────────────

def test_empleado_solo_ve_sus_propios_datos(client, data_dir):
    _seed_plantilla(["Ana", "Luis"])
    _sesion(client, uid="1", username="ana", rol="empleado", nombre="Ana", vinculado="Ana")

    r = client.get("/api/dashboard")
    assert r.status_code == 200
    assert r.get_json()["data"]["nombre"] == "Ana"


def test_empleado_no_puede_pedir_datos_de_otro(client, data_dir):
    _seed_plantilla(["Ana", "Luis"])
    _sesion(client, uid="1", username="ana", rol="empleado", nombre="Ana", vinculado="Ana")

    r = client.get("/api/dashboard?empleado=Luis")
    assert r.status_code == 403

    r2 = client.get("/api/dashboard/ics?empleado=Luis")
    assert r2.status_code == 403


def test_jefe_puede_consultar_a_cualquier_empleado(client, data_dir):
    _seed_plantilla(["Ana", "Luis"])
    _sesion(client, uid="2", username="encargado", rol="encargado", nombre="Encargado", vinculado=None)

    r = client.get("/api/dashboard?empleado=Ana")
    assert r.status_code == 200
    assert r.get_json()["data"]["nombre"] == "Ana"

    r2 = client.get("/api/dashboard?empleado=Luis")
    assert r2.status_code == 200
    assert r2.get_json()["data"]["nombre"] == "Luis"


def test_admin_sin_vincular_y_sin_parametro_recibe_error_claro(client, data_dir):
    _seed_plantilla(["Ana"])
    _sesion(client, uid="3", username="admin", rol="admin", nombre="Admin", vinculado=None)
    r = client.get("/api/dashboard")
    assert r.status_code == 403


# ─── Contadores correctos ────────────────────────────────────────────────────

def test_contador_peticiones_coincide_con_cupos(client, data_dir):
    import cupos
    _seed_plantilla(["Ana"])
    lunes = _lunes_actual()
    semana_futura = lunes + timedelta(weeks=10)  # semana sin generar, sirve para pedir libre
    _seed_peticion("Ana", semana_futura, ["Lunes"])
    _sesion(client, uid="1", username="ana", rol="empleado", nombre="Ana", vinculado="Ana")

    r = client.get("/api/dashboard")
    data = r.get_json()["data"]
    mes = semana_futura.strftime("%Y-%m")
    esperado = cupos.cupo_usado("Ana", mes, "recepcion")
    if data["contadores"]["peticiones_mes"]["mes"] == mes:
        assert data["contadores"]["peticiones_mes"]["usadas"] == esperado
    assert data["peticiones"]["activas"][0]["dias"] == ["Lunes"]
    assert data["peticiones"]["activas"][0]["estado"] == "activa"


def test_contador_festivos_y_compensacion_coinciden_con_ficheros(client, data_dir):
    from herramientas import guardar_festivos_trabajados, guardar_compensaciones
    _seed_plantilla(["Ana"])
    anio = date.today().year
    guardar_festivos_trabajados({
        f"{anio}-01-01": {"festivo": "Año Nuevo", "trabajadores": {"Ana": "Mañana"}},
        f"{anio}-12-25": {"festivo": "Navidad", "trabajadores": {"Ana": "Tarde"}},
        "2000-01-01": {"festivo": "Hace mucho", "trabajadores": {"Ana": "Noche"}},  # fuera del año
    }, "recepcion")
    guardar_compensaciones({"Ana": {"modalidad": "cobrar", "la_saldo": 3}}, "recepcion")

    _sesion(client, uid="1", username="ana", rol="empleado", nombre="Ana", vinculado="Ana")
    r = client.get("/api/dashboard")
    data = r.get_json()["data"]
    assert data["contadores"]["festivos_trabajados_anio"] == 2
    assert data["contadores"]["la_saldo"] == 3
    assert data["contadores"]["modalidad_compensacion"] == "cobrar"


def test_contador_vacaciones_restantes(client, data_dir):
    from herramientas import guardar_vacaciones, guardar_vacaciones_dotacion
    _seed_plantilla(["Ana"])
    anio = date.today().year
    guardar_vacaciones_dotacion({"Ana": {str(anio): 30}}, "recepcion")
    guardar_vacaciones([
        {"id": "x1", "nombre": "Ana", "inicio": f"{anio}-06-01", "fin": f"{anio}-06-10", "nota": ""},
    ], "recepcion")

    _sesion(client, uid="1", username="ana", rol="empleado", nombre="Ana", vinculado="Ana")
    r = client.get("/api/dashboard")
    vac = r.get_json()["data"]["contadores"]["vacaciones"]
    assert vac["dotacion"] == 30
    assert vac["tomados"] == 10
    assert vac["restantes"] == 20


# ─── Empleado sin historial (recién incorporado) ─────────────────────────────

def test_empleado_sin_historial_responde_sin_errores_con_ceros(client, data_dir):
    _seed_plantilla(["Nueva"])
    _sesion(client, uid="9", username="nueva", rol="empleado", nombre="Nueva", vinculado="Nueva")

    r = client.get("/api/dashboard")
    assert r.status_code == 200
    data = r.get_json()["data"]

    assert data["ahora_mismo"]["existe_horario"] is False
    assert data["ahora_mismo"]["turno_hoy"] is None
    assert all(d["turno"] is None for d in data["semana"]["dias"])
    assert data["contadores"]["festivos_trabajados_anio"] == 0
    assert data["contadores"]["la_saldo"] == 0
    assert data["contadores"]["vacaciones"]["tomados"] == 0
    assert data["peticiones"]["activas"] == []
    for periodo in ("3m", "anio"):
        h = data["historico"][periodo]
        assert h["findes"] == 0 and h["noches"] == 0 and h["festivos"] == 0
        assert all(v == 0 for v in h["turnos"].values())


# ─── Caché de estadísticas ────────────────────────────────────────────────────

def test_cache_se_calcula_una_vez_y_se_invalida_al_editar_historial(client, data_dir):
    import estadisticas as est

    _seed_plantilla(["Ana"])
    lunes = _lunes_actual()
    _seed_historial_semana(lunes, {"Ana": ["Libre", "Libre", "Mañana", "Mañana", "Mañana", "Libre", "Libre"]})
    _sesion(client, uid="4", username="encargado", rol="encargado", nombre="Encargado", vinculado=None)

    with patch.object(est, "_calcular_todos", wraps=est._calcular_todos) as spy:
        r1 = client.get("/api/dashboard?empleado=Ana")
        assert spy.call_count == 1
        turnos_antes = r1.get_json()["data"]["historico"]["anio"]["turnos"]["Mañana"]

        r2 = client.get("/api/dashboard?empleado=Ana")
        assert spy.call_count == 1  # segunda llamada usa caché, no recalcula
        assert r2.get_json()["data"]["historico"]["anio"]["turnos"]["Mañana"] == turnos_antes

        # Editar un turno de esa semana debe invalidar la caché (vía el endpoint real)
        r_edit = client.put("/api/horario/turno", json={
            "dept": "recepcion", "semana": lunes.isoformat(),
            "nombre": "Ana", "dia_idx": 0, "turno": "Mañana",
            "justificacion": "Corrección de prueba para invalidar caché",
        })
        assert r_edit.status_code == 200

        r3 = client.get("/api/dashboard?empleado=Ana")
        assert spy.call_count == 2  # se recalculó tras la invalidación
        assert r3.get_json()["data"]["historico"]["anio"]["turnos"]["Mañana"] == turnos_antes + 1


# ─── Export .ics ──────────────────────────────────────────────────────────────

def _parsear_ics_eventos(contenido: str) -> list[dict]:
    """Parseo manual mínimo de VEVENTs (sin librerías externas): valida que
    el fichero tiene una estructura VCALENDAR/VEVENT balanceada y devuelve
    los campos de cada evento como dicts."""
    assert contenido.startswith("BEGIN:VCALENDAR")
    assert contenido.rstrip().endswith("END:VCALENDAR")
    assert contenido.count("BEGIN:VEVENT") == contenido.count("END:VEVENT")

    eventos = []
    actual = None
    for linea in contenido.splitlines():
        if linea == "BEGIN:VEVENT":
            actual = {}
        elif linea == "END:VEVENT":
            eventos.append(actual)
            actual = None
        elif actual is not None and ":" in linea:
            campo, valor = linea.split(":", 1)
            actual[campo] = valor
    return eventos


def test_ics_valido_solo_eventos_propios_y_horas_correctas(client, data_dir):
    from datetime import datetime
    from herramientas import guardar_horarios_turnos

    _seed_plantilla(["Ana", "Luis"])
    lunes = _lunes_actual()
    _seed_historial_semana(lunes, {
        "Ana":  ["Mañana", "Libre", "Noche", "Libre", "Libre", "Libre", "Libre"],
        "Luis": ["Tarde", "Tarde", "Tarde", "Tarde", "Tarde", "Libre", "Libre"],
    })
    guardar_horarios_turnos({
        "Mañana":  [{"inicio": "07:00", "fin": "15:00"}],
        "Tarde":   [{"inicio": "15:00", "fin": "23:00"}],
        "Noche":   [{"inicio": "23:00", "fin": "07:00"}],
        "Partido": [{"inicio": "10:00", "fin": "13:00"}, {"inicio": "17:00", "fin": "20:00"}],
    }, "recepcion")

    _sesion(client, uid="1", username="ana", rol="empleado", nombre="Ana", vinculado="Ana")
    r = client.get("/api/dashboard/ics")
    assert r.status_code == 200
    assert r.headers["Content-Type"].startswith("text/calendar")

    eventos = _parsear_ics_eventos(r.data.decode("utf-8"))
    assert len(eventos) == 2  # Mañana + Noche de Ana; nada de Luis

    for ev in eventos:
        assert "Recepción" in ev["SUMMARY"]
        assert "Luis" not in ev["SUMMARY"]
        # DTSTART/DTEND deben tener formato ISO básico parseable
        datetime.strptime(ev["DTSTART"], "%Y%m%dT%H%M%S")
        datetime.strptime(ev["DTEND"], "%Y%m%dT%H%M%S")

    manana_ev = next(e for e in eventos if "Mañana" in e["SUMMARY"])
    assert manana_ev["DTSTART"].endswith("T070000")
    assert manana_ev["DTEND"].endswith("T150000")

    noche_ev = next(e for e in eventos if "Noche" in e["SUMMARY"])
    dtstart = datetime.strptime(noche_ev["DTSTART"], "%Y%m%dT%H%M%S")
    dtend   = datetime.strptime(noche_ev["DTEND"], "%Y%m%dT%H%M%S")
    assert dtend > dtstart  # el turno de Noche cruza medianoche: fin al día siguiente
    assert (dtend - dtstart).total_seconds() == 8 * 3600
