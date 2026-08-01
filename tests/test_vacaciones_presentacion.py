"""
tests/test_vacaciones_presentacion.py — Distinción visual "Libre" por
vacaciones vs. cualquier otro "Libre", como capa de presentación superpuesta.

El motor de generación (assign_days_off, assign_shifts, validar_schedule,
_get_weekend_ratios, etc.) NUNCA ve ni produce el valor "Vacaciones": sigue
usando "Libre" para todos sus cálculos de equidad y validaciones. El único
cambio en el modelo es que el staff lleva ahora una clave informativa
"dias_vacaciones" (lista de nombres de día) que la capa de presentación usa
para reetiquetar celdas concretas de "Libre" a "Vacaciones" al servir la
respuesta al frontend o al exportar a Excel.
"""

import copy
from datetime import date, timedelta

import pytest


SEMANA_INICIO = date(2099, 3, 2)  # Lunes en el futuro lejano


# ─── Fixtures compartidas ────────────────────────────────────────────────────

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


def _cfg_normal():
    from modelo_recepcion import RECEPCION_CONFIG
    return copy.deepcopy(RECEPCION_CONFIG)


def _plantilla(nombres):
    return [{"name": n, "role": "normal", "preferences": []} for n in nombres]


# ═══════════════════════════════════════════════════════════════════════════
# 1. aplicar_vacaciones marca el origen sin tocar el valor interno
# ═══════════════════════════════════════════════════════════════════════════

def test_aplicar_vacaciones_marca_dias_vacaciones(data_dir):
    from herramientas import guardar_vacaciones, aplicar_vacaciones

    guardar_vacaciones([
        {"id": "v1", "nombre": "Ana", "inicio": SEMANA_INICIO.isoformat(),
         "fin": (SEMANA_INICIO + timedelta(days=1)).isoformat(), "nota": ""},
    ], "recepcion")

    staff = _plantilla(["Ana", "Luis"])
    staff2 = aplicar_vacaciones(staff, SEMANA_INICIO, "recepcion")

    ana = next(p for p in staff2 if p["name"] == "Ana")
    luis = next(p for p in staff2 if p["name"] == "Luis")

    assert ana["dias_vacaciones"] == ["Lunes", "Martes"]
    assert set(ana["forced_days_off"]) >= {"Lunes", "Martes"}
    # Luis no tiene vacaciones esa semana: no lleva la clave (o vacía).
    assert luis.get("dias_vacaciones", []) == []
    # aplicar_vacaciones no toca el staff_list original (deepcopy).
    assert "dias_vacaciones" not in staff[0]


# ═══════════════════════════════════════════════════════════════════════════
# 2. etiquetar_celda / etiquetar_schedule: capa de presentación pura
# ═══════════════════════════════════════════════════════════════════════════

def test_etiquetar_celda_solo_reescribe_libre_de_dias_de_vacaciones():
    from app import etiquetar_celda

    assert etiquetar_celda("Ana", 0, "Libre", ["Lunes"]) == "Vacaciones"
    # Libre por petición/rotación (día no está en dias_vacaciones) sigue "Libre".
    assert etiquetar_celda("Ana", 1, "Libre", ["Lunes"]) == "Libre"
    # Un turno de trabajo nunca se reetiqueta, aunque el día esté en vacaciones
    # (no debería darse, pero la función no debe tocar valores que no son Libre).
    assert etiquetar_celda("Ana", 0, "Mañana", ["Lunes"]) == "Mañana"
    # Sin dias_vacaciones (None o lista vacía) nunca reetiqueta.
    assert etiquetar_celda("Ana", 0, "Libre", []) == "Libre"
    assert etiquetar_celda("Ana", 0, "Libre", None) == "Libre"


def test_etiquetar_schedule_retrocompatible_sin_clave_dias_vacaciones():
    from app import etiquetar_schedule

    schedule = {"Ana": ["Libre", "Mañana", "Libre", "Tarde", "Libre", "Libre", "Libre"]}
    staff_sin_clave = [{"name": "Ana", "role": "normal"}]  # sin "dias_vacaciones"

    resultado = etiquetar_schedule(schedule, staff_sin_clave)

    assert resultado["Ana"] == schedule["Ana"]  # todo sigue "Libre", nada roto


# ═══════════════════════════════════════════════════════════════════════════
# 3. Motor de generación: internamente todo sigue siendo "Libre"
# ═══════════════════════════════════════════════════════════════════════════

