"""
estadisticas.py — Estadísticas acumuladas de turnos por empleado, con caché en disco.

Recorrer historial.json completo (todas las semanas generadas) es caro y no
cambia salvo cuando se genera o edita una semana, se registran festivos
trabajados, o se tocan peticiones/vacaciones/compensaciones. Por eso el
resultado se cachea en cache_estadisticas.json (por departamento) y solo se
recalcula cuando:
  - la caché no existe todavía,
  - se invalidó explícitamente (invalidar_cache, llamada desde app.py en cada
    punto de escritura relevante), o
  - el hash de red de seguridad (_version_historial) no coincide, por si
    algún punto de invalidación se escapó.

La caché guarda, por empleado, tanto los totales acumulados como el desglose
semana a semana (campo "semanas") y día a día de festivos trabajados (campo
"festivos_dias"). Ese desglose es lo que permite servir el selector de
periodo (últimos 3 meses / año en curso) del dashboard sin volver a leer
historial.json en cada petición.
"""

import hashlib
import json
import os
from datetime import date, datetime, timedelta, timezone

from herramientas import (
    _dept_file, _cargar_json, guardar_json_atomico,
    cargar_historial, cargar_festivos_trabajados,
    cargar_plantilla, cargar_roles,
)
from modelo_base import _get_weekend_ratios

SHIFTS_CONTABLES = ["Mañana", "Tarde", "Noche", "Partido", "Libre"]

# Ventana de análisis para el bloque de equidad del dashboard: la misma que
# usa _get_weekend_ratios (52 semanas), para que findes/noches/festivos/
# turnos se comparen todos sobre el mismo periodo.
VENTANA_EQUIDAD_SEMANAS = 52


def _cache_path(dept_id: str = "recepcion") -> str:
    return _dept_file("cache_estadisticas.json", dept_id)


