"""
generar_referencia_motor_actual.py — Genera la "foto de referencia" del motor
heurístico ACTUAL (modelo_base.py / app.py, commit 6186a38 + 2 diffs inertes
sin comprometer — ver informe de Fase 5) para poder comparar contra
motor_cpsat.py.

Reemplaza a la foto original de Fase 0, que nunca se persistió a disco y se
perdió junto con el contexto de la conversación donde se generó (ver informe
de Fase 5, sección "Paso 0 bis"). A diferencia de aquella, esta SÍ se guarda
versionada en el repo (referencia_motor_actual.json) y se comenta el commit
que la generó.

Réplica fiel del flujo real de app.py (api_generar_horario /
_get_dept_config), en un DATA_DIR aislado en /tmp, para:
  - RECEPCION_CONFIG / RECEPCION STAFF_DEFAULT (8 personas)
  - MOZOS_CONFIG / MOZOS STAFF_DEFAULT (5 personas)

18 semanas encadenadas cada una, con generate_schedule_valido (motor real,
con reintentos — igual que producción). Arranque desde estado limpio: sin
historial previo (data-ejemplo/historial.json y mozos_historial.json están
vacíos, "{}" — no hay ninguna semana previa a esta migración que se pueda
usar como estado inicial real), sin vacaciones ni peticiones concedidas.
Esto es una decisión que afecta directamente a las métricas de equidad de
las primeras semanas (todos parten del mismo ratio medio, ver
_get_weekend_ratios) — se documenta aquí en vez de dejarla implícita.
"""
import copy
import json
import os
import shutil
import subprocess
from datetime import date, timedelta

SCRATCH_DATA = "/tmp/claude-1000/-home-marrtincho-Documentos-Hotel-HorariosRecepcion-cpsat/7b9da1cb-b95b-4bf4-9038-ba5d02d45add/scratchpad/referencia_data"
shutil.rmtree(SCRATCH_DATA, ignore_errors=True)
os.makedirs(SCRATCH_DATA, exist_ok=True)
os.environ["DATA_DIR"] = SCRATCH_DATA

from modelo_base import DAYS, generate_schedule_valido
from modelo_recepcion import RECEPCION_CONFIG, STAFF_DEFAULT as RECEPCION_STAFF
from modelo_mozos import MOZOS_CONFIG, STAFF_DEFAULT as MOZOS_STAFF
from herramientas import cargar_roles, registrar_semana, _semana_str

N_SEMANAS = 18
SEMANA_INICIAL = date(2026, 8, 3)  # lunes


def construir_cfg(base_cfg, dept_id):
    """Misma lógica que app.py::_get_dept_config, capas de roles/cobertura
    (capa de coverage por temporada se omite: se usa el min/max_coverage
    estático de modelo_recepcion.py/modelo_mozos.py, no hay perfiles de
    temporada configurados en este DATA_DIR aislado)."""
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


def _calcular_prev_last_shifts(schedule_semana_anterior, staff_list):
    resultado = {}
    for p in staff_list:
        turnos = schedule_semana_anterior.get(p["name"], [])
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


def generar_departamento(dept_id, base_cfg, staff_default):
    cfg = construir_cfg(base_cfg, dept_id)
    staff = copy.deepcopy(staff_default)

    cov_path = os.path.join(SCRATCH_DATA, f"night_covers_{dept_id}.json")
    semana = SEMANA_INICIAL
    prev_week_covers = {}
    prev_sunday_shifts = {}
    prev_last_shifts = {}

    semanas_out = []
    for i in range(N_SEMANAS):
        full_schedule, staff_final, ws_date, night_cover, this_week_covers, violaciones = \
            generate_schedule_valido(
                staff_list=copy.deepcopy(staff),
                week_start=semana,
                cfg=cfg,
                prev_week_covers=prev_week_covers,
                prev_sunday_shifts=prev_sunday_shifts,
                prev_last_shifts=prev_last_shifts,
            )

        all_covers = json.loads(open(cov_path, encoding="utf-8").read()) if os.path.exists(cov_path) else {}
        all_covers[_semana_str(semana)] = this_week_covers
        with open(cov_path, "w", encoding="utf-8") as f:
            json.dump(all_covers, f, ensure_ascii=False, indent=2)

        # Igual que app.py tras generate_schedule_valido: persiste la semana
        # en el historial real, para que _get_weekend_ratios/_get_shift_counts
        # (leídas por la semana SIGUIENTE, tanto en el heurístico como más
        # tarde en motor_cpsat) vean la evolución acumulada, no un historial
        # vacío en cada semana. Sin esto, term A (equidad) nunca tendría
        # presión real a lo largo de la cadena — se detectó al intentar
        # reproducir la dispersión de findes y no encontrar ninguna.
        registrar_semana(semana, full_schedule, staff_final, forzar=True, dept_id=dept_id)

        semanas_out.append({
            "semana": semana.isoformat(),
            "schedule": {p["name"]: full_schedule[p["name"]] for p in staff_final},
            "roles": {p["name"]: p.get("role") for p in staff_final},
            "violaciones": violaciones,
        })

        prev_sunday_shifts = {nom: t[6] for nom, t in full_schedule.items() if len(t) == 7}
        prev_last_shifts = _calcular_prev_last_shifts(full_schedule, staff_final)
        prev_week_covers = this_week_covers
        semana += timedelta(weeks=1)

    return semanas_out


def _git_commit_actual():
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=os.path.dirname(os.path.abspath(__file__))
        ).decode().strip()
    except Exception:
        return None


def main():
    salida = {
        "_meta": {
            "generado": date.today().isoformat(),
            "generador": "generar_referencia_motor_actual.py",
            "commit_modelo_base": _git_commit_actual(),
            "nota": (
                "Reemplaza la foto de Fase 0 original, nunca persistida y "
                "perdida. Arranque desde estado limpio (sin historial previo, "
                "sin vacaciones/peticiones) — ver docstring del script."
            ),
        },
        "recepcion": generar_departamento("recepcion", RECEPCION_CONFIG, RECEPCION_STAFF),
        "mozos": generar_departamento("mozos", MOZOS_CONFIG, MOZOS_STAFF),
    }

    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "referencia_motor_actual.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(salida, f, ensure_ascii=False, indent=2)
    print(f"Escrito {out_path}")
    print(f"recepcion: {len(salida['recepcion'])} semanas, mozos: {len(salida['mozos'])} semanas")
    for dept in ("recepcion", "mozos"):
        con_violaciones = [s for s in salida[dept] if s["violaciones"]]
        print(f"  {dept}: semanas con violaciones tras 50 reintentos = {len(con_violaciones)}")


if __name__ == "__main__":
    main()
