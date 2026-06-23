"""
app.py — Servidor Flask que conecta la interfaz web con modelo.py y herramientas.py
Ejecutar: python app.py
Abrir en el navegador: http://localhost:5000
"""

import copy
import os
import json
import secrets
from datetime import date, timedelta
from flask import Flask, jsonify, request, send_from_directory, session

# ── Importar lógica del proyecto ─────────────────────────────────────────────
from modelo_base import DAYS
from herramientas import (
    cargar_plantilla, guardar_plantilla, validar_plantilla,
    cargar_peticiones, guardar_peticiones,
    aplicar_peticiones, obtener_peticiones_semana,
    cargar_vacaciones, guardar_vacaciones,
    aplicar_vacaciones, vacaciones_activas_en_semana,
    cargar_historial, registrar_semana,
    registrar_festivos_semana, festivos_en_semana,
    cargar_festivos_trabajados,
    cargar_compensaciones, guardar_compensaciones,
    cargar_festivos, FESTIVOS_VALENCIA,
    ROLES_VALIDOS, MODALIDADES, BASE_DIR, DATA_DIR,
    _semana_str,
)
from auth import (
    inicializar_usuarios, autenticar, usuario_actual,
    login_usuario, logout_usuario, listar_usuarios,
    crear_usuario, editar_usuario, eliminar_usuario,
    login_requerido, permiso_requerido,
    registrar_accion, obtener_log,
    PERMISOS, ROLES_LABEL,
)

app = Flask(__name__, static_folder="static", static_url_path="/static")

# ── Clave secreta para firmar sesiones ────────────────────────────────────────
# Se genera una vez y se guarda en disco para que las sesiones sobrevivan reinicios
_secret_file = os.path.join(DATA_DIR, ".secret_key")
os.makedirs(DATA_DIR, exist_ok=True)
if os.path.exists(_secret_file):
    with open(_secret_file, "rb") as _f:
        app.secret_key = _f.read()
else:
    app.secret_key = secrets.token_bytes(32)
    with open(_secret_file, "wb") as _f:
        _f.write(app.secret_key)

app.config["SESSION_COOKIE_HTTPONLY"] = True   # JS no puede leer la cookie
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"  # protección CSRF básica

# Inicializar usuarios al arrancar (crea admin por defecto si no existe)
inicializar_usuarios()

# ─── HELPERS ─────────────────────────────────────────────────────────────────

def lunes_de(fecha_str: str) -> date:
    d = date.fromisoformat(fecha_str)
    return d - timedelta(days=d.weekday())

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
    nuevo = {
        "name":        nombre,
        "role":        body.get("role", "normal"),
        "preferences": body.get("preferences", []),
    }
    dept_id = body.get("dept","recepcion")
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
        staff[idx]["role"] = body["role"]
    if "preferences" in body:
        staff[idx]["preferences"] = body["preferences"]
    errores = validar_plantilla(staff, excluir_idx=idx)
    if errores:
        staff[idx] = original
        return err(errores[0])
    guardar_plantilla(staff, dept_id)
    if "comp" in body:
        _guardar_comp_empleado(staff[idx]["name"], body["comp"], dept_id)
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

# ═══════════════════════════════════════════════════════════════════════════════
# API — HORARIO
# ═══════════════════════════════════════════════════════════════════════════════

def _get_dept_config(dept_id: str):
    if dept_id == "mozos":
        from modelo_mozos import MOZOS_CONFIG
        return MOZOS_CONFIG
    from modelo_recepcion import RECEPCION_CONFIG
    return RECEPCION_CONFIG

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

