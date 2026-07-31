"""
generar_comparativa_cpsat.py — Paso 1 del informe de Fase 5.

Genera las MISMAS 18 semanas encadenadas que referencia_motor_actual.json
(mismas plantillas STAFF_DEFAULT, mismo cfg, mismo lunes de arranque, mismo
estado limpio de partida) pero con motor_cpsat.py, y calcula métricas
comparables: violaciones duras (validar_schedule, motor-agnóstico), equidad
de findes (dispersión de _get_weekend_ratios), penalizaciones de
continuidad (_contar_penalizaciones), y frecuencia de uso de "Partido".

Cada motor escribe su propio historial en un DATA_DIR aislado (para que
_get_weekend_ratios/_get_shift_counts, que leen historial real vía
herramientas.cargar_historial, reflejen la evolución propia de cada motor
semana a semana — no comparten estado entre sí).
"""
import copy
import json
import os
import shutil
from datetime import date, timedelta

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
SCRATCH = "/tmp/claude-1000/-home-marrtincho-Documentos-Hotel-HorariosRecepcion-cpsat/7b9da1cb-b95b-4bf4-9038-ba5d02d45add/scratchpad"
DATA_CPSAT = os.path.join(SCRATCH, "data_cpsat_comparativa")
shutil.rmtree(DATA_CPSAT, ignore_errors=True)
os.makedirs(DATA_CPSAT, exist_ok=True)
os.environ["DATA_DIR"] = DATA_CPSAT

from modelo_base import (
    DAYS, validar_schedule, _contar_penalizaciones, _get_weekend_ratios,
)
from modelo_recepcion import RECEPCION_CONFIG, STAFF_DEFAULT as RECEPCION_STAFF
from modelo_mozos import MOZOS_CONFIG, STAFF_DEFAULT as MOZOS_STAFF
from herramientas import cargar_roles, registrar_semana, _semana_str
from motor_cpsat import generar_semana_cpsat_con_cobertura_nocturna

N_SEMANAS = 18
SEMANA_INICIAL = date(2026, 8, 3)


def construir_cfg(base_cfg, dept_id):
    cfg = copy.deepcopy(base_cfg)
    roles = cargar_roles(dept_id)
    cfg.fixed_roles = {
        rid: r["turno_fijo"] for rid, r in roles.items()
        if r.get("turno_fijo") and rid != "night_fixed"
    }
    cfg.fixed_days_off = {
        rid: r["dias_libres_fijos"] for rid, r in roles.items()
        if r.get("dias_libres_fijos") and rid != "night_fixed"
    }
    nf = roles.get("night_fixed", {})
    if "dias_libres_fijos" in nf:
        cfg.night_fixed_days_off = nf["dias_libres_fijos"] or []
    if nf.get("dias_libres_sustituto"):
        cfg.night_cover_compensation = nf["dias_libres_sustituto"]
    cfg.cobertura_roles = {
        rid: {"cubre": r["cubre_ausencia_de"],
              "semana_completa": bool(r.get("cobertura_semana_completa", False)),
              "dias_libres_fijos": bool(r.get("cubrir_dias_libres_fijos", False))}
        for rid, r in roles.items() if r.get("cubre_ausencia_de")
    }
    cfg.roles_excluidos_equidad = {
        rid for rid, r in roles.items() if r.get("excluir_de_equidad")
    }
    return cfg


def _calcular_prev_last(schedule_semana, staff_list):
    resultado = {}
    for p in staff_list:
        turnos = schedule_semana.get(p["name"], [])
        ult, tras_libre = None, False
        for t in reversed(turnos):
            if t == "Libre":
                tras_libre = True
                continue
            if t and t not in ("SIN_ASIGNAR", ""):
                ult = t
                break
        if ult is not None:
            resultado[p["name"]] = (ult, tras_libre)
    return resultado


def _estado_rotacion_inicial(staff, cfg):
    """cobertura_nocturna.py es un prototipo aislado sin loader de producción
    (app.py todavía no lo usa) — se inicializa aquí igual que haría un
    primer arranque: orden = elegibles (no night_fixed, no no_night_roles),
    sin deudas, orden fijo activo."""
    elegibles = [p["name"] for p in staff
                 if p.get("role") != "night_fixed" and p.get("role") not in cfg.no_night_roles]
    return {
        "orden": elegibles,
        "siguiente_turno": elegibles[0] if elegibles else None,
        "deudas": {n: 0 for n in elegibles},
        "usar_orden_fijo": True,
    }


