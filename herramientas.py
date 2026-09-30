"""
herramientas.py — Gestión de plantilla, peticiones semanales e historial.
Soporte multi-departamento: todas las funciones clave aceptan dept_id.
"""

import json
import os
import shutil
from datetime import date, timedelta, datetime, timezone

from modelo_base import DAYS

# ─── RUTAS DE DATOS ──────────────────────────────────────────────────────────

BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
DATA_DIR        = os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data"))
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

CARACTERES_PROHIBIDOS_NOMBRE = set('<>"\'`')

def _nombre_seguro(nombre: str) -> bool:
    """El nombre se muestra sin escapar en varias vistas del frontend (listas
    de plantilla, peticiones, vacaciones...) y también se interpola dentro de
    atributos onclick con comillas simples. Prohibir estos caracteres evita
    que se pueda inyectar HTML/JS a través del nombre de un empleado."""
    return not (CARACTERES_PROHIBIDOS_NOMBRE & set(nombre or ""))

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
        if not _nombre_seguro(p.get("name", "")):
            errores.append(
                f"El nombre '{p['name']}' contiene caracteres no permitidos "
                f"({CARACTERES_PROHIBIDOS_NOMBRE})."
            )
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
                                     min_workers_dia: int = 5,
                                     peticiones_semana: dict | None = None) -> tuple:
    """
    Aplica peticiones de libre respetando prioridad histórica.
    Si conceder una petición deja el día con menos de min_workers_dia disponibles,
    se deniega priorizando a quienes más peticiones ya llevan concedidas.
    Soporta los formatos de petición antiguo (lista) y nuevo (dict con 'dias' y 'la').
    Devuelve (staff_modificado, peticiones_denegadas).

    `peticiones_semana` permite evaluar una semana alternativa (clon con
    cambios aún sin guardar) sin tocar disco — lo usa cupos.analizar_peticion
    para la prevalidación, replicando EXACTAMENTE esta misma política.
    """
    import copy
    if peticiones_semana is None:
        peticiones = obtener_peticiones_semana(week_start, dept_id)
    else:
        peticiones = peticiones_semana
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

    # Preferencia del lado del día consecutivo del par de libres
    # ("antes"/"despues", elegida al crear la petición por fecha): se propaga
    # a la persona para que el motor CP-SAT la trate como preferencia BLANDA
    # (ver motor_cpsat, rama del par de la restricción #7). Solo aplica cuando
    # se concedió UNA única fecha esa semana: con dos o más el patrón ya queda
    # cubierto por ambas (sin exigir consecutividad) y el lado no significa
    # nada — el POST limpia la preferencia en ese caso, y aquí se comprueba de
    # nuevo por si la entrada viniera editada a mano.
    for nombre, entrada in peticiones.items():
        if not isinstance(entrada, dict):
            continue
        if entrada.get("estado", "activa") != "activa":
            continue
        pref = entrada.get("preferencia")
        if pref not in ("antes", "despues"):
            continue
        dias = [d for d in entrada.get("dias", []) if d in DIAS_SEMANA]
        if len(dias) != 1:
            continue
        dia = dias[0]
        if any(den["nombre"] == nombre and den["dia"] == dia for den in denegadas):
            continue
        for p in staff_copia:
            if p["name"] == nombre:
                p["preferencia_par"] = pref
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
            # Marca de origen para la capa de presentación: qué días de "Libre"
            # son en realidad vacaciones (el motor sigue usando solo "Libre").
            person["dias_vacaciones"] = dias_vac
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
            if turno not in ("Libre", "Fuera"):
                # Quien estaba "Fuera" (presencia parcial) no pisa el hotel
                # ese día — no se registra como festivo trabajado.
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
# - cubre_ausencia_de: id del rol cuya ausencia cubre este rol, o None. Solo un
#   rol puede cubrir la ausencia de otro rol dado (sin duplicados ni ciclos,
#   ver validar_rol). Ej: "subjefe" cubre_ausencia_de "jefe".
# - cobertura_semana_completa: bool, se configura en el rol CUBRIDOR. False
#   (opción A) = cobertura solo los días sueltos de ausencia del cubierto.
#   True (opción B) = además, si la ausencia es de semana completa, el
#   cubridor da presencia total (turno fijo del cubierto todos sus días de
#   trabajo, libres entre semana, nunca Sáb+Dom).
# - cubrir_dias_libres_fijos: bool, se configura en el rol CUBRIDOR y solo
#   tiene efecto si cubre_ausencia_de apunta a un rol. Si es True, los días
#   libres FIJOS del rol cubierto (cfg.fixed_days_off, normalmente el
#   finde del jefe) se añaden también como días a cubrir de forma
#   permanente, no solo las ausencias reales (vacaciones/peticiones). Es
#   independiente de cobertura_semana_completa: se pueden combinar.
# - excluir_de_equidad: si es True, este rol no entra en el balanceo de
#   fines de semana libres ni en el desempate por histórico de turnos.
# - dias_presencia: lista de 1-6 índices de día (0-6) o [] (sin presencia
#   parcial). Contrato especial de presencia parcial (p.ej. refuerzo de fin
#   de semana [5,6], cobertura de 3 días sueltos): en esos días la persona
#   trabaja SIEMPRE (solo una vacación/petición concedida puede librarlos) y
#   el resto de días queda en el estado "Fuera" del horario, que no computa
#   en nada (ni libres, ni equidad, ni cobertura, ni festivos trabajados).
#   Con presencia activa: sin_noches por defecto True (desmarcable, solo es
#   el default de creación) y excluir_de_equidad forzado a True. No se
#   combina con dias_libres_fijos ni se admite en roles protegidos.
# ═══════════════════════════════════════════════════════════════════════════════

