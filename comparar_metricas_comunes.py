"""
comparar_metricas_comunes.py — Paso 1/2 del informe de Fase 5: métricas
calculadas de forma IDÉNTICA sobre referencia_motor_actual.json (heurístico)
y comparativa_cpsat_{dept}.json (CP-SAT), para que la comparación sea
justa. _contar_penalizaciones (modelo_base.py) NO sirve para esto: penaliza
además "repetir turno al volver de librar", una noción propia del
heurístico que el término B de motor_cpsat.py no modela — se implementa
aquí una métrica de "roturas de continuidad" que replica EXACTAMENTE lo que
el término B penaliza (cambiar de turno entre dos días consecutivos ambos
trabajados en Mañana/Tarde/Partido, sin librar en medio; Noche no participa),
aplicable por igual a los schedules de ambos motores.
"""
import json
import os

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
DAY_SHIFTS = ["Mañana", "Tarde", "Partido"]


def roturas_continuidad(schedule: dict, dias_totales: int = 7, arrastre: dict = None) -> int:
    """arrastre: {nombre: turno_dia_anterior_al_primero} para encadenar entre semanas."""
    arrastre = arrastre or {}
    total = 0
    for nombre, turnos in schedule.items():
        prev = arrastre.get(nombre)
        for t in turnos[:dias_totales]:
            if t in ("", "SIN_ASIGNAR", "Noche", "Libre"):
                prev = None if t == "Libre" else prev
                continue
            if prev is not None and prev in DAY_SHIFTS and t in DAY_SHIFTS and t != prev:
                total += 1
            prev = t
    return total


def contar_partido(schedule: dict) -> int:
    return sum(1 for turnos in schedule.values() for t in turnos if t == "Partido")


def analizar_secuencia(semanas: list) -> dict:
    total_roturas = 0
    total_partido = 0
    total_violaciones = 0
    arrastre = {}
    for s in semanas:
        schedule = s["schedule"]
        total_roturas += roturas_continuidad(schedule, arrastre=arrastre)
        total_partido += contar_partido(schedule)
        total_violaciones += len(s["violaciones"])
        arrastre = {nombre: turnos[6] for nombre, turnos in schedule.items() if len(turnos) == 7}
    return {
        "violaciones_duras": total_violaciones,
        "roturas_continuidad_termino_B": total_roturas,
        "uso_partido": total_partido,
    }


def main():
    with open(os.path.join(REPO_DIR, "referencia_motor_actual.json"), encoding="utf-8") as f:
        referencia = json.load(f)

    resultado = {}
    for dept_id in ("recepcion", "mozos"):
        heuristico = analizar_secuencia(referencia[dept_id])
        with open(os.path.join(REPO_DIR, f"comparativa_cpsat_{dept_id}.json"), encoding="utf-8") as f:
            cpsat_semanas = json.load(f)
        cpsat = analizar_secuencia(cpsat_semanas)
        resultado[dept_id] = {"heuristico": heuristico, "cpsat": cpsat}
        print(f"\n=== {dept_id} ===")
        print(f"  Heurístico: {heuristico}")
        print(f"  CP-SAT:     {cpsat}")

    with open(os.path.join(REPO_DIR, "comparativa_metricas_comunes.json"), "w", encoding="utf-8") as f:
        json.dump(resultado, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
