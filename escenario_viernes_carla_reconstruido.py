"""
escenario_viernes_carla_reconstruido.py — RECONSTRUCCIÓN, no recuperación.

El escenario original ("Viernes de Carla": una plantilla de 10 personas
donde el motor heurístico no encontraba solución un viernes concreto) vivió
solo en el contexto de una conversación anterior que ya no está disponible
y NUNCA se persistió a disco — no puede recuperarse. Este script construye
un escenario NUEVO y análogo, diseñado para intentar provocar la MISMA
CLASE de fallo (el heurístico, con sus 50 reintentos aleatorios, no logra
encontrar una asignación válida un día concreto pese a que matemáticamente
sí exista margen), no para reproducir el caso histórico específico.

Diseño de la plantilla (10 personas, sin night_fixed — mismo patrón que
diagnostico_regresiones.py, para forzar rotación de noches por bloques
entre los "normal"):
  1 jefe, 1 subjefe, 2 adjunto_recepcion (no_night_roles), 6 normal.

Ajuste que deja Jueves+Viernes de la semana 3 sin apenas margen:
  - 5 de los 6 "normal" (Ana, Diego, Carla, Luis, Miguel) tienen petición de
    días libres concedida Jueves+Viernes esa semana (acumulación
    desfavorable deliberada — en la vida real correspondería a peticiones
    de distintas personas que casualmente coinciden, o vacaciones ya
    concedidas antes de comprobar el impacto conjunto). Solo Pedro queda
    libre de restricción entre los "normal".
  - cfg.min_coverage/max_coverage se estrechan respecto a producción
    (Mañana 2-2, Tarde 2-2 en vez de 2-3/2-3) para quitarle al pass2 del
    heurístico el margen de maniobra que la reparación de min_libres_dia
    (commit 6186a38, 2026-07-29) ya le da en el caso general.

RESULTADO OBSERVADO (10/10 ejecuciones con distintas semillas aleatorias,
ver informe de Fase 5): generate_schedule_valido(max_intentos=50) NO
encuentra una asignación sin violaciones en la semana 3 en NINGUNA de las
10 ejecuciones — falla de forma consistente (no es un evento raro de un 1
en 50), con violaciones que incluyen "Jueves: 1 persona de Tarde/Mañana,
mínimo 2" y numerosos "sin turno asignable" en fin de semana, arrastrados
por la rotación de pares de días libres. Se reporta tal cual en el informe
de Fase 5, junto con la comprobación de si motor_cpsat.py sí encuentra
solución sobre el mismo escenario exacto (Paso 3 del plan) — eso es lo que
demuestra si es una limitación real del heurístico o si el margen creado
aquí ya cruza a infactibilidad matemática genuina.
"""
import copy
import json
import os
import random
import shutil
import sys
from datetime import date, timedelta

# Semilla fija: el heurístico usa random.choice/random.shuffle internamente
# (pair_usage, orden de rotación de bloques de noche) y este escenario falla
# de forma INTERMITENTE (no en el 100% de las ejecuciones — ver informe de
# Fase 5) según qué azar le toque. Fijar la semilla hace el resultado
# reproducible entre ejecuciones; SEED=2 y SEED=4 (ver bucle de exploración
# documentado en el informe) son dos de las que sí producen el fallo.
SEED = int(sys.argv[1]) if len(sys.argv) > 1 else 1
random.seed(SEED)

SCRATCH_DATA = "/tmp/claude-1000/-home-marrtincho-Documentos-Hotel-HorariosRecepcion-cpsat/7b9da1cb-b95b-4bf4-9038-ba5d02d45add/scratchpad/viernes_carla_data"
shutil.rmtree(SCRATCH_DATA, ignore_errors=True)
os.makedirs(SCRATCH_DATA, exist_ok=True)
os.environ["DATA_DIR"] = SCRATCH_DATA

from modelo_base import DAYS, generate_schedule_valido, validar_viabilidad
from modelo_recepcion import RECEPCION_CONFIG
from herramientas import cargar_roles

SEMANA_INICIAL = date(2026, 8, 3)  # lunes


def construir_cfg():
    cfg = copy.deepcopy(RECEPCION_CONFIG)
    roles = cargar_roles("recepcion")
    cfg.fixed_roles = {
        rid: r["turno_fijo"] for rid, r in roles.items()
        if r.get("turno_fijo") and rid != "night_fixed"
    }
    cfg.fixed_days_off = {
        rid: r["dias_libres_fijos"] for rid, r in roles.items()
        if r.get("dias_libres_fijos") and rid != "night_fixed"
    }
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