ROLES_DEFAULT = {
    "recepcion": {
        "night_fixed": {"label": "Turno Noche fijo", "icon": "★", "turno_fijo": "Noche",
                         "dias_libres_fijos": [3, 4], "dias_libres_sustituto": [5, 6],
                         "sin_noches": False, "unico": True, "protegido": True, "borrable": False,
                         "cubre_ausencia_de": None, "cobertura_semana_completa": False, "cubrir_dias_libres_fijos": False,
                         "excluir_de_equidad": True},
        "jefe":        {"label": "Jefe", "icon": "👑", "turno_fijo": "Mañana",
                         "dias_libres_fijos": [5, 6], "dias_libres_sustituto": None,
                         "sin_noches": True, "unico": True, "protegido": False, "borrable": True,
                         "cubre_ausencia_de": None, "cobertura_semana_completa": False, "cubrir_dias_libres_fijos": False,
                         "excluir_de_equidad": True},
        "subjefe":     {"label": "Subjefe", "icon": "⭐", "turno_fijo": None,
                         "dias_libres_fijos": None, "dias_libres_sustituto": None,
                         "sin_noches": True, "unico": False, "protegido": False, "borrable": True,
                         "cubre_ausencia_de": "jefe", "cobertura_semana_completa": False, "cubrir_dias_libres_fijos": False,
                         "excluir_de_equidad": True},
        "adjunto_recepcion": {"label": "Adjunto Recepción", "icon": "◉", "turno_fijo": None,
                         "dias_libres_fijos": None, "dias_libres_sustituto": None,
                         "sin_noches": True, "unico": False, "protegido": False, "borrable": True,
                         "cubre_ausencia_de": None, "cobertura_semana_completa": False, "cubrir_dias_libres_fijos": False,
                         "excluir_de_equidad": False},
        "normal":      {"label": "Empleado", "icon": "", "turno_fijo": None,
                         "dias_libres_fijos": None, "dias_libres_sustituto": None,
                         "sin_noches": False, "unico": False, "protegido": True, "borrable": False,
                         "cubre_ausencia_de": None, "cobertura_semana_completa": False, "cubrir_dias_libres_fijos": False,
                         "excluir_de_equidad": False},
    },
    "mozos": {
        "night_fixed": {"label": "Turno Noche fijo", "icon": "★", "turno_fijo": "Noche",
                         "dias_libres_fijos": [4, 5], "dias_libres_sustituto": [0, 1],
                         "sin_noches": False, "unico": True, "protegido": True, "borrable": False,
                         "cubre_ausencia_de": None, "cobertura_semana_completa": False, "cubrir_dias_libres_fijos": False,
                         "excluir_de_equidad": True},
        "normal":      {"label": "Mozo normal", "icon": "", "turno_fijo": None,
                         "dias_libres_fijos": None, "dias_libres_sustituto": None,
                         "sin_noches": False, "unico": False, "protegido": True, "borrable": False,
                         "cubre_ausencia_de": None, "cobertura_semana_completa": False, "cubrir_dias_libres_fijos": False,
                         "excluir_de_equidad": False},
    },
}