@app.route("/api/horario/generar", methods=["POST"])
@permiso_requerido("generar_horario")
def api_generar():
    body    = request.json or {}
    fecha   = body.get("semana", date.today().isoformat())
    dept_id = body.get("dept", "recepcion")
    if dept_id not in ("recepcion", "mozos"):
        dept_id = "recepcion"
    semana  = lunes_de(fecha)
    cfg     = _get_dept_config(dept_id)

    staff_base = cargar_plantilla(dept_id)
    staff = aplicar_vacaciones(staff_base, semana, dept_id)
    staff = aplicar_peticiones(staff, semana, dept_id)

    # Mozos: load who covered last week to give them Mon+Tue off this week
    prev_week_covers = {}
    if cfg.prefer_next_week_for_cover:
        prev_key = _semana_str(semana - timedelta(weeks=1))
        cov_path = os.path.join(DATA_DIR, f"night_covers_{dept_id}.json")
        if os.path.exists(cov_path):
            import json as _j
            prev_week_covers = _j.loads(open(cov_path, encoding="utf-8").read()).get(prev_key, {})

    from modelo_base import generate_schedule as gen_sched
    full_schedule, staff_final, ws_date, night_cover, this_week_covers = gen_sched(
        staff_list=copy.deepcopy(staff),
        week_start=semana,
        cfg=cfg,
        prev_week_covers=prev_week_covers,
    )

    if cfg.prefer_next_week_for_cover:
        import json as _j
        cov_path = os.path.join(DATA_DIR, f"night_covers_{dept_id}.json")
        all_covers = _j.loads(open(cov_path).read()) if os.path.exists(cov_path) else {}
        all_covers[_semana_str(semana)] = this_week_covers
        open(cov_path, "w").write(_j.dumps(all_covers, ensure_ascii=False, indent=2))

    advertencias = []
    for shift in ["Mañana", "Tarde", "Noche", "Partido"]:
        for day_idx, day in enumerate(DAYS):
            count = sum(1 for p in staff_final if full_schedule[p["name"]][day_idx] == shift)
            mn = cfg.min_coverage.get(shift, 0)
            mx = cfg.max_coverage.get(shift, 99)
            if not (mn <= count <= mx):
                advertencias.append(f"{day} – {shift}: {count} (mín {mn}, máx {mx})")
    from modelo_base import NO_MORNING_AFTER as NMA
    for p in staff_final:
        for i in range(1, 7):
            prev, curr = full_schedule[p["name"]][i-1], full_schedule[p["name"]][i]
            if prev in NMA and curr == "Mañana":
                advertencias.append(f"{p['name']}: {DAYS[i-1]} ({prev}) → {DAYS[i]} (Mañana)")

    festivos = [
        {"fecha": f["date"].isoformat(), "nombre": f["nombre"],
         "tipo": f["tipo"], "dia_semana": f["dia_semana"]}
        for f in festivos_en_semana(semana)
    ]

    registrar_semana(semana, full_schedule, staff_final, forzar=True, dept_id=dept_id)
    registrar_festivos_semana(semana, full_schedule, staff_final)
    _recalcular_la_desde_historial(dept_id)
    registrar_accion("GENERAR_HORARIO", f"Dept:{dept_id} Semana:{semana.isoformat()}")

    clean_staff = []
    skip = {"days_off","night_cover_this_week","covered_nights_prev_week","forced_days_off","extra_days_off"}
    for p in staff_final:
        clean_staff.append({k: v for k, v in p.items() if k not in skip})

    return ok({
        "semana": semana.isoformat(), "dept": dept_id,
        "schedule": {p["name"]: full_schedule[p["name"]] for p in staff_final},
        "staff": clean_staff,
        "advertencias": advertencias, "festivos": festivos, "dias": DAYS,
    })

