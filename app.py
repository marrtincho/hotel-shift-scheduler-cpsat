"""
app.py — Servidor Flask que conecta la interfaz web con modelo.py y herramientas.py
Ejecutar: python app.py
Abrir en el navegador: http://localhost:5000
"""

import copy
import os
import json
import re
import secrets
import uuid
from datetime import date, datetime, timedelta, timezone
from flask import Flask, jsonify, request, send_from_directory, session, Response

# ── Importar lógica del proyecto ─────────────────────────────────────────────
from modelo_base import DAYS, SHIFTS
from herramientas import (
    cargar_plantilla, guardar_plantilla, validar_plantilla,
    cargar_peticiones, guardar_peticiones,
    aplicar_peticiones, aplicar_peticiones_con_prioridad, obtener_peticiones_semana,
    obtener_prioridad_semana, guardar_prioridad_semana,
    cargar_vacaciones, guardar_vacaciones,
    aplicar_vacaciones, vacaciones_activas_en_semana,
    cargar_historial, guardar_historial, registrar_semana,
    registrar_festivos_semana, festivos_en_semana,
    cargar_festivos_trabajados,
    cargar_compensaciones, guardar_compensaciones,
    cargar_festivos, FESTIVOS_VALENCIA,
    MODALIDADES, BASE_DIR, DATA_DIR,
    cargar_roles, crear_rol, editar_rol, eliminar_rol,
    cargar_coverage_config, guardar_coverage_config,
    cargar_coverage_activa, validar_coverage_perfil,
    cargar_horarios_turnos, guardar_horarios_turnos, validar_horarios_turnos,
    TURNOS_CON_HORARIO,
    cargar_vacaciones_dotacion, guardar_vacaciones_dotacion,
    dotacion_vacaciones_de, dias_vacaciones_tomados_en_anio,
    DOTACION_VACACIONES_DEFAULT,
    _semana_str, DIAS_SEMANA,
    registrar_intervencion, cargar_intervenciones,
    motor_cpsat_activo, guardar_json_atomico,
)
from estadisticas import (
    invalidar_cache as invalidar_cache_estadisticas,
    estadisticas_de, resumen_periodo, equidad_de,
)
from auth import (
    inicializar_usuarios, autenticar, usuario_actual,
    login_usuario, logout_usuario, listar_usuarios,
    crear_usuario, editar_usuario, eliminar_usuario,
    login_requerido, permiso_requerido, tiene_permiso,
    registrar_accion, obtener_log,
    PERMISOS, ROLES_LABEL,
)
from cupos import (
    CUPO_MENSUAL_DEFAULT, cupo_total, cupo_usado, cupo_extra,
    cargar_cupos_extra, guardar_cupos_extra, validar_valor_cupo_extra,
    semana_generada, validar_nueva_peticion, peticiones_activas_de,
)

app = Flask(__name__, static_folder="static", static_url_path="/static")

# ── Clave secreta para firmar sesiones ────────────────────────────────────────
# Prioridad: variable de entorno SECRET_KEY > fichero persistido en disco.
# Sin SECRET_KEY en entorno, se genera una vez y se guarda en disco para que
# las sesiones sobrevivan reinicios.
_secret_file = os.path.join(DATA_DIR, ".secret_key")
os.makedirs(DATA_DIR, exist_ok=True)
_env_secret_key = os.environ.get("SECRET_KEY")
if _env_secret_key:
    app.secret_key = _env_secret_key
elif os.path.exists(_secret_file):
    with open(_secret_file, "rb") as _f:
        app.secret_key = _f.read()
else:
    app.secret_key = secrets.token_bytes(32)
    with open(_secret_file, "wb") as _f:
        _f.write(app.secret_key)
    # Restringir permisos: esta clave firma las sesiones de todos los
    # usuarios (incluido admin); si el fichero quedara legible por otros
    # usuarios del sistema, podrían falsificar cookies de sesión.
    os.chmod(_secret_file, 0o600)

app.config["SESSION_COOKIE_HTTPONLY"] = True   # JS no puede leer la cookie
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"  # protección CSRF básica

# Inicializar usuarios al arrancar (crea admin por defecto si no existe)
inicializar_usuarios()

# ─── HELPERS ─────────────────────────────────────────────────────────────────

def lunes_de(fecha_str: str) -> date:
    d = date.fromisoformat(fecha_str)
    return d - timedelta(days=d.weekday())

def _normalizar_dept(dept_id: str) -> str:
    """Único departamento válido si no es uno de los dos soportados: mismo
    fallback silencioso a 'recepcion' que usaban por separado todos los
    endpoints de horario/plantilla."""
    return dept_id if dept_id in ("recepcion", "mozos") else "recepcion"

def _night_covers_path(dept_id: str) -> str:
    return os.path.join(DATA_DIR, f"night_covers_{dept_id}.json")

def _leer_night_covers(dept_id: str) -> dict:
    """{semana_str: this_week_covers} de todas las semanas generadas de ese
    departamento -- ver api_generar sobre qué es this_week_covers."""
    path = _night_covers_path(dept_id)
    return json.loads(open(path, encoding="utf-8").read()) if os.path.exists(path) else {}

def _guardar_night_covers(dept_id: str, all_covers: dict) -> None:
    guardar_json_atomico(_night_covers_path(dept_id), all_covers)

_CAMPOS_INTERNOS_STAFF = {
    "days_off", "night_cover_this_week", "covered_nights_prev_week",
    "forced_days_off", "extra_days_off",
}

def _clean_staff_list(staff_list: list) -> list:
    """Quita del staff los campos de uso interno del motor de generación
    (días libres/forzados calculados) antes de devolverlo al frontend."""
    return [{k: v for k, v in p.items() if k not in _CAMPOS_INTERNOS_STAFF} for p in staff_list]

def _festivos_json(semana: date) -> list:
    """Festivos de la semana en el formato JSON que devuelven por igual
    api_generar, api_vaciar_horario y api_horario_semana."""
    return [
        {"fecha": f["date"].isoformat(), "nombre": f["nombre"],
         "tipo": f["tipo"], "dia_semana": f["dia_semana"]}
        for f in festivos_en_semana(semana)
    ]

def etiquetar_celda(nombre: str, dia_idx: int, valor_celda: str, dias_vacaciones_persona) -> str:
    """Capa de presentación: distingue "Libre" por vacaciones de cualquier
    otro "Libre" (petición, rotación, rol fijo) de cara al frontend, sin
    tocar el valor interno "Libre" que usa el motor de generación."""
    if valor_celda == "Libre" and DAYS[dia_idx] in (dias_vacaciones_persona or []):
        return "Vacaciones"
    return valor_celda

def etiquetar_schedule(schedule: dict, staff_list: list[dict]) -> dict:
    """Aplica etiquetar_celda a un schedule completo {nombre: [turnos]},
    usando la clave 'dias_vacaciones' de cada persona en staff_list (si no
    existe, p.ej. en semanas antiguas del historial, se trata como vacía)."""
    dias_vac_por_persona = {p["name"]: p.get("dias_vacaciones", []) for p in staff_list}
    return {
        nombre: [
            etiquetar_celda(nombre, i, turno, dias_vac_por_persona.get(nombre, []))
            for i, turno in enumerate(turnos)
        ]
        for nombre, turnos in schedule.items()
    }

def ok(data=None, **kwargs):
    payload = {"ok": True}
    if data is not None:
        payload["data"] = data
    payload.update(kwargs)
    return jsonify(payload)

def err(msg, code=400):
    return jsonify({"ok": False, "error": msg}), code

def get_dept() -> str:
    """Extract and validate dept_id from request args. Defaults to recepcion."""
    dept = request.args.get("dept", request.json.get("dept", "recepcion") if request.is_json else "recepcion")
    return dept if dept in ("recepcion", "mozos") else "recepcion"

def _validar_justificacion(texto) -> str | None:
    """Valida el texto de justificación obligatoria para intervenciones
    manuales sensibles (edición de turno, petición de origen jefe).
    Devuelve el mensaje de error en español, o None si es válida."""
    texto = (texto or "").strip()
    if not texto:
        return "La justificación es obligatoria: explica brevemente el motivo de esta intervención manual."
    if len(texto) < 5:
        return "La justificación es demasiado corta (mínimo 5 caracteres)."
    if len(texto) > 200:
        return "La justificación es demasiado larga (máximo 200 caracteres)."
    return None


# ─── AUTH — LOGIN / LOGOUT / SESIÓN ──────────────────────────────────────────

@app.route("/api/auth/login", methods=["POST"])
def api_login():
    body     = request.json or {}
    username = body.get("username", "").strip()
    password = body.get("password", "")
    if not username or not password:
        return err("Usuario y contraseña requeridos")
    usuario = autenticar(username, password)
    if not usuario:
        registrar_accion("LOGIN_FALLIDO", f"Usuario: {username}",
                         {"username": username, "rol": "—"})
        return err("Usuario o contraseña incorrectos", 401)
    login_usuario(usuario)
    registrar_accion("LOGIN", f"Sesión iniciada")
    return ok({
        "username": usuario["username"],
        "nombre":   usuario["nombre"],
        "rol":      usuario["rol"],
        "permisos": list(PERMISOS.get(usuario["rol"], set())),
        "vinculado": usuario.get("empleado_vinculado"),
    })

@app.route("/api/auth/logout", methods=["POST"])
def api_logout():
    registrar_accion("LOGOUT", "Sesión cerrada")
    logout_usuario()
    return ok(message="Sesión cerrada")

@app.route("/api/auth/me", methods=["GET"])
def api_me():
    u = usuario_actual()
    if not u:
        return jsonify({"ok": False, "error": "No autenticado", "login": True}), 401
    return ok({
        "username": u["username"],
        "nombre":   u["nombre"],
        "rol":      u["rol"],
        "permisos": list(PERMISOS.get(u["rol"], set())),
        "vinculado": u.get("vinculado"),
    })

@app.route("/api/auth/cambiar-password", methods=["POST"])
@login_requerido
def api_cambiar_password():
    body        = request.json or {}
    actual      = body.get("actual", "")
    nueva       = body.get("nueva", "")
    if not actual or not nueva:
        return err("Faltan campos")
    if len(nueva) < 6:
        return err("La contraseña debe tener al menos 6 caracteres")
    u = usuario_actual()
    # Verify current password
    from auth import autenticar as _auth
    if not _auth(u["username"], actual):
        return err("La contraseña actual es incorrecta")
    ok2, msg = editar_usuario(u["id"], {"password": nueva})
    if ok2:
        registrar_accion("CAMBIO_PASSWORD", "Contraseña propia cambiada")
    return ok(message=msg) if ok2 else err(msg)

# ─── ADMIN — GESTIÓN DE USUARIOS ─────────────────────────────────────────────

@app.route("/api/usuarios", methods=["GET"])
@permiso_requerido("gestionar_usuarios")
def api_get_usuarios():
    return ok(listar_usuarios())

@app.route("/api/usuarios", methods=["POST"])
@permiso_requerido("gestionar_usuarios")
def api_crear_usuario():
    body = request.json or {}
    username  = body.get("username", "").strip()
    nombre    = body.get("nombre", "").strip()
    rol       = body.get("rol", "empleado")
    password  = body.get("password", "")
    vinculado = body.get("empleado_vinculado")
    if not username or not nombre or not password:
        return err("Faltan campos obligatorios")
    if len(password) < 6:
        return err("La contraseña debe tener al menos 6 caracteres")
    ok2, msg = crear_usuario(username, nombre, rol, password, vinculado)
    if ok2:
        registrar_accion("CREAR_USUARIO", f"Usuario '{username}' ({rol}) creado")
    return ok(message=msg) if ok2 else err(msg)