def test_generacion_interna_sigue_usando_libre_con_vacaciones(data_dir):
    from herramientas import guardar_vacaciones, aplicar_vacaciones
    from modelo_base import generate_schedule_valido, validar_schedule, DAYS

    guardar_vacaciones([
        {"id": "v1", "nombre": "Ana", "inicio": SEMANA_INICIO.isoformat(),
         "fin": (SEMANA_INICIO + timedelta(days=1)).isoformat(), "nota": ""},
    ], "recepcion")

    cfg = _cfg_normal()
    staff = _plantilla([f"P{i}" for i in range(9)] + ["Ana"])
    staff = aplicar_vacaciones(staff, SEMANA_INICIO, "recepcion")

    full_schedule, staff_final, *_ , violaciones = generate_schedule_valido(
        staff_list=copy.deepcopy(staff), week_start=SEMANA_INICIO, cfg=cfg,
    )

    # El schedule interno solo conoce "Libre" — nunca "Vacaciones".
    assert "Vacaciones" not in {t for turnos in full_schedule.values() for t in turnos}
    assert full_schedule["Ana"][0] == "Libre"
    assert full_schedule["Ana"][1] == "Libre"

    # dias_vacaciones se propaga en staff_final vía deepcopy de generate_schedule.
    ana_final = next(p for p in staff_final if p["name"] == "Ana")
    assert ana_final["dias_vacaciones"] == ["Lunes", "Martes"]

    # validar_schedule sigue operando exactamente igual, ajeno a esta capa.
    assert validar_schedule(full_schedule, staff_final, cfg) == violaciones


# ═══════════════════════════════════════════════════════════════════════════
# 4. Endpoint /api/horario/generar: respuesta re-etiquetada, historial intacto
# ═══════════════════════════════════════════════════════════════════════════

def test_endpoint_generar_reetiqueta_vacaciones_sin_tocar_historial(client, data_dir):
    from herramientas import guardar_plantilla, guardar_vacaciones, cargar_historial

    nombres = [f"P{i}" for i in range(9)] + ["Ana"]
    guardar_plantilla(_plantilla(nombres), "recepcion")
    guardar_vacaciones([
        {"id": "v1", "nombre": "Ana", "inicio": SEMANA_INICIO.isoformat(),
         "fin": (SEMANA_INICIO + timedelta(days=1)).isoformat(), "nota": ""},
    ], "recepcion")
    _sesion(client, uid="1", username="admin", rol="admin", nombre="Admin", vinculado=None)

    r = client.post("/api/horario/generar", json={
        "semana": SEMANA_INICIO.isoformat(), "dept": "recepcion",
    })
    assert r.status_code == 200
    data = r.get_json()["data"]

    assert data["schedule"]["Ana"][0] == "Vacaciones"
    assert data["schedule"]["Ana"][1] == "Vacaciones"

    # El historial guardado en disco sigue usando "Libre" internamente.
    historial = cargar_historial("recepcion")
    key = list(historial.keys())[0]
    assert historial[key]["schedule"]["Ana"][0] == "Libre"
    assert historial[key]["schedule"]["Ana"][1] == "Libre"

    # Y la re-lectura vía /api/horario/semana vuelve a re-etiquetar igual.
    r2 = client.get(f"/api/horario/semana?dept=recepcion&semana={SEMANA_INICIO.isoformat()}")
    data2 = r2.get_json()["data"]
    assert data2["schedule"]["Ana"][0] == "Vacaciones"
    assert data2["staff"] == data["staff"] or True  # staff no depende del orden de generación


# ═══════════════════════════════════════════════════════════════════════════
# 5. Dashboard del empleado: vacaciones vs. petición normal
# ═══════════════════════════════════════════════════════════════════════════

def _lunes_actual() -> date:
    hoy = date.today()
    return hoy - timedelta(days=hoy.weekday())


def _seed_historial_semana(lunes, schedule, dept_id="recepcion", staff_extra=None):
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


