"""
modelo_base.py — Motor genérico de generación de horarios parametrizable.
Recibe un DeptConfig con todas las reglas del departamento.
modelo.py y modelo_mozos.py importan de aquí.
"""

import random
import copy
from datetime import date, timedelta
from dataclasses import dataclass, field
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

DAYS = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
SHIFTS = ["Mañana", "Tarde", "Noche", "Partido", "Libre"]
NO_MORNING_AFTER = {"Noche", "Tarde"}

@dataclass
class DeptConfig:
    """Todas las reglas de un departamento."""
    dept_id: str                     # "recepcion" | "mozos"
    dept_label: str                  # "Recepción" | "Mozos"
    night_fixed_days_off: list       # índices de días libres del fijo de noche
    night_cover_compensation: list   # días libres que recibe el sustituto
    min_coverage: dict               # mínimo por turno por día
    max_coverage: dict               # máximo por turno por día
    no_night_roles: set              # roles que nunca hacen noche
    fixed_roles: dict                # role → turno fijo (ej: "jefe" → "Mañana")
    fixed_days_off: dict             # role → [idx_dias] fijos (ej: "jefe" → [5,6])
    cover_consecutive: bool          # ¿el sustituto necesita libres consecutivos?
    night_rotation_key: str          # clave en night_rotation.json
    allow_non_consecutive_cover: bool # mozos: el sustituto puede tener libres no consec
    prefer_next_week_for_cover: bool  # mozos: preferir Lun+Mar semana siguiente


# ─── UTILIDADES ─────────────────────────────────────────────────────────────

def get_consecutive_day_pairs():
    return [(i, i+1) for i in range(len(DAYS)-1)]

def _turno_anterior(person_name, day_idx, schedule, prev_sunday_shifts=None):
    """Turno del día anterior. Para el lunes (day_idx=0), mira el domingo
    de la semana pasada (prev_sunday_shifts), no reinicia la regla."""
    if day_idx == 0:
        return (prev_sunday_shifts or {}).get(person_name)
    return schedule[person_name][day_idx-1]

def can_work_morning(person_name, day_idx, schedule, prev_sunday_shifts=None):
    return _turno_anterior(person_name, day_idx, schedule, prev_sunday_shifts) not in NO_MORNING_AFTER

def _turno_referencia(name, day_idx, schedule, prev_last_shifts=None):
    """Devuelve (ultimo_turno_trabajado, hubo_libre_en_medio).

    hubo_libre_en_medio=False → tirada abierta: mantener turno.
    hubo_libre_en_medio=True  → acaba de librar: rotar de turno.
    Cruza semana anterior via prev_last_shifts: dict {nombre: (turno, hubo_libre)}
    o {nombre: str} para compatibilidad con prev_sunday_shifts existente.
    """
    hubo_libre = False
    for i in range(day_idx - 1, -1, -1):
        t = schedule[name][i]
        if t == "Libre":
            hubo_libre = True
            continue
        if t and t != "SIN_ASIGNAR":
            return t, hubo_libre
    prev = (prev_last_shifts or {}).get(name)
    if isinstance(prev, (tuple, list)):
        return prev[0], hubo_libre or prev[1]
    return prev, hubo_libre

def _get_weekend_ratios(dept_id: str, staff_actual: list) -> dict:
    """
    Calcula el ratio finde-libre/semanas-trabajadas para cada persona del staff actual.
    Devuelve {"nombre": {"finds": n, "semanas": m, "ratio": r}}.
    Ventana máxima de 52 semanas. Excluye vacaciones completas y semanas en que
    el finde libre fue compensación por cobertura nocturna.
    Personas sin historial reciben el ratio medio del equipo.
    """
    try:
        from herramientas import cargar_historial
    except Exception:
        return {}

    historial = cargar_historial(dept_id)
    cutoff    = (date.today() - timedelta(weeks=52)).strftime("%Y-%m-%d")

    nombres   = {p["name"] for p in staff_actual}
    resultado = {n: {"finds": 0, "semanas": 0} for n in nombres}

    for semana_str in sorted(historial):
        if semana_str < cutoff:
            continue
        entrada      = historial[semana_str]
        schedule_sem = entrada.get("schedule", {})
        staff_semana = {p["name"] for p in entrada.get("staff", [])}

        # Días que el night_fixed tenía Libre esa semana = días de cobertura nocturna
        nf_en_staff  = next(
            (p for p in entrada.get("staff", []) if p.get("role") == "night_fixed"),
            None,
        )
        dias_cobertura: set = set()
        if nf_en_staff:
            nf_t           = schedule_sem.get(nf_en_staff["name"], [])
            dias_cobertura = {i for i, t in enumerate(nf_t) if t == "Libre"}

        for nombre in nombres:
            if nombre not in staff_semana:
                continue
            turnos = schedule_sem.get(nombre, [])
            if len(turnos) < 7:
                continue
            # Semana de vacaciones completa: no computa
            if all(t == "Libre" for t in turnos):
                continue
            resultado[nombre]["semanas"] += 1
            # Finde libre: verificar que no fue compensación nocturna
            if turnos[5] == "Libre" and turnos[6] == "Libre":
                fue_comp = bool(
                    dias_cobertura
                    and any(turnos[d] == "Noche" for d in dias_cobertura)
                )
                if not fue_comp:
                    resultado[nombre]["finds"] += 1

    # Ratio individual; media del equipo para quienes no tienen historial
    ratios_validos = [
        d["finds"] / d["semanas"]
        for d in resultado.values()
        if d["semanas"] > 0
    ]
    avg = sum(ratios_validos) / len(ratios_validos) if ratios_validos else 0.0
    for datos in resultado.values():
        datos["ratio"] = datos["finds"] / datos["semanas"] if datos["semanas"] > 0 else avg

    return resultado

def _get_shift_counts(dept_id: str) -> dict:
    """Lee el historial y cuenta cuántas veces cada persona ha hecho cada turno."""
    try:
        from herramientas import cargar_historial
        counts: dict = {}
        for entrada in cargar_historial(dept_id).values():
            for nombre, turnos in entrada.get("schedule", {}).items():
                c = counts.setdefault(nombre, {})
                for t in turnos:
                    c[t] = c.get(t, 0) + 1
        return counts
    except Exception:
        return {}