@app.route("/api/usuarios/<uid>", methods=["PUT"])
@permiso_requerido("gestionar_usuarios")
def api_editar_usuario(uid):
    body = request.json or {}
    ok2, msg = editar_usuario(uid, body)
    if ok2:
        registrar_accion("EDITAR_USUARIO", f"Usuario ID {uid} modificado")
    return ok(message=msg) if ok2 else err(msg)

@app.route("/api/usuarios/<uid>", methods=["DELETE"])
@permiso_requerido("gestionar_usuarios")
def api_eliminar_usuario(uid):
    u = usuario_actual()
    if u and u["id"] == uid:
        return err("No puedes eliminarte a ti mismo")
    ok2, msg = eliminar_usuario(uid)
    if ok2:
        registrar_accion("ELIMINAR_USUARIO", f"Usuario ID {uid} eliminado")
    return ok(message=msg) if ok2 else err(msg)

# ─── LOG DE ACTIVIDAD ────────────────────────────────────────────────────────

@app.route("/api/log", methods=["GET"])
@permiso_requerido("gestionar_usuarios")
def api_get_log():
    limite = request.args.get("limite", 100, type=int)
    return ok(obtener_log(limite))

# ─── AUDITORÍA DE INTERVENCIONES MANUALES (solo admin) ───────────────────────

_TURNOS_TRABAJO = {"Mañana", "Tarde", "Noche", "Partido"}
_TURNOS_LIBRE   = {"Libre", "Vacaciones"}

def _clasificar_edicion_turno(valor_anterior: str, valor_nuevo: str) -> str:
    """Heurística orientativa (no un juicio): favorable si se quita un turno
    de trabajo y se pone libre; desfavorable si es al revés; neutra si es
    entre dos turnos de trabajo."""
    if valor_anterior in _TURNOS_TRABAJO and valor_nuevo in _TURNOS_LIBRE:
        return "favorable"
    if valor_anterior in _TURNOS_LIBRE and valor_nuevo in _TURNOS_TRABAJO:
        return "desfavorable"
    return "neutra"

def _delta_cupo_extra(interv: dict) -> int:
    """Extrae cuánto cupo extra se otorgó de más (o de menos) de una
    intervención tipo cupo_extra, a partir de valor_anterior/valor_nuevo
    (valor_nuevo tiene el formato "AAAA-MM: antes→después")."""
    try:
        antes = int(interv.get("valor_anterior") or 0)
    except (TypeError, ValueError):
        antes = 0
    m = re.search(r"→\s*(-?\d+)", interv.get("valor_nuevo") or "")
    despues = int(m.group(1)) if m else antes
    return despues - antes

def _semanas_presentes_por_empleado(dept_id: str, desde: str, hasta: str) -> dict:
    """Cuenta, para cada empleado, en cuántas semanas del periodo [desde,
    hasta] aparece en el staff del historial y trabajó (excluye semanas de
    vacaciones completas) — misma lógica que _get_weekend_ratios en
    modelo_base.py, acotada al periodo de la auditoría en vez de la ventana
    fija de 52 semanas."""
    historial = cargar_historial(dept_id)
    conteo: dict = {}
    for semana_str, entrada in historial.items():
        if semana_str < desde or semana_str > hasta:
            continue
        schedule_sem = entrada.get("schedule", {})
        for p in entrada.get("staff", []):
            nombre = p.get("name")
            turnos = schedule_sem.get(nombre, [])
            if len(turnos) < 7 or all(t == "Libre" for t in turnos):
                continue
            conteo[nombre] = conteo.get(nombre, 0) + 1
    return conteo

def _media_y_desviacion(valores: list) -> tuple:
    """Media y desviación típica (poblacional) de una lista de valores.
    Devuelve (0.0, 0.0) si la lista está vacía."""
    n = len(valores)
    if n == 0:
        return 0.0, 0.0
    media    = sum(valores) / n
    varianza = sum((v - media) ** 2 for v in valores) / n
    return media, varianza ** 0.5

def _resumen_auditoria_vacio(nombre: str) -> dict:
    return {
        "empleado": nombre,
        "ediciones_turno_favorables": 0,
        "ediciones_turno_desfavorables": 0,
        "ediciones_turno_neutras": 0,
        "peticiones_origen_jefe": 0,
        "cupo_extra_total_otorgado": 0,
        "cambios_de_rol": 0,
        "ediciones_en_finde": 0,
    }

@app.route("/api/auditoria/intervenciones", methods=["GET"])
@permiso_requerido("ver_auditoria")
def api_auditoria_intervenciones():
    """Panel de auditoría de intervenciones manuales (solo admin): detecta
    tratos desiguales del encargado sin impedirle trabajar — es un registro
    para inspección posterior, no una restricción en el momento de actuar."""
    dept_id = request.args.get("dept", "recepcion")
    dept_id = _normalizar_dept(dept_id)
    hoy   = date.today()
    hasta = request.args.get("hasta") or hoy.isoformat()
    desde = request.args.get("desde") or (hoy - timedelta(days=90)).isoformat()

    intervenciones = cargar_intervenciones(dept_id=dept_id, desde=desde, hasta=hasta)
    detalle = sorted(intervenciones, key=lambda i: i.get("ts", ""), reverse=True)

    staff   = cargar_plantilla(dept_id)
    resumen = {p["name"]: _resumen_auditoria_vacio(p["name"]) for p in staff}
    totales = _resumen_auditoria_vacio(None)
    del totales["empleado"]

    for i in intervenciones:
        fila = resumen.get(i.get("empleado_afectado"))
        tipo = i.get("tipo")
        if tipo == "edicion_turno":
            clase = _clasificar_edicion_turno(i.get("valor_anterior"), i.get("valor_nuevo"))
            clave = f"ediciones_turno_{clase}s"  # favorable→favorables, neutra→neutras...
            totales[clave] += 1
            if fila is not None:
                fila[clave] += 1
            if i.get("dia_idx") in (5, 6):
                totales["ediciones_en_finde"] += 1
                if fila is not None:
                    fila["ediciones_en_finde"] += 1
        elif tipo == "peticion_jefe":
            totales["peticiones_origen_jefe"] += 1
            if fila is not None:
                fila["peticiones_origen_jefe"] += 1
        elif tipo == "cupo_extra":
            delta = _delta_cupo_extra(i)
            totales["cupo_extra_total_otorgado"] += delta
            if fila is not None:
                fila["cupo_extra_total_otorgado"] += delta
        elif tipo == "cambio_rol_plantilla":
            totales["cambios_de_rol"] += 1
            if fila is not None:
                fila["cambios_de_rol"] += 1

    # Normalización por exposición: comparar totales absolutos entre alguien
    # con 30 semanas de plantilla y alguien con 6 produce falsos positivos,
    # así que se añade la tasa por semana presente (comparable entre ambos).
    semanas_presentes = _semanas_presentes_por_empleado(dept_id, desde, hasta)
    filas = list(resumen.values())
    for fila in filas:
        n = semanas_presentes.get(fila["empleado"], 0)
        divisor = max(n, 1)
        fila["semanas_presentes"]    = n
        fila["tasa_favorables"]      = fila["ediciones_turno_favorables"] / divisor
        fila["tasa_desfavorables"]   = fila["ediciones_turno_desfavorables"] / divisor
        fila["tasa_peticiones_jefe"] = fila["peticiones_origen_jefe"] / divisor

    # Referencia del equipo (media y desviación típica) para cada tasa,
    # excluyendo a quien lleva menos de 4 semanas presentes en el periodo
    # (muestra insuficiente: distorsionaría la referencia, no aporta señal).
    con_muestra = [f for f in filas if f["semanas_presentes"] >= 4]
    referencia_equipo = {
        campo: dict(zip(("media", "desviacion"),
                        _media_y_desviacion([f[campo] for f in con_muestra])))
        for campo in ("tasa_favorables", "tasa_desfavorables", "tasa_peticiones_jefe")
    }
    totales["referencia_equipo"] = referencia_equipo

    return ok({
        "dept": dept_id, "desde": desde, "hasta": hasta,
        "resumen_por_empleado": filas,
        "detalle": detalle,
        "totales_periodo": totales,
    })

# ─── SERVIR INTERFAZ ─────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")

# ═══════════════════════════════════════════════════════════════════════════════
# API — PLANTILLA
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/plantilla", methods=["GET"])
@login_requerido
def api_get_plantilla():
    dept_id = request.args.get("dept","recepcion")
    staff = cargar_plantilla(dept_id)
    comp  = cargar_compensaciones(dept_id)
    ft    = cargar_festivos_trabajados(dept_id)

    # Calcular festivos trabajados y saldo LA por empleado
    for p in staff:
        nombre = p["name"]
        p["festivos_trabajados"] = sum(
            1 for info in ft.values() if nombre in info.get("trabajadores", {})
        )
        c = comp.get(nombre, {})
        p["comp"]       = c.get("modalidad", "cobrar")
        p["la_saldo"]   = c.get("la_saldo", 0)
        p["año_comp"]   = c.get("año_modificacion", None)

    return ok(staff)

@app.route("/api/plantilla", methods=["POST"])
@permiso_requerido("editar_plantilla")
def api_add_empleado():
    body = request.json or {}
    dept_id = body.get("dept","recepcion")
    nombre = (body.get("name") or "").strip()
    if not nombre:
        return err("El nombre es obligatorio")
    staff = cargar_plantilla(dept_id)
    if any(p["name"].lower() == nombre.lower() for p in staff):
        return err("Ya existe un empleado con ese nombre")
    rol = body.get("role", "normal")
    if rol not in cargar_roles(dept_id):
        return err(f"Categoría de empleado inválida: {rol}")
    nuevo = {
        "name":        nombre,
        "role":        rol,
        "preferences": body.get("preferences", []),
    }
    staff.append(nuevo)
    errores = validar_plantilla(staff, dept_id=dept_id)
    if errores:
        return err(errores[0])
    guardar_plantilla(staff, dept_id)
    _guardar_comp_empleado(nombre, body.get("comp", "cobrar"), dept_id)
    return ok(message="Empleado añadido")

@app.route("/api/plantilla/<int:idx>", methods=["PUT"])
@permiso_requerido("editar_plantilla")
def api_edit_empleado(idx):
    body    = request.json or {}
    dept_id = body.get("dept","recepcion")
    staff   = cargar_plantilla(dept_id)
    if idx < 0 or idx >= len(staff):
        return err("Índice inválido", 404)
    import copy as cp
    original = cp.deepcopy(staff[idx])
    nombre = (body.get("name") or "").strip()
    if nombre:
        otros = {p["name"].lower() for i, p in enumerate(staff) if i != idx}
        if nombre.lower() in otros:
            return err("Ya existe un empleado con ese nombre")
        staff[idx]["name"] = nombre
    if "role" in body:
        if body["role"] not in cargar_roles(dept_id):
            return err(f"Categoría de empleado inválida: {body['role']}")
        staff[idx]["role"] = body["role"]
    if "preferences" in body:
        staff[idx]["preferences"] = body["preferences"]
    errores = validar_plantilla(staff, excluir_idx=idx, dept_id=dept_id)
    if errores:
        staff[idx] = original
        return err(errores[0])
    guardar_plantilla(staff, dept_id)
    if "comp" in body:
        _guardar_comp_empleado(staff[idx]["name"], body["comp"], dept_id)
    if "role" in body and body["role"] != original["role"]:
        u = usuario_actual()
        registrar_intervencion(
            usuario=u["username"], rol_usuario=u["rol"], dept=dept_id, tipo="cambio_rol_plantilla",
            empleado_afectado=staff[idx]["name"], valor_anterior=original["role"],
            valor_nuevo=body["role"], justificacion=(body.get("justificacion") or "").strip(),
        )
    return ok(message="Empleado actualizado")

