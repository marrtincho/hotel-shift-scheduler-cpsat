"""
tests/test_presencia_noches.py — Presencia parcial (§1.8 de REGLAS.md) en la
cobertura nocturna (cobertura_nocturna.py): un rol con días de presencia que
tiene las noches habilitadas (sin_noches desmarcado → su rol NO está en
cfg.no_night_roles) solo puede cubrir noches dentro de sus días.

Invariantes verificadas:
  1. Elegibilidad: con `dias_cubrir` fuera de sus días, el titular de
     presencia no se elige (se salta, con deuda anotada si el orden fijo
     está activo, igual que cualquier bloqueado); dentro de sus días sí.
  2. Garantía de racha mínima: un arranque de bloque en el último día de
     presencia antes de un día fuera (p.ej. Domingo con presencia {5,6}) no
     se permite — el bloque se decidiría en una noche suelta
     (< MIN_NOCHES_TRAMO_PRESENCIA).
  3. Interrupción: el bloque de un titular de presencia se corta al llegar
     a un día fuera (persona_bloqueada_dia), ya con ≥2 noches cubiertas.
  4. Longitud: longitud_bloque_respaldo recorta al tramo de presencia
     disponible (patrón semanal cíclico) cuando cae en el rango 4-5.
  5. Sin presencia_roles todo esto es inerte (no-regresión).
  6. Integración CP-SAT: 6 semanas encadenadas con un refuerzo de finde
     ({5,6}) en la rotación de noches — sin INFEASIBLE espurio, el refuerzo
     cubre Sáb+Dom cada semana, sus días fuera quedan siempre "Fuera", y su
     bloque cruza el límite de semana retomándose/cortándose bien (el
     escenario se verificó empíricamente antes de fijar las aserciones,
     mismo patrón que los tests de racha de Fase 5 — no asumido).
"""

import copy
from datetime import date, timedelta

import pytest

from modelo_base import DAYS, DeptConfig
from cobertura_nocturna import (
    MIN_NOCHES_TRAMO_PRESENCIA,
    longitud_bloque_respaldo,
    persona_bloqueada_dia,
    seleccionar_candidato_cobertura_nocturna,
)


# ─── Helpers ────────────────────────────────────────────────────────────────

def _cfg(**overrides) -> DeptConfig:
    base = dict(
        dept_id="test", dept_label="Test",
        night_fixed_days_off=[3, 4],          # Jueves, Viernes
        night_cover_compensation=[5, 6],
        min_coverage={}, max_coverage={},
        no_night_roles={"jefe"},
        fixed_roles={}, fixed_days_off={},
        cover_consecutive=True, night_rotation_key="test",
        allow_non_consecutive_cover=False, prefer_next_week_for_cover=False,
    )
    base.update(overrides)
    return DeptConfig(**base)


def _staff_ref() -> list:
    """Ref (rol refuerzo, el de presencia en estos tests) + dos normales."""
    return [
        {"name": "Ref", "role": "refuerzo"},
        {"name": "N1",  "role": "normal"},
        {"name": "N2",  "role": "normal"},
    ]


def _estado(orden, siguiente=None, usar_orden_fijo=True) -> dict:
    return {
        "orden": list(orden),
        "siguiente_turno": siguiente if siguiente is not None else orden[0],
        "deudas": {},
        "usar_orden_fijo": usar_orden_fijo,
        "historico_balance": {},
        "ausencias_night_fixed_consecutivas": 0,
    }


# ─── 1. Elegibilidad: solo en sus días ──────────────────────────────────────

def test_refuerzo_con_noches_elegible_solo_en_sus_dias():
    """Con presencia {5,6} (Sáb/Dom) y sin_noches desmarcado, la rotación de
    orden fijo lo salta fuera de sus días (deuda anotada, como a cualquier
    bloqueado) y lo elige con normalidad dentro de ellos."""
    cfg = _cfg(presencia_roles={"refuerzo": {5, 6}})
    staff = _staff_ref()

    # Fuera de sus días: se salta con deuda y elige al siguiente del ciclo.
    estado = _estado(["Ref", "N1", "N2"], siguiente="Ref")
    elegido, estado = seleccionar_candidato_cobertura_nocturna(
        staff, cfg, estado, dias_cubrir={0}
    )
    assert elegido == "N1", "un Lunes el refuerzo está fuera: no es elegible"
    assert estado["deudas"].get("Ref") == 1, (
        "saltarse al titular por presencia debe anotarle deuda, igual que "
        "saltarse a alguien por vacación/petición — así recupera su turno"
    )

    # Dentro de sus días: elegible con normalidad.
    for dias in ({5}, {5, 6}):
        estado = _estado(["Ref", "N1", "N2"], siguiente="Ref")
        elegido, _ = seleccionar_candidato_cobertura_nocturna(
            staff, cfg, estado, dias_cubrir=dias
        )
        assert elegido == "Ref", f"dias_cubrir={dias}: el refuerzo está en sus días"

    # persona_bloqueada_dia: bloqueado Lun-Vie, libre Sáb/Dom. Quien no tiene
    # presencia nunca queda bloqueado por este motivo.
    for d in range(5):
        assert persona_bloqueada_dia(staff, cfg, "Ref", d)
    for d in (5, 6):
        assert not persona_bloqueada_dia(staff, cfg, "Ref", d)
    for d in range(7):
        assert not persona_bloqueada_dia(staff, cfg, "N1", d)


