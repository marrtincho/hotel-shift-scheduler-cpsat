"""
tests/test_motor_cpsat_integracion_app.py — Integración de extremo a extremo
del interruptor motor_cpsat_activo() dentro de app.py (endpoint real
/api/horario/generar, la función Flask "api_generar" -- no existe una
función suelta "generar_horario()"; ese endpoint es el punto de entrada
real que orquesta la generación).

No sustituye a la prueba manual, pero deja en la suite una verificación
permanente de que:
  1-2. Con el interruptor activo, _generar_con_motor_cpsat() adapta la
       salida de generar_semana_cpsat_con_cobertura_nocturna (motor_cpsat.py)
       sin romper el contrato de 6 valores que el resto de api_generar
       espera de generate_schedule_valido -- para Recepción y Mozos.
  3. Encadenando varias semanas con el interruptor activo,
     cpsat_estado_{dept}.json persiste entre llamadas y la llamada de la
     semana N+1 recibe exactamente el estado_rotacion/noches_ultimos_5_dias
     que quedó guardado en disco tras la semana N (no se reinicia a {}).
  4. Con el interruptor apagado (default), api_generar sigue invocando
     generate_schedule_valido con los mismos argumentos que antes de este
     cambio, y motor_cpsat.generar_semana_cpsat_con_cobertura_nocturna no
     se llama nunca -- regresión explícita, no solo "los tests generales
     pasan".
"""

import copy
import json
import os
from datetime import date, timedelta

import pytest


SEMANA_RECEPCION = date(2099, 5, 4)   # Lunes en el futuro lejano
SEMANA_MOZOS     = date(2099, 6, 1)   # Lunes en el futuro lejano, distinta


# ─── Fixtures compartidas (mismo patrón que test_permisos.py / test_vacaciones_presentacion.py) ──

