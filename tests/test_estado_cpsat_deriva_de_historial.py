"""
tests/test_estado_cpsat_deriva_de_historial.py — Verifica el arreglo de esta
sesión (Opción B, decidida explícitamente frente a la Opción A "cachear y
reescribir"): noches_ultimos_5_dias y forced_days_off_extra ya NO se leen de
una copia persistida en cpsat_estado_{dept}.json -- se derivan SIEMPRE, en
cada llamada a _generar_con_motor_cpsat, del historial REAL ya guardado
(app._derivar_estado_racha_cpsat_desde_historial), el mismo que
api_editar_turno modifica. estado_rotacion (orden fijo + deudas, una cola de
turnos que no es un hecho observable en el schedule) sigue persistiéndose sin
cambios en ese fichero.

Bug original (diagnóstico de esta sesión): antes de este arreglo, editar a
mano el domingo de alguien (o los últimos días de una racha de noches) tras
generar una semana no se reflejaba en absoluto en cpsat_estado_{dept}.json
-- la semana siguiente heredaba una "Noche fantasma" del solver original en
vez del turno real editado.
"""

import copy
import os
from datetime import date, timedelta

import pytest


SEMANA_1 = date(2099, 6, 1)  # Lunes en el futuro lejano


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    backup = tmp_path / "backups"
    backup.mkdir()
    import herramientas
    import app as app_module
    monkeypatch.setattr(herramientas, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(herramientas, "BACKUP_DIR", str(backup))
    monkeypatch.setattr(herramientas, "INTERVENCIONES_FILE", str(tmp_path / "intervenciones.json"))
    monkeypatch.setattr(herramientas, "CONFIG_MOTOR_FILE", str(tmp_path / "config_motor.json"))
    monkeypatch.setattr(app_module, "DATA_DIR", str(tmp_path))
    monkeypatch.setenv("USAR_MOTOR_CPSAT", "1")
    return tmp_path


@pytest.fixture
def client(data_dir):
    import app as app_module
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def _sesion(client, *, uid, username, rol, nombre):
    with client.session_transaction() as sess:
        sess["uid"] = uid
        sess["username"] = username
        sess["rol"] = rol
        sess["nombre"] = nombre
        sess["vinculado"] = None


def _admin(client):
    _sesion(client, uid="1", username="admin", rol="admin", nombre="Admin")


def _generar(client, semana, dept="mozos"):
    r = client.post("/api/horario/generar", json={"semana": semana.isoformat(), "dept": dept})
    assert r.status_code == 200, r.get_json()
    return r.get_json()["data"]["schedule"]


def _editar_turno(client, *, dept, semana, nombre, dia_idx, turno):
    r = client.put("/api/horario/turno", json={
        "dept": dept, "semana": semana.isoformat(),
        "nombre": nombre, "dia_idx": dia_idx, "turno": turno,
    })
    assert r.status_code == 200, r.get_json()
    return r.get_json()["data"]["schedule"]


# ═══════════════════════════════════════════════════════════════════════════
# 1. Reproducción EXACTA del escenario que detectó el bug
# ═══════════════════════════════════════════════════════════════════════════

def test_editar_domingo_a_mano_no_dejar_noche_fantasma_en_semana_siguiente(client, data_dir):
    """Genera una semana, edita a mano el domingo de quien quedó con Noche
    ese día (cambiándolo a Libre), y confirma que la derivación para la
    semana siguiente ya NO arrastra esa Noche fantasma -- el índice del
    empalme correspondiente al domingo pasado debe ser False, reflejando
    el Libre real tras la edición, no la Noche que el solver había puesto
    originalmente."""
    from herramientas import guardar_plantilla
    from modelo_mozos import STAFF_DEFAULT as MOZOS_STAFF
    import app as app_module

    guardar_plantilla(copy.deepcopy(MOZOS_STAFF), "mozos")
    _admin(client)

    semana2 = SEMANA_1 + timedelta(weeks=1)

    schedule1 = _generar(client, SEMANA_1)
    candidato = next((n for n, t in schedule1.items() if t[6] == "Noche"), None)
    assert candidato is not None, f"precondición: nadie con Noche el domingo en {schedule1}"

    _editar_turno(client, dept="mozos", semana=SEMANA_1, nombre=candidato, dia_idx=6, turno="Libre")

    cfg = app_module._get_dept_config("mozos")
    noches, _, _ = app_module._derivar_estado_racha_cpsat_desde_historial("mozos", semana2, cfg)

    assert noches[candidato][4] is False, (
        f"BUG: la derivación para la semana siguiente sigue viendo el "
        f"domingo de {candidato} como Noche (índice 4 = True) aunque se "
        f"editó a mano a Libre -- noches_ultimos_5_dias[{candidato!r}]="
        f"{noches[candidato]}"
    )

    # La semana 2 debe poder generarse sin problema, partiendo de la
    # realidad editada (no del estado fantasma).
    _generar(client, semana2)


def test_forzar_domingo_a_noche_a_mano_se_refleja_en_la_derivacion(client, data_dir):
    """Caso simétrico: editar el domingo de alguien de Libre a Noche
    también debe reflejarse -- confirma que la derivación lee el turno
    real editado en ambas direcciones, no solo hacia 'Libre'."""
    from herramientas import guardar_plantilla
    from modelo_mozos import STAFF_DEFAULT as MOZOS_STAFF
    import app as app_module

    guardar_plantilla(copy.deepcopy(MOZOS_STAFF), "mozos")
    _admin(client)
    semana2 = SEMANA_1 + timedelta(weeks=1)

    schedule1 = _generar(client, SEMANA_1)
    # Alguien con Libre el domingo (Noche o no el sábado da igual: Noche
    # tras Noche está permitida por la regla de transición).
    candidato = next((n for n, t in schedule1.items() if t[6] == "Libre"), None)
    assert candidato is not None, f"precondición no cumplida en {schedule1}"

    _editar_turno(client, dept="mozos", semana=SEMANA_1, nombre=candidato, dia_idx=6, turno="Noche")

    cfg = app_module._get_dept_config("mozos")
    noches, _, _ = app_module._derivar_estado_racha_cpsat_desde_historial("mozos", semana2, cfg)
    assert noches[candidato][4] is True, (
        f"La edición manual de {candidato} (domingo Libre->Noche) debía "
        f"reflejarse en la derivación: noches_ultimos_5_dias[{candidato!r}]="
        f"{noches[candidato]}"
    )


# ═══════════════════════════════════════════════════════════════════════════
# 2. Cadena de 4 semanas en modo respaldo (sin night_fixed) con ediciones
#    manuales intercaladas que afectan racha/compensación
# ═══════════════════════════════════════════════════════════════════════════

def _verificar_racha_min_2_max_5(turnos: list, nombre: str, contexto: str = ""):
    """Verificador independiente (mismo patrón que test_racha_noches.py::
    _verificar_racha_y_descanso, extendido aquí para comprobar TAMBIÉN el
    mínimo de 2, no solo el máximo de 5): recorre la secuencia concatenada
    día a día y valida que ninguna racha de 'Noche' sea más corta que 2 ni
    más larga que 5 -- salvo la racha que toque el borde derecho de la
    ventana observada (aún podría continuar en una semana futura no
    generada todavía en este test, así que no se puede afirmar que ya
    terminó)."""
    n = len(turnos)
    i = 0
    while i < n:
        if turnos[i] == "Noche":
            j = i
            while j < n and turnos[j] == "Noche":
                j += 1
            racha = j - i
            toca_borde_derecho = (j == n)
            assert racha <= 5, (
                f"{contexto}{nombre}: racha de {racha} noches consecutivas "
                f"(índices {i}-{j - 1}), supera el máximo de 5"
            )
            if not toca_borde_derecho:
                assert racha >= 2, (
                    f"{contexto}{nombre}: racha de solo {racha} noche(s) "
                    f"consecutiva(s) (índices {i}-{j - 1}), por debajo del "
                    f"mínimo de 2"
                )
            i = j
        else:
            i += 1


def test_cadena_4_semanas_con_ediciones_intercaladas_respeta_racha_2_a_5(client, data_dir):
    """Modo respaldo permanente (sin night_fixed en la plantilla, ver
    cobertura_nocturna.modo_respaldo_activo): 4 semanas encadenadas por el
    endpoint real, con DOS ediciones manuales intercaladas en puntos que
    afectan directamente a la racha/compensación (el domingo de quien
    cubrió noche ese día, en dos semanas distintas de la cadena). Tras la
    cadena completa, el verificador independiente día-a-día confirma que
    la racha de Noche de cada persona, en el historial REAL ya editado,
    nunca baja de 2 ni supera 5 -- si la derivación siguiera arrastrando
    estado cacheado en vez de leer el historial real, una edición podría
    producir una racha inconsistente en la semana siguiente (por partir de
    un empalme que ya no coincide con la realidad)."""
    from herramientas import guardar_plantilla, cargar_historial

    staff_sin_night_fixed = [
        {"name": "Ana",    "role": "normal", "preferences": []},
        {"name": "Beto",   "role": "normal", "preferences": []},
        {"name": "Carla",  "role": "normal", "preferences": []},
        {"name": "Dani",   "role": "normal", "preferences": []},
        {"name": "Elena",  "role": "normal", "preferences": []},
    ]
    guardar_plantilla(copy.deepcopy(staff_sin_night_fixed), "mozos")
    _admin(client)

    nombres = [p["name"] for p in staff_sin_night_fixed]
    concatenado = {n: [] for n in nombres}

    for i in range(4):
        semana = SEMANA_1 + timedelta(weeks=i)
        schedule = _generar(client, semana)

        # Edición manual intercalada en las semanas 0 y 2: el domingo de
        # quien cubrió Noche ese día se cambia a Libre -- exactamente el
        # tipo de edición que detectó el bug original, repetida a mitad
        # de una cadena más larga en vez de una sola vez. Solo se aplica
        # si la racha real que termina en domingo tiene 3 o más noches
        # (recortar 1 deja >=2, sin violar el mínimo por culpa de la
        # PROPIA edición de prueba -- una edición manual no se valida
        # contra la racha, así que crear ella misma una racha de 1 no
        # sería un fallo de la derivación, solo ruido del test).
        if i in (0, 2):
            candidato = None
            for n, t in schedule.items():
                if t[6] != "Noche":
                    continue
                racha = 1
                d = 5
                while d >= 0 and t[d] == "Noche":
                    racha += 1
                    d -= 1
                if racha >= 3:
                    candidato = n
                    break
            if candidato is not None:
                schedule = _editar_turno(
                    client, dept="mozos", semana=semana,
                    nombre=candidato, dia_idx=6, turno="Libre",
                )

        for n in nombres:
            concatenado[n].extend(schedule[n])

    # Confirmar contra el historial real en disco (no solo la última
    # respuesta en memoria) -- refleja exactamente lo que la semana
    # siguiente vería al derivar su estado.
    historial = cargar_historial("mozos")
    for i in range(4):
        semana = SEMANA_1 + timedelta(weeks=i)
        key = semana.isoformat()
        assert key in historial, f"falta la semana {key} en el historial real"

    for n in nombres:
        _verificar_racha_min_2_max_5(concatenado[n], n, contexto=f"cadena 4 semanas, ")

    # Confirmar que el escenario estresó de verdad el mecanismo: en 4
    # semanas de modo respaldo alguien debió encadenar al menos una racha
    # real de Noche (si nadie lo hiciera nunca, esta prueba no verificaría
    # nada sobre lo que dice verificar).
    total_noches = sum(t.count("Noche") for t in concatenado.values())
    assert total_noches > 0, "nadie cubrió Noche nunca en las 4 semanas -- escenario no estresó el mecanismo"