def _version_historial(dept_id: str) -> str:
    """Hash combinado de historial + festivos trabajados + plantilla + roles:
    red de seguridad para detectar cambios que se hayan escapado de la
    invalidación explícita. Incluye plantilla/roles porque el bloque de
    equidad (ver _calcular_equidad) depende de quién está activo y de qué
    roles tienen excluir_de_equidad=True, no solo del historial."""
    historial = cargar_historial(dept_id)
    festivos  = cargar_festivos_trabajados(dept_id)
    plantilla = cargar_plantilla(dept_id)
    roles     = cargar_roles(dept_id)
    payload = json.dumps([historial, festivos, plantilla, roles],
                         sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def invalidar_cache(dept_id: str = "recepcion") -> None:
    """Borra la caché de estadísticas de un departamento. Debe llamarse tras
    cualquier escritura que pueda afectar al histórico de turnos, festivos
    trabajados, peticiones, vacaciones o compensaciones de ese departamento."""
    path = _cache_path(dept_id)
    if os.path.exists(path):
        os.remove(path)


def _turnos_vacios() -> dict:
    return {s: 0 for s in SHIFTS_CONTABLES}


def _calcular_todos(dept_id: str) -> dict:
    """Recorre historial.json y festivos_trabajados.json una única vez y
    construye el dict completo de estadísticas por empleado."""
    historial = cargar_historial(dept_id)
    festivos  = cargar_festivos_trabajados(dept_id)
    ahora     = datetime.now(timezone.utc).isoformat()
    resultado: dict[str, dict] = {}

    def entrada(nombre: str) -> dict:
        return resultado.setdefault(nombre, {
            "turnos": _turnos_vacios(),
            "findes": 0, "noches": 0, "festivos": 0,
            "calculado_en": ahora,
            "semanas": [],
            "festivos_dias": [],
        })

    for semana_str, ent in sorted(historial.items()):
        schedule = ent.get("schedule", {})
        staff_by_name = {p["name"]: p for p in ent.get("staff", [])}
        for nombre, turnos in schedule.items():
            e = entrada(nombre)
            semana_turnos = _turnos_vacios()
            for t in turnos:
                if t in e["turnos"]:
                    e["turnos"][t] += 1
                    semana_turnos[t] += 1
            finde_libre    = len(turnos) >= 7 and turnos[5] == "Libre" and turnos[6] == "Libre"
            noche_cubierta = bool(staff_by_name.get(nombre, {}).get("night_cover_this_week"))
            if finde_libre:
                e["findes"] += 1
            if noche_cubierta:
                e["noches"] += 1
            e["semanas"].append({
                "semana": semana_str, "turnos": semana_turnos,
                "finde_libre": finde_libre, "noche_cubierta": noche_cubierta,
            })

    for fecha, info in festivos.items():
        for nombre, turno in info.get("trabajadores", {}).items():
            e = entrada(nombre)
            e["festivos"] += 1
            e["festivos_dias"].append({
                "fecha": fecha, "turno": turno, "festivo": info.get("festivo"),
            })

    resultado["__version_historial__"] = _version_historial(dept_id)
    resultado["__equidad__"] = _calcular_equidad(dept_id, resultado)
    return resultado


def _calcular_equidad(dept_id: str, resultado: dict) -> dict:
    """Bloque de equidad para el dashboard del empleado (findes/noches/
    festivos/turnos propios y media del equipo), calculado una única vez
    junto al resto de _calcular_todos y cacheado con él.

    Reutiliza _get_weekend_ratios (modelo_base.py) para el ratio de findes
    en vez de reimplementar esa lógica (ventana de 52 semanas, exención de
    findes que son compensación de cobertura nocturna, etc.). Se le pasa la
    plantilla completa con el rol de cada persona forzado a "normal" para
    obtener el finds/semanas propio de TODOS, incluidos los roles con
    excluir_de_equidad=True (que la función excluiría por defecto) — el rol
    pasado ahí solo decide inclusión, no afecta al cálculo en sí, que se
    basa en las instantáneas de historial.json.

    noches/festivos/turnos se acotan a la misma ventana de 52 semanas
    (usando el desglose semanal ya calculado en 'resultado') para que estén
    en el mismo periodo que el ratio de findes.
    """
    staff     = cargar_plantilla(dept_id)
    roles_cfg = cargar_roles(dept_id)
    excluidos = {rid for rid, r in roles_cfg.items() if r.get("excluir_de_equidad")}

    staff_forzado = [{**p, "role": "normal"} for p in staff]
    ratios_todos  = _get_weekend_ratios(dept_id, staff_forzado)

    cutoff = (date.today() - timedelta(weeks=VENTANA_EQUIDAD_SEMANAS)).isoformat()

    personas: dict[str, dict] = {}
    for p in staff:
        nombre = p["name"]
        info   = ratios_todos.get(nombre, {"finds": 0, "semanas": 0, "ratio": 0.0})

        stats  = resultado.get(nombre, {})
        turnos = _turnos_vacios()
        noches = 0
        for s in stats.get("semanas", []):
            if s.get("semana", "") < cutoff:
                continue
            for k, v in s.get("turnos", {}).items():
                turnos[k] = turnos.get(k, 0) + v
            if s.get("noche_cubierta"):
                noches += 1
        festivos = sum(
            1 for fd in stats.get("festivos_dias", [])
            if fd.get("fecha", "") >= cutoff
        )

        personas[nombre] = {
            "role": p["role"], "excluido": p["role"] in excluidos,
            "semanas": info["semanas"],
            "findes": info["finds"], "ratio_findes": info["ratio"],
            "noches": noches, "festivos": festivos, "turnos": turnos,
        }

    # Media del equipo: solo roles que participan en el balanceo y con al
    # menos 4 semanas de presencia (una incorporación reciente distorsiona
    # la referencia sin aportar señal — mismo criterio que en auditoría).
    elegibles = [d for d in personas.values() if not d["excluido"] and d["semanas"] >= 4]

    def _tasa_media(extractor) -> float:
        tasas = [extractor(d) / d["semanas"] for d in elegibles if d["semanas"] > 0]
        return sum(tasas) / len(tasas) if tasas else 0.0

    media_ratio_findes = (
        sum(d["ratio_findes"] for d in elegibles) / len(elegibles) if elegibles else 0.0
    )

    return {
        "personas": personas,
        "media_ratio_findes": media_ratio_findes,
        "tasa_media_noches": _tasa_media(lambda d: d["noches"]),
        "tasa_media_festivos": _tasa_media(lambda d: d["festivos"]),
        "tasa_media_turnos": {
            t: _tasa_media(lambda d, t=t: d["turnos"].get(t, 0))
            for t in SHIFTS_CONTABLES
        },
        "n_elegibles": len(elegibles),
    }


def _posicion(propio: float, media: float) -> str:
    """'en_media' si la diferencia relativa a la media es menor al 15% —
    evita que una diferencia trivial se lea como agravio. Si no, 'por_encima'
    o 'por_debajo' según corresponda."""
    if media == 0:
        return "en_media" if propio == 0 else "por_encima"
    diferencia_relativa = abs(propio - media) / media
    if diferencia_relativa < 0.15:
        return "en_media"
    return "por_encima" if propio > media else "por_debajo"


def equidad_de(nombre: str, dept_id: str = "recepcion") -> dict:
    """Bloque "Mi equidad" del dashboard: contadores propios normalizados
    por semanas presentes (ventana de 52 semanas) y, si el rol participa en
    el balanceo y hay al menos 4 semanas de presencia, la comparación con
    la media del equipo. NUNCA incluye nombres ni valores individuales de
    otros empleados — solo el propio dato y la media agregada."""
    cache    = obtener_estadisticas(dept_id)
    personas = cache.get("__equidad__", {}).get("personas", {})
    persona  = personas.get(nombre) or {
        "role": None, "excluido": False, "semanas": 0,
        "findes": 0, "ratio_findes": 0.0, "noches": 0,
        "festivos": 0, "turnos": _turnos_vacios(),
    }

    semanas = persona["semanas"]
    bloque = {
        "findes":   {"propios": persona["findes"], "semanas": semanas,
                     "ratio": round(persona["ratio_findes"], 4)},
        "noches":   {"propias": persona["noches"]},
        "festivos": {"propios": persona["festivos"]},
        "turnos":   {"propios": persona["turnos"]},
    }

    if persona["excluido"]:
        bloque["excluido_de_balanceo"] = True
        return bloque

    if semanas < 4:
        bloque["datos_insuficientes"] = True
        return bloque

    eq = cache.get("__equidad__", {})
    media_ratio_findes  = eq.get("media_ratio_findes", 0.0)
    tasa_media_noches   = eq.get("tasa_media_noches", 0.0)
    tasa_media_festivos = eq.get("tasa_media_festivos", 0.0)
    tasa_media_turnos   = eq.get("tasa_media_turnos", {})

    media_noches   = round(tasa_media_noches * semanas, 2)
    media_festivos = round(tasa_media_festivos * semanas, 2)

    bloque["findes"]["media_equipo"]   = round(media_ratio_findes, 4)
    bloque["findes"]["posicion"]       = _posicion(persona["ratio_findes"], media_ratio_findes)
    bloque["noches"]["media_equipo"]   = media_noches
    bloque["noches"]["posicion"]       = _posicion(persona["noches"], media_noches)
    bloque["festivos"]["media_equipo"] = media_festivos
    bloque["festivos"]["posicion"]     = _posicion(persona["festivos"], media_festivos)
    bloque["turnos"]["media_equipo"]   = {
        t: round(tasa_media_turnos.get(t, 0.0) * semanas, 2) for t in SHIFTS_CONTABLES
    }

    return bloque


def obtener_estadisticas(dept_id: str = "recepcion") -> dict:
    """Devuelve el dict de estadísticas por empleado del departamento,
    usando la caché en disco si sigue siendo válida, recalculando si no."""
    path  = _cache_path(dept_id)
    cache = _cargar_json(path, None)
    version_actual = _version_historial(dept_id)
    if cache is not None and cache.get("__version_historial__") == version_actual:
        return cache
    cache = _calcular_todos(dept_id)
    guardar_json_atomico(path, cache)
    return cache


def estadisticas_de(nombre: str, dept_id: str = "recepcion") -> dict:
    """Estadísticas acumuladas (todo el histórico) de un empleado concreto."""
    todas = obtener_estadisticas(dept_id)
    return todas.get(nombre, {
        "turnos": _turnos_vacios(), "findes": 0, "noches": 0, "festivos": 0,
        "calculado_en": None, "semanas": [], "festivos_dias": [],
    })


PERIODOS = ("3m", "anio")


def resumen_periodo(nombre: str, dept_id: str = "recepcion", periodo: str = "anio") -> dict:
    """Estadísticas del empleado acotadas a un periodo, calculadas a partir
    del desglose semanal ya cacheado (sin volver a leer historial.json).
    periodo: '3m' (últimos 3 meses) o 'anio' (año en curso)."""
    if periodo not in PERIODOS:
        periodo = "anio"
    hoy = date.today()
    desde = (hoy - timedelta(days=90)) if periodo == "3m" else date(hoy.year, 1, 1)

    e = estadisticas_de(nombre, dept_id)
    turnos = _turnos_vacios()
    findes = noches = festivos = 0

    for s in e.get("semanas", []):
        try:
            f = date.fromisoformat(s["semana"])
        except (KeyError, ValueError):
            continue
        if f < desde:
            continue
        for k, v in s["turnos"].items():
            turnos[k] = turnos.get(k, 0) + v
        if s.get("finde_libre"):
            findes += 1
        if s.get("noche_cubierta"):
            noches += 1

    for fd in e.get("festivos_dias", []):
        try:
            f = date.fromisoformat(fd["fecha"])
        except (KeyError, ValueError):
            continue
        if f >= desde:
            festivos += 1

    return {
        "periodo": periodo, "desde": desde.isoformat(),
        "turnos": turnos, "findes": findes, "noches": noches, "festivos": festivos,
    }
