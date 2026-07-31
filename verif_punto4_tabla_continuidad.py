"""
verif_punto4_tabla_continuidad.py — Fase 5, verificación puntual 4 (revisión
pedida por el usuario): tabla semana a semana de roturas de continuidad
(término B) para Recepción, heurístico vs CP-SAT, sobre las mismas 18
semanas de referencia_motor_actual.json / comparativa_cpsat_recepcion.json.
"""
import json
import os

from comparar_metricas_comunes import roturas_continuidad, contar_partido

REPO_DIR = os.path.dirname(os.path.abspath(__file__))

with open(os.path.join(REPO_DIR, "referencia_motor_actual.json"), encoding="utf-8") as f:
    heuristico = json.load(f)["recepcion"]
with open(os.path.join(REPO_DIR, "comparativa_cpsat_recepcion.json"), encoding="utf-8") as f:
    cpsat = json.load(f)

print(f"{'Semana':8s} {'B heur':>7s} {'B cpsat':>8s} {'Partido heur':>13s} {'Partido cpsat':>14s}")
arrastre_h, arrastre_c = {}, {}
tot_bh = tot_bc = tot_ph = tot_pc = 0
for i, (sh, sc) in enumerate(zip(heuristico, cpsat)):
    bh = roturas_continuidad(sh["schedule"], arrastre=arrastre_h)
    bc = roturas_continuidad(sc["schedule"], arrastre=arrastre_c)
    ph = contar_partido(sh["schedule"])
    pc = contar_partido(sc["schedule"])
    tot_bh += bh; tot_bc += bc; tot_ph += ph; tot_pc += pc
    print(f"{i+1:<8d} {bh:>7d} {bc:>8d} {ph:>13d} {pc:>14d}")
    arrastre_h = {n: t[6] for n, t in sh["schedule"].items() if len(t) == 7}
    arrastre_c = {n: t[6] for n, t in sc["schedule"].items() if len(t) == 7}

print(f"{'TOTAL':8s} {tot_bh:>7d} {tot_bc:>8d} {tot_ph:>13d} {tot_pc:>14d}")
