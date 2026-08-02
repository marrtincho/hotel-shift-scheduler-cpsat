"""
diagnostico_regresiones.py — Script de DIAGNÓSTICO (no es un test).

Genera 10 semanas encadenadas de Recepción con una plantilla de 10 personas
(1 jefe, 1 subjefe, 2 adjunto_recepcion, 6 normales — SIN night_fixed, para
forzar la rotación de noches por bloques) y vuelca por semana la información
pedida para diagnosticar los 3 bugs reportados. No corrige nada.

Usa un DATA_DIR aislado en /tmp para no tocar los datos reales del proyecto.
"""
import os
import shutil
import copy
import json
from datetime import date, timedelta

# ── DATA_DIR aislado ANTES de importar herramientas/modelo_base ─────────────
SCRATCH_DATA = "/tmp/claude-1000/-home-marrtincho-Documentos-Hotel-HorariosRecepcion/867a15a4-290c-4b4c-9631-c9e029e17ce9/scratchpad/diag_data"
shutil.rmtree(SCRATCH_DATA, ignore_errors=True)
os.makedirs(SCRATCH_DATA, exist_ok=True)
os.environ["DATA_DIR"] = SCRATCH_DATA

from modelo_base import (
    DAYS, generate_schedule_valido, validar_viabilidad, MAX_NOCHES_BLOQUE,
)
from modelo_recepcion import RECEPCION_CONFIG as BASE_CFG
from herramientas import (
    cargar_roles, cargar_historial, registrar_semana, _semana_str, DATA_DIR,
)

# ── Construcción de cfg como lo hace app.py::_get_dept_config ───────────────
def construir_cfg():
    cfg = copy.deepcopy(BASE_CFG)
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
    # min/max de coverage: se dejan los del modelo_recepcion.py (Mañana2-3,
    # Tarde2-3, Noche1-1, Partido0-1) — capacidad_max_dia = 8.
    return cfg


