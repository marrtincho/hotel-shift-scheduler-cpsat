"""
escenario_viernes_carla_cpsat.py — Paso 3 del informe de Fase 5.

Corre el MISMO escenario reconstruido de escenario_viernes_carla_reconstruido.py
(plantilla de 10 personas, sin night_fixed, días sueltos recurrentes +
ausencias de jefe acumuladas durante 8 semanas encadenadas) con
motor_cpsat.py en vez del heurístico, para ver si CP-SAT encuentra solución
donde el heurístico (con SEED=1) no logró eliminar una violación de
continuidad en la semana 8.
"""
import copy
import os
import random
import shutil
import sys
from datetime import date, timedelta

SEED = int(sys.argv[1]) if len(sys.argv) > 1 else 1
random.seed(SEED)

SCRATCH = "/tmp/claude-1000/-home-marrtincho-Documentos-Hotel-HorariosRecepcion-cpsat/7b9da1cb-b95b-4bf4-9038-ba5d02d45add/scratchpad"
DATA_DIR = os.path.join(SCRATCH, "viernes_carla_cpsat_data")
shutil.rmtree(DATA_DIR, ignore_errors=True)
os.makedirs(DATA_DIR, exist_ok=True)
os.environ["DATA_DIR"] = DATA_DIR

from modelo_base import DAYS
from herramientas import cargar_roles
from motor_cpsat import generar_semana_cpsat_con_cobertura_nocturna
from escenario_viernes_carla_reconstruido import (
    construir_cfg, construir_staff, SEMANA_INICIAL, SUELTOS_RECURRENTES, SEMANAS_AUSENCIA_JEFE,
)

N_SEMANAS_ESCENARIO = 8


def _estado_rotacion_inicial(staff, cfg):
    elegibles = [p["name"] for p in staff
                 if p.get("role") != "night_fixed" and p.get("role") not in cfg.no_night_roles]
    return {
        "orden": elegibles,
        "siguiente_turno": elegibles[0] if elegibles else None,
        "deudas": {n: 0 for n in elegibles},
        "usar_orden_fijo": True,
    }


def main():
    cfg = construir_cfg()
    staff = construir_staff()

    estado_rotacion = _estado_rotacion_inicial(staff, cfg)
    noches_ultimos_5_dias = {}
    prev_sunday_shifts = {}
    forced_extra_prev = {}

    semana = SEMANA_INICIAL
    for i in range(N_SEMANAS_ESCENARIO):
        staff_semana = copy.deepcopy(staff)
        for nombre, dia in SUELTOS_RECURRENTES.items():
            p = next(x for x in staff_semana if x["name"] == nombre)
            p["forced_days_off"] = [dia]
        if i in SEMANAS_AUSENCIA_JEFE:
            jefe = next(x for x in staff_semana if x["role"] == "jefe")
            jefe["forced_days_off"] = jefe.get("forced_days_off", []) + ["Miércoles"]
        for p in staff_semana:
            extra = forced_extra_prev.get(p["name"])
            if extra:
                p["forced_days_off"] = list(p.get("forced_days_off", [])) + extra

        print(f"\n{'#'*70}\n# SEMANA {i+1} ({semana.isoformat()})\n{'#'*70}")
        try:
            resultado = generar_semana_cpsat_con_cobertura_nocturna(
                staff_semana, semana, cfg, estado_rotacion, noches_ultimos_5_dias,
                prev_sunday_shifts=prev_sunday_shifts,
            )
        except RuntimeError as e:
            print(f"  INFACTIBLE: {e}")
            return

        full_schedule = resultado["schedule"]
        estado_rotacion = resultado["estado_rotacion"]
        noches_ultimos_5_dias = resultado["noches_ultimos_5_dias"]
        forced_extra_prev = resultado["forced_days_off_extra"]
        print("  Resuelto sin RuntimeError (factible para CP-SAT).")

        if i == N_SEMANAS_ESCENARIO - 1:
            print("\n  Horario completo semana 8:")
            for name, turnos in full_schedule.items():
                print(f"    {name:8s}: {turnos}")

        prev_sunday_shifts = {nom: t[6] for nom, t in full_schedule.items() if len(t) == 7}
        semana += timedelta(weeks=1)

    print("\nCP-SAT resolvió las 8 semanas del escenario reconstruido sin infactibilidad "
          "en ninguna (todas las restricciones duras satisfechas por construcción).")


if __name__ == "__main__":
    main()
