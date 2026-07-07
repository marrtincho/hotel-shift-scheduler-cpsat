"""
herramientas.py — Gestión de plantilla, peticiones semanales e historial.
Soporte multi-departamento: todas las funciones clave aceptan dept_id.
"""

import json
import os
import shutil
from datetime import date, timedelta

from modelo_base import DAYS

# ─── RUTAS DE DATOS ──────────────────────────────────────────────────────────

BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
DATA_DIR        = os.path.join(BASE_DIR, "data")
BACKUP_DIR      = os.path.join(DATA_DIR, "backups")

os.makedirs(DATA_DIR,   exist_ok=True)
os.makedirs(BACKUP_DIR, exist_ok=True)

def _dept_file(base_name: str, dept_id: str = "recepcion") -> str:
    """Returns dept-namespaced file path. recepcion uses legacy names for compatibility."""
    if dept_id == "recepcion":
        return os.path.join(DATA_DIR, base_name)
    return os.path.join(DATA_DIR, f"{dept_id}_{base_name}")

DIAS_SEMANA    = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]

# ─── UTILIDADES INTERNAS ─────────────────────────────────────────────────────

def _cargar_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default

def guardar_json_atomico(path: str, data):
    """Escribe JSON de forma atómica: primero a .tmp, luego os.replace().
    Garantiza que un crash a mitad de escritura nunca deja el archivo corrupto."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def _guardar_json(path, data):
    """Guarda JSON con backup automático y escritura atómica."""
    if os.path.exists(path):
        nombre = os.path.basename(path)
        ts     = date.today().strftime("%Y%m%d")
        backup = os.path.join(BACKUP_DIR, f"{nombre}.{ts}.bak")
        shutil.copy2(path, backup)
        # Mantener solo los últimos 10 backups por archivo para no acumular
        todos = sorted(
            f for f in os.listdir(BACKUP_DIR) if f.startswith(nombre)
        )
        for viejo in todos[:-10]:
            os.remove(os.path.join(BACKUP_DIR, viejo))
    guardar_json_atomico(path, data)

def _semana_str(week_start: date) -> str:
    return week_start.strftime("%Y-%m-%d")

# ═══════════════════════════════════════════════════════════════════════════════
# MÓDULO 1 — PLANTILLA
# ═══════════════════════════════════════════════════════════════════════════════

# Plantillas por defecto por departamento
PLANTILLA_DEFAULT = {
    "recepcion": [
        {"name": "Carlos",  "role": "night_fixed",       "preferences": ["Noche"]},
        {"name": "Juan",    "role": "jefe",               "preferences": ["Mañana"]},
        {"name": "Laura",   "role": "subjefe",            "preferences": ["Tarde"]},
        {"name": "Pedro",   "role": "normal",             "preferences": ["Mañana", "Partido"]},
        {"name": "Ana",     "role": "normal",             "preferences": []},
        {"name": "Miguel",  "role": "normal",             "preferences": ["Tarde"]},
        {"name": "Sofía",   "role": "adjunto_recepcion",  "preferences": ["Mañana"]},
        {"name": "Diego",   "role": "normal",             "preferences": []},
    ],
    "mozos": [
        {"name": "Fran",   "role": "night_fixed", "preferences": ["Noche"]},
        {"name": "Rubén",  "role": "normal",      "preferences": ["Mañana"]},
        {"name": "Gloria", "role": "normal",      "preferences": ["Tarde"]},
        {"name": "Tomás",  "role": "normal",      "preferences": []},
        {"name": "Inés",   "role": "normal",      "preferences": ["Mañana"]},
    ],
}

def cargar_plantilla(dept_id: str = "recepcion") -> list[dict]:
    import copy
    path  = _dept_file("plantilla.json", dept_id)
    datos = _cargar_json(path, None)
    if datos is not None:
        return datos
    plantilla = copy.deepcopy(PLANTILLA_DEFAULT.get(dept_id, []))
    guardar_plantilla(plantilla, dept_id)
    print(f"  ℹ Plantilla {dept_id} inicializada con datos por defecto.")
    return plantilla

def guardar_plantilla(staff: list[dict], dept_id: str = "recepcion"):
    _guardar_json(_dept_file("plantilla.json", dept_id), staff)

def validar_plantilla(staff: list[dict], excluir_idx: int = None,
                      dept_id: str = "recepcion") -> list[str]:
    """Comprueba restricciones de integridad. Devuelve lista de errores (vacía = OK)."""
    errores   = []
    roles_cfg = cargar_roles(dept_id)
    unicos    = {rid for rid, r in roles_cfg.items() if r.get("unico")}
    conteo_roles = {}
    for i, p in enumerate(staff):
        if i == excluir_idx:
            continue
        rol = p["role"]
        conteo_roles[rol] = conteo_roles.get(rol, 0) + 1

    for rol in unicos:
        if conteo_roles.get(rol, 0) > 1:
            label = roles_cfg.get(rol, {}).get("label", rol)
            errores.append(f"El rol '{label}' solo puede asignarse a una persona (hay {conteo_roles[rol]}).")

    return errores


# ═══════════════════════════════════════════════════════════════════════════════
# MÓDULO 2 — PETICIONES SEMANALES
# ═══════════════════════════════════════════════════════════════════════════════

def _migrar_formato_peticiones(peticiones: dict) -> bool:
    """
    Añade a cada petición existente los campos del sistema de cupos
    ('origen', 'estado', 'creada_en') si no los tiene, y normaliza el formato
    antiguo de lista a dict. Las peticiones migradas se marcan origen='jefe'
    para que nunca computen retroactivamente en el cupo de empleado
    (grandfathering de las peticiones creadas antes de este sistema).
    Devuelve True si se modificó algo (para decidir si hay que guardar).
    """
    cambiado = False
    hoy = date.today().isoformat()
    for semana_key, entradas in peticiones.items():
        if semana_key == "_prioridad" or not isinstance(entradas, dict):
            continue
        for nombre, entrada in list(entradas.items()):
            if isinstance(entrada, list):
                entrada = {"dias": entrada, "la": []}
                entradas[nombre] = entrada
                cambiado = True
            if not isinstance(entrada, dict):
                continue
            if "origen" not in entrada:
                entrada["origen"] = "jefe"
                cambiado = True
            if "estado" not in entrada:
                entrada["estado"] = "activa"
                cambiado = True
            if "creada_en" not in entrada:
                entrada["creada_en"] = hoy
                cambiado = True
    return cambiado

def cargar_peticiones(dept_id: str = "recepcion") -> dict:
    peticiones = _cargar_json(_dept_file("peticiones.json", dept_id), {})
    if _migrar_formato_peticiones(peticiones):
        guardar_peticiones(peticiones, dept_id)
    return peticiones

def guardar_peticiones(peticiones: dict, dept_id: str = "recepcion"):
    _guardar_json(_dept_file("peticiones.json", dept_id), peticiones)

def obtener_peticiones_semana(week_start: date, dept_id: str = "recepcion") -> dict:
    peticiones = cargar_peticiones(dept_id)
    return peticiones.get(_semana_str(week_start), {})

def obtener_prioridad_semana(week_start: date, dept_id: str = "recepcion") -> list | None:
    """Devuelve la lista de nombres en orden de prioridad manual, o None si es automática."""
    return cargar_peticiones(dept_id).get("_prioridad", {}).get(_semana_str(week_start))

def guardar_prioridad_semana(week_start: date, order: list | None, dept_id: str = "recepcion"):
    """Guarda o elimina el orden de prioridad manual para la semana."""
    pet  = cargar_peticiones(dept_id)
    prio = pet.setdefault("_prioridad", {})
    key  = _semana_str(week_start)
    if order is None:
        prio.pop(key, None)
        if not prio:
            del pet["_prioridad"]
    else:
        prio[key] = order
    guardar_peticiones(pet, dept_id)

def aplicar_peticiones(staff_list: list[dict], week_start: date, dept_id: str = "recepcion") -> list[dict]:
    """
    Devuelve una copia de staff_list con:
    - 'forced_days_off': días de libre solicitados normalmente (peticiones regulares)
    - 'extra_days_off':  días LA (libres extra que se añaden al par normal)
    Soporta tanto el formato antiguo (lista) como el nuevo (dict con 'dias' y 'la').
    """
    import copy
    peticiones = obtener_peticiones_semana(week_start, dept_id)
    staff_copia = copy.deepcopy(staff_list)
    for person in staff_copia:
        entrada = peticiones.get(person["name"])
        if not entrada:
            continue
        if isinstance(entrada, list):
            # Formato antiguo — solo días regulares
            dias_regulares, dias_la = entrada, []
        elif isinstance(entrada, dict):
            if entrada.get("estado", "activa") != "activa":
                continue
            dias_regulares = entrada.get("dias", [])
            dias_la        = entrada.get("la",   [])
        else:
            continue
        # Se combinan con lo que ya hubiera (p.ej. vacaciones) en vez de
        # sobrescribir, para no perder días libres ya asignados por otra vía.
        if dias_regulares:
            existentes = person.get("forced_days_off", [])
            person["forced_days_off"] = list(dict.fromkeys(existentes + dias_regulares))
        if dias_la:
            existentes_la = person.get("extra_days_off", [])
            person["extra_days_off"] = list(dict.fromkeys(existentes_la + dias_la))
    return staff_copia


def contar_peticiones_concedidas(dept_id: str = "recepcion") -> dict:
    """Cuenta cuántas peticiones de libre han sido concedidas por persona."""
    historial  = cargar_historial(dept_id)
    peticiones = cargar_peticiones(dept_id)
    concedidas: dict = {}
    for semana_str, entrada in historial.items():
        pets  = peticiones.get(semana_str, {})
        sched = entrada.get("schedule", {})
        for nombre, pet in pets.items():
            if isinstance(pet, list):
                dias = pet
            elif isinstance(pet, dict):
                if pet.get("estado", "activa") != "activa":
                    continue
                dias = pet.get("dias", [])
            else:
                continue
            for dia in dias:
                if dia not in DIAS_SEMANA:
                    continue
                idx    = DIAS_SEMANA.index(dia)
                turnos = sched.get(nombre, [])
                if len(turnos) > idx and turnos[idx] == "Libre":
                    concedidas[nombre] = concedidas.get(nombre, 0) + 1
    return concedidas


def aplicar_peticiones_con_prioridad(staff_list: list, week_start: date,
                                     dept_id: str = "recepcion",
                                     min_workers_dia: int = 5) -> tuple:
    """
    Aplica peticiones de libre respetando prioridad histórica.
    Si conceder una petición deja el día con menos de min_workers_dia disponibles,
    se deniega priorizando a quienes más peticiones ya llevan concedidas.
    Soporta los formatos de petición antiguo (lista) y nuevo (dict con 'dias' y 'la').
    Devuelve (staff_modificado, peticiones_denegadas).
    """
    import copy
    peticiones  = obtener_peticiones_semana(week_start, dept_id)
    concedidas  = contar_peticiones_concedidas(dept_id)
    staff_copia = copy.deepcopy(staff_list)

    # Disponibles por día antes de aplicar peticiones (ya incluye vacaciones)
    disponibles = [
        {p["name"] for p in staff_copia
         if DIAS_SEMANA[d] not in p.get("forced_days_off", [])}
        for d in range(7)
    ]

    # Aplanar peticiones regulares con su carga histórica
    manual_order = obtener_prioridad_semana(week_start, dept_id)
    solicitudes_raw = []
    for nombre, entrada in peticiones.items():
        if isinstance(entrada, list):
            dias = entrada
        elif isinstance(entrada, dict):
            if entrada.get("estado", "activa") != "activa":
                continue
            dias = entrada.get("dias", [])
        else:
            continue
        for dia in dias:
            if dia in DIAS_SEMANA:
                solicitudes_raw.append((concedidas.get(nombre, 0), nombre, dia))

    if manual_order:
        def _sort_key(item):
            count, nombre, dia = item
            try:
                return (0, manual_order.index(nombre), dia)
            except ValueError:
                return (1, count, nombre, dia)
        solicitudes_sorted = sorted(solicitudes_raw, key=_sort_key)
    else:
        solicitudes_sorted = sorted(solicitudes_raw)

    denegadas: list = []
    for _, nombre, dia in solicitudes_sorted:
        idx = DIAS_SEMANA.index(dia)
        if len(disponibles[idx] - {nombre}) < min_workers_dia:
            denegadas.append({"nombre": nombre, "dia": dia})
            continue
        for p in staff_copia:
            if p["name"] == nombre:
                prev = p.get("forced_days_off", [])
                if dia not in prev:
                    p["forced_days_off"] = prev + [dia]
                break
        disponibles[idx] -= {nombre}

    # Los días LA (libres extra) se aplican siempre, sin restricción de cobertura
    for nombre, entrada in peticiones.items():
        if not isinstance(entrada, dict):
            continue
        if entrada.get("estado", "activa") != "activa":
            continue
        dias_la = entrada.get("la", [])
        if not dias_la:
            continue
        for p in staff_copia:
            if p["name"] == nombre:
                existentes_la = p.get("extra_days_off", [])
                p["extra_days_off"] = list(dict.fromkeys(existentes_la + dias_la))
                break

    return staff_copia, denegadas


# ═══════════════════════════════════════════════════════════════════════════════
# MÓDULO 3 — HISTORIAL
# ═══════════════════════════════════════════════════════════════════════════════

def cargar_historial(dept_id: str = "recepcion") -> dict:
    return _cargar_json(_dept_file("historial.json", dept_id), {})

def guardar_historial(historial: dict, dept_id: str = "recepcion"):
    _guardar_json(_dept_file("historial.json", dept_id), historial)

def registrar_semana(week_start: date, schedule: dict, staff_list: list[dict],
                     forzar: bool = False, dept_id: str = "recepcion") -> bool:
    historial  = cargar_historial(dept_id)
    semana_str = _semana_str(week_start)
    if semana_str in historial and not forzar:
        return False
    historial[semana_str] = {
        "schedule": schedule,
        "staff":    [{k: v for k, v in p.items() if k != "days_off"} for p in staff_list],
    }
    guardar_historial(historial, dept_id)
    return True


# ═══════════════════════════════════════════════════════════════════════════════
# MÓDULO 4 — VACACIONES
# ═══════════════════════════════════════════════════════════════════════════════
#
# Estructura de vacaciones.json:
# [
#   {
#     "id":       "uuid corto",
#     "nombre":   "Pedro",
#     "inicio":   "2026-06-01",   ← primer día de vacaciones (cualquier día)
#     "fin":      "2026-06-14",   ← último día incluido
#     "nota":     "Verano"        ← opcional
#   }, ...
# ]
#
# Al generar el horario de una semana, las vacaciones activas esa semana
# se convierten automáticamente en forced_days_off para los días que solapan.
# ═══════════════════════════════════════════════════════════════════════════════

def cargar_vacaciones(dept_id: str = "recepcion") -> list[dict]:
    return _cargar_json(_dept_file("vacaciones.json", dept_id), [])

def guardar_vacaciones(vacaciones: list[dict], dept_id: str = "recepcion"):
    _guardar_json(_dept_file("vacaciones.json", dept_id), vacaciones)

def vacaciones_activas_en_semana(week_start: date, dept_id: str = "recepcion") -> dict:
    dias_semana = [week_start + timedelta(days=i) for i in range(7)]
    vacaciones  = cargar_vacaciones(dept_id)
    resultado: dict[str, list[str]] = {}

    for v in vacaciones:
        try:
            inicio = date.fromisoformat(v["inicio"])
            fin    = date.fromisoformat(v["fin"])
        except (KeyError, ValueError):
            continue
        nombre = v["nombre"]
        for i, dia_date in enumerate(dias_semana):
            if inicio <= dia_date <= fin:
                resultado.setdefault(nombre, [])
                resultado[nombre].append(DIAS_SEMANA[i])

    return resultado

def aplicar_vacaciones(staff_list: list[dict], week_start: date, dept_id: str = "recepcion") -> list[dict]:
    import copy
    vac    = vacaciones_activas_en_semana(week_start, dept_id)
    staff2 = copy.deepcopy(staff_list)
    for person in staff2:
        dias_vac = vac.get(person["name"], [])
        if dias_vac:
            existentes = person.get("forced_days_off", [])
            # Merge sin duplicados, manteniendo orden de la semana
            todos = list(dict.fromkeys(existentes + dias_vac))
            person["forced_days_off"] = todos
    return staff2


# ═══════════════════════════════════════════════════════════════════════════════
# MÓDULO 5 — FESTIVOS Y COMPENSACIONES
# ═══════════════════════════════════════════════════════════════════════════════
#
# Festivos oficiales Valencia ciudad = 12 autonómicos/nacionales + 2 locales
# Fuente: Decreto 100/2025 (DOGV 07/07/2025) + Resolución festivos locales 2025
#
# Estructura festivos.json:
# {
#   "YYYY": [
#     {"fecha": "YYYY-MM-DD", "nombre": "Año Nuevo", "tipo": "nacional"},
#     ...
#   ]
# }
#
# Estructura compensaciones.json:
# {
#   "nombre_empleado": {
#     "año_modificacion": 2026,          ← solo se puede cambiar en enero
#     "modalidad": "cobrar"|"librar_mismo_dia"|"dia_libre_eleccion"
#   }
# }
#
# Estructura festivos_trabajados.json:
# {
#   "YYYY-MM-DD": {
#     "festivo": "Nombre del festivo",
#     "trabajadores": {
#       "nombre": "turno"     ← turno que hizo ese día
#     }
#   }
# }
# ═══════════════════════════════════════════════════════════════════════════════

FESTIVOS_FILE = os.path.join(DATA_DIR, "festivos.json")

MODALIDADES = {
    "cobrar":            "Cobrar el festivo (plus económico)",
    "librar_mismo_dia":  "Librar ese día exacto en otra semana",
    "dia_libre_eleccion":"Día libre a elección (dentro del año)",
}

# Festivos oficiales Valencia ciudad embebidos por año.
# Cada año se añade una nueva clave. Si el año no está, se avisa al usuario.
FESTIVOS_VALENCIA = {
    2026: [
        {"fecha": "2026-01-01", "nombre": "Año Nuevo",                    "tipo": "nacional"},
        {"fecha": "2026-01-06", "nombre": "Epifanía del Señor",           "tipo": "nacional"},
        {"fecha": "2026-01-22", "nombre": "San Vicente Mártir",           "tipo": "local"},
        {"fecha": "2026-03-19", "nombre": "San José",                     "tipo": "autonómico"},
        {"fecha": "2026-04-03", "nombre": "Viernes Santo",                "tipo": "nacional"},
        {"fecha": "2026-04-06", "nombre": "Lunes de Pascua",              "tipo": "autonómico"},
        {"fecha": "2026-04-13", "nombre": "San Vicente Ferrer",           "tipo": "local"},
        {"fecha": "2026-05-01", "nombre": "Fiesta del Trabajo",           "tipo": "nacional"},
        {"fecha": "2026-06-24", "nombre": "San Juan",                     "tipo": "autonómico"},
        {"fecha": "2026-08-15", "nombre": "Asunción de la Virgen",        "tipo": "nacional"},
        {"fecha": "2026-10-09", "nombre": "Día de la Comunitat Valenciana","tipo": "autonómico"},
        {"fecha": "2026-10-12", "nombre": "Fiesta Nacional de España",    "tipo": "nacional"},
        {"fecha": "2026-12-08", "nombre": "Inmaculada Concepción",        "tipo": "nacional"},
        {"fecha": "2026-12-25", "nombre": "Natividad del Señor",          "tipo": "nacional"},
    ],
    # Añadir aquí años siguientes cuando se publiquen en el DOGV
}

def cargar_festivos(anio: int) -> list[dict]:
    """
    Devuelve la lista de festivos de Valencia ciudad para el año dado.
    Prioriza festivos.json (personalizable) sobre los embebidos en FESTIVOS_VALENCIA.
    """
    datos = _cargar_json(FESTIVOS_FILE, {})
    if str(anio) in datos:
        return datos[str(anio)]
    if anio in FESTIVOS_VALENCIA:
        return FESTIVOS_VALENCIA[anio]
    return []

def es_festivo(d: date) -> dict | None:
    """Devuelve el dict del festivo si la fecha lo es, o None."""
    festivos = cargar_festivos(d.year)
    for f in festivos:
        if date.fromisoformat(f["fecha"]) == d:
            return f
    return None

def festivos_en_semana(week_start: date) -> list[dict]:
    """Devuelve los festivos que caen dentro de la semana dada."""
    resultado = []
    for i in range(7):
        d = week_start + timedelta(days=i)
        f = es_festivo(d)
        if f:
            resultado.append({**f, "dia_semana": DIAS_SEMANA[i], "date": d})
    return resultado

# ── Compensaciones ───────────────────────────────────────────────────────────

def cargar_compensaciones(dept_id: str = "recepcion") -> dict:
    return _cargar_json(_dept_file("compensaciones.json", dept_id), {})

def guardar_compensaciones(comp: dict, dept_id: str = "recepcion"):
    _guardar_json(_dept_file("compensaciones.json", dept_id), comp)

# ── Registro de festivos trabajados ─────────────────────────────────────────

def cargar_festivos_trabajados(dept_id: str = "recepcion") -> dict:
    return _cargar_json(_dept_file("festivos_trabajados.json", dept_id), {})

def guardar_festivos_trabajados(datos: dict, dept_id: str = "recepcion"):
    _guardar_json(_dept_file("festivos_trabajados.json", dept_id), datos)

def registrar_festivos_semana(week_start: date, schedule: dict, staff_list: list[dict]):
    """
    Detecta automáticamente qué festivos caen en la semana y qué trabajadores
    estuvieron en turno activo (no Libre) ese día.
    """
    festivos = festivos_en_semana(week_start)
    if not festivos:
        return

    datos = cargar_festivos_trabajados()
    DIAS  = ["Lunes","Martes","Miércoles","Jueves","Viernes","Sábado","Domingo"]

    for f in festivos:
        fecha_str  = f["date"].isoformat()
        dia_idx    = DIAS.index(f["dia_semana"])
        trabajando = {}
        for person in staff_list:
            turno = schedule.get(person["name"], ["Libre"]*7)[dia_idx]
            if turno != "Libre":
                trabajando[person["name"]] = turno

        # No sobrescribir si ya existe (la primera vez que se genera es canónica)
        if fecha_str not in datos:
            datos[fecha_str] = {
                "festivo":      f["nombre"],
                "tipo":         f["tipo"],
                "trabajadores": trabajando,
            }
        else:
            # En regeneraciones, actualizar solo los trabajadores (el horario pudo cambiar)
            datos[fecha_str]["trabajadores"] = trabajando

    guardar_festivos_trabajados(datos)


# ═══════════════════════════════════════════════════════════════════════════════
# MÓDULO 6 — CATEGORÍAS DE EMPLEADO (roles)
# ═══════════════════════════════════════════════════════════════════════════════
#
# Estructura de roles.json: dict de role_id -> config.
# - turno_fijo: "Mañana"|"Tarde"|"Noche"|"Partido"|None (None = rota normalmente)
# - dias_libres_fijos: [idx, idx] (exactamente 2, índices 0-6 Lunes..Domingo) o None
# - dias_libres_sustituto: igual formato; solo aplica al rol night_fixed, son los
#   días que recibe quien lo sustituye (se ignora si el departamento usa el
#   mecanismo de "semana siguiente", ver DeptConfig.prefer_next_week_for_cover)
# - sin_noches: si es True, este rol nunca es elegible para cubrir al night_fixed
# - unico: solo puede haber un empleado con este rol en la plantilla
# - protegido: True para night_fixed/normal — no se pueden borrar ni renombrar,
#   y algunos campos no son editables (ver editar_rol)
# - borrable: False para night_fixed/normal
# ═══════════════════════════════════════════════════════════════════════════════

ROLES_DEFAULT = {
    "recepcion": {
        "night_fixed": {"label": "Turno Noche fijo", "icon": "★", "turno_fijo": "Noche",
                         "dias_libres_fijos": [3, 4], "dias_libres_sustituto": [5, 6],
                         "sin_noches": False, "unico": True, "protegido": True, "borrable": False},
        "jefe":        {"label": "Jefe", "icon": "👑", "turno_fijo": "Mañana",
                         "dias_libres_fijos": [5, 6], "dias_libres_sustituto": None,
                         "sin_noches": True, "unico": True, "protegido": False, "borrable": True},
        "subjefe":     {"label": "Subjefe", "icon": "⭐", "turno_fijo": None,
                         "dias_libres_fijos": None, "dias_libres_sustituto": None,
                         "sin_noches": True, "unico": False, "protegido": False, "borrable": True},
        "adjunto_recepcion": {"label": "Adjunto Recepción", "icon": "◉", "turno_fijo": None,
                         "dias_libres_fijos": None, "dias_libres_sustituto": None,
                         "sin_noches": True, "unico": False, "protegido": False, "borrable": True},
        "normal":      {"label": "Empleado", "icon": "", "turno_fijo": None,
                         "dias_libres_fijos": None, "dias_libres_sustituto": None,
                         "sin_noches": False, "unico": False, "protegido": True, "borrable": False},
    },
    "mozos": {
        "night_fixed": {"label": "Turno Noche fijo", "icon": "★", "turno_fijo": "Noche",
                         "dias_libres_fijos": [4, 5], "dias_libres_sustituto": [0, 1],
                         "sin_noches": False, "unico": True, "protegido": True, "borrable": False},
        "normal":      {"label": "Mozo normal", "icon": "", "turno_fijo": None,
                         "dias_libres_fijos": None, "dias_libres_sustituto": None,
                         "sin_noches": False, "unico": False, "protegido": True, "borrable": False},
    },
}

TURNOS_VALIDOS_ROL = {"Mañana", "Tarde", "Noche", "Partido"}

def cargar_roles(dept_id: str = "recepcion") -> dict:
    """Carga la config de categorías del departamento. La crea con defaults si no existe."""
    import copy
    path  = _dept_file("roles.json", dept_id)
    datos = _cargar_json(path, None)
    if datos is not None:
        return datos
    roles = copy.deepcopy(ROLES_DEFAULT.get(dept_id, {}))
    guardar_roles(roles, dept_id)
    print(f"  ℹ Categorías {dept_id} inicializadas con datos por defecto.")
    return roles

def guardar_roles(roles: dict, dept_id: str = "recepcion"):
    _guardar_json(_dept_file("roles.json", dept_id), roles)

def _validar_dias(valor, campo: str) -> list[str]:
    if valor is None:
        return []
    if not isinstance(valor, list) or len(valor) != 2:
        return [f"{campo} debe ser exactamente 2 días, o vacío"]
    if not all(isinstance(d, int) and 0 <= d <= 6 for d in valor):
        return [f"{campo} debe contener índices de día 0-6"]
    return []

def validar_rol(rol_id: str, datos: dict, dept_id: str = "recepcion",
                es_nuevo: bool = False) -> list[str]:
    """Valida los campos de una categoría. Devuelve lista de errores (vacía = OK)."""
    errores = []
    if es_nuevo:
        if not rol_id or not all(c.isalnum() or c == "_" for c in rol_id):
            errores.append("El id de categoría solo puede tener letras, números y guion bajo")
        elif rol_id in cargar_roles(dept_id):
            errores.append(f"Ya existe una categoría con id '{rol_id}'")

    turno = datos.get("turno_fijo")
    if turno is not None and turno not in TURNOS_VALIDOS_ROL:
        errores.append(f"Turno fijo inválido: {turno}")

    errores += _validar_dias(datos.get("dias_libres_fijos"), "Los días libres fijos")

    if rol_id == "night_fixed":
        fijos = datos.get("dias_libres_fijos")
        if fijos and len(_validar_dias(fijos, "x")) == 0:
            a, b = sorted(fijos)
            if b - a != 1:
                errores.append("Los días libres fijos del turno noche deben ser dos días consecutivos")

    return errores

def _dias_sustituto_noche(dias_libres_fijos: list) -> list:
    """Los dos días siguientes a los días libres fijos del turno noche
    (con vuelta de semana, p.ej. fijos=Sáb+Dom -> Lun+Mar)."""
    if not dias_libres_fijos or len(dias_libres_fijos) != 2:
        return None
    b = max(dias_libres_fijos)
    return sorted({(b + 1) % 7, (b + 2) % 7})

def crear_rol(rol_id: str, datos: dict, dept_id: str = "recepcion") -> tuple[bool, str]:
    rol_id  = rol_id.strip().lower().replace(" ", "_")
    errores = validar_rol(rol_id, datos, dept_id, es_nuevo=True)
    if errores:
        return False, errores[0]
    label = (datos.get("label") or rol_id).strip()
    if not label:
        return False, "El nombre de la categoría es obligatorio"
    roles = cargar_roles(dept_id)
    roles[rol_id] = {
        "label": label,
        "icon":  (datos.get("icon") or "").strip(),
        "turno_fijo":            datos.get("turno_fijo"),
        "dias_libres_fijos":     datos.get("dias_libres_fijos"),
        "dias_libres_sustituto": datos.get("dias_libres_sustituto"),
        "sin_noches": bool(datos.get("sin_noches", False)),
        "unico":      bool(datos.get("unico", False)),
        "protegido":  False,
        "borrable":   True,
    }
    guardar_roles(roles, dept_id)
    return True, "Categoría creada"

def editar_rol(rol_id: str, datos: dict, dept_id: str = "recepcion") -> tuple[bool, str]:
    roles = cargar_roles(dept_id)
    if rol_id not in roles:
        return False, "Categoría no encontrada"
    actual = roles[rol_id]

    # night_fixed: el turno fijo siempre es Noche, no es editable; los días del
    # sustituto tampoco los elige el admin, se calculan a partir de los días
    # libres fijos (ver _dias_sustituto_noche).
    if rol_id == "night_fixed":
        fijos_efectivos = datos.get("dias_libres_fijos", actual.get("dias_libres_fijos"))
        datos = {**datos, "turno_fijo": "Noche",
                 "dias_libres_sustituto": _dias_sustituto_noche(fijos_efectivos)}
    # normal: rol genérico por defecto — no tiene sentido fijarle turno/días/unicidad.
    if rol_id == "normal":
        datos = {k: v for k, v in datos.items()
                 if k not in ("turno_fijo", "dias_libres_fijos", "unico")}

    candidato = {**actual, **datos}
    errores = validar_rol(rol_id, candidato, dept_id, es_nuevo=False)
    if errores:
        return False, errores[0]

    for campo in ("label", "icon", "turno_fijo", "dias_libres_fijos",
                  "dias_libres_sustituto", "sin_noches", "unico"):
        if campo in datos:
            actual[campo] = datos[campo]
    roles[rol_id] = actual
    guardar_roles(roles, dept_id)
    return True, "Categoría actualizada"

def eliminar_rol(rol_id: str, dept_id: str = "recepcion") -> tuple[bool, str]:
    roles = cargar_roles(dept_id)
    if rol_id not in roles:
        return False, "Categoría no encontrada"
    if not roles[rol_id].get("borrable", True):
        return False, "Esta categoría no se puede eliminar"
    staff  = cargar_plantilla(dept_id)
    en_uso = [p["name"] for p in staff if p["role"] == rol_id]
    if en_uso:
        nombres = ", ".join(en_uso[:3]) + ("..." if len(en_uso) > 3 else "")
        return False, f"No se puede eliminar: {len(en_uso)} empleado(s) tienen esta categoría ({nombres})"
    del roles[rol_id]
    guardar_roles(roles, dept_id)
    return True, "Categoría eliminada"


# ═══════════════════════════════════════════════════════════════════════════════
# MÓDULO — COBERTURA POR TEMPORADA
# Gestiona perfiles de min/max coverage (normal, alta, baja) por departamento.
# El fichero data/coverage_config.json se crea automáticamente si no existe,
# usando los valores actuales de cada departamento como perfil "normal".
# ═══════════════════════════════════════════════════════════════════════════════

COVERAGE_CONFIG_FILE = os.path.join(DATA_DIR, "coverage_config.json")

# Valores por defecto: los actuales de cada departamento pasan al perfil "normal"
COVERAGE_CONFIG_DEFAULT: dict = {
    "recepcion": {
        "perfiles": {
            "normal": {
                "min_coverage": {"Mañana": 2, "Tarde": 2, "Noche": 1, "Partido": 0},
                "max_coverage": {"Mañana": 3, "Tarde": 3, "Noche": 1, "Partido": 1},
            },
            "alta": {
                # Temporada alta: máximos más amplios para absorber plantillas mayores
                "min_coverage": {"Mañana": 2, "Tarde": 2, "Noche": 1, "Partido": 0},
                "max_coverage": {"Mañana": 4, "Tarde": 4, "Noche": 1, "Partido": 2},
            },
            "baja": {
                # Temporada baja: mínimo de Tarde reducido a 1 para equipos más pequeños
                "min_coverage": {"Mañana": 2, "Tarde": 1, "Noche": 1, "Partido": 0},
                "max_coverage": {"Mañana": 3, "Tarde": 3, "Noche": 1, "Partido": 1},
            },
        },
        "activo": "normal",
    },
    "mozos": {
        "perfiles": {
            "normal": {
                "min_coverage": {"Mañana": 1, "Tarde": 1, "Noche": 1, "Partido": 0},
                "max_coverage": {"Mañana": 1, "Tarde": 1, "Noche": 1, "Partido": 2},
            },
            "alta": {
                "min_coverage": {"Mañana": 1, "Tarde": 1, "Noche": 1, "Partido": 0},
                "max_coverage": {"Mañana": 2, "Tarde": 2, "Noche": 1, "Partido": 2},
            },
            "baja": {
                "min_coverage": {"Mañana": 1, "Tarde": 1, "Noche": 1, "Partido": 0},
                "max_coverage": {"Mañana": 1, "Tarde": 1, "Noche": 1, "Partido": 2},
            },
        },
        "activo": "normal",
    },
}


def cargar_coverage_config() -> dict:
    """Carga la configuración de cobertura por temporada desde disco.
    Si el fichero no existe lo crea con los valores por defecto (mismo patrón
    que el resto de ficheros data/). Fusiona los defaults para garantizar que
    siempre existen todos los departamentos y perfiles."""
    import copy as _copy

    if not os.path.exists(COVERAGE_CONFIG_FILE):
        guardar_json_atomico(COVERAGE_CONFIG_FILE, COVERAGE_CONFIG_DEFAULT)
        return _copy.deepcopy(COVERAGE_CONFIG_DEFAULT)

    data = _cargar_json(COVERAGE_CONFIG_FILE, None)
    if not isinstance(data, dict):
        return _copy.deepcopy(COVERAGE_CONFIG_DEFAULT)

    # Asegurar que todos los depts y perfiles existen aunque el fichero sea antiguo
    for dept, dept_default in COVERAGE_CONFIG_DEFAULT.items():
        dept_data = data.setdefault(dept, _copy.deepcopy(dept_default))
        dept_data.setdefault("activo", "normal")
        perfiles = dept_data.setdefault("perfiles", {})
        for nombre_perfil, vals in dept_default["perfiles"].items():
            perfiles.setdefault(nombre_perfil, _copy.deepcopy(vals))

    return data


def guardar_coverage_config(data: dict) -> None:
    """Guarda la config de cobertura de forma atómica."""
    guardar_json_atomico(COVERAGE_CONFIG_FILE, data)


def cargar_coverage_activa(dept_id: str) -> dict:
    """Devuelve {"min_coverage": {...}, "max_coverage": {...}} del perfil activo
    para el departamento dado. Nunca lanza excepción: cae al perfil 'normal'."""
    config   = cargar_coverage_config()
    dept_cfg = config.get(dept_id, COVERAGE_CONFIG_DEFAULT.get(dept_id, {}))
    activo   = dept_cfg.get("activo", "normal")
    perfiles = dept_cfg.get("perfiles", {})
    perfil   = perfiles.get(activo) or perfiles.get("normal") or next(iter(perfiles.values()), {})
    # Garantizar que min/max existen aunque el perfil esté incompleto
    fallback = COVERAGE_CONFIG_DEFAULT.get(dept_id, {}).get("perfiles", {}).get("normal", {})
    return {
        "min_coverage": perfil.get("min_coverage", fallback.get("min_coverage", {})),
        "max_coverage": perfil.get("max_coverage", fallback.get("max_coverage", {})),
    }


def validar_coverage_perfil(datos: dict) -> list[str]:
    """Valida un perfil de cobertura. Devuelve lista de errores (vacía = válido).
    Reglas: valores enteros no negativos, min <= max por turno, Noche >= 1."""
    errores: list[str] = []
    turnos = ["Mañana", "Tarde", "Noche", "Partido"]
    for campo in ("min_coverage", "max_coverage"):
        if campo not in datos:
            errores.append(f"Falta el campo '{campo}'")
            continue
        for turno in turnos:
            v = datos[campo].get(turno)
            if v is None:
                errores.append(f"{campo}['{turno}'] es obligatorio")
            elif not isinstance(v, int) or v < 0:
                errores.append(f"{campo}['{turno}'] debe ser un entero no negativo")
    if not errores:
        for turno in turnos:
            mn = datos["min_coverage"].get(turno, 0)
            mx = datos["max_coverage"].get(turno, 0)
            if mn > mx:
                errores.append(f"min > max para turno '{turno}' ({mn} > {mx})")
        if datos["min_coverage"].get("Noche", 0) < 1:
            errores.append("min_coverage['Noche'] debe ser al menos 1")
    return errores