TURNOS_VALIDOS_ROL = {"Mañana", "Tarde", "Noche", "Partido"}

_DEFAULTS_ROL = {
    "dias_presencia": [],
    "cubre_ausencia_de": None,
    "cobertura_semana_completa": False,
    "cubrir_dias_libres_fijos": False,
    "excluir_de_equidad": False,
}

def _completar_defaults_rol(r: dict) -> dict:
    """Retrocompatibilidad: roles.json guardados antes de añadir estos campos
    no los tienen. Se autocompletan con su default al cargar, sin reescribir
    el fichero (se persisten solos en cuanto se edite ese rol)."""
    for campo, default in _DEFAULTS_ROL.items():
        r.setdefault(campo, default)
    return r

def cargar_roles(dept_id: str = "recepcion") -> dict:
    """Carga la config de categorías del departamento. La crea con defaults si no existe."""
    import copy
    path  = _dept_file("roles.json", dept_id)
    datos = _cargar_json(path, None)
    if datos is not None:
        for r in datos.values():
            _completar_defaults_rol(r)
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

def _validar_presencia(valor) -> list[str]:
    """Valida dias_presencia: None/[] (sin presencia parcial) o lista de
    1-6 índices de día (0-6) sin duplicados."""
    if valor is None or valor == []:
        return []
    if not isinstance(valor, list):
        return ["Los días de presencia deben ser una lista de días, o vacío"]
    if not all(isinstance(d, int) and not isinstance(d, bool) and 0 <= d <= 6
               for d in valor):
        return ["Los días de presencia deben contener índices de día 0-6"]
    if len(valor) > 6:
        return ["Los días de presencia admiten como máximo 6 días (nunca los 7)"]
    if len(set(valor)) != len(valor):
        return ["Los días de presencia no pueden repetir días"]
    return []

def _normalizar_presencia(valor) -> list:
    """Normaliza dias_presencia a lista ordenada de índices (o [])."""
    if not valor:
        return []
    return sorted(set(valor))