@app.route("/api/plantilla/<int:idx>", methods=["DELETE"])
@permiso_requerido("editar_plantilla")
def api_delete_empleado(idx):
    dept_id = request.args.get("dept","recepcion")
    staff = cargar_plantilla(dept_id)
    if idx < 0 or idx >= len(staff):
        return err("Índice inválido", 404)
    nombre = staff.pop(idx)["name"]
    guardar_plantilla(staff, dept_id)
    return ok(message=f"'{nombre}' eliminado")

def _guardar_comp_empleado(nombre: str, modalidad: str, dept_id: str = "recepcion"):
    comp = cargar_compensaciones(dept_id)
    if nombre not in comp:
        comp[nombre] = {}
    comp[nombre]["modalidad"] = modalidad
    if "la_saldo" not in comp[nombre]:
        comp[nombre]["la_saldo"] = 0
    guardar_compensaciones(comp, dept_id)
    invalidar_cache_estadisticas(dept_id)

# ═══════════════════════════════════════════════════════════════════════════════
# API — CATEGORÍAS / ROLES DE EMPLEADO
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/roles", methods=["GET"])
@login_requerido
def api_get_roles():
    dept_id = request.args.get("dept", "recepcion")
    roles   = cargar_roles(dept_id)
    staff   = cargar_plantilla(dept_id)
    conteo  = {}
    for p in staff:
        conteo[p["role"]] = conteo.get(p["role"], 0) + 1
    return ok({rid: {**r, "en_uso": conteo.get(rid, 0)} for rid, r in roles.items()})

@app.route("/api/roles", methods=["POST"])
@permiso_requerido("gestionar_roles")
def api_crear_rol():
    body    = request.json or {}
    dept_id = body.get("dept", "recepcion")
    rol_id  = (body.get("id") or "").strip().lower().replace(" ", "_")
    if not rol_id:
        return err("El id de categoría es obligatorio")
    ok2, msg = crear_rol(rol_id, body, dept_id)
    if ok2:
        registrar_accion("CREAR_ROL", f"Dept:{dept_id} Rol:{rol_id}")
        return ok(message=msg)
    return err(msg)

@app.route("/api/roles/<rol_id>", methods=["PUT"])
@permiso_requerido("gestionar_roles")
def api_editar_rol(rol_id):
    body    = request.json or {}
    dept_id = body.get("dept", "recepcion")
    ok2, msg = editar_rol(rol_id, body, dept_id)
    if ok2:
        registrar_accion("EDITAR_ROL", f"Dept:{dept_id} Rol:{rol_id}")
        return ok(message=msg)
    return err(msg)

@app.route("/api/roles/<rol_id>", methods=["DELETE"])
@permiso_requerido("gestionar_roles")
def api_eliminar_rol(rol_id):
    dept_id  = request.args.get("dept", "recepcion")
    ok2, msg = eliminar_rol(rol_id, dept_id)
    if ok2:
        registrar_accion("ELIMINAR_ROL", f"Dept:{dept_id} Rol:{rol_id}")
        return ok(message=msg)
    return err(msg)

# ═══════════════════════════════════════════════════════════════════════════════
# API — HORARIO
# ═══════════════════════════════════════════════════════════════════════════════

def _get_dept_config(dept_id: str):
    """
    Devuelve el DeptConfig del departamento con tres capas de parches:
      1. Roles editables (fixed_roles, fixed_days_off, night_fixed_days_off…)
         desde data/roles*.json.
      2. min_coverage y max_coverage del perfil de temporada activo
         desde data/coverage_config.json.
    Los valores estáticos de modelo_recepcion/modelo_mozos actúan solo como
    fallback si los ficheros de datos no existen todavía.
    """
    if dept_id == "mozos":
        from modelo_mozos import MOZOS_CONFIG as base
    else:
        from modelo_recepcion import RECEPCION_CONFIG as base
    cfg = copy.deepcopy(base)

    # — Parche de roles editables —
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

    # — Parche de cobertura de ausencia entre roles (p.ej. subjefe → jefe) —
    cfg.cobertura_roles = {
        rid: {"cubre": r["cubre_ausencia_de"],
              "semana_completa": bool(r.get("cobertura_semana_completa", False)),
              "dias_libres_fijos": bool(r.get("cubrir_dias_libres_fijos", False))}
        for rid, r in roles.items() if r.get("cubre_ausencia_de")
    }
    cfg.roles_excluidos_equidad = {
        rid for rid, r in roles.items() if r.get("excluir_de_equidad")
    }

    # — Parche de cobertura por temporada —
    coverage = cargar_coverage_activa(dept_id)
    cfg.min_coverage = coverage["min_coverage"]
    cfg.max_coverage = coverage["max_coverage"]

    return cfg

def _semana_bloqueada(semana: date) -> bool:
    """Las semanas anteriores a la actual quedan fijas; solo el admin puede regenerarlas."""
    return semana < lunes_de(date.today().isoformat())

def _calcular_prev_last_shifts(schedule_semana_anterior: dict, staff_list: list) -> dict:
    """Para cada persona devuelve (último turno trabajado, si terminó librando).
    Itera el schedule del domingo al lunes. Usado para prev_last_shifts."""
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


def _estado_rotacion_inicial_cpsat(staff_list: list, cfg) -> dict:
    """Semilla de estado_rotacion (cobertura_nocturna.py) para el primer uso
    del motor CP-SAT en un departamento: sin esto, _elegir_orden_fijo revienta
    con 'orden está vacío' porque {} no trae ningún candidato. Mismo criterio
    de elegibilidad que seleccionar_candidato_cobertura_nocturna: fuera
    night_fixed (cubre sus propias noches) y los roles sin noche de cfg."""
    elegibles = [p["name"] for p in staff_list
                 if p.get("role") != "night_fixed" and p.get("role") not in cfg.no_night_roles]
    return {
        "orden": elegibles,
        "siguiente_turno": elegibles[0] if elegibles else None,
        "deudas": {n: 0 for n in elegibles},
        "usar_orden_fijo": True,
        "historico_balance": {},
        "ausencias_night_fixed_consecutivas": 0,
    }


def _generar_con_motor_cpsat(staff_list: list, week_start: date, cfg, dept_id: str,
                             prev_sunday_shifts: dict = None):
    """Adapta generar_semana_cpsat_con_cobertura_nocturna (motor_cpsat.py) a
    la misma tupla de retorno que generate_schedule_valido (modelo_base.py),
    para que el resto de generar_horario() no necesite saber qué motor se usó.

    El motor CP-SAT necesita estado propio entre semanas (estado_rotacion de
    la cobertura nocturna, noches_ultimos_5_dias para la restricción de
    racha, y forced_days_off_extra -- el equivalente CP-SAT de
    prev_week_covers/next_week_rest_days del heurístico) que no encaja en
    night_covers_{dept}.json (formato propio del heurístico) -- se persiste
    aparte en cpsat_estado_{dept}.json.
    """
    from motor_cpsat import generar_semana_cpsat_con_cobertura_nocturna

    estado_path = os.path.join(DATA_DIR, f"cpsat_estado_{dept_id}.json")
    estado_previo = json.loads(open(estado_path, encoding="utf-8").read()) \
        if os.path.exists(estado_path) else {}
    estado_rotacion       = estado_previo.get("estado_rotacion") \
        or _estado_rotacion_inicial_cpsat(staff_list, cfg)
    noches_ultimos_5_dias = estado_previo.get("noches_ultimos_5_dias", {})
    forced_extra          = estado_previo.get("forced_days_off_extra", {})

    for person in staff_list:
        dias = forced_extra.get(person["name"])
        if dias:
            existentes = person.get("forced_days_off", [])
            person["forced_days_off"] = list(dict.fromkeys(existentes + dias))

    resultado = generar_semana_cpsat_con_cobertura_nocturna(
        staff_list=staff_list,
        week_start=week_start,
        cfg=cfg,
        estado_rotacion=estado_rotacion,
        noches_ultimos_5_dias=noches_ultimos_5_dias,
        prev_sunday_shifts=prev_sunday_shifts,
    )

    guardar_json_atomico(estado_path, {
        "estado_rotacion":       resultado["estado_rotacion"],
        "noches_ultimos_5_dias": resultado["noches_ultimos_5_dias"],
        "forced_days_off_extra": resultado["forced_days_off_extra"],
    })

    return resultado["schedule"], staff_list, week_start, {}, {}, []


def _calcular_advertencias(full_schedule: dict, staff_final: list[dict], cfg,
                           prev_sunday_shifts: dict = None) -> list[dict]:
    """Fuente única de advertencias del horario, en formato estructurado para
    que el frontend pueda resaltar exactamente las celdas en conflicto.
    Cada advertencia: {"tipo", "mensaje", "celdas": [{"nombre","dia_idx"}, ...]}.
    Usada tanto tras generar como tras editar un turno a mano, para que
    ambos flujos produzcan el mismo formato."""
    advertencias = []

    # 1. Cobertura: min/max de personas por turno y día incumplido.
    for shift in ["Mañana", "Tarde", "Noche", "Partido"]:
        for day_idx, day in enumerate(DAYS):
            personas = [p for p in staff_final if full_schedule[p["name"]][day_idx] == shift]
            mn = cfg.min_coverage.get(shift, 0)
            mx = cfg.max_coverage.get(shift, 99)
            if not (mn <= len(personas) <= mx):
                advertencias.append({
                    "tipo": "cobertura",
                    "mensaje": f"{day} – {shift}: {len(personas)} (mín {mn}, máx {mx})",
                    "celdas": [{"nombre": p["name"], "dia_idx": day_idx} for p in personas],
                })

    # 2. Descanso: no Mañana tras Tarde/Noche, incluyendo el cruce con el
    # domingo de la semana anterior si se conoce (prev_sunday_shifts).
    from modelo_base import NO_MORNING_AFTER as NMA
    for p in staff_final:
        nombre = p["name"]
        turnos = full_schedule[nombre]
        for i in range(7):
            if i == 0:
                prev_shift = (prev_sunday_shifts or {}).get(nombre)
                if prev_shift is None or prev_shift not in NMA or turnos[0] != "Mañana":
                    continue
                # El domingo anterior pertenece a la semana pasada: no hay
                # celda que resaltar para él en esta parrilla, solo la actual.
                advertencias.append({
                    "tipo": "descanso",
                    "mensaje": f"{nombre}: Domingo anterior ({prev_shift}) → {DAYS[0]} (Mañana)",
                    "celdas": [{"nombre": nombre, "dia_idx": 0}],
                })
                continue
            prev_shift, curr = turnos[i - 1], turnos[i]
            if prev_shift in NMA and curr == "Mañana":
                advertencias.append({
                    "tipo": "descanso",
                    "mensaje": f"{nombre}: {DAYS[i-1]} ({prev_shift}) → {DAYS[i]} (Mañana)",
                    "celdas": [{"nombre": nombre, "dia_idx": i - 1},
                               {"nombre": nombre, "dia_idx": i}],
                })

    # 3. Celdas sin turno asignable.
    for p in staff_final:
        nombre = p["name"]
        for day_idx, turno in enumerate(full_schedule[nombre]):
            if turno == "SIN_ASIGNAR":
                advertencias.append({
                    "tipo": "sin_asignar",
                    "mensaje": f"{nombre}: {DAYS[day_idx]} sin turno asignable",
                    "celdas": [{"nombre": nombre, "dia_idx": day_idx}],
                })

    return advertencias