def _night_covers(history: dict, nombre: str) -> tuple:
    """Lee (covers, pending) del historial de noches. Soporta formato antiguo (int)."""
    v = history.get(nombre, 0)
    if isinstance(v, int):
        return v, 0
    return v.get("covers", 0), v.get("pending", 0)

def _load_night_history(rotation_key: str) -> dict:
    try:
        from herramientas import DATA_DIR
        import json, os
        path = os.path.join(DATA_DIR, f"night_rotation_{rotation_key}.json")
        return json.loads(open(path, encoding="utf-8").read()) if os.path.exists(path) else {}
    except Exception:
        return {}

def _save_night_history(rotation_key: str, history: dict):
    try:
        from herramientas import DATA_DIR, guardar_json_atomico
        import os
        path = os.path.join(DATA_DIR, f"night_rotation_{rotation_key}.json")
        guardar_json_atomico(path, history)
    except Exception:
        pass

# ─── CHEQUEO DE VIABILIDAD PREVIO A LA GENERACIÓN ───────────────────────────

def validar_viabilidad(staff_activo: list, cfg: DeptConfig) -> tuple[bool, str]:
    """Comprueba antes de generar si el equipo tiene capacidad para el horario.

    Cada persona trabaja 5 de 7 días (libra 2), independientemente del rol.
    La fórmula compara los días-turno disponibles con las necesidades semanales:
      - días_trabajo = personas_activas × 5
      - necesidad_semana = sum(min_coverage) × 7
      - capacidad_semana = sum(max_coverage) × 7

    Devuelve (True, '') si es viable, o (False, mensaje_accionable) si no lo es.
    """
    # Excluir personas de vacaciones completas (todos los días de la semana forzados)
    todos_dias = set(DAYS)
    personas_activas = [
        p for p in staff_activo
        if todos_dias - set(p.get("forced_days_off", []) + p.get("extra_days_off", [])) != set()
    ]
    n = len(personas_activas)

    dias_trabajo      = n * 5
    necesidad_min_dia = sum(cfg.min_coverage.get(s, 0) for s in cfg.min_coverage)
    capacidad_max_dia = sum(cfg.max_coverage.get(s, 0) for s in cfg.max_coverage)
    necesidad_semana  = necesidad_min_dia * 7
    capacidad_semana  = capacidad_max_dia * 7

    if dias_trabajo < necesidad_semana:
        # Redondeo hacia arriba: cuántas personas adicionales harían falta
        faltan = -(-(necesidad_semana - dias_trabajo) // 5)
        return False, (
            f"Personal insuficiente: {n} persona(s) activa(s) aportan {dias_trabajo} "
            f"días-turno semanales, pero la cobertura mínima requiere {necesidad_semana}. "
            f"Añade al menos {faltan} persona(s) o reduce los mínimos en la configuración "
            f"de temporada."
        )

    if dias_trabajo > capacidad_semana:
        sobran = -(-(dias_trabajo - capacidad_semana) // 5)
        return False, (
            f"Personal excesivo: {n} persona(s) activa(s) aportan {dias_trabajo} "
            f"días-turno semanales, pero los máximos de cobertura solo admiten {capacidad_semana}. "
            f"Reduce el equipo en {sobran} persona(s) o aumenta los máximos en la configuración "
            f"de temporada."
        )

    return True, ""


# ─── SELECCIÓN DEL SUSTITUTO NOCTURNO ───────────────────────────────────────

def select_night_covers(staff_list: list, week_start: date, cfg: DeptConfig) -> tuple:
    """
    Selects one normal employee to cover the night_fixed's days off.
    - Recepción: covers Thu+Fri, gets Sat+Sun off.
    - Mozos: covers Fri+Sat nights, gets non-consecutive days off
      (preferably Mon+Tue of the NEXT week, handled via next_week_la flag).
    Returns ({day_idx: chosen_name, ...}, updated_history) WITHOUT saving to disk.
    The caller is responsible for persisting the history once the schedule is accepted.
    """
    blocked = set()
    for p in staff_list:
        forced = p.get("forced_days_off", [])
        for day_name in [DAYS[i] for i in cfg.night_fixed_days_off]:
            if day_name in forced:
                blocked.add(p["name"])

    try:
        from herramientas import cargar_roles
        roles_cfg = cargar_roles(cfg.dept_id)
        eligible_roles = {rid for rid, r in roles_cfg.items()
                           if not r.get("sin_noches") and rid != "night_fixed"}
    except Exception:
        eligible_roles = {"normal"}

    eligible = [
        p for p in staff_list
        if p["role"] in eligible_roles and p["name"] not in blocked
    ]
    if not eligible:
        return {}, {}

    history = _load_night_history(cfg.night_rotation_key)

    try:
        from herramientas import cargar_compensaciones
        comp = cargar_compensaciones(cfg.dept_id)
    except Exception:
        comp = {}

    def sort_key(p):
        covers, pending = _night_covers(history, p["name"])
        prefers_cobrar  = 0 if comp.get(p["name"], {}).get("modalidad", "cobrar") == "cobrar" else 1
        return (covers - pending, prefers_cobrar, p["name"])

    # Si el primero en la rotación global está bloqueado por una petición,
    # acumula su deuda para que tenga prioridad en la semana siguiente.
    all_candidates = [p for p in staff_list if p["role"] in eligible_roles]
    sorted_all      = sorted(all_candidates, key=sort_key)
    if sorted_all and sorted_all[0]["name"] in blocked:
        deudor          = sorted_all[0]["name"]
        cov_d, pend_d   = _night_covers(history, deudor)
        history[deudor] = {"covers": cov_d, "pending": pend_d + 1}

    chosen_person = sorted(eligible, key=sort_key)[0]
    chosen_name   = chosen_person["name"]

    # Compensation days off
    if cfg.prefer_next_week_for_cover:
        # Mark with a flag — actual days off assigned in next week's generation
        # For THIS week: give them the least-used consecutive pair that avoids cover days
        chosen_person["night_cover_this_week"] = True
        # They get no forced days off this week from coverage (their normal rotation applies)
        # but we note it so next week gives them Lun+Mar
        chosen_person["covered_nights_prev_week"] = True
        # Excepción mozos: si tras la última noche cubierta (p.ej. Sábado, si
        # cubre Vie+Sáb) todavía queda un día dentro de la MISMA semana antes
        # del domingo inclusive, ese día debe librarse obligatoriamente —
        # nunca puede empezar un turno normal recién saliendo de una noche.
        last_day = max(cfg.night_fixed_days_off) if cfg.night_fixed_days_off else -1
        if 0 <= last_day < 6:
            chosen_person["dia_descanso_tras_cobertura"] = last_day + 1
    else:
        # Recepción style: el sustituto descansa justo después de su última noche
        # de cobertura. Si esos días de compensación caen dentro de la misma
        # semana (p.ej. cubre Jue+Vie, descansa Sáb+Dom) se aplican ya — pero si
        # la última noche cubierta es Sábado o Domingo, "el día siguiente" cae
        # en la semana QUE VIENE (Domingo→Lunes), así que ese descanso no se
        # puede aplicar todavía: se guarda en next_week_rest_days para que el
        # llamador lo traslade a la generación de la semana siguiente.
        last_day = max(cfg.night_fixed_days_off) if cfg.night_fixed_days_off else -1
        same_week_idx = [i for i in cfg.night_cover_compensation if i > last_day]
        next_week_idx = [i for i in cfg.night_cover_compensation if i <= last_day]
        if same_week_idx:
            comp_day_names = [DAYS[i] for i in same_week_idx]
            existentes = chosen_person.get("forced_days_off", [])
            chosen_person["forced_days_off"] = list(dict.fromkeys(existentes + comp_day_names))
        if next_week_idx:
            chosen_person["next_week_rest_days"] = [DAYS[i] for i in next_week_idx]

    covers, pending         = _night_covers(history, chosen_name)
    history[chosen_name]    = {"covers": covers + 1, "pending": 0}

    return {day_idx: chosen_name for day_idx in cfg.night_fixed_days_off}, history


MAX_NOCHES_BLOQUE = 5

def _plan_noches_bloques(staff_list: list, cfg: DeptConfig) -> tuple[dict, dict]:
    """Planifica las noches de la semana cuando night_fixed está ausente,
    en bloques rotatorios de hasta MAX_NOCHES_BLOQUE noches consecutivas.

    El estado persiste entre semanas en night_rotation_{key}_bloques.json:
      {"_actual": {"nombre": ..., "hechas": n}, "<persona>": {"covers": total_noches}}
    de modo que quien cerró la semana con 3 noches continúa la siguiente
    hasta completar su bloque, y entonces descansa 2 días y entra el siguiente.

    Devuelve (asignaciones {day_idx: nombre}, estado_actualizado).
    IMPORTANTE: NO guarda el estado en disco — eso lo hace el llamador solo
    cuando el horario generado se acepta (evita corromper la rotación con los
    reintentos de generate_schedule_valido).
    """
    rotation_key = cfg.night_rotation_key + "_bloques"
    estado = _load_night_history(rotation_key)
    actual = dict(estado.get("_actual", {"nombre": None, "hechas": 0}))

    blocked_roles = cfg.no_night_roles | {"night_fixed"} | set(cfg.fixed_roles)

    def _de_vacaciones(p):
        return set(p.get("forced_days_off", [])) >= set(DAYS)

    def _covers(nombre):
        v = estado.get(nombre, 0)
        return v if isinstance(v, int) else v.get("covers", 0)

    def _dar_descanso(person, primer_dia):
        """Dos días de descanso tras la última noche; los que caigan fuera
        de la semana se trasladan vía next_week_rest_days."""
        dentro = [d for d in (primer_dia, primer_dia + 1) if d <= 6]
        if dentro:
            nombres = [DAYS[d] for d in dentro]
            person["forced_days_off"] = list(dict.fromkeys(
                person.get("forced_days_off", []) + nombres))
        faltan = 2 - len(dentro)
        if faltan > 0:
            person["next_week_rest_days"] = [DAYS[i] for i in range(faltan)]

    asignaciones: dict = {}
    for day_idx in range(7):
        dia      = DAYS[day_idx]
        p_actual = next((p for p in staff_list
                         if p["name"] == actual["nombre"]), None)
        indisponible = (
            p_actual is None
            or _de_vacaciones(p_actual)
            or dia in p_actual.get("forced_days_off", [])
        )

        if actual["hechas"] >= MAX_NOCHES_BLOQUE or indisponible:
            # Cerrar el bloque del saliente: descanso de 2 días desde hoy.
            # (Si está indisponible por vacaciones/petición ese día, el
            # dedup de forced_days_off absorbe cualquier solape.)
            if p_actual is not None and actual["hechas"] > 0 and not _de_vacaciones(p_actual):
                _dar_descanso(p_actual, day_idx)

            saliente   = actual["nombre"]
            candidatos = [
                p for p in staff_list
                if p["role"] not in blocked_roles
                and p["name"] != saliente
                and not _de_vacaciones(p)
                and dia not in p.get("forced_days_off", [])
            ]
            if not candidatos:
                # Último recurso: permitir repetir al saliente si nadie más puede
                candidatos = [p for p in staff_list
                              if p["role"] not in blocked_roles
                              and not _de_vacaciones(p)
                              and dia not in p.get("forced_days_off", [])]
            if not candidatos:
                continue  # noche sin cubrir → la detectará validar_schedule
            # Rotación justa por total de noches hechas; desempate estable por nombre
            nuevo  = sorted(candidatos, key=lambda p: (_covers(p["name"]), p["name"]))[0]
            actual = {"nombre": nuevo["name"], "hechas": 0}

        asignaciones[day_idx] = actual["nombre"]
        actual["hechas"] += 1
        c = _covers(actual["nombre"])
        prev = estado.get(actual["nombre"], {})
        pend = prev.get("pending", 0) if isinstance(prev, dict) else 0
        estado[actual["nombre"]] = {"covers": c + 1, "pending": pend}

    # Si cerró la semana con el bloque completo, el descanso cae la semana
    # que viene (Lun+Mar) y el lunes arrancará bloque nuevo con otra persona.
    if actual["hechas"] >= MAX_NOCHES_BLOQUE:
        p_actual = next((p for p in staff_list
                         if p["name"] == actual["nombre"]), None)
        if p_actual is not None:
            p_actual["next_week_rest_days"] = ["Lunes", "Martes"]

    # Marcar a todos los trabajadores del bloque para que assign_days_off los
    # excluya del cálculo de K y les asigne su par libre evitando sus noches.
    # Sin este flag, la persona que empieza bloque a mitad de semana (p.ej.
    # sábado) permanece en normal_people, inflando K en 1 y dejando el finde
    # con un trabajador menos del necesario en Mañana/Tarde.
    for p in staff_list:
        if p["name"] in set(asignaciones.values()):
            p["night_cover_this_week"] = True

    estado["_actual"] = actual
    return asignaciones, estado

# ─── ASIGNACIÓN DE LIBRES ────────────────────────────────────────────────────

def assign_days_off(staff_list: list, night_cover: dict, cfg: DeptConfig) -> dict:
    """Asignación de libres respetando la config del departamento y balanceando
    los fines de semana libres a largo plazo mediante ratio proporcional."""
    if night_cover is None:
        night_cover = {}

    cover_by_person = {}
    for day_idx, name in night_cover.items():
        cover_by_person.setdefault(name, []).append(day_idx)

    pairs      = get_consecutive_day_pairs()
    schedule   = {p["name"]: ["" for _ in DAYS] for p in staff_list}
    pair_usage = {pair: 0 for pair in pairs}

    # ── Roles con días fijos ────────────────────────────────────────────────
    for role, days in cfg.fixed_days_off.items():
        person = next((p for p in staff_list if p["role"] == role), None)
        if person:
            for d in days:
                schedule[person["name"]][d] = "Libre"
            person["days_off"] = list(days)
            pair = tuple(sorted(days))
            if pair in pair_usage:
                pair_usage[pair] += 1

    # Night fixed: días fijos libres
    nf = next((p for p in staff_list if p["role"] == "night_fixed"), None)
    if nf:
        for d in cfg.night_fixed_days_off:
            schedule[nf["name"]][d] = "Libre"
        nf["days_off"] = list(cfg.night_fixed_days_off)
        # Solo contar el par si el night_fixed trabaja esta semana.
        # De vacaciones completas no ocupa slot de rotación y sesgaría Pasada B.
        nf_vacation = set(nf.get("forced_days_off", [])) >= set(DAYS)
        if not nf_vacation:
            pair = tuple(cfg.night_fixed_days_off)
            if pair in pair_usage:
                pair_usage[pair] += 1

    # Vacaciones y peticiones de libre: se respetan siempre y por completo,
    # sin importar el rol (incluso para turnos fijos como jefe o night_fixed).
    for person in staff_list:
        forced_indices = [DAYS.index(d) for d in person.get("forced_days_off", []) if d in DAYS]
        extra_indices  = [DAYS.index(d) for d in person.get("extra_days_off", [])  if d in DAYS]
        for idx in forced_indices + extra_indices:
            schedule[person["name"]][idx] = "Libre"
        if forced_indices or extra_indices:
            person["days_off"] = sorted(set(person.get("days_off", [])) | set(forced_indices) | set(extra_indices))

    # ── Personas sin turno fijo ─────────────────────────────────────────────
    fixed_roles = set(cfg.fixed_days_off.keys()) | {"night_fixed"}
    no_fixed    = [p for p in staff_list if p["role"] not in fixed_roles]

    def _is_normal_rotation(p: dict) -> bool:
        """Persona que caerá en la rotación normal (sin forzados ni cobertura)."""
        fi = [DAYS.index(d) for d in p.get("forced_days_off", []) if d in DAYS]
        return (
            len(fi) == 0
            and p.get("dia_descanso_tras_cobertura") is None
            and not p.get("night_cover_this_week")
        )

    normal_people = [p for p in no_fixed if _is_normal_rotation(p)]

    # K = cuántas personas pueden librarse el finde sin romper cobertura mínima.
    # Normalmente, Noche la cubre el night_fixed y no cuenta en min_workers.
    # Si night_fixed no existe o libra toda la semana (vacaciones), la Noche entra
    # en la rotación normal y hay que reservar ese slot para no dejar el finde corto.
    nf_person = next((p for p in staff_list if p["role"] == "night_fixed"), None)
    night_fixed_ausente = (
        nf_person is None
        or all(schedule[nf_person["name"]][d] == "Libre" for d in range(7))
    )
    # Si night_cover cubre los 7 días, las noches ya están planificadas para toda
    # la semana → no hace falta reservar un slot extra en el cálculo de K.
    noche_cubierta = len(night_cover) >= 7
    extra_night = (
        cfg.min_coverage.get("Noche", 0)
        if (night_fixed_ausente and not noche_cubierta)
        else 0
    )
    min_workers = sum(cfg.min_coverage.get(s, 0) for s in ["Mañana", "Tarde", "Partido"]) + extra_night
    K           = max(0, len(normal_people) - min_workers)

    # Calcular ratios de equidad proporcional para los no-fijos
    ratios    = _get_weekend_ratios(cfg.dept_id, no_fixed)
    avg_ratio = (
        sum(d["ratio"] for d in ratios.values()) / len(ratios)
        if ratios else 0.0
    )

    # Shuffle previo → desempate aleatorio al ordenar por ratio
    random.shuffle(normal_people)
    normal_people.sort(
        key=lambda p: ratios.get(p["name"], {}).get("ratio", avg_ratio)
    )

    # Asignar K slots de finde (ratio menor → prioridad mayor)
    weekend_assigned: set = set()
    for p in normal_people:
        if len(weekend_assigned) >= K:
            break
        mw = set(cover_by_person.get(p["name"], []))
        if not (mw & {5, 6}):
            weekend_assigned.add(p["name"])

    # ── Bucle principal: forzados, compensaciones y cobertura nocturna ────────
    for person in staff_list:
        if person["role"] in fixed_roles:
            continue
        if _is_normal_rotation(person):
            continue  # se procesan en las dos pasadas siguientes

        forced_indices = [DAYS.index(d) for d in person.get("forced_days_off", []) if d in DAYS]
        must_work      = set(cover_by_person.get(person["name"], []))

        if len(forced_indices) >= 2:
            # Ya marcados Libre arriba; registrar par para pair_usage
            pair = tuple(sorted(forced_indices[:2]))
            if pair in pair_usage:
                pair_usage[pair] += 1

        elif person.get("night_cover_this_week"):
            # Trabajador de bloque nocturno (incluye mozos cover sin forced).
            # Los días en forced_days_off ya están marcados Libre arriba.
            # Si hay un par consecutivo que evita must_work, se usa; si no,
            # se asignan hasta 2 días libres sueltos de los días disponibles.
            ya_libre   = [d for d in range(7) if schedule[person["name"]][d] == "Libre"]
            sin_noche  = [d for d in range(7) if d not in must_work
                          and schedule[person["name"]][d] != "Libre"]
            valid_pairs = [p for p in pairs
                           if not (must_work & set(p))
                           and all(schedule[person["name"]][d] != "Libre" for d in p)]
            if valid_pairs:
                chosen = min(valid_pairs, key=lambda p: pair_usage[p])
                pair_usage[chosen] += 1
                schedule[person["name"]][chosen[0]] = "Libre"
                schedule[person["name"]][chosen[1]] = "Libre"
                person["days_off"] = list(chosen)
            else:
                # Sin par consecutivo libre: asignar días sueltos hasta 2 en total.
                extra = sin_noche[:max(0, 2 - len(ya_libre))]
                for d in extra:
                    schedule[person["name"]][d] = "Libre"
                total = sorted(ya_libre + extra)
                person["days_off"] = total
                off_pair = tuple(sorted(total[:2])) if len(total) >= 2 else None
                if off_pair and off_pair in pair_usage:
                    pair_usage[off_pair] += 1
                else:
                    pair_usage[min(pairs, key=lambda p: pair_usage[p])] += 1

        elif len(forced_indices) == 1:
            fi         = forced_indices[0]
            candidates = [(fi - 1, fi)] if fi > 0 else []
            if fi < len(DAYS) - 1:
                candidates.append((fi, fi + 1))
            valid = [c for c in candidates if c in pair_usage and not (must_work & set(c))]
            if not valid:
                valid = [c for c in candidates if c in pair_usage]
            chosen = (
                min(valid, key=lambda p: pair_usage[p])
                if valid
                else sorted(pairs, key=lambda p: pair_usage[p])[0]
            )
            schedule[person["name"]][chosen[0]] = "Libre"
            schedule[person["name"]][chosen[1]] = "Libre"
            person["days_off"] = list(chosen)
            pair_usage[chosen] += 1

        elif person.get("dia_descanso_tras_cobertura") is not None:
            # Mozos cover (Vie+Sáb): descanso obligatorio tras la última noche
            dia = person["dia_descanso_tras_cobertura"]
            schedule[person["name"]][dia] = "Libre"
            person["days_off"] = sorted(set(person.get("days_off", [])) | {dia})

    # ── Pasada A: finde libre por ratio (K personas, ratio ascendente) ────────
    for person in normal_people:
        if person["name"] not in weekend_assigned:
            continue
        must_work   = set(cover_by_person.get(person["name"], []))
        valid_pairs = [p for p in pairs if not (must_work & set(p))]
        if not valid_pairs:
            valid_pairs = pairs
        chosen = (5, 6) if (5, 6) in valid_pairs else min(valid_pairs, key=lambda p: pair_usage[p])
        pair_usage[chosen] += 1
        schedule[person["name"]][chosen[0]] = "Libre"
        schedule[person["name"]][chosen[1]] = "Libre"
        person["days_off"] = list(chosen)

    # ── Pasada B: rotación sin finde, orden aleatorio para desempate ──────────
    # Se excluyen todos los pares que contengan Sábado(5) o Domingo(6) para
    # no reducir la cobertura del finde más allá del K acordado.
    resto = [p for p in normal_people if p["name"] not in weekend_assigned]
    random.shuffle(resto)
    for person in resto:
        must_work   = set(cover_by_person.get(person["name"], []))
        valid_pairs = [p for p in pairs if not (must_work & set(p))]
        if not valid_pairs:
            valid_pairs = pairs
        # Preferir pares sin días de fin de semana; recaer en (4,5) solo si
        # no hay otra opción y en (5,6) solo si tampoco hay ninguna más.
        sin_finde = [p for p in valid_pairs if 5 not in p and 6 not in p]
        if not sin_finde:
            sin_finde = [p for p in valid_pairs if p != (5, 6)]
        if not sin_finde:
            sin_finde = valid_pairs
        min_uso   = min(pair_usage[p] for p in sin_finde)
        candidatos = [p for p in sin_finde if pair_usage[p] == min_uso]
        chosen    = random.choice(candidatos)
        pair_usage[chosen] += 1
        schedule[person["name"]][chosen[0]] = "Libre"
        schedule[person["name"]][chosen[1]] = "Libre"
        person["days_off"] = list(chosen)

    return schedule

# ─── ASIGNACIÓN DE TURNOS ────────────────────────────────────────────────────

def allowed_shifts(person, day_idx, schedule, coverage, cfg: DeptConfig, prev_sunday_shifts=None):
    result = []
    for shift in ["Mañana", "Tarde", "Partido"]:
        if coverage.get(shift, 0) >= cfg.max_coverage.get(shift, 99):
            continue
        if shift == "Mañana" and not can_work_morning(person["name"], day_idx, schedule, prev_sunday_shifts):
            continue
        result.append(shift)
    # Noche: disponible cuando aún no está cubierta y el rol lo permite.
    # Con night_fixed presente y activo, Pass 1 ya asigna coverage["Noche"]=max,
    # por lo que esta rama nunca se activa en el caso normal. Solo entra en juego
    # cuando night_fixed está de baja/vacaciones o no existe en la plantilla.
    if (coverage.get("Noche", 0) < cfg.max_coverage.get("Noche", 0)
            and person["role"] not in cfg.no_night_roles
            and person["role"] != "night_fixed"):
        result.append("Noche")

    # Partido solo cuando los turnos con cobertura mínima están cubiertos
    if "Partido" in result:
        priority_unfilled = any(
            coverage.get(s, 0) < cfg.min_coverage.get(s, 0)
            for s in ["Mañana", "Tarde", "Noche"]
            if cfg.min_coverage.get(s, 0) > 0
        )
        if priority_unfilled:
            sin_partido = [s for s in result if s != "Partido"]
            if sin_partido:
                result = sin_partido
    return result

def _morning_coverage_ok(shift, day_idx, person_name, schedule, staff_list, cfg):
    """Retorna False si asignar 'shift' a person_name el día day_idx dejaría
    menos de min_coverage['Mañana'] personas elegibles para Mañana el día siguiente.
    Solo aplica a Tarde/Noche en días 0-5 (el domingo no tiene día siguiente en la semana)."""
    if day_idx >= 6 or shift not in ("Tarde", "Noche"):
        return True
    min_man = cfg.min_coverage.get("Mañana", 0)
    if min_man == 0:
        return True
    next_day = day_idx + 1
    # Elegibles para Mañana el día siguiente: trabajan ese día Y no acaban hoy en Tarde/Noche
    eligible = sum(
        1 for p in staff_list
        if schedule[p["name"]][next_day] != "Libre"
        and schedule[p["name"]][day_idx] not in ("Tarde", "Noche")
    )
    # Si la persona trabaja el día siguiente, estaba contada en eligible y hay
    # que restarla (pasará a Tarde/Noche → ya no podrá hacer Mañana).
    # Si libra el día siguiente, NO estaba contada y no se resta nada: asignarle
    # Tarde/Noche hoy no cambia en absoluto la cobertura de Mañana del día siguiente.
    if schedule[person_name][next_day] != "Libre":
        eligible -= 1
    return eligible >= min_man

def assign_shifts(staff_list: list, days_off_schedule: dict,
                  night_cover: dict, cfg: DeptConfig, prev_sunday_shifts: dict = None,
                  shift_history: dict = None, prev_last_shifts: dict = None) -> tuple:
    """Asigna turnos y devuelve (schedule, violaciones).
    Si una persona no tiene ningún turno legal disponible, su celda queda como
    'SIN_ASIGNAR' y se registra en la lista de violaciones."""
    if night_cover is None:
        night_cover = {}

    violaciones: list[str] = []
    schedule = copy.deepcopy(days_off_schedule)

    # Pass 1: fixed roles
    for day_idx in range(len(DAYS)):
        for person in staff_list:
            if schedule[person["name"]][day_idx] == "Libre":
                continue
            if person["role"] == "night_fixed":
                schedule[person["name"]][day_idx] = "Noche"
            elif person["name"] == night_cover.get(day_idx):
                schedule[person["name"]][day_idx] = "Noche"
            elif person["role"] in cfg.fixed_roles:
                schedule[person["name"]][day_idx] = cfg.fixed_roles[person["role"]]

    # Pass 2: day by day
    for day_idx in range(len(DAYS)):
        available = [p for p in staff_list if schedule[p["name"]][day_idx] == ""]
        random.shuffle(available)

        coverage = {s: 0 for s in cfg.max_coverage}
        for person in staff_list:
            s = schedule[person["name"]][day_idx]
            if s in coverage:
                coverage[s] += 1

        def unfilled():
            needed = []
            for shift in ["Mañana", "Tarde", "Noche", "Partido"]:
                deficit = cfg.min_coverage.get(shift, 0) - coverage.get(shift, 0)
                if deficit > 0:
                    needed.extend([shift] * deficit)
            return needed

        def _coste_turno(person, s):
            """(coste_continuidad, coste_historico).
            Preferencia blanda: dentro de tirada mantener turno;
            al volver de librar rotar turno. Noche nunca penaliza."""
            ult, tras_libre = _turno_referencia(
                person["name"], day_idx, schedule, prev_last_shifts)
            hist       = (shift_history or {}).get(person["name"], {})
            prefs      = set(person.get("preferences", []))
            coste_hist = hist.get(s, 0) + (-0.5 if s in prefs else 0)
            if ult is None or s == "Noche":
                return (0, coste_hist)
            if not tras_libre:
                # Tirada abierta → mantener turno
                return (0 if s == ult else 1, coste_hist)
            # Tras librar → rotar; entre los distintos gana el menos hecho
            return (1 if s == ult else 0, coste_hist)

        # Sub-pass A: fill deficits — usa _coste_turno para escoger.
        for person in list(available):
            needed = unfilled()
            if not needed:
                break
            opts = allowed_shifts(person, day_idx, schedule, coverage, cfg, prev_sunday_shifts)
            fillable = [s for s in opts if s in needed
                        if _morning_coverage_ok(s, day_idx, person["name"], schedule, staff_list, cfg)]
            if not fillable:
                continue
            chosen = min(fillable, key=lambda s: _coste_turno(person, s))
            schedule[person["name"]][day_idx] = chosen
            coverage[chosen] += 1
            available.remove(person)

        # Sub-pass B: heurística more-constrained-first; _b_priority (coste mínimo
        # de la mejor opción) desempata. Quien tiene menos opciones va primero.
        def _b_priority(p):
            opts_p = [s for s in allowed_shifts(p, day_idx, schedule, coverage,
                                                 cfg, prev_sunday_shifts)
                      if _morning_coverage_ok(s, day_idx, p["name"],
                                              schedule, staff_list, cfg)]
            return min((_coste_turno(p, s) for s in opts_p), default=(9, 9))

        def _b_n_opciones(p):
            """Número de turnos legales disponibles (incluyendo look-ahead)."""
            opts = allowed_shifts(p, day_idx, schedule, coverage, cfg, prev_sunday_shifts)
            return len([s for s in opts
                        if _morning_coverage_ok(s, day_idx, p["name"], schedule, staff_list, cfg)])

        for person in sorted(available, key=lambda p: (_b_n_opciones(p), _b_priority(p))):
            opts = allowed_shifts(person, day_idx, schedule, coverage, cfg, prev_sunday_shifts)
            # Filtro de mirada adelante: no asignar Tarde/Noche si deja
            # insuficientes candidatos a Mañana el día siguiente.
            opts = [s for s in opts
                    if _morning_coverage_ok(s, day_idx, person["name"], schedule, staff_list, cfg)]
            if not opts:
                # Sin turno legal: registrar violación en vez de romper límites.
                schedule[person["name"]][day_idx] = "SIN_ASIGNAR"
                violaciones.append(
                    f"{DAYS[day_idx]}: {person['name']} sin turno asignable"
                )
                continue
            chosen = min(opts, key=lambda s: _coste_turno(person, s))
            schedule[person["name"]][day_idx] = chosen
            coverage[chosen] = coverage.get(chosen, 0) + 1

    return schedule, violaciones

# ─── FUNCIÓN PRINCIPAL ───────────────────────────────────────────────────────

def generate_schedule(staff_list: list, week_start: date, cfg: DeptConfig,
                      prev_week_covers: dict = None, prev_sunday_shifts: dict = None,
                      _save_night_state: bool = True, prev_last_shifts: dict = None):
    """
    Main schedule generation function.
    prev_week_covers: {name: True} (mozos) o {name: [día,...]} (recepción) —
    quién cubrió noches la semana pasada y debe descansar esos días esta semana,
    porque su descanso de compensación cayó tras el fin de la semana anterior.
    prev_sunday_shifts: {name: turno} — turno del domingo de la semana anterior,
    para la regla no-Mañana-tras-Tarde/Noche entre semanas.
    prev_last_shifts: {name: (turno, hubo_libre)} — último turno trabajado y si
    terminó librando, para las reglas de continuidad de tirada y rotación de turno.
    _save_night_state: si True (defecto), persiste el historial nocturno al final.
    generate_schedule_valido lo llama con False para evitar guardar en cada reintento.
    """
    staff_list = copy.deepcopy(staff_list)

    # Aplicar el descanso de compensación pendiente de la semana pasada.
    if prev_week_covers:
        for person in staff_list:
            dias = prev_week_covers.get(person["name"])
            if not dias:
                continue
            if cfg.prefer_next_week_for_cover:
                # Mozos: siempre Lun+Mar de la semana siguiente, por diseño.
                person["forced_days_off"] = ["Lunes", "Martes"]
            elif isinstance(dias, list):
                existentes = person.get("forced_days_off", [])
                person["forced_days_off"] = list(dict.fromkeys(existentes + dias))

    shift_history = _get_shift_counts(cfg.dept_id)
    nf_person = next((p for p in staff_list if p["role"] == "night_fixed"), None)
    nf_ausente = (nf_person is None
                  or set(nf_person.get("forced_days_off", [])) >= set(DAYS))

    if nf_ausente and cfg.night_fixed_days_off:
        # Rotación por bloques: cubre las 7 noches, no hace falta sustituto normal
        night_cover, history_state = _plan_noches_bloques(staff_list, cfg)
        noches_estado = (history_state, cfg.night_rotation_key + "_bloques")
    else:
        night_cover, history_state = select_night_covers(staff_list, week_start, cfg)
        noches_estado = (history_state, cfg.night_rotation_key)

    days_off_sched = assign_days_off(staff_list, night_cover, cfg)
    full_schedule, _ = assign_shifts(staff_list, days_off_sched, night_cover, cfg,
                                     prev_sunday_shifts, shift_history=shift_history,
                                     prev_last_shifts=prev_last_shifts)

    # Registrar quién necesita descanso de compensación la semana que viene.
    this_week_covers = {}
    if cfg.prefer_next_week_for_cover:
        for person in staff_list:
            if person.get("night_cover_this_week"):
                this_week_covers[person["name"]] = True
    else:
        for person in staff_list:
            dias = person.get("next_week_rest_days")
            if dias:
                this_week_covers[person["name"]] = dias

    if _save_night_state:
        history_dict, rotation_key = noches_estado
        _save_night_history(rotation_key, history_dict)

    return full_schedule, staff_list, week_start, night_cover, this_week_covers, noches_estado

# ─── VALIDACIÓN Y GENERACIÓN CON REINTENTOS ──────────────────────────────────

def validar_schedule(schedule: dict, staff_list: list, cfg: DeptConfig,
                     prev_sunday_shifts: dict = None) -> list[str]:
    """Valida el horario generado y devuelve lista de violaciones en español.
    Lista vacía significa horario válido."""
    violaciones: list[str] = []

    # 1. Límites de cobertura por turno y día
    turnos_a_validar = set(cfg.min_coverage) | set(cfg.max_coverage)
    for shift in turnos_a_validar:
        mn = cfg.min_coverage.get(shift, 0)
        mx = cfg.max_coverage.get(shift, 99)
        for day_idx, day in enumerate(DAYS):
            count = sum(1 for p in staff_list
                        if schedule[p["name"]][day_idx] == shift)
            if count < mn:
                violaciones.append(
                    f"{day}: {count} personas de {shift}, mínimo {mn}"
                )
            elif count > mx:
                violaciones.append(
                    f"{day}: {count} personas de {shift}, máximo {mx}"
                )

    # 2. Regla no-Mañana-tras-Tarde/Noche (incluye cruce con semana anterior)
    for p in staff_list:
        turnos = schedule[p["name"]]
        for i in range(7):
            if i == 0:
                prev_shift = (prev_sunday_shifts or {}).get(p["name"])
                prev_label = "Domingo anterior"
            else:
                prev_shift = turnos[i - 1]
                prev_label = DAYS[i - 1]
            if prev_shift in NO_MORNING_AFTER and turnos[i] == "Mañana":
                violaciones.append(
                    f"{p['name']}: {prev_label} ({prev_shift}) → {DAYS[i]} (Mañana)"
                )

    # 3. Celdas sin turno asignable
    for p in staff_list:
        for day_idx, turno in enumerate(schedule[p["name"]]):
            if turno == "SIN_ASIGNAR":
                violaciones.append(
                    f"{p['name']}: {DAYS[day_idx]} sin turno asignable"
                )

    return violaciones


def _contar_penalizaciones(schedule, staff_list, prev_last_shifts=None):
    """Penaliza: cambio dentro de tirada + repetición de turno al volver de librar.
    Cuanto menor, mejor. Noche y SIN_ASIGNAR no cuentan."""
    pen = 0
    for p in staff_list:
        prev = (prev_last_shifts or {}).get(p["name"])
        if isinstance(prev, (tuple, list)):
            ult, tras_libre = prev[0], prev[1]
        else:
            ult, tras_libre = prev, False
        for t in schedule[p["name"]]:
            if t in ("", "SIN_ASIGNAR", "Noche"):
                continue
            if t == "Libre":
                tras_libre = True
                continue
            if ult is not None:
                if not tras_libre and t != ult:
                    pen += 1      # rompió la tirada
                elif tras_libre and t == ult:
                    pen += 1      # volvió de librar al mismo turno
            ult, tras_libre = t, False
    return pen


def generate_schedule_valido(staff_list, week_start, cfg,
                              prev_week_covers=None, prev_sunday_shifts=None,
                              max_intentos=50, prev_last_shifts=None):
    candidato_valido    = None
    mejor_pen_valido    = None
    mejor_estado_valido = None
    mejor_resultado     = None
    mejor_violaciones   = None
    mejor_estado        = None

    for _ in range(max_intentos):
        *resultado, noches_estado = generate_schedule(
            staff_list, week_start, cfg, prev_week_covers, prev_sunday_shifts,
            _save_night_state=False, prev_last_shifts=prev_last_shifts)
        violaciones = validar_schedule(resultado[0], resultado[1], cfg, prev_sunday_shifts)
        if not violaciones:
            pen = _contar_penalizaciones(resultado[0], resultado[1], prev_last_shifts)
            if pen == 0:
                history_dict, rotation_key = noches_estado
                _save_night_history(rotation_key, history_dict)
                return (*resultado, [])
            if mejor_pen_valido is None or pen < mejor_pen_valido:
                candidato_valido    = resultado
                mejor_pen_valido    = pen
                mejor_estado_valido = noches_estado
        elif mejor_violaciones is None or len(violaciones) < len(mejor_violaciones):
            mejor_resultado, mejor_violaciones = resultado, violaciones
            mejor_estado = noches_estado

    if candidato_valido is not None:
        history_dict, rotation_key = mejor_estado_valido
        _save_night_history(rotation_key, history_dict)
        return (*candidato_valido, [])
    if mejor_estado is not None:
        history_dict, rotation_key = mejor_estado
        _save_night_history(rotation_key, history_dict)
    return (*mejor_resultado, mejor_violaciones)

# ─── EXCEL EXPORT ────────────────────────────────────────────────────────────

SHIFT_COLORS = {
    "Mañana":  "FFF59D",
    "Tarde":   "A5D6A7",
    "Noche":   "90CAF9",
    "Partido": "FFCC80",
    "Libre":   "EF9A9A",
}
HEADER_COLOR = "1565C0"
SUB_HEADER   = "1976D2"

def thin_border():
    s = Side(style="thin", color="BBBBBB")
    return Border(left=s, right=s, top=s, bottom=s)

def _write_dept_section(ws, start_row: int, schedule: dict, staff_list: list,
                        week_start: date, cfg: DeptConfig,
                        role_icons: dict = None) -> int:
    """
    Escribe una sección de un departamento en la hoja ws a partir de start_row.
    Devuelve la primera fila libre tras la sección (incluye filas de cobertura).
    """
    week_end = week_start + timedelta(days=6)
    title    = (f"{cfg.dept_label} — Semana "
                f"{week_start.strftime('%d/%m/%Y')} – {week_end.strftime('%d/%m/%Y')}")

    # Fila de título del departamento
    title_end = get_column_letter(8)
    ws.merge_cells(f"A{start_row}:{title_end}{start_row}")
    tc = ws.cell(row=start_row, column=1, value=title)
    tc.font      = Font(bold=True, size=14, color="FFFFFF")
    tc.fill      = PatternFill("solid", start_color=HEADER_COLOR)
    tc.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[start_row].height = 28

    # Cabecera de días
    hdr_row = start_row + 1
    hc = ws.cell(row=hdr_row, column=1, value="Empleado")
    hc.font      = Font(bold=True, color="FFFFFF")
    hc.fill      = PatternFill("solid", start_color=SUB_HEADER)
    hc.alignment = Alignment(horizontal="center", vertical="center")

    for col_offset in range(7):
        day_date = week_start + timedelta(days=col_offset)
        cell = ws.cell(row=hdr_row, column=col_offset + 2)
        cell.value     = f"{DAYS[col_offset]}\n{day_date.strftime('%d/%m')}"
        cell.font      = Font(bold=True, color="FFFFFF")
        cell.fill      = PatternFill("solid", start_color=SUB_HEADER)
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.row_dimensions[hdr_row].height = 32

    # Filas de empleados
    icons = role_icons or {}
    data_start = hdr_row + 1
    for row_offset, person in enumerate(staff_list):
        r    = data_start + row_offset
        icon = icons.get(person["role"], "")
        nc   = ws.cell(row=r, column=1,
                       value=f"{person['name']} {icon}".strip())
        nc.font      = Font(bold=True)
        nc.alignment = Alignment(vertical="center", horizontal="left")
        nc.border    = thin_border()
        ws.row_dimensions[r].height = 22
        for col_idx in range(7):
            shift = schedule[person["name"]][col_idx]
            cell  = ws.cell(row=r, column=col_idx + 2, value=shift)
            cell.fill      = PatternFill("solid", start_color=SHIFT_COLORS.get(shift, "FFFFFF"))
            cell.alignment = Alignment(horizontal="center", vertical="center")
            cell.border    = thin_border()
            cell.font      = Font(size=10)

    # Filas de cobertura (fórmulas COUNTIF)
    summary_row = data_start + len(staff_list) + 1
    ws.cell(row=summary_row, column=1, value="COBERTURA").font = Font(bold=True, size=11)
    for s_idx, shift in enumerate(["Mañana", "Tarde", "Noche", "Partido"]):
        r     = summary_row + 1 + s_idx
        label = ws.cell(row=r, column=1, value=shift)
        label.font      = Font(bold=True)
        label.fill      = PatternFill("solid", start_color=SHIFT_COLORS[shift])
        label.border    = thin_border()
        label.alignment = Alignment(horizontal="center")
        ds = data_start
        de = data_start + len(staff_list) - 1
        for col_idx in range(2, 9):
            day_col = get_column_letter(col_idx)
            cell    = ws.cell(row=r, column=col_idx,
                              value=f'=COUNTIF({day_col}{ds}:{day_col}{de},"{shift}")')
            cell.alignment = Alignment(horizontal="center")
            cell.border    = thin_border()
            cell.fill      = PatternFill("solid", start_color=SHIFT_COLORS[shift])

    return summary_row + 5   # +4 filas cobertura +1 fila en blanco de separación


def export_to_excel(schedule, staff_list, week_start, filename, cfg: DeptConfig,
                    role_icons: dict = None,
                    extra_sections: list = None):
    """
    Genera el Excel del horario.
    extra_sections: lista de (schedule, staff_list, cfg, role_icons) adicionales
    que se escriben debajo del primero (p.ej. mozos debajo de recepción).
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "Horario Semanal"

    ws.column_dimensions["A"].width = 18
    for col in range(2, 9):
        ws.column_dimensions[get_column_letter(col)].width = 13

    next_row = _write_dept_section(ws, 1, schedule, staff_list, week_start, cfg, role_icons)

    for sched_ex, staff_ex, cfg_ex, icons_ex in (extra_sections or []):
        next_row = _write_dept_section(ws, next_row, sched_ex, staff_ex,
                                       week_start, cfg_ex, icons_ex)

    ws.freeze_panes = "B3"
    wb.save(filename)
    return filename
