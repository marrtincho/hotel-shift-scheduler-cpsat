"""
analizar_referencia_heuristico.py — calcula, sobre referencia_motor_actual.json
(motor heurístico), las mismas métricas que generar_comparativa_cpsat.py
calcula para motor_cpsat.py: penalización de continuidad, uso de Partido, y
dispersión de ratio de findes libres (leída del historial real que
generar_referencia_motor_actual.py ya persistió en su DATA_DIR aislado).
"""
import json
import os
from datetime import date

SCRATCH_DATA = "/tmp/claude-1000/-home-marrtincho-Documentos-Hotel-HorariosRecepcion-cpsat/7b9da1cb-b95b-4bf4-9038-ba5d02d45add/scratchpad/referencia_data"
os.environ["DATA_DIR"] = SCRATCH_DATA

from modelo_base import _contar_penalizaciones, _get_weekend_ratios
from modelo_recepcion import STAFF_DEFAULT as RECEPCION_STAFF
from modelo_mozos import STAFF_DEFAULT as MOZOS_STAFF

REPO_DIR = os.path.dirname(os.path.abspath(__file__))


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


def analizar(dept_id, staff_default, semanas):
    total_pen = 0
    total_partido = 0
    total_violaciones = 0
    prev_last_shifts = {}
    for s in semanas:
        schedule = s["schedule"]
        staff_list = [{"name": n, "role": r} for n, r in s["roles"].items()]
        pen = _contar_penalizaciones(schedule, staff_list, prev_last_shifts)
        total_pen += pen
        total_violaciones += len(s["violaciones"])
        for turnos in schedule.values():
            total_partido += sum(1 for t in turnos if t == "Partido")
        prev_last_shifts = _calcular_prev_last(schedule, staff_list)

    ratios = _get_weekend_ratios(dept_id, staff_default)
    valores = [d["ratio"] for d in ratios.values()]
    dispersion = None
    if valores:
        dispersion = {"min": min(valores), "max": max(valores), "dispersion": max(valores) - min(valores),
                      "detalle": {n: round(d["ratio"], 3) for n, d in ratios.items()}}

    return {
        "violaciones_duras": total_violaciones,
        "penalizacion_continuidad": total_pen,
        "uso_partido": total_partido,
        "dispersion_findes": dispersion,
    }


def main():
    with open(os.path.join(REPO_DIR, "referencia_motor_actual.json"), encoding="utf-8") as f:
        referencia = json.load(f)

    resumen = {}
    for dept_id, staff_default in (("recepcion", RECEPCION_STAFF), ("mozos", MOZOS_STAFF)):
        resumen[dept_id] = analizar(dept_id, staff_default, referencia[dept_id])
        print(f"{dept_id}: {json.dumps(resumen[dept_id], ensure_ascii=False, indent=2)}")

    with open(os.path.join(REPO_DIR, "resumen_heuristico.json"), "w", encoding="utf-8") as f:
        json.dump(resumen, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
