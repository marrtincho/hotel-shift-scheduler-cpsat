"""
tests/test_racha_noches.py — Valida las restricciones de racha de Noche
(ventana deslizante de motor_cpsat.py, sesión 4): máximo 5 noches en
cualquier ventana de 6 días consecutivos, y 2 días de descanso obligatorio
tras agotar una racha de 5.

Cubre los 4 escenarios pedidos, cada uno con 15+ semanas encadenadas, y
verifica con un CONTEO INDEPENDIENTE (recorriendo día a día la salida
concatenada, sin confiar en que el solver dice que es válido) que ninguna
persona supera nunca 5 noches en ninguna ventana de 6 días, en toda la
cadena.

No usa DATA_DIR/herramientas más allá de lo que motor_cpsat.py ya necesita
internamente para _get_weekend_ratios/_get_shift_counts (fase 2) — de ahí
el fixture _data_dir_aislado (autouse), igual que tests/test_motor_cpsat.py.
"""

import copy
from datetime import date, timedelta

import pytest

from modelo_base import DAYS, DeptConfig
from motor_cpsat import (
    generar_semana_cpsat_con_cobertura_nocturna,
    _construir_modelo,
    _resolver_y_extraer,
    agregar_restriccion_racha_noches,
)

SEMANA_INICIO = date(2099, 2, 3)  # Lunes en futuro lejano