def _tiene_ciclo_cobertura(rol_id: str, cubre_id: str, roles_todos: dict) -> bool:
    """True si, siguiendo la cadena cubre_ausencia_de a partir de cubre_id,
    se vuelve a llegar a rol_id (ciclo A cubre B, B cubre A, o cadenas más
    largas que cierran sobre sí mismas)."""
    visto   = set()
    actual  = cubre_id
    while actual is not None:
        if actual == rol_id:
            return True
        if actual in visto:
            break  # ciclo entre otros roles, no involucra a rol_id
        visto.add(actual)
        actual = roles_todos.get(actual, {}).get("cubre_ausencia_de")
    return False

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

    # — Presencia parcial (contrato especial) —
    errores += _validar_presencia(datos.get("dias_presencia"))
    if datos.get("dias_presencia"):
        if datos.get("dias_libres_fijos"):
            errores.append(
                "Una categoría con días de presencia no puede tener días libres fijos "
                "(los días fuera de presencia ya son libres de facto)"
            )
        if not es_nuevo and cargar_roles(dept_id).get(rol_id, {}).get("protegido"):
            errores.append("Los roles protegidos del sistema no admiten días de presencia")

    if rol_id == "night_fixed":
        fijos = datos.get("dias_libres_fijos")
        if fijos and len(_validar_dias(fijos, "x")) == 0:
            a, b = sorted(fijos)
            if b - a != 1:
                errores.append("Los días libres fijos del turno noche deben ser dos días consecutivos")

    # — Cobertura de ausencia de otro rol —
    cubre = datos.get("cubre_ausencia_de")
    if cubre:
        if cubre == rol_id:
            errores.append("Un rol no puede cubrir su propia ausencia")
        else:
            roles_todos = cargar_roles(dept_id)
            if cubre not in roles_todos:
                errores.append(f"El rol a cubrir '{cubre}' no existe")
            else:
                duplicado = next(
                    (rid for rid, r in roles_todos.items()
                     if rid != rol_id and r.get("cubre_ausencia_de") == cubre),
                    None,
                )
                if duplicado:
                    errores.append(
                        f"El rol '{cubre}' ya está cubierto por '{duplicado}': "
                        f"solo un rol puede cubrir la ausencia de otro"
                    )
                if _tiene_ciclo_cobertura(rol_id, cubre, roles_todos):
                    errores.append("La cobertura de ausencia no puede formar un ciclo entre roles")

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
    dias_presencia = _normalizar_presencia(datos.get("dias_presencia"))
    # Con presencia parcial, sin_noches solo se PREMARCA por defecto (el admin
    # puede desmarcarlo si el contrato concreto requiere cubrir noches); si
    # viene un valor explícito, se respeta. excluir_de_equidad sí queda
    # forzado: un refuerzo de 2 días no puede entrar en el balanceo de findes.
    sin_noches = datos.get("sin_noches")
    if sin_noches is None:
        sin_noches = bool(dias_presencia)
    roles[rol_id] = {
        "label": label,
        "icon":  (datos.get("icon") or "").strip(),
        "turno_fijo":            datos.get("turno_fijo"),
        "dias_libres_fijos":     datos.get("dias_libres_fijos"),
        "dias_libres_sustituto": datos.get("dias_libres_sustituto"),
        "dias_presencia":        dias_presencia,
        "sin_noches": bool(sin_noches),
        "unico":      bool(datos.get("unico", False)),
        "protegido":  False,
        "borrable":   True,
        "cubre_ausencia_de":          datos.get("cubre_ausencia_de"),
        "cobertura_semana_completa":  bool(datos.get("cobertura_semana_completa", False)),
        "cubrir_dias_libres_fijos":   bool(datos.get("cubrir_dias_libres_fijos", False)),
        "excluir_de_equidad":         True if dias_presencia else bool(datos.get("excluir_de_equidad", False)),
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
                 if k not in ("turno_fijo", "dias_libres_fijos", "unico", "dias_presencia")}

    candidato = {**actual, **datos}
    errores = validar_rol(rol_id, candidato, dept_id, es_nuevo=False)
    if errores:
        return False, errores[0]

    # Coherencia de presencia parcial (mismo contrato que crear_rol): con
    # días de presencia, la categoría queda excluida de la equidad.
    if candidato.get("dias_presencia"):
        datos["excluir_de_equidad"] = True

    for campo in ("label", "icon", "turno_fijo", "dias_libres_fijos",
                  "dias_libres_sustituto", "sin_noches", "unico",
                  "cubre_ausencia_de", "cobertura_semana_completa",
                  "cubrir_dias_libres_fijos", "excluir_de_equidad"):
        if campo in datos:
            actual[campo] = datos[campo]
    if "dias_presencia" in datos:
        actual["dias_presencia"] = _normalizar_presencia(datos["dias_presencia"])
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


# ═══════════════════════════════════════════════════════════════════════════════
# MÓDULO 7 — HORARIOS CONCRETOS POR TURNO
# ═══════════════════════════════════════════════════════════════════════════════
#
# Estructura de horarios_turnos.json (uno por departamento, mismo patrón que
# el resto de ficheros dept-scoped):
# {
#   "Mañana":  [{"inicio": "07:00", "fin": "15:00"}],
#   "Tarde":   [{"inicio": "15:00", "fin": "23:00"}],
#   "Noche":   [{"inicio": "23:00", "fin": "07:00"}],   ← cruza medianoche
#   "Partido": [{"inicio": "10:00", "fin": "13:00"}, {"inicio": "17:00", "fin": "20:00"}]
# }
# Cada turno tiene una lista de tramos horarios (Partido puede tener varios).
# Usado para mostrar el horario concreto del turno de hoy en el dashboard del
# empleado y para generar los eventos del export .ics.
# ═══════════════════════════════════════════════════════════════════════════════

HORARIOS_TURNOS_DEFAULT = {
    "Mañana":  [{"inicio": "07:00", "fin": "15:00"}],
    "Tarde":   [{"inicio": "15:00", "fin": "23:00"}],
    "Noche":   [{"inicio": "23:00", "fin": "07:00"}],
    # Partido es un turno CONTINUO de 10:00 a 18:00 (confirmado con el
    # usuario). Antes estaba guardado como dos tramos partidos
    # (10:00-13:00 y 17:00-20:00), que no es el horario real que se trabaja.
    # La variante excepcional de 12:00-20:00 no se modela: la lista de
    # tramos significa "un turno con varios tramos el mismo día", no
    # "varias variantes alternativas del turno", así que representarla
    # requeriría cambiar el esquema del fichero.
    "Partido": [{"inicio": "10:00", "fin": "18:00"}],
}