def test_arranque_en_ultimo_dia_de_presencia_exige_dos_noches():
    """El Domingo es el último día de presencia de {5,6} antes del Lunes
    fuera: un bloque que arrancara ahí cubriría 1 sola noche antes de
    cortarse — no se ofrece como arranque. Con una presencia que sí encadena
    dos noches desde el Domingo ({6,0}), sí es elegible ese día."""
    staff = _staff_ref()

    cfg = _cfg(presencia_roles={"refuerzo": {5, 6}})
    estado = _estado(["Ref", "N1", "N2"], siguiente="Ref")
    elegido, _ = seleccionar_candidato_cobertura_nocturna(
        staff, cfg, estado, dias_cubrir={6}
    )
    assert elegido != "Ref", (
        "Domingo con presencia {5,6}: el arranque sería una noche suelta "
        "(el Lunes queda fuera) — no debe elegirse al refuerzo"
    )

    cfg_ok = _cfg(presencia_roles={"refuerzo": {6, 0}})
    estado = _estado(["Ref", "N1", "N2"], siguiente="Ref")
    elegido, _ = seleccionar_candidato_cobertura_nocturna(
        staff, cfg_ok, estado, dias_cubrir={6}
    )
    assert elegido == "Ref", (
        "Domingo con presencia {6,0}: encadena Domingo+Lunes — sí puede "
        "arrancar el bloque ese día"
    )


def test_rol_con_sin_noches_marcado_nunca_elegible():
    """El default de la categoría (sin_noches premarcado → rol en
    cfg.no_night_roles) lo deja fuera de la rotación de noches aunque tenga
    presencia: el filtro de no_night_roles decide antes."""
    cfg = _cfg(presencia_roles={"refuerzo": {5, 6}},
               no_night_roles={"jefe", "refuerzo"})
    staff = _staff_ref()
    estado = _estado(["Ref", "N1", "N2"], siguiente="Ref")

    elegido, _ = seleccionar_candidato_cobertura_nocturna(
        staff, cfg, estado, dias_cubrir={5}
    )
    assert elegido == "N1", (
        "aunque sea Sábado (día de presencia del refuerzo), con sin_noches "
        "marcado el rol sigue en no_night_roles y nunca es elegible"
    )


# ─── 2. Interrupción del bloque y longitud ──────────────────────────────────

def test_bloque_se_interrumpe_al_llegar_a_un_dia_fuera():
    """Réplica del núcleo del bucle de modo respaldo (motor_cpsat.py):
    titular con presencia {0,1,2} (Lun-Mié) — el bloque se corta el Jueves
    (día fuera) con 3 noches ya cubiertas (≥ MIN_NOCHES_TRAMO_PRESENCIA)."""
    cfg = _cfg(presencia_roles={"refuerzo": {0, 1, 2}})
    staff = _staff_ref()

    assert longitud_bloque_respaldo(staff, cfg, "Ref", 0) == 5, (
        "tramo 3 < BLOQUE_RESPALDO_MIN: no se recorta la longitud "
        "declarada, el bloque simplemente se interrumpe al llegar el corte"
    )

    bloqueada_en = [d for d in range(7) if persona_bloqueada_dia(staff, cfg, "Ref", d)]
    assert bloqueada_en == [3, 4, 5, 6]

    cubiertas = 0
    for d in range(7):
        if persona_bloqueada_dia(staff, cfg, "Ref", d):
            break  # motor_cpsat: bloque = None y arranca uno nuevo con otro
        cubiertas += 1
    assert cubiertas == 3
    assert cubiertas >= MIN_NOCHES_TRAMO_PRESENCIA, (
        "la interrupción no puede dejar una racha de menos de 2 noches"
    )