def _prev_sunday_shifts(dept_id: str, semana: date) -> dict:
    """Turno del domingo de la semana anterior por persona (si esa semana ya
    tiene horario), para detectar la regla no-Mañana-tras-Tarde/Noche cuando
    cruza el fin de semana anterior."""
    historial_prev = cargar_historial(dept_id)
    prev_key = _semana_str(semana - timedelta(weeks=1))
    prev_sched = historial_prev.get(prev_key, {}).get("schedule", {})
    return {nombre: turnos[6] for nombre, turnos in prev_sched.items() if len(turnos) == 7}

def _recalcular_la_desde_historial(dept_id: str = "recepcion"):
    from herramientas import cargar_historial, cargar_festivos
    historial = cargar_historial(dept_id)
    comp      = cargar_compensaciones(dept_id)
    staff     = cargar_plantilla(dept_id)
    for nombre in [p["name"] for p in staff]:
        if nombre in comp:
            comp[nombre]["la_saldo"] = 0
        else:
            comp[nombre] = {"modalidad": "cobrar", "la_saldo": 0}
    for semana_str, entrada in historial.items():
        semana      = date.fromisoformat(semana_str)
        schedule    = entrada.get("schedule", {})
        fest_fechas = {f["fecha"] for f in cargar_festivos(semana.year)}
        for i in range(7):
            dia_fecha = (semana + timedelta(days=i)).isoformat()
            if dia_fecha not in fest_fechas:
                continue
            for nombre, turnos in schedule.items():
                if len(turnos) > i and turnos[i] == "Libre":
                    if comp.get(nombre, {}).get("modalidad", "cobrar") == "cobrar":
                        comp[nombre]["la_saldo"] = comp[nombre].get("la_saldo", 0) + 1
    guardar_compensaciones(comp, dept_id)
    invalidar_cache_estadisticas(dept_id)

@app.route("/api/horario/generar", methods=["POST"])
@permiso_requerido("generar_horario")
def api_generar():
    body    = request.json or {}
    fecha   = body.get("semana", date.today().isoformat())
    dept_id = body.get("dept", "recepcion")
    dept_id = _normalizar_dept(dept_id)
    semana  = lunes_de(fecha)
    cfg     = _get_dept_config(dept_id)

    if _semana_bloqueada(semana) and usuario_actual()["rol"] != "admin":
        return err("Esta semana ya pasó y quedó fija. Solo un administrador puede modificarla.", 403)

    staff_base = cargar_plantilla(dept_id)
    staff = aplicar_vacaciones(staff_base, semana, dept_id)
    min_w = sum(cfg.min_coverage.values())
    staff, pets_denegadas = aplicar_peticiones_con_prioridad(staff, semana, dept_id, min_w)

    # Chequeo de viabilidad previo: evita intentar generar con plantilla
    # manifiestamente inviable (demasiado pocas o demasiadas personas activas).
    from modelo_base import validar_viabilidad
    viable, msg_viabilidad = validar_viabilidad(staff, cfg)
    if not viable:
        return err(msg_viabilidad, 422)

    # Quién cubrió noches la semana pasada y debe descansar esos días esta
    # semana: mozos siempre (Lun+Mar de la semana siguiente, por diseño) y
    # recepción cuando la compensación del sustituto cae tras el domingo
    # (p.ej. cubre Sáb+Dom, descansa Lun+Mar — de la semana que viene, no de
    # la misma).
    prev_key = _semana_str(semana - timedelta(weeks=1))
    prev_week_covers = _leer_night_covers(dept_id).get(prev_key, {})

    # Las reglas de descanso (no Mañana tras Tarde/Noche, descanso obligatorio
    # tras Noche) no se reinician al cambiar de semana: se mira qué turno hizo
    # cada persona el domingo de la semana anterior, si ese horario existe.
    historial_prev = cargar_historial(dept_id)
    prev_sem_key = _semana_str(semana - timedelta(weeks=1))
    prev_sunday_shifts = {}
    prev_last_shifts   = {}
    if prev_sem_key in historial_prev:
        prev_sched = historial_prev[prev_sem_key].get("schedule", {})
        prev_sunday_shifts = {nombre: turnos[6] for nombre, turnos in prev_sched.items() if len(turnos) == 7}
        prev_last_shifts   = _calcular_prev_last_shifts(prev_sched, staff)

    if motor_cpsat_activo():
        full_schedule, staff_final, ws_date, night_cover, this_week_covers, violaciones = \
            _generar_con_motor_cpsat(
                staff_list=copy.deepcopy(staff),
                week_start=semana,
                cfg=cfg,
                dept_id=dept_id,
                prev_sunday_shifts=prev_sunday_shifts,
            )
    else:
        from modelo_base import generate_schedule_valido
        full_schedule, staff_final, ws_date, night_cover, this_week_covers, violaciones = \
            generate_schedule_valido(
                staff_list=copy.deepcopy(staff),
                week_start=semana,
                cfg=cfg,
                prev_week_covers=prev_week_covers,
                prev_sunday_shifts=prev_sunday_shifts,
                prev_last_shifts=prev_last_shifts,
            )

    all_covers = _leer_night_covers(dept_id)
    all_covers[_semana_str(semana)] = this_week_covers
    _guardar_night_covers(dept_id, all_covers)

    # Se recalculan las advertencias en formato estructurado en vez de usar
    # `violaciones` (strings sueltos, uso interno de generate_schedule_valido
    # para su lógica de reintentos) para que el formato sea idéntico al de
    # los demás endpoints que devuelven "advertencias" al frontend.
    advertencias = _calcular_advertencias(full_schedule, staff_final, cfg, prev_sunday_shifts)

    festivos = _festivos_json(semana)

    registrar_semana(semana, full_schedule, staff_final, forzar=True, dept_id=dept_id)
    registrar_festivos_semana(semana, full_schedule, staff_final)
    _recalcular_la_desde_historial(dept_id)
    invalidar_cache_estadisticas(dept_id)
    if dept_id != "recepcion":
        # registrar_festivos_semana no acepta dept_id y siempre escribe en el
        # fichero de festivos trabajados de "recepcion"; invalidamos también
        # ese caché por seguridad hasta que se corrija upstream.
        invalidar_cache_estadisticas("recepcion")
    registrar_accion("GENERAR_HORARIO", f"Dept:{dept_id} Semana:{semana.isoformat()}")

    clean_staff = _clean_staff_list(staff_final)

    return ok({
        "semana": semana.isoformat(), "dept": dept_id,
        "schedule": etiquetar_schedule(
            {p["name"]: full_schedule[p["name"]] for p in staff_final}, staff_final),
        "staff": clean_staff,
        "advertencias": advertencias, "festivos": festivos, "dias": DAYS,
        "peticiones_denegadas": pets_denegadas,
    })

@app.route("/api/horario/vaciar", methods=["POST"])
@permiso_requerido("vaciar_horario_manual")
def api_vaciar_horario():
    """Deja el horario de la semana en blanco (todas las celdas SIN_ASIGNAR,
    sin pasar por el motor de generación) para que el admin las asigne a
    mano, celda a celda, con /api/horario/turno. A diferencia de
    api_generar, no ejecuta ningún solver -- solo prepara la plantilla
    (vacaciones, peticiones) y guarda un horario vacío en el historial."""
    body    = request.json or {}
    fecha   = body.get("semana", date.today().isoformat())
    dept_id = body.get("dept", "recepcion")
    dept_id = _normalizar_dept(dept_id)
    semana  = lunes_de(fecha)
    cfg     = _get_dept_config(dept_id)

    if _semana_bloqueada(semana) and usuario_actual()["rol"] != "admin":
        return err("Esta semana ya pasó y quedó fija. Solo un administrador puede modificarla.", 403)

    staff_base = cargar_plantilla(dept_id)
    staff = aplicar_vacaciones(staff_base, semana, dept_id)
    min_w = sum(cfg.min_coverage.values())
    staff, pets_denegadas = aplicar_peticiones_con_prioridad(staff, semana, dept_id, min_w)

    full_schedule = {p["name"]: ["SIN_ASIGNAR"] * 7 for p in staff}

    registrar_semana(semana, full_schedule, staff, forzar=True, dept_id=dept_id)
    _recalcular_la_desde_historial(dept_id)
    invalidar_cache_estadisticas(dept_id)
    if dept_id != "recepcion":
        invalidar_cache_estadisticas("recepcion")
    registrar_accion("VACIAR_HORARIO_MANUAL", f"Dept:{dept_id} Semana:{semana.isoformat()}")

    advertencias = _calcular_advertencias(full_schedule, staff, cfg, _prev_sunday_shifts(dept_id, semana))
    festivos = _festivos_json(semana)

    clean_staff = _clean_staff_list(staff)

    return ok({
        "semana": semana.isoformat(), "dept": dept_id,
        "schedule": etiquetar_schedule(full_schedule, staff),
        "staff": clean_staff,
        "advertencias": advertencias, "festivos": festivos, "dias": DAYS,
        "peticiones_denegadas": pets_denegadas,
    })

@app.route("/api/horario/semana", methods=["GET"])
@login_requerido
def api_horario_semana():
    """Devuelve el horario ya generado de una semana sin volver a generarlo."""
    dept_id = request.args.get("dept", "recepcion")
    dept_id = _normalizar_dept(dept_id)
    semana    = lunes_de(request.args.get("semana", date.today().isoformat()))
    historial = cargar_historial(dept_id)
    key       = _semana_str(semana)
    bloqueada = _semana_bloqueada(semana)

    if key not in historial:
        return ok({"existe": False, "semana": key, "dept": dept_id, "bloqueada": bloqueada})

    entrada      = historial[key]
    cfg          = _get_dept_config(dept_id)
    advertencias = _calcular_advertencias(entrada["schedule"], entrada["staff"], cfg,
                                          _prev_sunday_shifts(dept_id, semana))
    festivos = _festivos_json(semana)
    return ok({
        "existe": True, "semana": key, "dept": dept_id, "bloqueada": bloqueada,
        "schedule": etiquetar_schedule(entrada["schedule"], entrada["staff"]),
        "staff": entrada["staff"],
        "advertencias": advertencias, "festivos": festivos, "dias": DAYS,
    })

@app.route("/api/horario/semana", methods=["DELETE"])
@permiso_requerido("eliminar_horario_semana")
def api_eliminar_horario_semana():
    """Borra el horario generado de una semana, dejándola como si nunca se
    hubiera generado (vuelve al estado 'Aún no se ha generado')."""
    dept_id = request.args.get("dept", "recepcion")
    dept_id = _normalizar_dept(dept_id)
    semana = lunes_de(request.args.get("semana", date.today().isoformat()))

    if _semana_bloqueada(semana) and usuario_actual()["rol"] != "admin":
        return err("Esta semana ya pasó y quedó fija. Solo un administrador puede modificarla.", 403)

    historial = cargar_historial(dept_id)
    key = _semana_str(semana)
    if key not in historial:
        return err("Esta semana no tiene horario generado", 404)

    del historial[key]
    guardar_historial(historial, dept_id)

    # Compensación de cobertura nocturna que esta semana le debía a la
    # siguiente (this_week_covers, ver api_generar): si se borra el
    # horario que la generó, no debe seguir aplicándose.
    all_covers = _leer_night_covers(dept_id)
    if all_covers.pop(key, None) is not None:
        _guardar_night_covers(dept_id, all_covers)

    _recalcular_la_desde_historial(dept_id)
    invalidar_cache_estadisticas(dept_id)
    if dept_id != "recepcion":
        invalidar_cache_estadisticas("recepcion")
    registrar_accion("ELIMINAR_HORARIO", f"Dept:{dept_id} Semana:{key}")

    return ok({"semana": key, "dept": dept_id, "existe": False})