TURNOS_CON_HORARIO = ["Mañana", "Tarde", "Noche", "Partido"]


def cargar_horarios_turnos(dept_id: str = "recepcion") -> dict:
    """Carga los tramos horarios por turno. Si el fichero no existe se crea
    con los valores por defecto. Rellena turnos que falten con el default
    para que el fichero siempre tenga las 4 claves."""
    import copy as _copy
    path = _dept_file("horarios_turnos.json", dept_id)
    data = _cargar_json(path, None)
    if data is None:
        data = _copy.deepcopy(HORARIOS_TURNOS_DEFAULT)
        guardar_json_atomico(path, data)
        return data
    cambiado = False
    for turno, default in HORARIOS_TURNOS_DEFAULT.items():
        if turno not in data:
            data[turno] = _copy.deepcopy(default)
            cambiado = True
    if cambiado:
        guardar_json_atomico(path, data)
    return data


def guardar_horarios_turnos(data: dict, dept_id: str = "recepcion") -> None:
    guardar_json_atomico(_dept_file("horarios_turnos.json", dept_id), data)


def validar_horarios_turnos(data: dict) -> list[str]:
    """Valida el formato de horarios_turnos.json. Devuelve lista de errores
    (vacía = válido). No valida que inicio < fin porque los turnos de Noche
    cruzan la medianoche legítimamente."""
    import re
    errores: list[str] = []
    patron_hora = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
    for turno in TURNOS_CON_HORARIO:
        tramos = data.get(turno)
        if not isinstance(tramos, list) or not tramos:
            errores.append(f"'{turno}' debe tener al menos un tramo horario")
            continue
        for tramo in tramos:
            if not isinstance(tramo, dict):
                errores.append(f"Tramo inválido en '{turno}'")
                continue
            for campo in ("inicio", "fin"):
                v = tramo.get(campo)
                if not isinstance(v, str) or not patron_hora.match(v):
                    errores.append(f"'{turno}.{campo}' debe tener formato HH:MM (recibido: {v!r})")
    return errores


DESCANSO_MINIMO_HORAS = 9  # descanso mínimo entre el fin de un turno y el
# comienzo del turno del día siguiente. Regla de negocio (confirmada con el
# usuario), no una constante técnica: es lo que hace que "Mañana tras Tarde"
# esté prohibido (23:00 -> 07:00 son solo 8h) y lo que, con los horarios por
# defecto, prohíbe también Tarde y Partido el día después de una Noche.
#
# Hasta ahora esta regla vivía IMPLÍCITA y por duplicado en dos sitios que
# no se hablaban entre sí: modelo_base.NO_MORNING_AFTER (que solo conoce las
# transiciones que acaban en Mañana) y la restricción #3 de
# motor_cpsat._construir_modelo (que sí bloquea las cuatro). El resultado era
# que app._calcular_advertencias, apoyada en NO_MORNING_AFTER, no avisaba de
# Noche->Tarde (8h) ni de Noche->Partido (3h) cuando una edición manual las
# creaba. transiciones_prohibidas() es ahora la fuente única de la regla,
# derivada de los horarios realmente configurados.


