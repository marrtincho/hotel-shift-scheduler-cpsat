"""
tests/test_auditoria.py — Panel de auditoría de intervenciones manuales
(solo admin): registro estructurado en intervenciones.json, endpoint
/api/auditoria/intervenciones y justificación obligatoria en edición de
turno y petición de origen jefe.

Todas las pruebas usan un DATA_DIR temporal y sesiones forjadas en el test
client de Flask, aislando también auth.LOG_FILE/USERS_FILE (que se fijan a
partir de DATA_DIR en el momento de importar auth.py, no se pueden aislar
solo parcheando herramientas.DATA_DIR) para no tocar datos reales.
"""

from datetime import date, timedelta

import pytest

SEMANA_INICIO = date(2099, 8, 3)  # Lunes en futuro lejano


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    backup = tmp_path / "backups"
    backup.mkdir()

    import herramientas
    import cupos
    import auth
    import app as app_module

    monkeypatch.setattr(herramientas, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(herramientas, "BACKUP_DIR", str(backup))
    monkeypatch.setattr(herramientas, "INTERVENCIONES_FILE", str(tmp_path / "intervenciones.json"))
    monkeypatch.setattr(cupos, "CUPOS_EXTRA_FILE", str(tmp_path / "cupos_extra.json"))
    monkeypatch.setattr(app_module, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(auth, "USERS_FILE", str(tmp_path / "usuarios.json"))
    monkeypatch.setattr(auth, "LOG_FILE", str(tmp_path / "actividad.json"))
    return tmp_path


@pytest.fixture
def client(data_dir):
    import app as app_module
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def _sesion(client, *, uid, username, rol, nombre, vinculado=None):
    with client.session_transaction() as sess:
        sess["uid"] = uid
        sess["username"] = username
        sess["rol"] = rol
        sess["nombre"] = nombre
        sess["vinculado"] = vinculado


def _seed_plantilla(nombres, dept_id="recepcion"):
    from herramientas import guardar_plantilla
    guardar_plantilla(
        [{"name": n, "role": "normal", "preferences": []} for n in nombres],
        dept_id,
    )


def _seed_historial_semana(lunes, schedule, dept_id="recepcion"):
    from herramientas import cargar_historial, guardar_historial, _semana_str
    historial = cargar_historial(dept_id)
    historial[_semana_str(lunes)] = {
        "schedule": schedule,
        "staff": [{"name": n, "role": "normal", "preferences": []} for n in schedule],
    }
    guardar_historial(historial, dept_id)


def _editar_turno(client, *, nombre, dia_idx, turno, justificacion="Ajuste operativo de cobertura",
                  semana=SEMANA_INICIO, dept="recepcion"):
    return client.put("/api/horario/turno", json={
        "dept": dept, "semana": semana.isoformat(), "nombre": nombre,
        "dia_idx": dia_idx, "turno": turno, "justificacion": justificacion,
    })


def _seed_semanas_presentes(nombre, n_semanas, dept_id="recepcion", inicio=SEMANA_INICIO):
    """Crea/actualiza n_semanas de historial consecutivas en las que
    'nombre' aparece en el staff y trabaja (no vacaciones completas), para
    poder controlar "semanas_presentes" en los tests de normalización por
    exposición. Se FUSIONA con lo que ya hubiera esa semana (en vez de
    sobrescribir la entrada entera) para poder llamarse varias veces con
    distintos empleados sobre el mismo rango de semanas sin pisarse."""
    from herramientas import cargar_historial, guardar_historial, _semana_str
    historial = cargar_historial(dept_id)
    for i in range(n_semanas):
        key = _semana_str(inicio + timedelta(weeks=i))
        entrada = historial.setdefault(key, {"schedule": {}, "staff": []})
        entrada["schedule"][nombre] = ["Mañana", "Mañana", "Mañana", "Tarde", "Tarde", "Libre", "Libre"]
        if not any(p["name"] == nombre for p in entrada["staff"]):
            entrada["staff"].append({"name": nombre, "role": "normal", "preferences": []})
    guardar_historial(historial, dept_id)


def _registrar_favorables(nombre, n, dept_id="recepcion"):
    from herramientas import registrar_intervencion
    for _ in range(n):
        registrar_intervencion(
            usuario="admin", rol_usuario="admin", dept=dept_id, tipo="edicion_turno",
            empleado_afectado=nombre, valor_anterior="Mañana", valor_nuevo="Libre",
        )


# Rango amplio que cubre tanto las semanas simuladas en 2099 (historial de
# prueba) como el timestamp real de hoy (con el que registrar_intervencion
# marca cada intervención), necesarios ambos para que semanas_presentes y
# las intervenciones filtradas por fecha coincidan en el mismo periodo.
_RANGO_AMPLIO = {"desde": "2020-01-01", "hasta": "2100-01-01"}


# ═══════════════════════════════════════════════════════════════════════════
# 1. Clasificación favorable / desfavorable / neutra
# ═══════════════════════════════════════════════════════════════════════════

def test_edicion_a_libre_es_favorable(client, data_dir):
    _seed_plantilla(["Ana"])
    _seed_historial_semana(SEMANA_INICIO, {"Ana": ["Mañana"] * 7})
    _sesion(client, uid="1", username="admin", rol="admin", nombre="Admin")

    r = _editar_turno(client, nombre="Ana", dia_idx=0, turno="Libre")
    assert r.status_code == 200

    r_aud = client.get(f"/api/auditoria/intervenciones?dept=recepcion")
    fila = next(f for f in r_aud.get_json()["data"]["resumen_por_empleado"] if f["empleado"] == "Ana")
    assert fila["ediciones_turno_favorables"] == 1
    assert fila["ediciones_turno_desfavorables"] == 0
    assert fila["ediciones_turno_neutras"] == 0


def test_edicion_desde_libre_a_turno_es_desfavorable(client, data_dir):
    _seed_plantilla(["Ana"])
    _seed_historial_semana(SEMANA_INICIO, {"Ana": ["Libre"] * 7})
    _sesion(client, uid="1", username="admin", rol="admin", nombre="Admin")

    r = _editar_turno(client, nombre="Ana", dia_idx=0, turno="Tarde")
    assert r.status_code == 200

    r_aud = client.get("/api/auditoria/intervenciones?dept=recepcion")
    fila = next(f for f in r_aud.get_json()["data"]["resumen_por_empleado"] if f["empleado"] == "Ana")
    assert fila["ediciones_turno_desfavorables"] == 1
    assert fila["ediciones_turno_favorables"] == 0


def test_edicion_entre_dos_turnos_es_neutra(client, data_dir):
    _seed_plantilla(["Ana"])
    _seed_historial_semana(SEMANA_INICIO, {"Ana": ["Mañana"] * 7})
    _sesion(client, uid="1", username="admin", rol="admin", nombre="Admin")

    r = _editar_turno(client, nombre="Ana", dia_idx=0, turno="Tarde")
    assert r.status_code == 200

    r_aud = client.get("/api/auditoria/intervenciones?dept=recepcion")
    fila = next(f for f in r_aud.get_json()["data"]["resumen_por_empleado"] if f["empleado"] == "Ana")
    assert fila["ediciones_turno_neutras"] == 1
    assert fila["ediciones_turno_favorables"] == 0
    assert fila["ediciones_turno_desfavorables"] == 0


# ═══════════════════════════════════════════════════════════════════════════
# 2. Contador de ediciones en fin de semana
# ═══════════════════════════════════════════════════════════════════════════

def test_edicion_en_sabado_incrementa_ediciones_en_finde(client, data_dir):
    _seed_plantilla(["Ana"])
    _seed_historial_semana(SEMANA_INICIO, {"Ana": ["Mañana"] * 7})
    _sesion(client, uid="1", username="admin", rol="admin", nombre="Admin")

    r = _editar_turno(client, nombre="Ana", dia_idx=5, turno="Libre")  # Sábado
    assert r.status_code == 200

    r_aud = client.get("/api/auditoria/intervenciones?dept=recepcion")
    data = r_aud.get_json()["data"]
    fila = next(f for f in data["resumen_por_empleado"] if f["empleado"] == "Ana")
    assert fila["ediciones_en_finde"] == 1
    assert data["totales_periodo"]["ediciones_en_finde"] == 1


def test_edicion_entre_semana_no_cuenta_como_finde(client, data_dir):
    _seed_plantilla(["Ana"])
    _seed_historial_semana(SEMANA_INICIO, {"Ana": ["Mañana"] * 7})
    _sesion(client, uid="1", username="admin", rol="admin", nombre="Admin")

    r = _editar_turno(client, nombre="Ana", dia_idx=1, turno="Libre")  # Martes
    assert r.status_code == 200

    r_aud = client.get("/api/auditoria/intervenciones?dept=recepcion")
    fila = next(f for f in r_aud.get_json()["data"]["resumen_por_empleado"]
                if f["empleado"] == "Ana")
    assert fila["ediciones_en_finde"] == 0


# ═══════════════════════════════════════════════════════════════════════════
# 3. Petición de origen jefe vs. petición propia del empleado
# ═══════════════════════════════════════════════════════════════════════════

def test_peticion_origen_jefe_queda_registrada(client, data_dir):
    _seed_plantilla(["Ana"])
    _sesion(client, uid="1", username="encargado", rol="encargado", nombre="Encargado")

    r = client.post("/api/peticiones", json={
        "nombre": "Ana", "semana": SEMANA_INICIO.isoformat(), "dias": ["Lunes"],
        "justificacion": "Acuerdo verbal con la empleada por cambio de turno familiar",
        "dept": "recepcion",
    })
    assert r.status_code == 200

    _sesion(client, uid="9", username="admin", rol="admin", nombre="Admin")
    r_aud = client.get("/api/auditoria/intervenciones?dept=recepcion")
    fila = next(f for f in r_aud.get_json()["data"]["resumen_por_empleado"] if f["empleado"] == "Ana")
    assert fila["peticiones_origen_jefe"] == 1


def test_peticion_propia_del_empleado_no_genera_intervencion(client, data_dir):
    _seed_plantilla(["Ana"])
    _sesion(client, uid="1", username="ana", rol="empleado", nombre="Ana", vinculado="Ana")

    r = client.post("/api/peticiones", json={
        "nombre": "Ana", "semana": SEMANA_INICIO.isoformat(), "dias": ["Lunes"],
        "dept": "recepcion",
    })
    assert r.status_code == 200

    from herramientas import cargar_intervenciones
    assert cargar_intervenciones() == []


# ═══════════════════════════════════════════════════════════════════════════
# 4. Cupo extra y cambio de rol
# ═══════════════════════════════════════════════════════════════════════════

def test_cambio_de_cupo_extra_queda_registrado(client, data_dir):
    _seed_plantilla(["Ana"])
    _sesion(client, uid="1", username="encargado", rol="encargado", nombre="Encargado")

    r = client.put("/api/cupos/extra", json={"nombre": "Ana", "mes": "2099-09", "extra": 2})
    assert r.status_code == 200

    from herramientas import cargar_intervenciones
    intervenciones = cargar_intervenciones()
    cupo_int = [i for i in intervenciones if i["tipo"] == "cupo_extra"]
    assert len(cupo_int) == 1
    assert cupo_int[0]["empleado_afectado"] == "Ana"
    assert cupo_int[0]["valor_anterior"] == "0"
    assert cupo_int[0]["valor_nuevo"] == "2099-09: 0→2"


def test_cambio_de_rol_en_plantilla_queda_registrado(client, data_dir):
    _seed_plantilla(["Ana"])
    _sesion(client, uid="1", username="encargado", rol="encargado", nombre="Encargado")

    r = client.put("/api/plantilla/0", json={"role": "subjefe"})
    assert r.status_code == 200

    from herramientas import cargar_intervenciones
    rol_int = [i for i in cargar_intervenciones() if i["tipo"] == "cambio_rol_plantilla"]
    assert len(rol_int) == 1
    assert rol_int[0]["empleado_afectado"] == "Ana"
    assert rol_int[0]["valor_anterior"] == "normal"
    assert rol_int[0]["valor_nuevo"] == "subjefe"


def test_editar_solo_el_nombre_no_genera_intervencion_de_rol(client, data_dir):
    _seed_plantilla(["Ana"])
    _sesion(client, uid="1", username="encargado", rol="encargado", nombre="Encargado")

    r = client.put("/api/plantilla/0", json={"name": "Ana María"})
    assert r.status_code == 200

    from herramientas import cargar_intervenciones
    assert cargar_intervenciones() == []


# ═══════════════════════════════════════════════════════════════════════════
# 5. Edición de turno — justificación ahora opcional (se quitó el requisito)
# ═══════════════════════════════════════════════════════════════════════════

def test_editar_turno_sin_justificacion_funciona(client, data_dir):
    """NOTA (requisito de negocio): se quitó el
    requisito de justificación obligatoria (mínimo 5 caracteres) al cambiar
    un turno a mano. El campo sigue aceptándose y guardándose si se manda
    (ver test_editar_turno_con_justificacion_valida_se_guarda), pero ya no
    bloquea la edición si viene vacío."""
    _seed_plantilla(["Ana"])
    _seed_historial_semana(SEMANA_INICIO, {"Ana": ["Mañana"] * 7})
    _sesion(client, uid="1", username="admin", rol="admin", nombre="Admin")

    r = client.put("/api/horario/turno", json={
        "dept": "recepcion", "semana": SEMANA_INICIO.isoformat(),
        "nombre": "Ana", "dia_idx": 0, "turno": "Libre",
    })
    assert r.status_code == 200

    from herramientas import cargar_intervenciones
    intervenciones = cargar_intervenciones()
    assert len(intervenciones) == 1
    assert intervenciones[0]["justificacion"] == ""


def test_editar_turno_con_justificacion_valida_se_guarda(client, data_dir):
    _seed_plantilla(["Ana"])
    _seed_historial_semana(SEMANA_INICIO, {"Ana": ["Mañana"] * 7})
    _sesion(client, uid="1", username="admin", rol="admin", nombre="Admin")

    r = _editar_turno(client, nombre="Ana", dia_idx=0, turno="Libre",
                      justificacion="Cambio acordado por cita médica de la empleada")
    assert r.status_code == 200

    from herramientas import cargar_intervenciones
    intervenciones = cargar_intervenciones()
    assert len(intervenciones) == 1
    assert intervenciones[0]["justificacion"] == "Cambio acordado por cita médica de la empleada"
    assert intervenciones[0]["tipo"] == "edicion_turno"
    assert intervenciones[0]["valor_anterior"] == "Mañana"
    assert intervenciones[0]["valor_nuevo"] == "Libre"
    assert intervenciones[0]["usuario"] == "admin"
    assert intervenciones[0]["rol_usuario"] == "admin"


def test_peticion_jefe_sin_justificacion_falla(client, data_dir):
    _seed_plantilla(["Ana"])
    _sesion(client, uid="1", username="encargado", rol="encargado", nombre="Encargado")

    r = client.post("/api/peticiones", json={
        "nombre": "Ana", "semana": SEMANA_INICIO.isoformat(), "dias": ["Lunes"],
        "dept": "recepcion",
    })
    assert r.status_code == 400
    assert "justificaci" in r.get_json()["error"].lower()


# ═══════════════════════════════════════════════════════════════════════════
# 6. Permisos del endpoint de auditoría
# ═══════════════════════════════════════════════════════════════════════════

def test_admin_accede_a_auditoria(client, data_dir):
    _seed_plantilla(["Ana"])
    _sesion(client, uid="1", username="admin", rol="admin", nombre="Admin")
    r = client.get("/api/auditoria/intervenciones")
    assert r.status_code == 200


def test_encargado_no_accede_a_auditoria(client, data_dir):
    _sesion(client, uid="1", username="encargado", rol="encargado", nombre="Encargado")
    r = client.get("/api/auditoria/intervenciones")
    assert r.status_code == 403


def test_empleado_no_accede_a_auditoria(client, data_dir):
    _sesion(client, uid="1", username="ana", rol="empleado", nombre="Ana", vinculado="Ana")
    r = client.get("/api/auditoria/intervenciones")
    assert r.status_code == 403


# ═══════════════════════════════════════════════════════════════════════════
# 7. El registro no rota
# ═══════════════════════════════════════════════════════════════════════════

def test_intervenciones_no_rota_tras_1500_registros(data_dir):
    from herramientas import registrar_intervencion, cargar_intervenciones

    for i in range(1500):
        registrar_intervencion(
            usuario="admin", rol_usuario="admin", dept="recepcion", tipo="cambio_rol_plantilla",
            empleado_afectado=f"Persona{i}", valor_anterior="normal", valor_nuevo="subjefe",
        )

    assert len(cargar_intervenciones()) == 1500


# ═══════════════════════════════════════════════════════════════════════════
# 8. Filtro por fechas
# ═══════════════════════════════════════════════════════════════════════════

def test_filtro_por_fechas_devuelve_solo_el_periodo_pedido(data_dir):
    import json
    from herramientas import INTERVENCIONES_FILE, cargar_intervenciones

    entradas = [
        {"ts": "2099-01-10T10:00:00+00:00", "usuario": "admin", "rol_usuario": "admin",
         "dept": "recepcion", "tipo": "cambio_rol_plantilla", "empleado_afectado": "Fuera de rango",
         "semana": None, "dia_idx": None, "valor_anterior": "normal", "valor_nuevo": "subjefe",
         "justificacion": ""},
        {"ts": "2099-06-15T10:00:00+00:00", "usuario": "admin", "rol_usuario": "admin",
         "dept": "recepcion", "tipo": "cambio_rol_plantilla", "empleado_afectado": "Dentro de rango",
         "semana": None, "dia_idx": None, "valor_anterior": "normal", "valor_nuevo": "subjefe",
         "justificacion": ""},
        {"ts": "2099-12-31T10:00:00+00:00", "usuario": "admin", "rol_usuario": "admin",
         "dept": "recepcion", "tipo": "cambio_rol_plantilla", "empleado_afectado": "Fuera de rango 2",
         "semana": None, "dia_idx": None, "valor_anterior": "normal", "valor_nuevo": "subjefe",
         "justificacion": ""},
    ]
    with open(INTERVENCIONES_FILE, "w", encoding="utf-8") as f:
        json.dump(entradas, f)

    resultado = cargar_intervenciones(desde="2099-06-01", hasta="2099-06-30")
    assert len(resultado) == 1
    assert resultado[0]["empleado_afectado"] == "Dentro de rango"


# ═══════════════════════════════════════════════════════════════════════════
# 9. Normalización por exposición: tasas por semana presente
# ═══════════════════════════════════════════════════════════════════════════

def test_tasas_normalizadas_dan_el_mismo_valor_pese_a_totales_distintos(client, data_dir):
    """4 favorables en 20 semanas y 2 favorables en 10 semanas deben dar la
    MISMA tasa (0.2), aunque los totales absolutos sean distintos."""
    _seed_plantilla(["Ana", "Luis"])
    _seed_semanas_presentes("Ana", 20)
    _seed_semanas_presentes("Luis", 10)
    _registrar_favorables("Ana", 4)
    _registrar_favorables("Luis", 2)

    _sesion(client, uid="1", username="admin", rol="admin", nombre="Admin")
    r = client.get("/api/auditoria/intervenciones", query_string={"dept": "recepcion", **_RANGO_AMPLIO})
    resumen = r.get_json()["data"]["resumen_por_empleado"]
    fila_ana  = next(f for f in resumen if f["empleado"] == "Ana")
    fila_luis = next(f for f in resumen if f["empleado"] == "Luis")

    assert fila_ana["semanas_presentes"] == 20
    assert fila_luis["semanas_presentes"] == 10
    assert fila_ana["ediciones_turno_favorables"] == 4
    assert fila_luis["ediciones_turno_favorables"] == 2
    assert fila_ana["tasa_favorables"] == pytest.approx(0.2)
    assert fila_luis["tasa_favorables"] == pytest.approx(0.2)


def test_media_del_equipo_excluye_semanas_presentes_insuficientes(client, data_dir):
    """La media de referencia del equipo no debe distorsionarse por alguien
    recién incorporado con pocas semanas presentes (< 4)."""
    _seed_plantilla(["Ana", "Luis", "Nueva"])
    _seed_semanas_presentes("Ana", 20)
    _seed_semanas_presentes("Luis", 10)
    _seed_semanas_presentes("Nueva", 2)  # < 4: fuera de la referencia
    _registrar_favorables("Ana", 4)     # tasa 0.2
    _registrar_favorables("Luis", 2)    # tasa 0.2
    _registrar_favorables("Nueva", 5)   # tasa 2.5 — distorsionaría si entrase

    _sesion(client, uid="1", username="admin", rol="admin", nombre="Admin")
    r = client.get("/api/auditoria/intervenciones", query_string={"dept": "recepcion", **_RANGO_AMPLIO})
    ref = r.get_json()["data"]["totales_periodo"]["referencia_equipo"]
    assert ref["tasa_favorables"]["media"] == pytest.approx(0.2)


def test_empleado_sin_intervenciones_aparece_con_tasas_a_cero(client, data_dir):
    _seed_plantilla(["Ana", "SinIntervenciones"])
    _seed_semanas_presentes("Ana", 5)
    _seed_semanas_presentes("SinIntervenciones", 5)
    _registrar_favorables("Ana", 1)

    _sesion(client, uid="1", username="admin", rol="admin", nombre="Admin")
    r = client.get("/api/auditoria/intervenciones", query_string={"dept": "recepcion", **_RANGO_AMPLIO})
    resumen = r.get_json()["data"]["resumen_por_empleado"]
    empleados = {f["empleado"] for f in resumen}
    assert "SinIntervenciones" in empleados  # no se omite de la lista

    fila = next(f for f in resumen if f["empleado"] == "SinIntervenciones")
    assert fila["semanas_presentes"] == 5
    assert fila["tasa_favorables"] == 0
    assert fila["tasa_desfavorables"] == 0
    assert fila["tasa_peticiones_jefe"] == 0


def test_periodo_sin_intervenciones_responde_sin_error_con_ceros(client, data_dir):
    _seed_plantilla(["Ana"])
    _sesion(client, uid="1", username="admin", rol="admin", nombre="Admin")

    r = client.get("/api/auditoria/intervenciones?dept=recepcion")
    assert r.status_code == 200
    data = r.get_json()["data"]
    assert data["detalle"] == []

    fila = next(f for f in data["resumen_por_empleado"] if f["empleado"] == "Ana")
    assert fila["semanas_presentes"] == 0
    assert fila["tasa_favorables"] == 0
    assert fila["tasa_desfavorables"] == 0
    assert fila["tasa_peticiones_jefe"] == 0

    ref = data["totales_periodo"]["referencia_equipo"]
    for campo in ("tasa_favorables", "tasa_desfavorables", "tasa_peticiones_jefe"):
        assert ref[campo]["media"] == 0
        assert ref[campo]["desviacion"] == 0


def test_auditoria_con_tasas_sigue_siendo_solo_para_admin(client, data_dir):
    """Repite la comprobación de permisos ahora que el endpoint devuelve
    también las tasas normalizadas: encargado y empleado deben seguir sin
    poder acceder al panel de auditoría."""
    _seed_plantilla(["Ana"])

    _sesion(client, uid="1", username="admin", rol="admin", nombre="Admin")
    assert client.get("/api/auditoria/intervenciones").status_code == 200

    _sesion(client, uid="2", username="encargado", rol="encargado", nombre="Encargado")
    assert client.get("/api/auditoria/intervenciones").status_code == 403

    _sesion(client, uid="3", username="ana", rol="empleado", nombre="Ana", vinculado="Ana")
    assert client.get("/api/auditoria/intervenciones").status_code == 403