def construir_staff():
    return [
        {"name": "Juan",    "role": "jefe",               "preferences": ["Mañana"]},
        {"name": "Laura",   "role": "subjefe",            "preferences": ["Tarde"]},
        {"name": "Sofia",   "role": "adjunto_recepcion",  "preferences": ["Mañana"]},
        {"name": "Marta",   "role": "adjunto_recepcion",  "preferences": ["Tarde"]},
        {"name": "Pedro",   "role": "normal",             "preferences": ["Mañana", "Partido"]},
        {"name": "Ana",     "role": "normal",             "preferences": []},
        {"name": "Miguel",  "role": "normal",             "preferences": ["Tarde"]},
        {"name": "Diego",   "role": "normal",             "preferences": []},
        {"name": "Carla",   "role": "normal",             "preferences": []},
        {"name": "Luis",    "role": "normal",             "preferences": ["Noche"]},
    ]


def main():
    cfg = construir_cfg()
    staff = construir_staff()
    n = len(staff)
    cap_max_dia = sum(cfg.max_coverage.values())
    cap_min_dia = sum(cfg.min_coverage.values())
    print(f"Plantilla: {n} personas — max_coverage={cfg.max_coverage} (total {cap_max_dia}), "
          f"min_coverage={cfg.min_coverage} (total {cap_min_dia})")

    viable, msg = validar_viabilidad(staff, cfg)
    print(f"validar_viabilidad (solo totales semanales) → viable={viable} msg={msg!r}\n")

    prev_sunday_shifts = {}
    prev_last_shifts = {}
    prev_week_covers = {}

    N_SEMANAS_ESCENARIO = 8
    # Días sueltos (no en pareja consecutiva) para varios "normal", TODAS las
    # semanas — simula un compromiso recurrente parcial (p.ej. estudios,
    # cuidado familiar unos días fijos) que obliga a assign_days_off a
    # completar cada uno con un SEGUNDO día de pareja cada semana, consumiendo
    # de forma sostenida las opciones de pareja disponibles y acumulando
    # desequilibrio en pair_usage con el paso de las semanas — a diferencia
    # del ajuste de una sola semana (que el heurístico post-reparación
    # absorbe sin problema, ver runs previos documentados en el informe).
    SUELTOS_RECURRENTES = {
        "Ana": "Martes", "Diego": "Miércoles", "Carla": "Jueves",
        "Luis": "Viernes", "Miguel": "Lunes",
    }
    # Además, el jefe falta (petición concedida) un miércoles de por medio,
    # activando el término/cobertura de ausencia (subjefe cubre) y
    # reduciendo también la flexibilidad de Laura semana sí, semana no.
    SEMANAS_AUSENCIA_JEFE = {1, 3, 5, 7}

    semana = SEMANA_INICIAL
    for i in range(N_SEMANAS_ESCENARIO):
        staff_semana = copy.deepcopy(staff)
        for nombre, dia in SUELTOS_RECURRENTES.items():
            p = next(x for x in staff_semana if x["name"] == nombre)
            p["forced_days_off"] = [dia]
        if i in SEMANAS_AUSENCIA_JEFE:
            jefe = next(x for x in staff_semana if x["role"] == "jefe")
            jefe["forced_days_off"] = jefe.get("forced_days_off", []) + ["Miércoles"]

        print(f"\n{'#'*70}\n# SEMANA {i+1} ({semana.isoformat()})\n{'#'*70}")
        full_schedule, staff_final, ws_date, night_cover, this_week_covers, violaciones = \
            generate_schedule_valido(
                staff_list=staff_semana,
                week_start=semana,
                cfg=cfg,
                prev_week_covers=prev_week_covers,
                prev_sunday_shifts=prev_sunday_shifts,
                prev_last_shifts=prev_last_shifts,
                max_intentos=50,
            )

        print(f"  violaciones tras 50 intentos: {violaciones if violaciones else 'NINGUNA (encontró solución válida)'}")
        if violaciones:
            print("\n  Turnos del viernes (día 4):")
            for p in staff_final:
                print(f"    {p['name']:8s} ({p['role']:18s}): {full_schedule[p['name']][4]}")
            sin_asignar = [(p["name"], DAYS[d]) for p in staff_final for d in range(7)
                           if full_schedule[p["name"]][d] == "SIN_ASIGNAR"]
            print(f"  SIN_ASIGNAR: {sin_asignar if sin_asignar else 'ninguna'}")

        prev_sunday_shifts = {nom: t[6] for nom, t in full_schedule.items() if len(t) == 7}

        def _calc_prev_last(sched, staff_list):
            r = {}
            for p in staff_list:
                turnos = sched.get(p["name"], [])
                ult, tras_libre = None, False
                for t in reversed(turnos):
                    if t == "Libre":
                        tras_libre = True
                        continue
                    if t and t not in ("SIN_ASIGNAR", ""):
                        ult = t
                        break
                if ult is not None:
                    r[p["name"]] = (ult, tras_libre)
            return r

        prev_last_shifts = _calc_prev_last(full_schedule, staff_final)
        prev_week_covers = this_week_covers
        semana += timedelta(weeks=1)


if __name__ == "__main__":
    main()