def test_longitud_bloque_recorta_a_la_presencia_disponible():
    """longitud_bloque_respaldo: el tramo de presencia disponible (patrón
    semanal cíclico) trunca igual que una vacación — recorte explícito solo
    dentro del rango 4-5; fuera de él se deja 5 y el corte llega por
    interrupción."""
    staff = _staff_ref()

    # Tramo 4 (Lun-Jue): recorte exacto a 4 — el bloque termina de forma
    # natural justo antes del Viernes fuera.
    cfg_4 = _cfg(presencia_roles={"refuerzo": {0, 1, 2, 3}})
    assert longitud_bloque_respaldo(staff, cfg_4, "Ref", 0) == 4

    # Tramo 5 (Lun-Vie): cabe entero en el rango → 5.
    cfg_5 = _cfg(presencia_roles={"refuerzo": {0, 1, 2, 3, 4}})
    assert longitud_bloque_respaldo(staff, cfg_5, "Ref", 0) == 5

    # Tramo 3 (Lun-Mié): por debajo del mínimo → 5 (interrupción).
    cfg_3 = _cfg(presencia_roles={"refuerzo": {0, 1, 2}})
    assert longitud_bloque_respaldo(staff, cfg_3, "Ref", 0) == 5

    # Refuerzo finde: tramo Sáb-Dom = 2 → 5 (interrupción el Lunes).
    cfg_finde = _cfg(presencia_roles={"refuerzo": {5, 6}})
    assert longitud_bloque_respaldo(staff, cfg_finde, "Ref", 5) == 5

    # Combinada con vacación: gana el corte que llega antes (min).
    staff_forced_vie = copy.deepcopy(staff)
    staff_forced_vie[0]["forced_days_off"] = ["Viernes"]
    assert longitud_bloque_respaldo(staff_forced_vie, cfg_4, "Ref", 0) == 4
    assert longitud_bloque_respaldo(staff_forced_vie, _cfg(), "Ref", 0) == 4

    staff_forced_mie = copy.deepcopy(staff)
    staff_forced_mie[0]["forced_days_off"] = ["Miércoles"]
    assert longitud_bloque_respaldo(staff_forced_mie, cfg_4, "Ref", 0) == 5, (
        "la vacación del Miércoles (cabe=2) manda: 5 declaradas, "
        "interrupción tras 2 noches"
    )


# ─── 3. No-regresión: sin presencia_roles todo es inerte ────────────────────

def test_sin_presencia_roles_comportamiento_intacto():
    cfg = _cfg()  # presencia_roles vacío
    staff = _staff_ref()

    for d in range(7):
        assert not persona_bloqueada_dia(staff, cfg, "Ref", d)
    assert longitud_bloque_respaldo(staff, cfg, "Ref", 0) == 5

    estado = _estado(["Ref", "N1"], siguiente="Ref")
    elegido, _ = seleccionar_candidato_cobertura_nocturna(
        staff, cfg, estado, dias_cubrir={0}
    )
    assert elegido == "Ref", (
        "sin presencia_roles no hay ninguna exclusión nueva: el siguiente "
        "del ciclo cubre con normalidad"
    )


# ─── 4. Integración CP-SAT: refuerzo de finde en la rotación de noches ──────