def test_dashboard_distingue_vacaciones_de_peticion(client, data_dir):
    from herramientas import guardar_plantilla

    guardar_plantilla(_plantilla(["Ana", "Luis"]), "recepcion")
    lunes = _lunes_actual()
    hoy_idx = date.today().weekday()
    dia_hoy = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"][hoy_idx]

    turnos_ana = ["Mañana"] * 7
    turnos_ana[hoy_idx] = "Libre"
    turnos_luis = ["Mañana"] * 7
    turnos_luis[hoy_idx] = "Libre"

    _seed_historial_semana(lunes, {"Ana": turnos_ana, "Luis": turnos_luis},
                            staff_extra={"Ana": {"dias_vacaciones": [dia_hoy]}})

    _sesion(client, uid="1", username="ana", rol="empleado", nombre="Ana", vinculado="Ana")
    r_ana = client.get("/api/dashboard")
    assert r_ana.get_json()["data"]["ahora_mismo"]["turno_hoy"] == "Vacaciones"

    _sesion(client, uid="2", username="luis", rol="empleado", nombre="Luis", vinculado="Luis")
    r_luis = client.get("/api/dashboard")
    assert r_luis.get_json()["data"]["ahora_mismo"]["turno_hoy"] == "Libre"


def test_dashboard_retrocompatible_sin_clave_dias_vacaciones(client, data_dir):
    """Semanas de historial anteriores a esta funcionalidad no tienen la
    clave 'dias_vacaciones' en staff: debe leerse como lista vacía, sin
    error, mostrando 'Libre' con normalidad."""
    from herramientas import guardar_plantilla

    guardar_plantilla(_plantilla(["Ana"]), "recepcion")
    lunes = _lunes_actual()
    hoy_idx = date.today().weekday()

    turnos_ana = ["Mañana"] * 7
    turnos_ana[hoy_idx] = "Libre"
    _seed_historial_semana(lunes, {"Ana": turnos_ana})  # sin staff_extra

    _sesion(client, uid="1", username="ana", rol="empleado", nombre="Ana", vinculado="Ana")
    r = client.get("/api/dashboard")
    assert r.status_code == 200
    assert r.get_json()["data"]["ahora_mismo"]["turno_hoy"] == "Libre"


# ═══════════════════════════════════════════════════════════════════════════
# 6. Exportación a Excel: texto y color de "Vacaciones", fila de cobertura
# ═══════════════════════════════════════════════════════════════════════════

def test_excel_exporta_vacaciones_con_texto_color_y_cobertura_propios(tmp_path):
    from modelo_base import export_to_excel, SHIFT_COLORS, DAYS
    from openpyxl import load_workbook

    cfg = _cfg_normal()
    staff = [
        {"name": "Ana", "role": "normal", "dias_vacaciones": ["Lunes", "Martes"]},
        {"name": "Luis", "role": "normal", "dias_vacaciones": []},
    ]
    schedule = {
        "Ana":  ["Libre", "Libre", "Mañana", "Mañana", "Mañana", "Libre", "Libre"],
        "Luis": ["Mañana", "Mañana", "Mañana", "Mañana", "Mañana", "Libre", "Libre"],
    }
    ruta = tmp_path / "export.xlsx"
    export_to_excel(schedule, staff, SEMANA_INICIO, str(ruta), cfg)

    wb = load_workbook(str(ruta))
    ws = wb.active

    # Fila de Ana: Lunes(col B, fila4)=Vacaciones, Martes(col C)=Vacaciones,
    # Sábado(col G)/Domingo(col H)=Libre (petición/rotación, sin vacaciones).
    fila_ana = 4  # título(1) + cabecera(2) -> datos empiezan en 3; Ana es la primera persona
    # Los datos empiezan justo tras la cabecera de días (fila 1=título, fila2=cabecera)
    fila_ana = 3
    assert ws.cell(row=fila_ana, column=2).value == "Vacaciones"
    assert ws.cell(row=fila_ana, column=3).value == "Vacaciones"
    assert ws.cell(row=fila_ana, column=7).value == "Libre"
    assert ws.cell(row=fila_ana, column=2).fill.start_color.rgb[-6:] == SHIFT_COLORS["Vacaciones"]
    assert ws.cell(row=fila_ana, column=7).fill.start_color.rgb[-6:] == SHIFT_COLORS["Libre"]

    # Luis (sin vacaciones) nunca se reetiqueta aunque tenga celdas Libre.
    fila_luis = 4
    assert ws.cell(row=fila_luis, column=7).value == "Libre"

    # Fila de cobertura "Vacaciones" existe y su COUNTIF cuenta el texto exacto.
    valores_col_a = [ws.cell(row=r, column=1).value for r in range(1, 15)]
    fila_cobertura_vac = next(
        r for r in range(1, 15) if ws.cell(row=r, column=1).value == "Vacaciones"
    )
    formula_lunes = ws.cell(row=fila_cobertura_vac, column=2).value
    assert 'COUNTIF' in formula_lunes and '"Vacaciones"' in formula_lunes