@app.route("/api/horario/turno", methods=["PUT"])
@permiso_requerido("generar_horario")
def api_editar_turno():
    """Modifica manualmente el turno de una persona un día concreto en un horario ya generado."""
    body    = request.json or {}
    dept_id = body.get("dept", "recepcion")
    dept_id = _normalizar_dept(dept_id)
    nombre  = body.get("nombre")
    dia_idx = body.get("dia_idx")
    turno   = body.get("turno")
    justificacion = (body.get("justificacion") or "").strip()
    semana  = lunes_de(body.get("semana", date.today().isoformat()))

    if _semana_bloqueada(semana) and usuario_actual()["rol"] != "admin":
        return err("Esta semana ya pasó y quedó fija. Solo un administrador puede modificarla.", 403)
    if not isinstance(dia_idx, int) or not (0 <= dia_idx <= 6):
        return err("Día inválido")
    if turno not in SHIFTS:
        return err(f"Turno inválido: {turno}")
    error_justificacion = _validar_justificacion(justificacion)
    if error_justificacion:
        return err(error_justificacion)

    historial = cargar_historial(dept_id)
    key = _semana_str(semana)
    if key not in historial:
        return err("Esta semana no tiene horario generado", 404)
    entrada = historial[key]
    if nombre not in entrada["schedule"]:
        return err("Empleado no encontrado en esta semana")

    valor_anterior = entrada["schedule"][nombre][dia_idx]
    entrada["schedule"][nombre][dia_idx] = turno
    guardar_historial(historial, dept_id)
    _recalcular_la_desde_historial(dept_id)
    registrar_accion("EDITAR_TURNO", f"Dept:{dept_id} Semana:{key} {nombre} {DAYS[dia_idx]}->{turno}")
    u = usuario_actual()
    registrar_intervencion(
        usuario=u["username"], rol_usuario=u["rol"], dept=dept_id, tipo="edicion_turno",
        empleado_afectado=nombre, semana=key, dia_idx=dia_idx,
        valor_anterior=valor_anterior, valor_nuevo=turno, justificacion=justificacion,
    )

    cfg = _get_dept_config(dept_id)
    advertencias = _calcular_advertencias(entrada["schedule"], entrada["staff"], cfg,
                                          _prev_sunday_shifts(dept_id, semana))
    return ok({
        "semana": key, "dept": dept_id,
        "schedule": etiquetar_schedule(entrada["schedule"], entrada["staff"]),
        "staff": entrada["staff"],
        "advertencias": advertencias, "dias": DAYS,
    })

def _extra_dept_section(key: str, semana: date, dept_excluido: str) -> list:
    """Devuelve la lista extra_sections con el otro departamento si tiene
    horario generado para esa semana, vacía en caso contrario."""
    otro = "mozos" if dept_excluido == "recepcion" else "recepcion"
    hist_otro = cargar_historial(otro)
    if key not in hist_otro:
        return []
    e = hist_otro[key]
    cfg_otro   = _get_dept_config(otro)
    icons_otro = {rid: r.get("icon", "") for rid, r in cargar_roles(otro).items()}
    return [(e["schedule"], e["staff"], cfg_otro, icons_otro)]