def generar_cpsat_departamento(dept_id, base_cfg, staff_default):
    cfg = construir_cfg(base_cfg, dept_id)
    staff = copy.deepcopy(staff_default)

    estado_rotacion = _estado_rotacion_inicial(staff, cfg)
    noches_ultimos_5_dias = {}
    prev_sunday_shifts = {}
    prev_last_shifts = {}

    semana = SEMANA_INICIAL
    semanas_out = []
    forced_extra_prev = {}
    for i in range(N_SEMANAS):
        staff_semana = copy.deepcopy(staff)
        for p in staff_semana:
            extra = forced_extra_prev.get(p["name"])
            if extra:
                p["forced_days_off"] = list(p.get("forced_days_off", [])) + extra

        resultado = generar_semana_cpsat_con_cobertura_nocturna(
            staff_semana, semana, cfg, estado_rotacion, noches_ultimos_5_dias,
            prev_sunday_shifts=prev_sunday_shifts,
        )
        full_schedule = resultado["schedule"]
        estado_rotacion = resultado["estado_rotacion"]
        noches_ultimos_5_dias = resultado["noches_ultimos_5_dias"]
        forced_extra_prev = resultado["forced_days_off_extra"]

        violaciones = validar_schedule(full_schedule, staff_semana, cfg, prev_sunday_shifts)
        pen = _contar_penalizaciones(full_schedule, staff_semana, prev_last_shifts)

        semanas_out.append({
            "semana": semana.isoformat(),
            "schedule": full_schedule,
            "roles": {p["name"]: p.get("role") for p in staff_semana},
            "violaciones": violaciones,
            "penalizacion_continuidad": pen,
        })

        registrar_semana(semana, full_schedule, staff_semana, forzar=True, dept_id=dept_id)

        prev_sunday_shifts = {nom: t[6] for nom, t in full_schedule.items() if len(t) == 7}
        prev_last_shifts = _calcular_prev_last(full_schedule, staff_semana)
        semana += timedelta(weeks=1)

    return cfg, semanas_out


def _weekend_dispersion(dept_id, staff_default):
    ratios = _get_weekend_ratios(dept_id, staff_default)
    valores = [d["ratio"] for d in ratios.values()]
    if not valores:
        return None
    return {"min": min(valores), "max": max(valores), "dispersion": max(valores) - min(valores),
            "detalle": {n: round(d["ratio"], 3) for n, d in ratios.items()}}


def _contar_partido(semanas_out):
    total = 0
    for s in semanas_out:
        for turnos in s["schedule"].values():
            total += sum(1 for t in turnos if t == "Partido")
    return total


def _contar_violaciones(semanas_out):
    return sum(len(s["violaciones"]) for s in semanas_out)


def _sumar_continuidad(semanas_out):
    return sum(s["penalizacion_continuidad"] for s in semanas_out)


def main():
    resumen = {}
    for dept_id, base_cfg, staff_default in (
        ("recepcion", RECEPCION_CONFIG, RECEPCION_STAFF),
        ("mozos", MOZOS_CONFIG, MOZOS_STAFF),
    ):
        print(f"\n{'='*78}\nGenerando {N_SEMANAS} semanas CP-SAT — {dept_id}\n{'='*78}")
        cfg, semanas_out = generar_cpsat_departamento(dept_id, base_cfg, staff_default)

        n_violaciones = _contar_violaciones(semanas_out)
        pen_continuidad = _sumar_continuidad(semanas_out)
        partido = _contar_partido(semanas_out)
        dispersion = _weekend_dispersion(dept_id, staff_default)

        print(f"  Violaciones duras (validar_schedule) en {N_SEMANAS} semanas: {n_violaciones}")
        print(f"  Penalización de continuidad acumulada (_contar_penalizaciones): {pen_continuidad}")
        print(f"  Usos de 'Partido' en {N_SEMANAS} semanas: {partido}")
        print(f"  Dispersión de ratio de findes libres (min/max/rango): {dispersion}")

        resumen[dept_id] = {
            "violaciones_duras": n_violaciones,
            "penalizacion_continuidad": pen_continuidad,
            "uso_partido": partido,
            "dispersion_findes": dispersion,
        }

        out_path = os.path.join(REPO_DIR, f"comparativa_cpsat_{dept_id}.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(semanas_out, f, ensure_ascii=False, indent=2)
        print(f"  Detalle escrito en {out_path}")

    resumen_path = os.path.join(REPO_DIR, "comparativa_cpsat_resumen.json")
    with open(resumen_path, "w", encoding="utf-8") as f:
        json.dump(resumen, f, ensure_ascii=False, indent=2)
    print(f"\nResumen escrito en {resumen_path}")


if __name__ == "__main__":
    main()