@pytest.fixture(autouse=True)
def _data_dir_aislado(tmp_path, monkeypatch):
    import herramientas
    backup = tmp_path / "backups"
    backup.mkdir()
    monkeypatch.setattr(herramientas, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(herramientas, "BACKUP_DIR", str(backup))
    monkeypatch.setattr(herramientas, "FESTIVOS_FILE", str(tmp_path / "festivos.json"))


def _cfg(**overrides) -> DeptConfig:
    base = dict(
        dept_id="test", dept_label="Test",
        night_fixed_days_off=[3, 4],          # Jueves, Viernes
        night_cover_compensation=[5, 6],
        min_coverage={"Mañana": 2, "Tarde": 2, "Noche": 1, "Partido": 0},
        max_coverage={"Mañana": 3, "Tarde": 3, "Noche": 1, "Partido": 1},
        no_night_roles={"jefe", "subjefe"},
        fixed_roles={"jefe": "Mañana"}, fixed_days_off={"jefe": [5, 6]},
        cover_consecutive=True, night_rotation_key="test",
        allow_non_consecutive_cover=False, prefer_next_week_for_cover=False,
    )
    base.update(overrides)
    return DeptConfig(**base)


def _staff_base(nombres_normales):
    """night_fixed + jefe + N normales. Probado manualmente factible con
    6 normales bajo _cfg() por defecto — con menos, la cobertura mínima
    diaria (Mañana2+Tarde2+Noche1=5) deja demasiado poco margen y el
    modelo puede volverse infactible por simple falta de personal, algo
    ajeno a la restricción de racha que este archivo prueba."""
    staff = [
        {"name": "NocheFijo", "role": "night_fixed"},
        {"name": "Jefe", "role": "jefe"},
    ]
    staff += [{"name": n, "role": "normal"} for n in nombres_normales]
    return staff


def _estado_rotacion_inicial(orden):
    return {
        "orden": list(orden),
        "siguiente_turno": orden[0],
        "deudas": {},
        "usar_orden_fijo": True,
        "historico_balance": {},
        "ausencias_night_fixed_consecutivas": 0,
    }


def _simular_cadena(nombres_normales, n_semanas, patron_ausencia=None, estado_sucio=None,
                     devolver_trazas=False):
    """Encadena n_semanas semanas de generar_semana_cpsat_con_cobertura_nocturna.

    patron_ausencia: lista de bool (una por semana), True si night_fixed
    está de vacaciones/baja esa semana (forced_days_off = toda la semana).
    Si None, night_fixed siempre presente.

    estado_sucio: noches_ultimos_5_dias inicial (para el escenario de
    "estado sucio" — alguien entra a la simulación ya a un paso del
    límite). Si None, arranca limpio (todo False).

    devolver_trazas: si True, además del dict de turnos concatenados,
    devuelve la lista de estado_rotacion (uno por semana, tras esa semana)
    — para comprobar directamente el contador de semanas consecutivas de
    ausencia en vez de inferirlo indirectamente de la carga de Noche.

    Devuelve {nombre: [turnos concatenados cronológicamente de TODAS las
    semanas]} para todas las personas del staff (incluye NocheFijo/Jefe),
    o (ese dict, trazas) si devolver_trazas=True.
    """
    cfg = _cfg()
    orden = list(nombres_normales)
    estado_rotacion = _estado_rotacion_inicial(orden)
    noches_ultimos_5_dias = dict(estado_sucio or {})
    forced_days_off_extra = {}

    nombres_todos = ["NocheFijo", "Jefe"] + orden
    concatenado = {n: [] for n in nombres_todos}
    trazas = []

    for i in range(n_semanas):
        semana = SEMANA_INICIO + timedelta(weeks=i)
        staff = _staff_base(orden)

        if patron_ausencia and patron_ausencia[i]:
            nf = next(p for p in staff if p["role"] == "night_fixed")
            nf["forced_days_off"] = list(DAYS)

        for p in staff:
            extra = forced_days_off_extra.get(p["name"])
            if extra:
                existentes = p.get("forced_days_off", [])
                p["forced_days_off"] = list(dict.fromkeys(existentes + extra))

        resultado = generar_semana_cpsat_con_cobertura_nocturna(
            staff, semana, cfg, estado_rotacion, noches_ultimos_5_dias
        )

        for n in nombres_todos:
            concatenado[n].extend(resultado["schedule"][n])

        estado_rotacion = resultado["estado_rotacion"]
        noches_ultimos_5_dias = resultado["noches_ultimos_5_dias"]
        forced_days_off_extra = resultado["forced_days_off_extra"]
        trazas.append(estado_rotacion)

    if devolver_trazas:
        return concatenado, trazas
    return concatenado


def _verificar_racha_y_descanso(turnos: list, nombre: str, contexto: str = ""):
    """Recorre día a día (independiente del modelo CP-SAT) la secuencia
    concatenada de turnos de una persona a lo largo de TODA la cadena de
    semanas, y verifica:
      1. Ninguna racha de "Noche" consecutivos supera 5.
      2. Tras una racha de exactamente 5, los 2 días siguientes (si existen
         dentro de los datos disponibles) son "Libre".
    """
    n = len(turnos)
    i = 0
    while i < n:
        if turnos[i] == "Noche":
            j = i
            while j < n and turnos[j] == "Noche":
                j += 1
            racha = j - i
            assert racha <= 5, (
                f"{contexto}{nombre}: racha de {racha} noches consecutivas "
                f"(índices {i}-{j - 1} de la cadena concatenada), supera el "
                f"máximo de 5"
            )
            if racha == 5:
                for k in range(j, min(j + 2, n)):
                    assert turnos[k] == "Libre", (
                        f"{contexto}{nombre}: índice {k} de la cadena es "
                        f"{turnos[k]!r}, debía ser 'Libre' (descanso "
                        f"obligatorio tras racha de 5 noches terminada en "
                        f"índice {j - 1})"
                    )
            i = j
        else:
            i += 1


def _verificar_todos(concatenado: dict, contexto: str = ""):
    for nombre, turnos in concatenado.items():
        _verificar_racha_y_descanso(turnos, nombre, contexto)


# ─── Escenario 1: estado limpio, night_fixed presente toda la simulación ────

def test_estado_limpio_night_fixed_siempre_presente():
    concatenado = _simular_cadena(
        ["N1", "N2", "N3", "N4", "N5", "N6"], n_semanas=16
    )
    _verificar_todos(concatenado, contexto="[escenario 1] ")

    # NocheFijo, sin días libres de racha (solo cfg.night_fixed_days_off,
    # que rompe la racha cada semana), no debería siquiera acercarse a 5.
    racha_max_nf = 0
    racha = 0
    for t in concatenado["NocheFijo"]:
        racha = racha + 1 if t == "Noche" else 0
        racha_max_nf = max(racha_max_nf, racha)
    assert racha_max_nf == 5, (
        f"NocheFijo: racha máxima esperada exactamente 5 (Sáb+Dom de una "
        f"semana + Lun+Mar+Mié de la siguiente, ya que Jue+Vie son sus "
        f"días libres fijos), observada {racha_max_nf}"
    )


# ─── Escenario 2: estado "sucio" — alguien entra a un paso del límite ──────

def test_estado_sucio_fuerza_descanso_inmediato():
    nombres = ["N1", "N2", "N3", "N4", "N5", "N6"]
    # N1 entra con 4 de sus últimos 5 días ya trabajados como Noche.
    estado_sucio = {"N1": [False, True, True, True, True]}

    concatenado = _simular_cadena(nombres, n_semanas=16, estado_sucio=estado_sucio)
    _verificar_todos(concatenado, contexto="[escenario 2] ")

    # Confirmación específica: en los primeros días de la simulación, si N1
    # trabaja Noche el lunes (completando su racha a 5), los 2 días
    # siguientes deben ser Libre — ya cubierto por _verificar_racha_y_descanso,
    # pero lo revalidamos explícitamente aquí porque es el caso que este
    # escenario existe para probar.
    #
    # CORRECCIÓN (Fase 5, revisión de la comparativa): esta rama tenía
    # además un "else: assert turnos_n1[0] == 'Libre'" que asumía que el
    # lunes de N1 solo podía ser 'Noche' o 'Libre' — bug del test, no del
    # modelo. Con esta cfg (min/max_coverage Noche=1, NocheFijo presente y
    # no ausente esa semana), NocheFijo ya ocupa el ÚNICO cupo de Noche del
    # lunes por su turno fijo, así que N1 no puede estructuralmente
    # trabajar Noche ese día (violaría max_coverage) — pero nada en la
    # restricción de racha (ni ninguna otra) le prohíbe a N1 hacer
    # Mañana/Tarde/Partido el lunes si la racha no se completa; solo actúa
    # cuando una racha de 5 SÍ se completa. Verificado que N1 puede
    # legítimamente terminar en 'Tarde' ese día sin que
    # _verificar_racha_y_descanso (llamado arriba, independiente y sin
    # asumir nada sobre quién debía hacer qué) señale ningún problema.
    turnos_n1 = concatenado["N1"]
    if turnos_n1[0] == "Noche":
        assert turnos_n1[1] == "Libre" and turnos_n1[2] == "Libre", (
            f"N1 entró con 4/5 últimos días en Noche; si el lunes también "
            f"fue Noche (racha completada a 5), los 2 días siguientes "
            f"debían ser Libre. Primeros días: {turnos_n1[:5]}"
        )


# ─── Escenario 3: alternancia moderada de night_fixed (2-3 sem / 2-3 sem) ──

def _patron_bloques(n_semanas, bloque_presente=(2, 3), bloque_ausente=(2, 3)):
    """Genera un patrón de ausencia en bloques de tamaño variable (elige
    determinísticamente 2 o 3 alternando el tamaño, para que el patrón sea
    reproducible entre corridas sin depender de random)."""
    patron = []
    presente = True
    tam_idx = 0
    while len(patron) < n_semanas:
        tam = (bloque_presente if presente else bloque_ausente)[tam_idx % 2]
        patron += [not presente] * tam  # patron guarda "ausente" (bool)
        presente = not presente
        tam_idx += 1
    return patron[:n_semanas]


def test_alternancia_moderada_night_fixed():
    nombres = ["N1", "N2", "N3", "N4", "N5", "N6"]
    n_semanas = 18
    patron = _patron_bloques(n_semanas, bloque_presente=(2, 3), bloque_ausente=(2, 3))
    assert any(patron) and not all(patron), "el patrón debe alternar presencia/ausencia"

    concatenado, trazas = _simular_cadena(
        nombres, n_semanas=n_semanas, patron_ausencia=patron, devolver_trazas=True
    )
    _verificar_todos(concatenado, contexto="[escenario 3] ")

    # Verificación DIRECTA: con bloques de 2-3 semanas de ausencia, el
    # contador de ausencias_night_fixed_consecutivas debe llegar a >=2 en
    # algún momento (confirma que el modo respaldo sí se activó de verdad
    # en este escenario, no que se quedó inactivo silenciosamente).
    assert any(e["ausencias_night_fixed_consecutivas"] >= 2 for e in trazas), (
        "con bloques de 2-3 semanas de ausencia, el contador debió alcanzar "
        f">=2 en algún momento. Trazas: "
        f"{[e['ausencias_night_fixed_consecutivas'] for e in trazas]}"
    )

    # Y, de forma complementaria, la carga de Noche del equipo normal debe
    # haber subido por encima de cero durante esos tramos.
    total_noches_normales = sum(
        1 for n in nombres for t in concatenado[n] if t == "Noche"
    )
    assert total_noches_normales > 0, (
        "con night_fixed ausente en varios tramos, algún miembro normal del "
        "equipo debió cubrir Noche — no se observó ninguna"
    )


# ─── Escenario 4: alternancia agresiva (cada semana) ────────────────────────

def test_alternancia_agresiva_night_fixed_cada_semana():
    nombres = ["N1", "N2", "N3", "N4", "N5", "N6"]
    n_semanas = 16
    patron = [i % 2 == 1 for i in range(n_semanas)]  # ausente, presente, ausente, ...

    concatenado, trazas = _simular_cadena(
        nombres, n_semanas=n_semanas, patron_ausencia=patron, devolver_trazas=True
    )
    _verificar_todos(concatenado, contexto="[escenario 4] ")

    # Verificación DIRECTA (no inferida por conteo de turnos): con ausencias
    # de 1 sola semana seguidas de una presente, el contador de
    # ausencias_night_fixed_consecutivas nunca debe alcanzar 2 — el modo
    # respaldo (que exige >=2 consecutivas) no debería activarse nunca por
    # esa vía en este escenario, solo el gap normal de
    # cfg.night_fixed_days_off vía seleccionar_candidato_cobertura_nocturna
    # en modo normal.
    for i, estado in enumerate(trazas):
        assert estado["ausencias_night_fixed_consecutivas"] < 2, (
            f"semana {i + 1}: ausencias_night_fixed_consecutivas="
            f"{estado['ausencias_night_fixed_consecutivas']} — no debería "
            f"llegar a 2 con ausencias de solo 1 semana seguidas de una "
            f"presente (patrón: {patron})"
        )


# ─── Control negativo: confirma que la suite SÍ detectaría una regresión ────

def test_control_negativo_sin_restriccion_de_racha_6_noches_es_factible():
    """Demuestra que agregar_restriccion_racha_noches es la que realmente
    bloquea las rachas largas — no una coincidencia de otra parte del
    modelo. Fuerza manualmente 6 noches consecutivas (Lunes-Sábado) para
    una persona:
      - SIN la restricción de racha: factible (control negativo — confirma
        que el resto del modelo, por sí solo, SÍ permitiría esto; si este
        efectivamente).
      - CON la restricción: infactible (RuntimeError), tal como debe ser
        para "estructuralmente imposible de romper" — si alguien comenta
        agregar_restriccion_racha_noches por error, este test lo detecta
        (el primer bloque dejaría de ser el único factible)."""
    cfg = _cfg()
    # Sin night_fixed a propósito: night_fixed ya está forzado a Noche vía
    # Fase 1 en los días fuera de cfg.night_fixed_days_off, y chocaría con
    # max_coverage["Noche"]=1 al forzar también a N1 esos mismos días — eso
    # sería un conflicto de cobertura, no de racha, y contaminaría el
    # control negativo (infactible por el motivo equivocado).
    staff = [{"name": "Jefe", "role": "jefe"}] + [
        {"name": n, "role": "normal"} for n in ("N1", "N2", "N3", "N4", "N5", "N6")
    ]

    def _forzar_racha_6(model, x):
        for d in range(6):  # Lunes..Sábado
            model.Add(x["N1", d, "Noche"] == 1)

    model1, x1, names1, by_name1 = _construir_modelo(staff, SEMANA_INICIO, cfg)
    _forzar_racha_6(model1, x1)
    sched1 = _resolver_y_extraer(model1, x1, names1)  # no debe lanzar
    assert sched1["N1"][:6] == ["Noche"] * 6, (
        "control negativo mal construido: sin la restricción de racha, "
        "6 noches seguidas debería ser trivialmente factible"
    )

    model2, x2, names2, by_name2 = _construir_modelo(staff, SEMANA_INICIO, cfg)
    _forzar_racha_6(model2, x2)
    noches_vacio = {n: [False] * 5 for n in names2}
    agregar_restriccion_racha_noches(model2, x2, names2, noches_vacio)
    with pytest.raises(RuntimeError):
        _resolver_y_extraer(model2, x2, names2)