@app.route("/api/horario/exportar", methods=["POST"])
@permiso_requerido("exportar_horario")
def api_exportar():
    """Exporta el horario vigente (vista "Horario semanal"). Antes escribía
    el .xlsx en el disco del servidor y solo devolvía la ruta — inútil para
    cualquier usuario que no tenga acceso al filesystem del servidor (todo
    el mundo, en un despliegue remoto tras Cloudflare Tunnel). Ahora envía
    el fichero directamente como descarga, igual que
    /api/horario/exportar-semana."""
    from flask import send_file
    body    = request.json or {}
    dept_id = body.get("dept", "recepcion")
    semana  = lunes_de(body.get("semana", date.today().isoformat()))
    cfg     = _get_dept_config(dept_id)
    historial = cargar_historial(dept_id)
    key = _semana_str(semana)
    if key not in historial:
        return err("Genera el horario antes de exportar")
    entrada = historial[key]
    ruta    = os.path.join(BASE_DIR, f"horario_{key}.xlsx")
    from modelo_base import export_to_excel as exp_xl
    icons = {rid: r.get("icon", "") for rid, r in cargar_roles(dept_id).items()}
    extra = _extra_dept_section(key, semana, dept_id)
    exp_xl(entrada["schedule"], entrada["staff"], semana, ruta, cfg,
           role_icons=icons, extra_sections=extra)
    return send_file(ruta, as_attachment=True,
                     download_name=f"horario_{key}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

@app.route("/api/horario/semanas", methods=["GET"])
@login_requerido
def api_semanas():
    dept_id   = request.args.get("dept", "recepcion")
    historial = cargar_historial(dept_id)
    resultado = []
    for key in sorted(historial.keys(), reverse=True):
        entrada = historial[key]
        semana  = date.fromisoformat(key)
        fin     = semana + timedelta(days=6)
        resultado.append({
            "semana": key, "dept": dept_id,
            "label":  f"{semana.strftime('%d/%m')} – {fin.strftime('%d/%m/%Y')}",
            "schedule": etiquetar_schedule(entrada["schedule"], entrada["staff"]),
            "staff": entrada["staff"],
        })
    return ok(resultado)

@app.route("/api/horario/exportar-semana/<semana_str>", methods=["GET"])
@permiso_requerido("exportar_horario")
def api_exportar_semana(semana_str):
    from flask import send_file
    dept_id   = request.args.get("dept", "recepcion")
    cfg       = _get_dept_config(dept_id)
    historial = cargar_historial(dept_id)
    if semana_str not in historial:
        return err("Semana no encontrada", 404)
    entrada = historial[semana_str]
    semana  = date.fromisoformat(semana_str)
    ruta    = os.path.join(BASE_DIR, f"horario_{semana_str}.xlsx")
    from modelo_base import export_to_excel as exp_xl
    icons = {rid: r.get("icon", "") for rid, r in cargar_roles(dept_id).items()}
    extra = _extra_dept_section(semana_str, semana, dept_id)
    exp_xl(entrada["schedule"], entrada["staff"], semana, ruta, cfg,
           role_icons=icons, extra_sections=extra)
    return send_file(ruta, as_attachment=True,
                     download_name=f"horario_{semana_str}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

# ═══════════════════════════════════════════════════════════════════════════════
# API — PETICIONES
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/peticiones", methods=["GET"])
@login_requerido
def api_get_peticiones():
    fecha   = request.args.get("semana", date.today().isoformat())
    dept_id = request.args.get("dept","recepcion")
    semana  = lunes_de(fecha)
    u       = usuario_actual()
    raw     = obtener_peticiones_semana(semana, dept_id)
    result  = {}
    for nombre, entrada in raw.items():
        if isinstance(entrada, list):
            entrada = {"dias": entrada, "la": []}
        if not isinstance(entrada, dict):
            continue
        if entrada.get("estado", "activa") != "activa":
            continue
        result[nombre] = {
            "dias": entrada.get("dias",[]), "la": entrada.get("la",[]),
            "motivo": entrada.get("motivo") or None,
            "origen": entrada.get("origen", "jefe"),
            "creada_en": entrada.get("creada_en"),
        }
    if u and u.get("rol") == "empleado":
        vinculado = u.get("vinculado")
        result = {k: v for k, v in result.items() if k == vinculado} if vinculado else {}
        return ok(result)
    priority_order = obtener_prioridad_semana(semana, dept_id)
    return ok(result, priority_order=priority_order)

@app.route("/api/peticiones", methods=["POST"])
@login_requerido
def api_add_peticion():
    u       = usuario_actual()
    es_propia = tiene_permiso("editar_peticiones_propias") and not tiene_permiso("editar_peticiones")
    if not tiene_permiso("editar_peticiones") and not tiene_permiso("editar_peticiones_propias"):
        return err("Sin permisos", 403)
    body    = request.json or {}
    fecha   = body.get("semana")
    nombre  = body.get("nombre","").strip()
    dias    = body.get("dias",[])
    dept_id = body.get("dept","recepcion")
    motivo  = (body.get("motivo") or "").strip() or None
    justificacion = (body.get("justificacion") or "").strip()
    if not fecha or not nombre or not dias:
        return err("Faltan datos")
    if es_propia:
        vinculado = u.get("vinculado")
        if not vinculado:
            return err("Tu cuenta no está vinculada a ningún empleado de la plantilla", 403)
        if nombre != vinculado:
            return err("Solo puedes crear peticiones para ti mismo", 403)
    else:
        # Petición creada por encargado/admin en nombre de otro empleado
        # (origen "jefe"): requiere justificación, a diferencia de una
        # petición propia del empleado.
        error_justificacion = _validar_justificacion(justificacion)
        if error_justificacion:
            return err(error_justificacion)
    semana      = lunes_de(fecha)
    rol_usuario = "empleado" if es_propia else u["rol"]
    ok_cupo, msg_cupo = validar_nueva_peticion(nombre, semana, dias, rol_usuario, dept_id)
    if not ok_cupo:
        return err(msg_cupo)
    pet    = cargar_peticiones(dept_id)
    key    = _semana_str(semana)
    if key not in pet:
        pet[key] = {}
    existing = pet[key].get(nombre, {})
    if isinstance(existing, list):
        existing = {"dias": existing, "la": []}
    existing["dias"] = dias
    if motivo is not None:
        existing["motivo"] = motivo
    elif "motivo" in existing:
        del existing["motivo"]
    existing["origen"] = "empleado" if rol_usuario == "empleado" else "jefe"
    existing["estado"] = "activa"
    existing.setdefault("creada_en", date.today().isoformat())
    pet[key][nombre] = existing
    guardar_peticiones(pet, dept_id)
    invalidar_cache_estadisticas(dept_id)
    registrar_accion("CREAR_PETICION", f"Dept:{dept_id} {nombre} semana {key} ({existing['origen']})")
    if existing["origen"] == "jefe":
        registrar_intervencion(
            usuario=u["username"], rol_usuario=u["rol"], dept=dept_id, tipo="peticion_jefe",
            empleado_afectado=nombre, semana=key, valor_nuevo=", ".join(dias),
            justificacion=justificacion,
        )
    return ok(message="Petición guardada")

# ═══════════════════════════════════════════════════════════════════════════════
# API — PETICIONES (cancelar / modificar individual)
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/peticiones/<semana>/<nombre>", methods=["DELETE"])
@login_requerido
def api_delete_peticion(semana, nombre):
    """Cancela una petición activa (estado -> 'cancelada'). Nunca se borra
    físicamente, para conservar el histórico a efectos de cupo y auditoría."""
    u = usuario_actual()
    es_propia = tiene_permiso("editar_peticiones_propias") and not tiene_permiso("editar_peticiones")
    if not tiene_permiso("editar_peticiones") and not tiene_permiso("editar_peticiones_propias"):
        return err("Sin permisos", 403)
    if es_propia and nombre != u.get("vinculado"):
        return err("Solo puedes cancelar tus propias peticiones", 403)
    dept_id  = request.args.get("dept","recepcion")
    semana_d = lunes_de(semana)
    if semana_generada(semana_d, dept_id):
        return err("Esa semana ya está publicada; habla con el encargado para un cambio manual.")
    pet      = cargar_peticiones(dept_id)
    key = _semana_str(semana_d)
    if key not in pet or nombre not in pet[key]:
        return err("Petición no encontrada", 404)
    entrada = pet[key][nombre]
    if isinstance(entrada, list):
        entrada = {"dias": entrada, "la": []}
    entrada["estado"] = "cancelada"
    pet[key][nombre] = entrada
    guardar_peticiones(pet, dept_id)
    invalidar_cache_estadisticas(dept_id)
    registrar_accion("CANCELAR_PETICION", f"Dept:{dept_id} {nombre} semana {key}")
    return ok(message="Petición cancelada")

@app.route("/api/peticiones/<semana>/<nombre>", methods=["PUT"])
@permiso_requerido("editar_peticiones")
def api_modificar_peticion(semana, nombre):
    """Modifica días y/o el empleado de una petición existente. Solo jefe/admin.
    Al pasar por gestión manual, la petición queda marcada origen='jefe'."""
    body     = request.json or {}
    dept_id  = body.get("dept","recepcion")
    semana_d = lunes_de(semana)
    pet      = cargar_peticiones(dept_id)
    key      = _semana_str(semana_d)
    if key not in pet or nombre not in pet[key]:
        return err("Petición no encontrada", 404)
    if semana_generada(semana_d, dept_id):
        return err("Esa semana ya está publicada; habla con el encargado para un cambio manual.")

    entrada = pet[key][nombre]
    if isinstance(entrada, list):
        entrada = {"dias": entrada, "la": []}

    if "dias" in body:
        dias_nuevos = body.get("dias") or []
        if not dias_nuevos:
            return err("Debes indicar al menos un día")
        for dia in dias_nuevos:
            if dia not in DIAS_SEMANA:
                return err(f"Día inválido: {dia}")
        entrada["dias"] = dias_nuevos
    if "motivo" in body:
        motivo = (body.get("motivo") or "").strip() or None
        if motivo is not None:
            entrada["motivo"] = motivo
        elif "motivo" in entrada:
            del entrada["motivo"]

    nuevo_nombre = (body.get("nombre") or "").strip() or nombre
    entrada["origen"] = "jefe"
    del pet[key][nombre]
    pet[key][nuevo_nombre] = entrada
    guardar_peticiones(pet, dept_id)
    invalidar_cache_estadisticas(dept_id)
    registrar_accion("MODIFICAR_PETICION", f"Dept:{dept_id} {nombre}->{nuevo_nombre} semana {key}")
    return ok(message="Petición modificada")

@app.route("/api/peticiones/<semana>/prioridad", methods=["PUT"])
@permiso_requerido("editar_peticiones")
def api_set_prioridad_peticiones(semana):
    body    = request.json or {}
    dept_id = body.get("dept","recepcion")
    order   = body.get("order")   # lista de nombres o null para restablecer automático
    if order is not None and not isinstance(order, list):
        return err("order debe ser una lista o null")
    guardar_prioridad_semana(lunes_de(semana), order, dept_id)
    msg = "Prioridad manual guardada" if order else "Prioridad restablecida a automática"
    return ok(message=msg)

# ═══════════════════════════════════════════════════════════════════════════════
# API — CUPOS DE PETICIONES
# ═══════════════════════════════════════════════════════════════════════════════

def _mes_actual_y_siguiente() -> tuple:
    hoy = date.today()
    if hoy.month == 12:
        siguiente = date(hoy.year + 1, 1, 1)
    else:
        siguiente = date(hoy.year, hoy.month + 1, 1)
    return hoy.strftime("%Y-%m"), siguiente.strftime("%Y-%m")

@app.route("/api/cupos/resumen", methods=["GET"])
@login_requerido
def api_cupos_resumen():
    """Transparencia de cupos: para cada empleado, mes actual y siguiente,
    peticiones usadas/cupo total y lista de peticiones activas.
    Visible para cualquier usuario autenticado."""
    dept_id = request.args.get("dept","recepcion")
    staff   = cargar_plantilla(dept_id)
    mes_actual, mes_siguiente = _mes_actual_y_siguiente()
    resultado = []
    for p in staff:
        nombre = p["name"]
        fila = {"nombre": nombre}
        for etiqueta, mes in (("mes_actual", mes_actual), ("mes_siguiente", mes_siguiente)):
            fila[etiqueta] = {
                "mes":    mes,
                "usadas": cupo_usado(nombre, mes, dept_id),
                "total":  cupo_total(nombre, mes),
            }
        fila["activas"] = peticiones_activas_de(nombre, dept_id)
        resultado.append(fila)
    return ok(resultado)

@app.route("/api/cupos/mio", methods=["GET"])
@login_requerido
def api_cupos_mio():
    """Cupo del empleado vinculado al usuario actual, para el mes de la semana
    indicada. Alimenta el aviso 'Peticiones <mes>: X de Y usadas' del
    formulario de petición del empleado."""
    u = usuario_actual()
    vinculado = u.get("vinculado")
    if not vinculado:
        return err("Tu cuenta no está vinculada a ningún empleado de la plantilla", 403)
    dept_id = request.args.get("dept","recepcion")
    fecha   = request.args.get("semana", date.today().isoformat())
    semana  = lunes_de(fecha)
    mes     = semana.strftime("%Y-%m")
    return ok({
        "nombre": vinculado, "mes": mes,
        "usadas": cupo_usado(vinculado, mes, dept_id),
        "total":  cupo_total(vinculado, mes),
    })

def _dept_y_rol_de(nombre: str) -> tuple:
    """Busca en qué departamento está un empleado por nombre. Devuelve
    (dept_id, role) o (None, None) si no está en ninguna plantilla."""
    for d in ("recepcion", "mozos"):
        match = next((p for p in cargar_plantilla(d) if p["name"] == nombre), None)
        if match:
            return d, match["role"]
    return None, None

def _resolver_objetivo_dashboard():
    """Determina de qué empleado se piden datos en un endpoint del dashboard
    y valida que el usuario autenticado tenga permiso para verlos: siempre
    puede ver los suyos; solo jefe/admin (permiso 'ver_estadisticas') puede
    pasar ?empleado=Otro para consultar los de un tercero.
    Devuelve (objetivo, es_propio, error_response_o_None)."""
    u = usuario_actual()
    objetivo  = (request.args.get("empleado") or "").strip() or u.get("vinculado")
    es_propio = objetivo == u.get("vinculado")
    if not es_propio and not tiene_permiso("ver_estadisticas"):
        return None, False, err("No puedes consultar el panel de otro empleado", 403)
    if not objetivo:
        return None, False, err("Tu cuenta no está vinculada a ningún empleado de la plantilla", 403)
    return objetivo, es_propio, None

_DEPT_LABEL = {"recepcion": "Recepción", "mozos": "Mozos"}

@app.route("/api/dashboard", methods=["GET"])
@login_requerido
def api_dashboard():
    """Panel personal del empleado en una sola llamada: turno de hoy/mañana,
    semana en curso con detalle por día, contadores (peticiones/vacaciones/
    festivos/LA), peticiones activas e histórico de turnos. Sin datos de
    otros empleados salvo los compañeros de turno del día."""
    objetivo, es_propio, error = _resolver_objetivo_dashboard()
    if error:
        return error

    dept_id, role = _dept_y_rol_de(objetivo)
    if not dept_id:
        return err("Empleado no encontrado en ninguna plantilla", 404)

    hoy             = date.today()
    manana          = hoy + timedelta(days=1)
    historial       = cargar_historial(dept_id)
    horarios_turnos = cargar_horarios_turnos(dept_id)
    lunes_actual    = lunes_de(hoy.isoformat())

    # "Libre" y "Vacaciones" son ambos, de cara al dashboard, días sin
    # trabajar: "Vacaciones" es solo una etiqueta de presentación sobre el
    # mismo "Libre" interno (ver etiquetar_celda).
    LIBRE_LIKE = {"Libre", "Vacaciones"}

    def semana_de(fecha):
        return historial.get(_semana_str(lunes_de(fecha.isoformat())))

    def turno_dia(fecha):
        ent = semana_de(fecha)
        if not ent:
            return None
        turnos = ent.get("schedule", {}).get(objetivo)
        if not turnos:
            return None
        idx = (fecha - lunes_de(fecha.isoformat())).days
        if not (0 <= idx < len(turnos)):
            return None
        persona = next((p for p in ent.get("staff", []) if p["name"] == objetivo), {})
        return etiquetar_celda(objetivo, idx, turnos[idx], persona.get("dias_vacaciones", []))

    def companeros_dia(fecha, turno):
        if not turno or turno in LIBRE_LIKE:
            return []
        ent = semana_de(fecha)
        if not ent:
            return []
        idx = (fecha - lunes_de(fecha.isoformat())).days
        out = []
        for nombre, turnos in ent.get("schedule", {}).items():
            if nombre == objetivo:
                continue
            if 0 <= idx < len(turnos) and turnos[idx] == turno:
                out.append(nombre)
        return sorted(out)

    def horario_de(turno):
        return horarios_turnos.get(turno) if turno and turno not in LIBRE_LIKE else None

    def proximo_no_libre(desde):
        """Busca hacia delante el próximo día con turno distinto de Libre/Vacaciones.
        Se detiene si llega a una semana sin generar (no se puede predecir)."""
        for i in range(1, 29):
            f = desde + timedelta(days=i)
            t = turno_dia(f)
            if t is None:
                return None
            if t not in LIBRE_LIKE:
                return {"fecha": f.isoformat(), "dia": DIAS_SEMANA[f.weekday()], "turno": t}
        return None

    # ── Bloque 1 — AHORA MISMO ──────────────────────────────────────────
    turno_hoy    = turno_dia(hoy)
    turno_manana = turno_dia(manana)
    ahora_mismo = {
        "fecha": hoy.isoformat(), "dia": DIAS_SEMANA[hoy.weekday()],
        "existe_horario": semana_de(hoy) is not None,
        "turno_hoy": turno_hoy, "horario_hoy": horario_de(turno_hoy),
        "companeros_hoy": companeros_dia(hoy, turno_hoy),
        "turno_manana": turno_manana, "horario_manana": horario_de(turno_manana),
        "regreso": proximo_no_libre(hoy) if turno_hoy in LIBRE_LIKE else None,
    }

    # ── Bloque 2 — MI SEMANA ────────────────────────────────────────────
    dias_semana = []
    for i in range(7):
        f = lunes_actual + timedelta(days=i)
        t = turno_dia(f)
        dias_semana.append({
            "fecha": f.isoformat(), "dia": DIAS_SEMANA[i], "hoy": f == hoy,
            "turno": t, "horario": horario_de(t), "companeros": companeros_dia(f, t),
            "es_regreso": False,
        })
    if ahora_mismo["regreso"]:
        for d in dias_semana:
            if d["fecha"] == ahora_mismo["regreso"]["fecha"]:
                d["es_regreso"] = True

    # ── Bloque 3 — MIS CONTADORES ───────────────────────────────────────
    mes_actual, mes_siguiente = _mes_actual_y_siguiente()
    cupo_actual    = {"mes": mes_actual, "usadas": cupo_usado(objetivo, mes_actual, dept_id),
                       "total": cupo_total(objetivo, mes_actual)}
    cupo_siguiente = {"mes": mes_siguiente, "usadas": cupo_usado(objetivo, mes_siguiente, dept_id),
                       "total": cupo_total(objetivo, mes_siguiente)}
    anio = hoy.year
    dotacion  = dotacion_vacaciones_de(objetivo, anio, dept_id)
    tomados   = dias_vacaciones_tomados_en_anio(objetivo, anio, dept_id)
    comp      = cargar_compensaciones(dept_id).get(objetivo, {})
    stats_emp = estadisticas_de(objetivo, dept_id)
    festivos_anio = sum(1 for fd in stats_emp.get("festivos_dias", []) if fd["fecha"].startswith(str(anio)))

    contadores = {
        "peticiones_mes": cupo_actual,
        "vacaciones": {"año": anio, "dotacion": dotacion, "tomados": tomados,
                       "restantes": max(0, dotacion - tomados)},
        "festivos_trabajados_anio": festivos_anio,
        "la_saldo": comp.get("la_saldo", 0),
        "modalidad_compensacion": comp.get("modalidad", "cobrar"),
    }

    # ── Bloque 4 — MIS PETICIONES ───────────────────────────────────────
    activas = peticiones_activas_de(objetivo, dept_id)
    for p in activas:
        p["estado"] = "activa"
    peticiones_bloque = {
        "activas": activas,
        "cupo": {"mes_actual": cupo_actual, "mes_siguiente": cupo_siguiente},
    }

    # ── Bloque 5 — MI HISTÓRICO ─────────────────────────────────────────
    historico = {
        "3m":   resumen_periodo(objetivo, dept_id, "3m"),
        "anio": resumen_periodo(objetivo, dept_id, "anio"),
    }

    # ── Bloque 6 — MI EQUIDAD ────────────────────────────────────────────
    # Nunca incluye datos individuales de otros empleados (ver equidad_de).
    equidad = equidad_de(objetivo, dept_id)

    return ok({
        "nombre": objetivo, "dept": dept_id, "dept_label": _DEPT_LABEL.get(dept_id, dept_id),
        "role": role, "es_propio": es_propio,
        "ahora_mismo": ahora_mismo,
        "semana": {"key": _semana_str(lunes_actual), "dias": dias_semana},
        "contadores": contadores,
        "peticiones": peticiones_bloque,
        "historico": historico,
        "equidad": equidad,
    })


def _generar_ics(dept_id: str, nombre: str) -> str:
    """Genera el contenido de un fichero .ics (RFC 5545, hora local sin
    zona horaria) con los turnos de 'nombre': semana actual + 4 siguientes,
    solo semanas ya generadas en el historial. Un VEVENT por tramo horario
    de cada turno trabajado (Partido puede generar 2 eventos ese día)."""
    historial       = cargar_historial(dept_id)
    horarios_turnos = cargar_horarios_turnos(dept_id)
    hoy             = date.today()
    lunes_actual    = lunes_de(hoy.isoformat())
    dtstamp         = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    lineas = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Hotel Manager//Turnos//ES", "CALSCALE:GREGORIAN"]
    for semana_idx in range(5):  # semana actual + 4 siguientes
        lunes_semana = lunes_actual + timedelta(weeks=semana_idx)
        entrada = historial.get(_semana_str(lunes_semana))
        if not entrada:
            continue
        turnos = entrada.get("schedule", {}).get(nombre)
        if not turnos:
            continue
        for i, turno in enumerate(turnos):
            tramos = horarios_turnos.get(turno)
            if turno == "Libre" or not tramos:
                continue
            dia = lunes_semana + timedelta(days=i)
            for tramo in tramos:
                try:
                    h_ini = datetime.strptime(tramo["inicio"], "%H:%M").time()
                    h_fin = datetime.strptime(tramo["fin"], "%H:%M").time()
                except (KeyError, ValueError):
                    continue
                inicio_dt = datetime.combine(dia, h_ini)
                fin_dt    = datetime.combine(dia, h_fin)
                if h_fin <= h_ini:
                    fin_dt += timedelta(days=1)  # turno cruza medianoche (p.ej. Noche)
                lineas += [
                    "BEGIN:VEVENT",
                    f"UID:{uuid.uuid4()}@hotel-manager",
                    f"DTSTAMP:{dtstamp}",
                    f"DTSTART:{inicio_dt.strftime('%Y%m%dT%H%M%S')}",
                    f"DTEND:{fin_dt.strftime('%Y%m%dT%H%M%S')}",
                    f"SUMMARY:Turno {turno} — {_DEPT_LABEL.get(dept_id, dept_id)}",
                    "END:VEVENT",
                ]
    lineas.append("END:VCALENDAR")
    return "\r\n".join(lineas) + "\r\n"

@app.route("/api/dashboard/ics", methods=["GET"])
@login_requerido
def api_dashboard_ics():
    """Descarga autenticada de un .ics con los turnos del empleado (semana
    actual + 4 siguientes ya generadas). No es un endpoint de suscripción
    por URL: requiere la sesión de la app (Cloudflare Access delante), así
    que una app de calendario externa no podría refrescarlo por su cuenta;
    el usuario debe descargarlo e importarlo manualmente."""
    objetivo, _es_propio, error = _resolver_objetivo_dashboard()
    if error:
        return error
    dept_id, _role = _dept_y_rol_de(objetivo)
    if not dept_id:
        return err("Empleado no encontrado en ninguna plantilla", 404)

    contenido = _generar_ics(dept_id, objetivo)
    nombre_fichero = re.sub(r"[^A-Za-z0-9_.-]", "_", objetivo) or "turnos"
    resp = Response(contenido, mimetype="text/calendar")
    resp.headers["Content-Disposition"] = f'attachment; filename="turnos_{nombre_fichero}.ics"'
    return resp

@app.route("/api/cupos/extra", methods=["GET"])
@permiso_requerido("editar_peticiones")
def api_get_cupos_extra():
    return ok(cargar_cupos_extra())

@app.route("/api/cupos/extra", methods=["PUT"])
@permiso_requerido("editar_peticiones")
def api_set_cupo_extra():
    body   = request.json or {}
    nombre = (body.get("nombre") or "").strip()
    mes    = (body.get("mes") or "").strip()
    if not nombre or not mes:
        return err("Faltan nombre o mes")
    try:
        date.fromisoformat(mes + "-01")
    except ValueError:
        return err("Formato de mes inválido, usa AAAA-MM")
    ok_valor, extra, msg_valor = validar_valor_cupo_extra(body.get("extra"))
    if not ok_valor:
        return err(msg_valor)
    cupos = cargar_cupos_extra()
    anterior = cupos.get(nombre, {}).get(mes, 0)
    cupos.setdefault(nombre, {})[mes] = extra
    guardar_cupos_extra(cupos)
    registrar_accion("CUPO_EXTRA", f"{nombre} {mes}: {extra}")
    u = usuario_actual()
    dept_id, _role = _dept_y_rol_de(nombre)
    registrar_intervencion(
        usuario=u["username"], rol_usuario=u["rol"], dept=dept_id or "recepcion",
        tipo="cupo_extra", empleado_afectado=nombre, semana=None,
        valor_anterior=str(anterior), valor_nuevo=f"{mes}: {anterior}→{extra}",
        justificacion=(body.get("justificacion") or "").strip(),
    )
    return ok({"nombre": nombre, "mes": mes, "extra": extra})

# ═══════════════════════════════════════════════════════════════════════════════
# API — LA: ASIGNAR EN FECHA ESPECÍFICA
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/compensaciones/<nombre>/usar-la", methods=["POST"])
@permiso_requerido("editar_compensaciones")
def api_usar_la(nombre):
    body    = request.json or {}
    dept_id = body.get("dept","recepcion")
    fecha   = body.get("semana")
    dia     = body.get("dia")
    if not fecha or not dia:
        return err("Faltan semana y día")
    comp = cargar_compensaciones(dept_id)
    if comp.get(nombre, {}).get("la_saldo", 0) < 1:
        return err(f"{nombre} no tiene días LA disponibles")

    semana = lunes_de(fecha)
    # Store LA under a separate key so modelo_base.py treats it as extra
    pet = cargar_peticiones(dept_id)
    key = _semana_str(semana)
    if key not in pet:
        pet[key] = {}
    entry = pet[key].get(nombre, {})
    if not isinstance(entry, dict):
        # Migrate old list format
        entry = {"dias": entry if isinstance(entry, list) else [], "la": []}
    la_dias = entry.get("la", [])
    if dia not in la_dias:
        la_dias.append(dia)
    entry["la"] = la_dias
    entry.setdefault("origen", "jefe")
    entry.setdefault("estado", "activa")
    entry.setdefault("creada_en", date.today().isoformat())
    pet[key][nombre] = entry
    guardar_peticiones(pet, dept_id)

    comp[nombre]["la_saldo"] = comp[nombre].get("la_saldo", 1) - 1
    guardar_compensaciones(comp, dept_id)
    invalidar_cache_estadisticas(dept_id)
    return ok({
        "la_saldo": comp[nombre]["la_saldo"],
        "message":  f"LA asignado: {nombre} libre extra el {dia} semana del {semana.strftime('%d/%m/%Y')}"
    })

# ═══════════════════════════════════════════════════════════════════════════════
# API — VACACIONES
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/vacaciones", methods=["GET"])
@login_requerido
def api_get_vacaciones():
    dept_id = request.args.get("dept","recepcion")
    vac = cargar_vacaciones(dept_id)
    hoy = date.today()
    for v in vac:
        ini = date.fromisoformat(v["inicio"])
        fin = date.fromisoformat(v["fin"])
        v["dias"]   = (fin - ini).days + 1
        v["estado"] = "activo" if ini <= hoy <= fin else ("pasado" if fin < hoy else "próximo")
    return ok(sorted(vac, key=lambda v: v["inicio"]))

@app.route("/api/vacaciones", methods=["POST"])
@permiso_requerido("editar_vacaciones")
def api_add_vacaciones():
    body   = request.json or {}
    dept_id = body.get("dept","recepcion")
    nombre = body.get("nombre","").strip()
    inicio = body.get("inicio","")
    fin    = body.get("fin","")
    nota   = body.get("nota","")
    dept_id= body.get("dept","recepcion")
    if not nombre or not inicio or not fin:
        return err("Faltan datos obligatorios")
    if fin < inicio:
        return err("La fecha fin debe ser posterior al inicio")
    import random, string
    vid = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    vac = cargar_vacaciones(dept_id)
    vac.append({"id": vid, "nombre": nombre, "inicio": inicio, "fin": fin, "nota": nota})
    guardar_vacaciones(vac, dept_id)
    invalidar_cache_estadisticas(dept_id)
    return ok(message="Vacaciones registradas")

@app.route("/api/vacaciones/<vid>", methods=["DELETE"])
@permiso_requerido("editar_vacaciones")
def api_delete_vacaciones(vid):
    dept_id  = request.args.get("dept","recepcion")
    vac      = cargar_vacaciones(dept_id)
    original = len(vac)
    vac      = [v for v in vac if v.get("id") != vid]
    if len(vac) == original:
        return err("Periodo no encontrado", 404)
    guardar_vacaciones(vac, dept_id)
    invalidar_cache_estadisticas(dept_id)
    return ok(message="Periodo eliminado")

# ─── DOTACIÓN ANUAL DE VACACIONES ────────────────────────────────────────────

@app.route("/api/vacaciones-dotacion", methods=["GET"])
@permiso_requerido("ver_vacaciones")
def api_get_vacaciones_dotacion():
    """Dotación anual de vacaciones de todos los empleados de la plantilla,
    para la tabla de administración. anio: año a mostrar (por defecto el actual)."""
    dept_id = request.args.get("dept", "recepcion")
    anio    = request.args.get("año", date.today().year, type=int)
    staff   = cargar_plantilla(dept_id)
    return ok([
        {
            "nombre":   p["name"],
            "año":      anio,
            "dotacion": dotacion_vacaciones_de(p["name"], anio, dept_id),
            "tomados":  dias_vacaciones_tomados_en_anio(p["name"], anio, dept_id),
        }
        for p in staff
    ])

@app.route("/api/vacaciones-dotacion", methods=["PUT"])
@permiso_requerido("editar_vacaciones")
def api_set_vacaciones_dotacion():
    """Body: {dept, nombre, año, dias}. Fija la dotación de un empleado para
    un año concreto (días naturales)."""
    body    = request.json or {}
    dept_id = body.get("dept", "recepcion")
    nombre  = (body.get("nombre") or "").strip()
    anio    = body.get("año")
    dias    = body.get("dias")
    if not nombre or not anio:
        return err("Faltan datos obligatorios")
    if not isinstance(dias, int) or isinstance(dias, bool) or dias < 0:
        return err("Los días deben ser un entero mayor o igual a 0")
    dotacion = cargar_vacaciones_dotacion(dept_id)
    dotacion.setdefault(nombre, {})[str(anio)] = dias
    guardar_vacaciones_dotacion(dotacion, dept_id)
    registrar_accion("EDITAR_DOTACION_VACACIONES", f"Dept:{dept_id} {nombre} {anio} -> {dias} días")
    return ok(message="Dotación actualizada")

# ═══════════════════════════════════════════════════════════════════════════════
# API — FESTIVOS
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/festivos", methods=["GET"])
@login_requerido
def api_get_festivos():
    anio       = request.args.get("año", date.today().year, type=int)
    festivos   = cargar_festivos(anio)
    trabajados = cargar_festivos_trabajados()
    for f in festivos:
        f["registrado"] = f["fecha"] in trabajados
        if f["registrado"]:
            f["trabajadores"] = trabajados[f["fecha"]]["trabajadores"]
    return ok(festivos)

@app.route("/api/festivos/trabajados", methods=["GET"])
@login_requerido
def api_festivos_trabajados():
    ft    = cargar_festivos_trabajados()
    staff = cargar_plantilla()
    resumen = []
    for p in staff:
        nombre = p["name"]
        dias = [
            {"fecha": fecha, "festivo": info["festivo"],
             "turno": info["trabajadores"].get(nombre)}
            for fecha, info in sorted(ft.items())
            if nombre in info.get("trabajadores", {})
        ]
        resumen.append({"nombre": nombre, "dias": dias, "total": len(dias)})
    return ok(resumen)

# ═══════════════════════════════════════════════════════════════════════════════
# API — COMPENSACIONES
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/compensaciones", methods=["GET"])
@login_requerido
def api_get_comp():
    dept_id = request.args.get("dept","recepcion")
    staff   = cargar_plantilla(dept_id)
    comp    = cargar_compensaciones(dept_id)
    ft      = cargar_festivos_trabajados(dept_id)
    hoy     = date.today()
    resultado = []
    for p in staff:
        nombre = p["name"]
        c = comp.get(nombre, {})
        resultado.append({
            "nombre":    nombre,
            "role":      p["role"],
            "modalidad": c.get("modalidad","cobrar"),
            "la_saldo":  c.get("la_saldo",0),
            "año_mod":   c.get("año_modificacion"),
            "modificable": hoy.month == 1,
            "festivos_trabajados": sum(
                1 for info in ft.values() if nombre in info.get("trabajadores",{})
            ),
        })
    return ok(resultado)

@app.route("/api/compensaciones/<nombre>", methods=["PUT"])
@permiso_requerido("editar_compensaciones")
def api_edit_comp(nombre):
    body    = request.json or {}
    dept_id = body.get("dept", request.args.get("dept","recepcion"))
    comp    = cargar_compensaciones(dept_id)
    hoy     = date.today()
    if nombre not in comp:
        comp[nombre] = {"la_saldo": 0}
    if "modalidad" in body:
        if hoy.month != 1 and comp[nombre].get("año_modificacion") == hoy.year:
            return err("La modalidad solo puede modificarse en enero, una vez al año")
        comp[nombre]["modalidad"] = body["modalidad"]
        comp[nombre]["año_modificacion"] = hoy.year
    if "la_saldo" in body:
        comp[nombre]["la_saldo"] = max(0, int(body["la_saldo"]))
    if "ajuste_la" in body:
        delta = int(body["ajuste_la"])
        comp[nombre]["la_saldo"] = max(0, comp[nombre].get("la_saldo",0) + delta)
    guardar_compensaciones(comp, dept_id)
    invalidar_cache_estadisticas(dept_id)
    return ok({"la_saldo": comp[nombre]["la_saldo"]})

# ═══════════════════════════════════════════════════════════════════════════════
# API — ESTADÍSTICAS Y META
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/estadisticas", methods=["GET"])
@login_requerido
def api_estadisticas():
    dept_id    = request.args.get("dept","recepcion")
    historial  = cargar_historial(dept_id)
    staff      = cargar_plantilla(dept_id)
    comp       = cargar_compensaciones(dept_id)
    peticiones = cargar_peticiones(dept_id)
    stats = {}
    for p in staff:
        nombre = p["name"]
        stats[nombre] = {
            "nombre": nombre, "role": p["role"],
            "modalidad": comp.get(nombre,{}).get("modalidad","cobrar"),
            "la_saldo":  comp.get(nombre,{}).get("la_saldo",0),
            "Mañana":0,"Tarde":0,"Noche":0,"Partido":0,"Libre":0,
            "peticiones":0,"fds_libres":0,"semanas":0,
        }
    for semana_str, entrada in historial.items():
        schedule   = entrada.get("schedule",{})
        semana_pet = peticiones.get(semana_str,{})
        for nombre, turnos in schedule.items():
            if nombre not in stats:
                continue
            stats[nombre]["semanas"] += 1
            for t in turnos:
                if t in stats[nombre]:
                    stats[nombre][t] += 1
            if len(turnos)>=7 and turnos[5]=="Libre" and turnos[6]=="Libre":
                stats[nombre]["fds_libres"] += 1
        for nombre, ep in semana_pet.items():
            if nombre not in stats:
                continue
            dias_sol = ep if isinstance(ep,list) else ep.get("dias",[])
            for dia in dias_sol:
                if dia in DAYS:
                    idx = DAYS.index(dia)
                    if schedule.get(nombre,[None]*7)[idx]=="Libre":
                        stats[nombre]["peticiones"] += 1
    return ok(list(stats.values()))

@app.route("/api/historial/limpiar", methods=["POST"])
@permiso_requerido("gestionar_usuarios")
def api_limpiar_historial():
    """Borra el historial de semanas generadas de todos los departamentos."""
    from herramientas import guardar_historial
    guardar_historial({}, "recepcion")
    guardar_historial({}, "mozos")
    invalidar_cache_estadisticas("recepcion")
    invalidar_cache_estadisticas("mozos")
    registrar_accion("LIMPIAR_HISTORIAL",
                     "Historial de horarios eliminado (recepción + mozos)")
    return ok(message="Historial eliminado correctamente")

@app.route("/api/meta", methods=["GET"])
@login_requerido
def api_meta():
    dept_id   = request.args.get("dept","recepcion")
    historial = cargar_historial(dept_id)
    ft        = cargar_festivos_trabajados(dept_id)
    comp      = cargar_compensaciones(dept_id)
    total_la  = sum(c.get("la_saldo",0) for c in comp.values())
    return ok({
        "semanas_guardadas":    len(historial),
        "festivos_registrados": len(ft),
        "total_la_pendientes":  total_la,
        "año": date.today().year,
    })

# ─── COVERAGE POR TEMPORADA ──────────────────────────────────────────────────

@app.route("/api/coverage", methods=["GET"])
@login_requerido
def api_coverage_get():
    """Devuelve la configuración completa de coverage de un departamento:
    todos los perfiles y cuál está activo."""
    dept_id = request.args.get("dept", "recepcion")
    dept_id = _normalizar_dept(dept_id)
    config   = cargar_coverage_config()
    dept_cfg = config.get(dept_id, {})
    return ok({
        "dept":     dept_id,
        "activo":   dept_cfg.get("activo", "normal"),
        "perfiles": dept_cfg.get("perfiles", {}),
    })


@app.route("/api/coverage/activo", methods=["PUT"])
@permiso_requerido("generar_horario")
def api_coverage_set_activo():
    """Cambia el perfil activo para un departamento.
    Body: {dept, perfil}"""
    body    = request.json or {}
    dept_id = body.get("dept", "recepcion")
    dept_id = _normalizar_dept(dept_id)
    perfil = body.get("perfil", "").strip()
    if not perfil:
        return err("El campo 'perfil' es obligatorio")

    config   = cargar_coverage_config()
    dept_cfg = config.get(dept_id, {})
    if perfil not in dept_cfg.get("perfiles", {}):
        nombres = list(dept_cfg.get("perfiles", {}).keys())
        return err(f"Perfil '{perfil}' no existe. Perfiles disponibles: {nombres}")

    dept_cfg["activo"] = perfil
    config[dept_id]    = dept_cfg
    guardar_coverage_config(config)
    registrar_accion("COVERAGE_ACTIVO", f"Dept:{dept_id} Perfil:{perfil}")
    return ok({"dept": dept_id, "activo": perfil})


@app.route("/api/coverage/perfil", methods=["PUT"])
@permiso_requerido("generar_horario")
def api_coverage_edit_perfil():
    """Crea o edita un perfil de cobertura para un departamento.
    Body: {dept, perfil_id, min_coverage, max_coverage}"""
    body      = request.json or {}
    dept_id   = body.get("dept", "recepcion")
    dept_id = _normalizar_dept(dept_id)
    perfil_id = body.get("perfil_id", "").strip()
    if not perfil_id:
        return err("El campo 'perfil_id' es obligatorio")

    datos = {
        "min_coverage": body.get("min_coverage"),
        "max_coverage": body.get("max_coverage"),
    }
    if datos["min_coverage"] is None or datos["max_coverage"] is None:
        return err("Se requieren 'min_coverage' y 'max_coverage'")

    errores = validar_coverage_perfil(datos)
    if errores:
        return err("Datos de cobertura inválidos: " + "; ".join(errores))

    config             = cargar_coverage_config()
    dept_cfg           = config.setdefault(dept_id, {"perfiles": {}, "activo": "normal"})
    dept_cfg.setdefault("perfiles", {})[perfil_id] = datos
    config[dept_id]    = dept_cfg
    guardar_coverage_config(config)
    registrar_accion("COVERAGE_PERFIL", f"Dept:{dept_id} Perfil:{perfil_id}")
    return ok({"dept": dept_id, "perfil_id": perfil_id, "datos": datos})

# ─── HORARIOS CONCRETOS POR TURNO ────────────────────────────────────────────

@app.route("/api/horarios-turnos", methods=["GET"])
@login_requerido
def api_get_horarios_turnos():
    """Tramos horarios (inicio/fin) de cada turno. Usado para mostrar el
    horario concreto del turno de hoy en 'Mi panel' y para generar el .ics."""
    dept_id = get_dept()
    return ok(cargar_horarios_turnos(dept_id))

@app.route("/api/horarios-turnos", methods=["PUT"])
@permiso_requerido("generar_horario")
def api_set_horarios_turnos():
    """Body: {dept, horarios: {Mañana:[{inicio,fin}], ...}}"""
    body     = request.json or {}
    dept_id  = body.get("dept", "recepcion")
    dept_id = _normalizar_dept(dept_id)
    horarios = body.get("horarios")
    if not isinstance(horarios, dict):
        return err("Falta el campo 'horarios'")
    errores = validar_horarios_turnos(horarios)
    if errores:
        return err("Horarios inválidos: " + "; ".join(errores))
    guardar_horarios_turnos(horarios, dept_id)
    registrar_accion("HORARIOS_TURNOS", f"Dept:{dept_id} horarios de turno actualizados")
    return ok(message="Horarios de turno actualizados")


# ─── ARRANCAR ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    os.makedirs(os.path.join(BASE_DIR, "static"), exist_ok=True)
    port = int(os.environ.get("PORT", 5000))
    print("\n🏨 Hotel Manager — Servidor iniciado")
    print(f"   Abre tu navegador en:  http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