@pytest.fixture(autouse=True)
def _data_dir_aislado(tmp_path, monkeypatch):
    """El motor inicializa categorías por defecto del departamento al
    arrancar: sin este aislamiento escribiría en el data/ real (mismo
    fixture que tests/test_racha_respaldo_multisemana.py)."""
    import herramientas
    backup = tmp_path / "backups"
    backup.mkdir()
    monkeypatch.setattr(herramientas, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(herramientas, "BACKUP_DIR", str(backup))
    monkeypatch.setattr(herramientas, "FESTIVOS_FILE", str(tmp_path / "festivos.json"))


SEMANA_INICIAL = date(2099, 4, 6)  # Lunes en futuro lejano
N_SEMANAS = 6


def _cfg_integracion() -> DeptConfig:
    return DeptConfig(
        dept_id="test_noches_presencia", dept_label="Test noches presencia",
        night_fixed_days_off=[3, 4], night_cover_compensation=[5, 6],
        min_coverage={"Mañana": 2, "Tarde": 2, "Noche": 1, "Partido": 0},
        max_coverage={"Mañana": 3, "Tarde": 3, "Noche": 1, "Partido": 1},
        no_night_roles={"jefe"},
        fixed_roles={"jefe": "Mañana"}, fixed_days_off={"jefe": [5, 6]},
        cover_consecutive=True, night_rotation_key="test_noches_presencia",
        allow_non_consecutive_cover=False, prefer_next_week_for_cover=False,
        presencia_roles={"refuerzo": {5, 6}},
    )


def _staff_integracion() -> list:
    return ([{"name": "Jefe", "role": "jefe"}]
            + [{"name": f"N{i}", "role": "normal"} for i in range(1, 7)]
            + [{"name": "Ref", "role": "refuerzo"}])


def _estado_sesgado(staff: list, cfg: DeptConfig) -> dict:
    """Balance puro con sesgo fuerte hacia Ref (covers=0 frente a 1000):
    Ref es el único elegible en sus días que nunca ha hecho noches, así que
    gana todos los arranques de bloque que caigan dentro de su presencia —
    el escenario determinista que verifica el camino completo (elegido vía
    seleccionar_candidato → longitud → continuación/corte → modelo)."""
    elegibles = [p["name"] for p in staff
                 if p.get("role") != "night_fixed" and p.get("role") not in cfg.no_night_roles]
    return {
        "orden": elegibles,
        "siguiente_turno": elegibles[0],
        "deudas": {n: 0 for n in elegibles},
        "usar_orden_fijo": False,
        "historico_balance": {
            "Ref": {"covers": 0, "pending": 0},
            **{n: {"covers": 1000, "pending": 0} for n in elegibles if n != "Ref"},
        },
        "ausencias_night_fixed_consecutivas": 0,
    }


def test_seis_semanas_encadenadas_refuerzo_finde_sin_infeasible():
    """Escenario completo en modo respaldo permanente (sin night_fixed):
    Ref (presencia {5,6}, noches habilitadas) debe cubrir Sáb+Dom de cada
    semana — sus días — y quedar "Fuera" de Lun a Vie; los normales cubren
    Lun-Vie. Sin INFEASIBLE espurio en la cadena y con el corte de su
    bloque al llegar el Lunes funcionando a través del límite de semana."""
    from motor_cpsat import generar_semana_cpsat_con_cobertura_nocturna

    cfg = _cfg_integracion()
    staff = _staff_integracion()
    names = [p["name"] for p in staff]
    normales = [f"N{i}" for i in range(1, 7)]

    estado = _estado_sesgado(staff, cfg)
    noches_ultimos_5_dias = {}
    prev_sunday_shifts = {}
    forced_extra_prev = {}

    noches_ref_totales = 0
    for i in range(N_SEMANAS):
        semana = SEMANA_INICIAL + timedelta(weeks=i)
        staff_semana = copy.deepcopy(staff)
        for p in staff_semana:
            extra = forced_extra_prev.get(p["name"])
            if extra:
                existentes = p.get("forced_days_off", [])
                p["forced_days_off"] = list(dict.fromkeys(existentes + extra))

        try:
            resultado = generar_semana_cpsat_con_cobertura_nocturna(
                staff_semana, semana, cfg, estado, noches_ultimos_5_dias,
                prev_sunday_shifts=prev_sunday_shifts,
            )
        except RuntimeError as e:
            pytest.fail(
                f"INFEASIBLE espurio en la semana {i + 1} ({semana.isoformat()}) "
                f"con un refuerzo de presencia en la rotación de noches: {e}"
            )

        sched = resultado["schedule"]

        # Cobertura exacta de Noche cada día (min=max=1 en esta cfg).
        for d in range(7):
            cubren = [n for n in names if sched[n][d] == "Noche"]
            assert len(cubren) == 1, (
                f"semana {i + 1} ({semana}), {DAYS[d]}: {len(cubren)} Noches "
                f"({cubren!r}) — se esperaba exactamente 1"
            )

        # Ref solo hace noches en Sáb/Dom; sus días fuera quedan "Fuera".
        assert sched["Ref"][5] == "Noche" and sched["Ref"][6] == "Noche", (
            f"semana {i + 1}: el refuerzo de finde debía cubrir Sáb+Dom "
            f"(sus únicos días elegibles): {sched['Ref']}"
        )
        for d in range(5):
            assert sched["Ref"][d] == "Fuera", (
                f"semana {i + 1}, {DAYS[d]}: el refuerzo está fuera de "
                f"contrato — la celda debe ser 'Fuera': {sched['Ref']}"
            )
        noches_ref_totales += 2

        # Los normales no hacen noches el fin de semana (las cubre el ref).
        for n in normales:
            assert sched[n][5] != "Noche" and sched[n][6] != "Noche", (
                f"semana {i + 1}: {n} hizo Noche en fin de semana, que "
                f"cubre el refuerzo en este escenario: {sched[n]}"
            )

        estado = resultado["estado_rotacion"]
        noches_ultimos_5_dias = resultado["noches_ultimos_5_dias"]
        forced_extra_prev = resultado["forced_days_off_extra"]
        prev_sunday_shifts = {n: sched[n][6] for n in names}

    assert noches_ref_totales == 2 * N_SEMANAS
    # El bloque del refuerzo quedó vivo al cruzar la última semana (2/5) —
    # se cortará el próximo Lunes (día fuera), nunca completa un bloque de 5.
    bloque = estado.get("bloque_actual") or {}
    assert bloque.get("nombre") == "Ref"
    assert bloque.get("noches_cubiertas") == 2