@app.route("/api/horario/exportar", methods=["POST"])
@permiso_requerido("exportar_horario")
def api_exportar():
    body    = request.json or {}
    dept_id = body.get("dept", "recepcion")
    semana  = lunes_de(body.get("semana", date.today().isoformat()))
    cfg     = _get_dept_config(dept_id)
    historial = cargar_historial(dept_id)
    key = _semana_str(semana)
    if key not in historial:
        return err("Genera el horario antes de exportar")
    entrada = historial[key]
    ruta    = os.path.join(BASE_DIR, f"horario_{dept_id}_{key}.xlsx")
    from modelo_base import export_to_excel as exp_xl
    exp_xl(entrada["schedule"], entrada["staff"], semana, ruta, cfg)
    return ok({"ruta": ruta})

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
            "schedule": entrada["schedule"], "staff": entrada["staff"],
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
    ruta    = os.path.join(BASE_DIR, f"horario_{dept_id}_{semana_str}.xlsx")
    from modelo_base import export_to_excel as exp_xl
    exp_xl(entrada["schedule"], entrada["staff"], semana, ruta, cfg)
    return send_file(ruta, as_attachment=True,
                     download_name=f"horario_{dept_id}_{semana_str}.xlsx",
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
    raw     = obtener_peticiones_semana(semana, dept_id)
    result  = {}
    for nombre, entrada in raw.items():
        if isinstance(entrada, list):
            result[nombre] = {"dias": entrada, "la": []}
        elif isinstance(entrada, dict):
            result[nombre] = {"dias": entrada.get("dias",[]), "la": entrada.get("la",[])}
    return ok(result)

@app.route("/api/peticiones", methods=["POST"])
@permiso_requerido("editar_peticiones")
def api_add_peticion():
    body    = request.json or {}
    fecha   = body.get("semana")
    nombre  = body.get("nombre","").strip()
    dias    = body.get("dias",[])
    dept_id = body.get("dept","recepcion")
    if not fecha or not nombre or not dias:
        return err("Faltan datos")
    semana = lunes_de(fecha)
    pet    = cargar_peticiones(dept_id)
    key    = _semana_str(semana)
    if key not in pet:
        pet[key] = {}
    existing = pet[key].get(nombre, {})
    if isinstance(existing, list):
        existing = {"dias": existing, "la": []}
    existing["dias"] = dias
    pet[key][nombre] = existing
    guardar_peticiones(pet, dept_id)
    return ok(message="Petición guardada")

# ═══════════════════════════════════════════════════════════════════════════════
# API — PETICIONES (DELETE individual)
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/peticiones/<semana>/<nombre>", methods=["DELETE"])
@permiso_requerido("editar_peticiones")
def api_delete_peticion(semana, nombre):
    dept_id  = request.args.get("dept","recepcion")
    pet      = cargar_peticiones(dept_id)
    semana_d = lunes_de(semana)
    key = _semana_str(semana_d)
    if key not in pet or nombre not in pet[key]:
        return err("Petición no encontrada", 404)
    del pet[key][nombre]
    if not pet[key]:
        del pet[key]
    guardar_peticiones(pet, dept_id)
    return ok(message="Petición eliminada")

# ═══════════════════════════════════════════════════════════════════════════════
# API — LA: ASIGNAR EN FECHA ESPECÍFICA
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/compensaciones/<nombre>/usar-la", methods=["POST"])
@permiso_requerido("editar_compensaciones")
def api_usar_la(nombre):
    body   = request.json or {}
    dept_id = body.get("dept","recepcion")
    fecha  = body.get("semana")
    dia    = body.get("dia")
    if not fecha or not dia:
        return err("Faltan semana y día")
    comp = cargar_compensaciones()
    if comp.get(nombre, {}).get("la_saldo", 0) < 1:
        return err(f"{nombre} no tiene días LA disponibles")

    semana = lunes_de(fecha)
    # Store LA under a separate key so modelo.py treats it as extra
    pet = cargar_peticiones()
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
    pet[key][nombre] = entry
    guardar_peticiones(pet, dept_id)

    comp[nombre]["la_saldo"] = comp[nombre].get("la_saldo", 1) - 1
    guardar_compensaciones(comp, dept_id)
    return ok({
        "la_saldo": comp[nombre]["la_saldo"],
        "message":  f"LA asignado: {nombre} libre extra el {dia} semana del {semana.strftime('%d/%m/%Y')}"
    })

    fecha   = body.get("semana")
    dia     = body.get("dia")
    dept_id = body.get("dept","recepcion")
    if not fecha or not dia:
        return err("Faltan semana y día")
    comp = cargar_compensaciones(dept_id)
    if comp.get(nombre, {}).get("la_saldo", 0) < 1:
        return err(f"{nombre} no tiene días LA disponibles")
    semana = lunes_de(fecha)
    pet = cargar_peticiones(dept_id)
    key = _semana_str(semana)
    if key not in pet:
        pet[key] = {}
    entry = pet[key].get(nombre, {})
    if not isinstance(entry, dict):
        entry = {"dias": entry if isinstance(entry, list) else [], "la": []}
    la_dias = entry.get("la", [])
    if dia not in la_dias:
        la_dias.append(dia)
    entry["la"] = la_dias
    pet[key][nombre] = entry
    guardar_peticiones(pet, dept_id)
    comp[nombre]["la_saldo"] = comp[nombre].get("la_saldo", 1) - 1
    guardar_compensaciones(comp, dept_id)
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
    return ok(message="Periodo eliminado")

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

# ─── ARRANCAR ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    os.makedirs(os.path.join(BASE_DIR, "static"), exist_ok=True)
    print("\n🏨 Hotel Manager — Servidor iniciado")
    print("   Abre tu navegador en:  http://localhost:5000\n")
    app.run(host="0.0.0.0", port=5000, debug=False)