def _minutos_del_dia(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _inicio_fin_turno(tramos: list) -> tuple:
    """(inicio, fin) de un turno en minutos desde las 00:00 del día en que
    EMPIEZA. El inicio es el del primer tramo y el fin el del último — para
    un turno partido (varios tramos) lo que importa de cara al descanso es
    cuándo entra por primera vez y cuándo sale por última.

    Un tramo cuyo `fin` no es posterior a su `inicio` cruza la medianoche
    (el caso de Noche, 23:00->07:00), así que su fin se expresa como
    minutos del día siguiente sumándole 24h — la misma convención que ya
    asume validar_horarios_turnos al no exigir inicio < fin."""
    inicios = [_minutos_del_dia(t["inicio"]) for t in tramos]
    fines = []
    for t in tramos:
        ini, fin = _minutos_del_dia(t["inicio"]), _minutos_del_dia(t["fin"])
        fines.append(fin + 24 * 60 if fin <= ini else fin)
    return min(inicios), max(fines)


def transiciones_prohibidas(horarios: dict = None) -> set:
    """Conjunto de pares (turno_del_día_anterior, turno_del_día_siguiente)
    cuyo descanso intermedio es MENOR que DESCANSO_MINIMO_HORAS.

    `horarios` con el formato de horarios_turnos.json; si es None se usan
    los valores por defecto (HORARIOS_TURNOS_DEFAULT) en vez de leer disco,
    para que quien solo quiera la tabla canónica no dependa de DATA_DIR.

    Con los horarios por defecto devuelve exactamente:
        (Tarde, Mañana)   8h
        (Noche, Mañana)   0h
        (Noche, Tarde)    8h
        (Noche, Partido)  3h
    que es justo lo que bloquea la restricción #3 de motor_cpsat (ver el
    test que ancla esa equivalencia para que no puedan divergir).
    """
    horarios = horarios if horarios is not None else HORARIOS_TURNOS_DEFAULT
    minimo = DESCANSO_MINIMO_HORAS * 60
    limites = {}
    for turno in TURNOS_CON_HORARIO:
        tramos = horarios.get(turno)
        if isinstance(tramos, list) and tramos:
            limites[turno] = _inicio_fin_turno(tramos)

    prohibidas = set()
    for previo, (_, fin_previo) in limites.items():
        for siguiente, (inicio_siguiente, _) in limites.items():
            # El turno siguiente empieza al día siguiente: +24h sobre el
            # mismo origen con el que se midió el fin del turno previo.
            descanso = (inicio_siguiente + 24 * 60) - fin_previo
            if descanso < minimo:
                prohibidas.add((previo, siguiente))
    return prohibidas


# ═══════════════════════════════════════════════════════════════════════════════
# MÓDULO 8 — DOTACIÓN ANUAL DE VACACIONES
# ═══════════════════════════════════════════════════════════════════════════════
#
# Estructura de vacaciones_dotacion.json (dept-scoped):
# {
#   "nombre_empleado": {"2026": 30, "2027": 30}   ← días naturales/año, editable
# }
# Si un empleado o año no tiene entrada explícita se usa DOTACION_VACACIONES_DEFAULT.
# "Vacaciones restantes del año" = dotación del año - días de vacaciones.json
# que caen dentro de ese año natural.
# ═══════════════════════════════════════════════════════════════════════════════

DOTACION_VACACIONES_DEFAULT = 30  # días naturales/año


def cargar_vacaciones_dotacion(dept_id: str = "recepcion") -> dict:
    return _cargar_json(_dept_file("vacaciones_dotacion.json", dept_id), {})


def guardar_vacaciones_dotacion(data: dict, dept_id: str = "recepcion") -> None:
    guardar_json_atomico(_dept_file("vacaciones_dotacion.json", dept_id), data)


def dotacion_vacaciones_de(nombre: str, anio: int, dept_id: str = "recepcion") -> int:
    """Dotación de días de vacaciones de un empleado para un año dado."""
    datos = cargar_vacaciones_dotacion(dept_id)
    valor = datos.get(nombre, {}).get(str(anio))
    return valor if isinstance(valor, int) and valor >= 0 else DOTACION_VACACIONES_DEFAULT


def dias_vacaciones_tomados_en_anio(nombre: str, anio: int, dept_id: str = "recepcion") -> int:
    """Cuenta los días naturales de periodos de vacaciones.json de 'nombre'
    que caen dentro del año natural indicado (recorta periodos a caballo)."""
    inicio_anio = date(anio, 1, 1)
    fin_anio    = date(anio, 12, 31)
    total = 0
    for v in cargar_vacaciones(dept_id):
        if v.get("nombre") != nombre:
            continue
        try:
            inicio = date.fromisoformat(v["inicio"])
            fin    = date.fromisoformat(v["fin"])
        except (KeyError, ValueError):
            continue
        solape_inicio = max(inicio, inicio_anio)
        solape_fin    = min(fin, fin_anio)
        if solape_inicio <= solape_fin:
            total += (solape_fin - solape_inicio).days + 1
    return total


# ═══════════════════════════════════════════════════════════════════════════════
# MÓDULO 9 — AUDITORÍA DE INTERVENCIONES MANUALES
# ═══════════════════════════════════════════════════════════════════════════════
#
# Registro estructurado e independiente del log operativo de actividad.json
# (que rota a 1000 entradas y usa texto libre). intervenciones.json guarda
# cada intervención manual del encargado/admin sobre el horario o la
# plantilla con campos estructurados, para poder analizarla en el panel de
# auditoría (solo accesible al rol admin). NO rota: es un registro de
# auditoría, no un log operativo — si algún día crece demasiado, se
# archivará por año, pero eso no se implementa aquí.
#
# Estructura de intervenciones.json: lista de objetos
# {
#   "ts":                "<ISO 8601 UTC>",
#   "usuario":            "<username que hizo la acción>",
#   "rol_usuario":         "<rol del usuario>",
#   "dept":               "recepcion" | "mozos",
#   "tipo":                "edicion_turno" | "peticion_jefe" | "cupo_extra" |
#                           "cambio_rol_plantilla",
#   "empleado_afectado":  "<nombre en plantilla>",
#   "semana":             "<YYYY-MM-DD del lunes>" | null,
#   "dia_idx":            <0-6> | null,
#   "valor_anterior":     "<str>" | null,
#   "valor_nuevo":        "<str>" | null,
#   "justificacion":      "<str>",
# }
# ═══════════════════════════════════════════════════════════════════════════════

INTERVENCIONES_FILE = os.path.join(DATA_DIR, "intervenciones.json")

TIPOS_INTERVENCION = {
    "edicion_turno", "peticion_jefe", "cupo_extra", "cambio_rol_plantilla",
}


def cargar_intervenciones(dept_id: str = None, desde: str = None, hasta: str = None) -> list[dict]:
    """Carga intervenciones.json, opcionalmente filtrado por departamento y
    por rango de fechas (desde/hasta en formato ISO "YYYY-MM-DD", ambos
    inclusive, comparados contra la fecha del campo "ts")."""
    intervenciones = _cargar_json(INTERVENCIONES_FILE, [])
    if dept_id:
        intervenciones = [i for i in intervenciones if i.get("dept") == dept_id]
    if desde:
        intervenciones = [i for i in intervenciones if i.get("ts", "")[:10] >= desde]
    if hasta:
        intervenciones = [i for i in intervenciones if i.get("ts", "")[:10] <= hasta]
    return intervenciones


def registrar_intervencion(*, usuario: str, rol_usuario: str, dept: str, tipo: str,
                           empleado_afectado: str, semana: str = None, dia_idx: int = None,
                           valor_anterior: str = None, valor_nuevo: str = None,
                           justificacion: str = "") -> None:
    """Añade una entrada al registro de auditoría. Escritura atómica, sin
    rotación (registro de auditoría, no log operativo)."""
    if tipo not in TIPOS_INTERVENCION:
        raise ValueError(f"Tipo de intervención desconocido: {tipo}")
    intervenciones = _cargar_json(INTERVENCIONES_FILE, [])
    intervenciones.append({
        "ts":                datetime.now(timezone.utc).isoformat(),
        "usuario":           usuario,
        "rol_usuario":       rol_usuario,
        "dept":              dept,
        "tipo":              tipo,
        "empleado_afectado": empleado_afectado,
        "semana":            semana,
        "dia_idx":           dia_idx,
        "valor_anterior":    valor_anterior,
        "valor_nuevo":       valor_nuevo,
        "justificacion":     justificacion or "",
    })
    guardar_json_atomico(INTERVENCIONES_FILE, intervenciones)


# ═══════════════════════════════════════════════════════════════════════════════
# MÓDULO 5 — INTERRUPTOR DEL MOTOR CP-SAT
# ═══════════════════════════════════════════════════════════════════════════════
#
# Apagado por defecto: generate_schedule_valido (modelo_base.py, heurístico)
# sigue siendo el motor en producción salvo que se active explícitamente.
# La variable de entorno, si está definida, tiene prioridad sobre el fichero
# de configuración persistida.
# ═══════════════════════════════════════════════════════════════════════════════

CONFIG_MOTOR_FILE = os.path.join(DATA_DIR, "config_motor.json")


def motor_cpsat_activo() -> bool:
    """True si el motor CP-SAT (motor_cpsat.py) debe usarse en vez del
    heurístico (modelo_base.py) para generar horarios."""
    env = os.environ.get("USAR_MOTOR_CPSAT")
    if env is not None:
        return env.strip().lower() in ("1", "true", "yes", "on")
    data = _cargar_json(CONFIG_MOTOR_FILE, {})
    return bool(data.get("activo", False))