# ── Plantilla de 10 personas pedida por el usuario ───────────────────────────
def construir_staff():
    staff = [
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
    return staff


def turno_de(schedule, nombre, day_idx):
    t = schedule.get(nombre)
    return t[day_idx] if t else None


def main():
    cfg   = construir_cfg()
    staff = construir_staff()

    n = len(staff)
    cap_max_dia = sum(cfg.max_coverage.values())
    print("=" * 78)
    print(f"Plantilla: {n} personas — {[p['role'] for p in staff]}")
    print(f"max_coverage = {cfg.max_coverage}  → capacidad_max_dia = {cap_max_dia}")
    print(f"Mínimo obligatorio de personas libres/día si se respeta la capacidad "
          f"máxima: {n} - {cap_max_dia} = {n - cap_max_dia}")
    print("=" * 78)

    viable, msg = validar_viabilidad(staff, cfg)
    print(f"\nvalidar_viabilidad → viable={viable}  msg={msg!r}")
    print("(Esta función NO mira distribución diaria, solo totales semanales — "
          "ver hipótesis BUG 1)\n")

    bloques_path = os.path.join(DATA_DIR, "night_rotation_recepcion_bloques.json")

    semana = date(2026, 8, 3)  # un lunes
    assert semana.weekday() == 0

    prev_sunday_shifts = {}
    prev_last_shifts   = {}
    prev_week_covers   = {}
    noches_por_persona = {p["name"]: [] for p in staff}  # secuencia de "N"/"." por semana/día

    # Para BUG 2: forzamos que el jefe tenga una ausencia (petición concedida)
    # un día de alguna semana intermedia, para ver por qué vía cubre el subjefe.
    semana_ausencia_jefe = 4  # (0-indexado) quinta semana de las 10

    for i in range(10):
        staff_semana = copy.deepcopy(staff)
        if i == semana_ausencia_jefe:
            jefe = next(p for p in staff_semana if p["role"] == "jefe")
            jefe["forced_days_off"] = ["Miércoles"]

        print("\n" + "#" * 78)
        print(f"# SEMANA {i+1}  ({_semana_str(semana)})")
        print("#" * 78)

        # --- d) estado de bloques ANTES de generar ---
        estado_antes = json.loads(open(bloques_path, encoding="utf-8").read()) \
            if os.path.exists(bloques_path) else {}
        print(f"\n[d] night_rotation_recepcion_bloques.json ANTES: {estado_antes}")

        full_schedule, staff_final, ws_date, night_cover, this_week_covers, violaciones = \
            generate_schedule_valido(
                staff_list=staff_semana,
                week_start=semana,
                cfg=cfg,
                prev_week_covers=prev_week_covers,
                prev_sunday_shifts=prev_sunday_shifts,
                prev_last_shifts=prev_last_shifts,
            )

        estado_despues = json.loads(open(bloques_path, encoding="utf-8").read()) \
            if os.path.exists(bloques_path) else {}
        print(f"[d] night_rotation_recepcion_bloques.json DESPUÉS: {estado_despues}")

        if violaciones:
            print(f"\n!! generate_schedule_valido NO encontró candidato sin violaciones. "
                  f"Violaciones del mejor intento: {violaciones}")

        # --- a) libran/trabajan por día ---
        print("\n[a] Libres/Trabajan por día:")
        for day_idx, day in enumerate(DAYS):
            libres = [p["name"] for p in staff_final
                      if full_schedule[p["name"]][day_idx] == "Libre"]
            trabajan = n - len(libres)
            print(f"    {day:10s}: libran={len(libres):2d} {libres!s:45s} "
                  f"trabajan={trabajan}")

        # --- b) capacidad máxima diaria ---
        print(f"\n[b] Capacidad máxima diaria (según cfg.max_coverage): {cap_max_dia}")

        # --- c) SIN_ASIGNAR ---
        sin_asignar = [
            (p["name"], DAYS[d])
            for p in staff_final
            for d in range(7)
            if full_schedule[p["name"]][d] == "SIN_ASIGNAR"
        ]
        print(f"\n[c] Celdas SIN_ASIGNAR: {sin_asignar if sin_asignar else 'ninguna'}")

        # --- e) chequeo cruzado: días con SIN_ASIGNAR vs. libran < (n - cap) ---
        for day_idx, day in enumerate(DAYS):
            libres_dia = sum(1 for p in staff_final
                              if full_schedule[p["name"]][day_idx] == "Libre")
            hay_sin_asignar_dia = any(d == day for (_, d) in sin_asignar)
            if hay_sin_asignar_dia:
                print(f"    -> {day}: libran={libres_dia} "
                      f"(mínimo necesario={n - cap_max_dia}) "
                      f"{'[CUMPLE hipótesis: libran menos del mínimo]' if libres_dia < (n - cap_max_dia) else '[NO explica el hueco por sí solo]'}")

        # --- d) turno jefe/subjefe por día, marcando coincidencias ---
        print("\n[jefe/subjefe] Turnos por día:")
        for day_idx, day in enumerate(DAYS):
            t_jefe = turno_de(full_schedule, "Juan", day_idx)
            t_sub  = turno_de(full_schedule, "Laura", day_idx)
            coincide = (t_jefe == t_sub and t_jefe not in (None, "Libre"))
            marca = "  <-- COINCIDEN" if coincide else ""
            print(f"    {day:10s}: jefe={t_jefe!s:12s} subjefe={t_sub!s:12s}{marca}")
            if coincide:
                laura = next(p for p in staff_final if p["name"] == "Laura")
                dias_cob = laura.get("dias_cobertura_jefatura") or []
                cob_semana_completa = bool(laura.get("cobertura_total_semana"))
                if day_idx in dias_cob or cob_semana_completa:
                    via = "COBERTURA DE AUSENCIA (subjefe cubre al jefe ausente/fin de semana)"
                else:
                    via = "ROTACIÓN NORMAL (coincidencia incidental, no la causa la cobertura)"
                print(f"        vía: {via}")
                print(f"        detalle: cubre_rol={laura.get('cubre_rol')} "
                      f"dias_cobertura_jefatura={dias_cob} "
                      f"cobertura_total_semana={cob_semana_completa} "
                      f"night_cover_this_week={laura.get('night_cover_this_week')}")

        # --- d) secuencia de noches consecutivas por persona ---
        print("\n[noches] Turnos 'Noche' de la semana por persona:")
        for p in staff_final:
            noches_semana = [DAYS[d][:3] for d in range(7)
                              if full_schedule[p["name"]][d] == "Noche"]
            marca = [d for d in range(7) if full_schedule[p["name"]][d] == "Noche"]
            noches_por_persona[p["name"]].append(marca)
            if noches_semana:
                print(f"    {p['name']:8s}: {noches_semana}")

        # Persistir para encadenar la siguiente semana (igual que app.py)
        registrar_semana(semana, full_schedule, staff_final, forzar=True, dept_id="recepcion")

        prev_sunday_shifts = {nom: t[6] for nom, t in full_schedule.items() if len(t) == 7}

        def _calcular_prev_last(schedule_sem, staff_list):
            resultado = {}
            for p in staff_list:
                turnos = schedule_sem.get(p["name"], [])
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

        prev_last_shifts = _calcular_prev_last(full_schedule, staff_final)
        prev_week_covers = this_week_covers
        semana += timedelta(weeks=1)

    # ── Resumen final: secuencias de noches consecutivas por persona a lo largo
    # de las 10 semanas, para detectar bloques > MAX_NOCHES_BLOQUE consecutivos.
    print("\n" + "=" * 78)
    print(f"RESUMEN — secuencia de noches consecutivas por persona (MAX_NOCHES_BLOQUE={MAX_NOCHES_BLOQUE})")
    print("=" * 78)
    for nombre, semanas_dias in noches_por_persona.items():
        # aplanar a una secuencia continua de 70 días (10 semanas x 7), marcando 1/0
        flat = []
        for dias in semanas_dias:
            for d in range(7):
                flat.append(1 if d in dias else 0)
        # detectar rachas
        rachas = []
        actual = 0
        for v in flat:
            if v:
                actual += 1
            else:
                if actual:
                    rachas.append(actual)
                actual = 0
        if actual:
            rachas.append(actual)
        if rachas:
            excede = [r for r in rachas if r > MAX_NOCHES_BLOQUE]
            print(f"  {nombre:8s}: rachas={rachas} "
                  f"{'  <-- EXCEDE MAX_NOCHES_BLOQUE' if excede else ''}")


if __name__ == "__main__":
    main()