@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    backup = tmp_path / "backups"
    backup.mkdir()
    import herramientas
    import app as app_module
    monkeypatch.setattr(herramientas, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(herramientas, "BACKUP_DIR", str(backup))
    monkeypatch.setattr(herramientas, "INTERVENCIONES_FILE", str(tmp_path / "intervenciones.json"))
    # CONFIG_MOTOR_FILE (interruptor del motor CP-SAT) se calcula sobre
    # herramientas.DATA_DIR en el momento de IMPORTAR el módulo, no se
    # recalcula solo por monkeypatchear DATA_DIR arriba -- sin esto, el
    # fallback de motor_cpsat_activo() leería el fichero REAL de
    # producción (data/config_motor.json) en vez de quedar aislado en
    # tmp_path, contaminando estos tests con el estado real del switch.
    monkeypatch.setattr(herramientas, "CONFIG_MOTOR_FILE", str(tmp_path / "config_motor.json"))
    monkeypatch.setattr(app_module, "DATA_DIR", str(tmp_path))
    monkeypatch.delenv("USAR_MOTOR_CPSAT", raising=False)
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


def _admin(client):
    _sesion(client, uid="1", username="admin", rol="admin", nombre="Admin")


TURNOS_VALIDOS = {"Mañana", "Tarde", "Noche", "Partido", "Libre", "SIN_ASIGNAR"}


def _asserts_forma_respuesta_generacion(data, nombres_esperados):
    """Misma forma de respuesta tanto si generó el heurístico como el CP-SAT:
    api_generar solo puede devolver esto si el contrato de 6 valores que
    espera de la función de generación (generate_schedule_valido o
    _generar_con_motor_cpsat) se respetó -- si _generar_con_motor_cpsat
    devolviera una tupla con otra forma, el unpacking de la línea 848 de
    app.py lanzaría antes de llegar aquí."""
    assert set(data["schedule"].keys()) == set(nombres_esperados)
    for nombre, turnos in data["schedule"].items():
        assert len(turnos) == 7
        assert all(t in TURNOS_VALIDOS or t == "Vacaciones" for t in turnos)
    assert {p["name"] for p in data["staff"]} == set(nombres_esperados)
    assert isinstance(data["advertencias"], list)
    assert len(data["dias"]) == 7


# ═══════════════════════════════════════════════════════════════════════════
# 1-2. Contrato de 6 valores respetado con el interruptor activo
# ═══════════════════════════════════════════════════════════════════════════

def test_generar_con_cpsat_activo_recepcion_respeta_contrato_de_seis_valores(
        client, data_dir, monkeypatch):
    from herramientas import guardar_plantilla
    from modelo_recepcion import STAFF_DEFAULT as RECEPCION_STAFF

    guardar_plantilla(copy.deepcopy(RECEPCION_STAFF), "recepcion")
    monkeypatch.setenv("USAR_MOTOR_CPSAT", "1")
    _admin(client)

    r = client.post("/api/horario/generar", json={
        "semana": SEMANA_RECEPCION.isoformat(), "dept": "recepcion",
    })
    assert r.status_code == 200, r.get_json()
    data = r.get_json()["data"]
    _asserts_forma_respuesta_generacion(data, [p["name"] for p in RECEPCION_STAFF])

    # Verificación directa del contrato de la tupla de 6 valores, comparando
    # forma (no valores -- son motores distintos) con la rama heurística.
    from app import _generar_con_motor_cpsat
    from modelo_base import generate_schedule_valido
    from app import _get_dept_config

    cfg = _get_dept_config("recepcion")
    resultado_cpsat = _generar_con_motor_cpsat(
        staff_list=copy.deepcopy(RECEPCION_STAFF), week_start=SEMANA_RECEPCION,
        cfg=cfg, dept_id="recepcion",
    )
    resultado_heuristico = generate_schedule_valido(
        staff_list=copy.deepcopy(RECEPCION_STAFF), week_start=SEMANA_RECEPCION, cfg=cfg,
    )
    assert len(resultado_cpsat) == len(resultado_heuristico) == 6
    tipos_esperados = (dict, list, date, dict, dict, list)
    for valor_cpsat, valor_heuristico, tipo in zip(
            resultado_cpsat, resultado_heuristico, tipos_esperados):
        assert isinstance(valor_cpsat, tipo)
        assert isinstance(valor_heuristico, tipo)


def test_generar_con_cpsat_activo_mozos_respeta_contrato_de_seis_valores(
        client, data_dir, monkeypatch):
    from herramientas import guardar_plantilla
    from modelo_mozos import STAFF_DEFAULT as MOZOS_STAFF

    guardar_plantilla(copy.deepcopy(MOZOS_STAFF), "mozos")
    monkeypatch.setenv("USAR_MOTOR_CPSAT", "1")
    _admin(client)

    r = client.post("/api/horario/generar", json={
        "semana": SEMANA_MOZOS.isoformat(), "dept": "mozos",
    })
    assert r.status_code == 200, r.get_json()
    data = r.get_json()["data"]
    _asserts_forma_respuesta_generacion(data, [p["name"] for p in MOZOS_STAFF])

    from app import _generar_con_motor_cpsat, _get_dept_config
    from modelo_base import generate_schedule_valido

    cfg = _get_dept_config("mozos")
    resultado_cpsat = _generar_con_motor_cpsat(
        staff_list=copy.deepcopy(MOZOS_STAFF), week_start=SEMANA_MOZOS,
        cfg=cfg, dept_id="mozos",
    )
    resultado_heuristico = generate_schedule_valido(
        staff_list=copy.deepcopy(MOZOS_STAFF), week_start=SEMANA_MOZOS, cfg=cfg,
    )
    assert len(resultado_cpsat) == len(resultado_heuristico) == 6
    tipos_esperados = (dict, list, date, dict, dict, list)
    for valor_cpsat, valor_heuristico, tipo in zip(
            resultado_cpsat, resultado_heuristico, tipos_esperados):
        assert isinstance(valor_cpsat, tipo)
        assert isinstance(valor_heuristico, tipo)


# ═══════════════════════════════════════════════════════════════════════════
# 3. Estado del motor CP-SAT persiste y se recupera entre semanas encadenadas
# ═══════════════════════════════════════════════════════════════════════════

def test_estado_cpsat_persiste_y_se_recupera_entre_semanas_encadenadas(
        client, data_dir, monkeypatch):
    """NOTA (esta sesión — Opción B del diagnóstico de "Noche fantasma" tras
    edición manual): estado_rotacion (orden fijo + deudas, una cola de
    turnos que no es un hecho observable en el schedule) sigue
    persistiéndose sin cambios en cpsat_estado_{dept}.json. noches_ultimos_
    5_dias y forced_days_off_extra YA NO se guardan ahí -- se derivan
    siempre del historial real en cada llamada (app._derivar_estado_racha_
    cpsat_desde_historial), así que este test ya no los busca en el
    fichero: en su lugar, confirma que lo que recibió generar_semana_cpsat_
    con_cobertura_nocturna en cada llamada coincide con lo que esa función
    de derivación calcula a partir del historial real ya guardado tras la
    semana anterior (ver tests/test_estado_cpsat_deriva_de_historial.py
    para la cobertura completa de ese mecanismo, incluida su interacción
    con ediciones manuales)."""
    import app as app_module
    import motor_cpsat
    from herramientas import guardar_plantilla
    from modelo_mozos import STAFF_DEFAULT as MOZOS_STAFF

    guardar_plantilla(copy.deepcopy(MOZOS_STAFF), "mozos")
    monkeypatch.setenv("USAR_MOTOR_CPSAT", "1")
    _admin(client)

    llamadas = []
    original = motor_cpsat.generar_semana_cpsat_con_cobertura_nocturna

    def _espia(*args, **kwargs):
        llamadas.append({
            "estado_rotacion": copy.deepcopy(kwargs.get("estado_rotacion")),
            "noches_ultimos_5_dias": copy.deepcopy(kwargs.get("noches_ultimos_5_dias")),
        })
        return original(*args, **kwargs)

    monkeypatch.setattr(motor_cpsat, "generar_semana_cpsat_con_cobertura_nocturna", _espia)

    estado_path = os.path.join(str(data_dir), "cpsat_estado_mozos.json")
    estados_en_disco = []
    cfg = app_module._get_dept_config("mozos")

    for i in range(3):
        semana = SEMANA_MOZOS + timedelta(weeks=i)
        r = client.post("/api/horario/generar", json={
            "semana": semana.isoformat(), "dept": "mozos",
        })
        assert r.status_code == 200, r.get_json()
        assert os.path.exists(estado_path)
        with open(estado_path, encoding="utf-8") as f:
            estados_en_disco.append(json.load(f))

    assert len(llamadas) == 3

    # El fichero ya solo persiste estado_rotacion -- ver nota de la
    # docstring arriba (Opción B del diagnóstico de esta sesión).
    for estado in estados_en_disco:
        assert set(estado.keys()) == {"estado_rotacion"}

    # Semana 1: no había fichero de estado previo -> arranca de la semilla
    # inicial (_estado_rotacion_inicial_cpsat), no de un estado_rotacion vacío.
    assert llamadas[0]["estado_rotacion"]["orden"]
    assert llamadas[0]["noches_ultimos_5_dias"] == {}

    # Semana 2 y 3: estado_rotacion se recupera EXACTAMENTE de lo
    # persistido tras la llamada anterior (sin cambios respecto a antes),
    # y noches_ultimos_5_dias coincide con lo que la derivación calcula a
    # partir del historial real ya guardado tras la semana anterior (no
    # con ninguna copia cacheada -- ya no existe ninguna).
    semana2 = SEMANA_MOZOS + timedelta(weeks=1)
    semana3 = SEMANA_MOZOS + timedelta(weeks=2)
    noches_esperadas_2, _ = app_module._derivar_estado_racha_cpsat_desde_historial(
        "mozos", semana2, cfg)
    noches_esperadas_3, _ = app_module._derivar_estado_racha_cpsat_desde_historial(
        "mozos", semana3, cfg)

    assert llamadas[1]["estado_rotacion"] == estados_en_disco[0]["estado_rotacion"]
    assert llamadas[1]["noches_ultimos_5_dias"] == noches_esperadas_2
    assert llamadas[2]["estado_rotacion"] == estados_en_disco[1]["estado_rotacion"]
    assert llamadas[2]["noches_ultimos_5_dias"] == noches_esperadas_3


# ═══════════════════════════════════════════════════════════════════════════
# 4. Con el interruptor apagado (default): regresión explícita
# ═══════════════════════════════════════════════════════════════════════════

def test_con_interruptor_apagado_sigue_llamando_solo_al_heuristico(
        client, data_dir, monkeypatch):
    """Regresión explícita: sin USAR_MOTOR_CPSAT (default apagado),
    api_generar debe invocar generate_schedule_valido con los mismos
    argumentos de siempre y NUNCA tocar motor_cpsat -- si estos dos
    commits hubieran cambiado el comportamiento por defecto, uno de los
    dos espías de abajo lo detectaría."""
    import modelo_base
    import motor_cpsat
    from herramientas import guardar_plantilla
    from modelo_recepcion import STAFF_DEFAULT as RECEPCION_STAFF

    guardar_plantilla(copy.deepcopy(RECEPCION_STAFF), "recepcion")
    monkeypatch.delenv("USAR_MOTOR_CPSAT", raising=False)  # explícito: apagado
    _admin(client)

    llamadas_heuristico = []
    original_heuristico = modelo_base.generate_schedule_valido

    def _espia_heuristico(*args, **kwargs):
        llamadas_heuristico.append(kwargs)
        return original_heuristico(*args, **kwargs)

    def _espia_cpsat_no_debe_llamarse(*args, **kwargs):
        raise AssertionError(
            "motor_cpsat.generar_semana_cpsat_con_cobertura_nocturna no debe "
            "llamarse con el interruptor apagado (default)"
        )

    monkeypatch.setattr(modelo_base, "generate_schedule_valido", _espia_heuristico)
    monkeypatch.setattr(
        motor_cpsat, "generar_semana_cpsat_con_cobertura_nocturna",
        _espia_cpsat_no_debe_llamarse,
    )

    r = client.post("/api/horario/generar", json={
        "semana": SEMANA_RECEPCION.isoformat(), "dept": "recepcion",
    })
    assert r.status_code == 200, r.get_json()
    data = r.get_json()["data"]
    _asserts_forma_respuesta_generacion(data, [p["name"] for p in RECEPCION_STAFF])

    # generate_schedule_valido se llamó exactamente una vez, con la misma
    # firma de argumentos que ya usaba antes de introducir el interruptor.
    assert len(llamadas_heuristico) == 1
    kwargs = llamadas_heuristico[0]
    assert set(kwargs.keys()) == {
        "staff_list", "week_start", "cfg",
        "prev_week_covers", "prev_sunday_shifts", "prev_last_shifts",
    }
    assert kwargs["week_start"] == SEMANA_RECEPCION
    assert {p["name"] for p in kwargs["staff_list"]} == {p["name"] for p in RECEPCION_STAFF}
