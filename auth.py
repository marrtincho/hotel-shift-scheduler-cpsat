"""
auth.py — Sistema de autenticación y autorización para Hotel Manager.

Roles:
  admin     — Acceso total. Puede gestionar usuarios, plantilla, horarios y datos.
  encargado — Puede generar horarios, ver y gestionar peticiones/vacaciones/festivos.
              No puede gestionar usuarios ni cambiar configuración crítica.
  empleado  — Solo puede ver su propio horario y enviar peticiones de libre.

Seguridad:
  - Contraseñas hasheadas con PBKDF2-SHA256 (werkzeug)
  - Sesiones firmadas con SECRET_KEY (cookie segura, sin datos en servidor)
  - Sesión se invalida al cerrar el navegador (session.permanent = False)
  - Log de actividad con timestamp, usuario, acción e IP
"""

import json
import os
import hashlib
import secrets
import functools
from datetime import datetime, timezone
from flask import session, request, jsonify

# ─── RUTAS ────────────────────────────────────────────────────────────────────

try:
    from herramientas import DATA_DIR
except ImportError:
    DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

os.makedirs(DATA_DIR, exist_ok=True)

USERS_FILE   = os.path.join(DATA_DIR, "usuarios.json")
LOG_FILE     = os.path.join(DATA_DIR, "actividad.json")

# ─── PERMISOS POR ROL ─────────────────────────────────────────────────────────

PERMISOS = {
    "admin": {
        "gestionar_usuarios",
        "gestionar_roles",
        "ver_plantilla", "editar_plantilla",
        "generar_horario", "exportar_horario",
        "ver_historial", "eliminar_historial",
        "ver_festivos", "ver_compensaciones", "editar_compensaciones",
        "ver_vacaciones", "editar_vacaciones",
        "ver_peticiones", "editar_peticiones",
        "ver_estadisticas",
        "ver_propio_horario",
    },
    "encargado": {
        "ver_plantilla",
        "generar_horario", "exportar_horario",
        "ver_historial",
        "ver_festivos", "ver_compensaciones", "editar_compensaciones",
        "ver_vacaciones", "editar_vacaciones",
        "ver_peticiones", "editar_peticiones",
        "ver_estadisticas",
        "ver_propio_horario",
    },
    "empleado": {
        "ver_propio_horario",
        "ver_peticiones",
        "editar_peticiones_propias",
    },
}

ROLES_LABEL = {
    "admin":     "Administrador",
    "encargado": "Encargado",
    "empleado":  "Empleado",
}

# ─── HASH DE CONTRASEÑAS ──────────────────────────────────────────────────────

def _hash_password(password: str, salt: str = None) -> tuple[str, str]:
    """Returns (hash, salt). Uses PBKDF2-HMAC-SHA256 with 260000 iterations."""
    if salt is None:
        salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 260000)
    return dk.hex(), salt

def _verify_password(password: str, stored_hash: str, salt: str) -> bool:
    computed, _ = _hash_password(password, salt)
    return secrets.compare_digest(computed, stored_hash)

# ─── GESTIÓN DE USUARIOS ──────────────────────────────────────────────────────

def _cargar_usuarios() -> list[dict]:
    if not os.path.exists(USERS_FILE):
        return []
    with open(USERS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def _guardar_usuarios(usuarios: list[dict]):
    # Backup antes de escribir
    if os.path.exists(USERS_FILE):
        backup = USERS_FILE + ".bak"
        import shutil
        shutil.copy2(USERS_FILE, backup)
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(usuarios, f, ensure_ascii=False, indent=2)

def inicializar_usuarios():
    """
    Si no hay usuarios, crea el admin por defecto.
    Llamar una vez al arrancar la app.
    """
    usuarios = _cargar_usuarios()
    if not usuarios:
        pw_hash, salt = _hash_password("admin1234")
        usuarios = [{
            "id":       "1",
            "username": "admin",
            "nombre":   "Administrador",
            "rol":      "admin",
            "hash":     pw_hash,
            "salt":     salt,
            "activo":   True,
            "empleado_vinculado": None,  # nombre en plantilla, para empleados
        }]
        _guardar_usuarios(usuarios)
        print("  ⚠  Usuarios inicializados. Credenciales por defecto:")
        print("      Usuario: admin")
        print("      Contraseña: admin1234")
        print("      ¡Cámbiala en Ajustes → Usuarios!\n")

def autenticar(username: str, password: str) -> dict | None:
    """Devuelve el usuario si las credenciales son correctas, o None."""
    usuarios = _cargar_usuarios()
    for u in usuarios:
        if u["username"].lower() == username.lower() and u.get("activo", True):
            if _verify_password(password, u["hash"], u["salt"]):
                return u
    return None

def listar_usuarios() -> list[dict]:
    """Devuelve usuarios sin los campos hash/salt."""
    return [
        {k: v for k, v in u.items() if k not in ("hash", "salt")}
        for u in _cargar_usuarios()
    ]

def crear_usuario(username: str, nombre: str, rol: str,
                  password: str, empleado_vinculado: str = None) -> tuple[bool, str]:
    if rol not in PERMISOS:
        return False, f"Rol inválido: {rol}"
    usuarios = _cargar_usuarios()
    if any(u["username"].lower() == username.lower() for u in usuarios):
        return False, "Ese nombre de usuario ya existe"
    pw_hash, salt = _hash_password(password)
    nuevo_id = str(max((int(u["id"]) for u in usuarios), default=0) + 1)
    usuarios.append({
        "id":                nuevo_id,
        "username":          username,
        "nombre":            nombre,
        "rol":               rol,
        "hash":              pw_hash,
        "salt":              salt,
        "activo":            True,
        "empleado_vinculado": empleado_vinculado,
    })
    _guardar_usuarios(usuarios)
    return True, "Usuario creado"

def editar_usuario(uid: str, datos: dict) -> tuple[bool, str]:
    """
    datos puede contener: nombre, rol, activo, empleado_vinculado, password.
    username no se puede cambiar (es el identificador de login).
    """
    usuarios = _cargar_usuarios()
    idx = next((i for i, u in enumerate(usuarios) if u["id"] == uid), None)
    if idx is None:
        return False, "Usuario no encontrado"

    u = usuarios[idx]

    # Proteger: no puede desactivarse o degradarse el último admin activo
    if u["rol"] == "admin":
        admins_activos = [x for x in usuarios if x["rol"] == "admin" and x.get("activo", True)]
        if len(admins_activos) == 1:
            if datos.get("rol") not in (None, "admin"):
                return False, "No puedes cambiar el rol del único administrador activo"
            if datos.get("activo") is False:
                return False, "No puedes desactivar el único administrador activo"

    for campo in ("nombre", "rol", "activo", "empleado_vinculado"):
        if campo in datos:
            u[campo] = datos[campo]

    if "password" in datos and datos["password"]:
        u["hash"], u["salt"] = _hash_password(datos["password"])

    usuarios[idx] = u
    _guardar_usuarios(usuarios)
    return True, "Usuario actualizado"

def eliminar_usuario(uid: str) -> tuple[bool, str]:
    usuarios = _cargar_usuarios()
    target = next((u for u in usuarios if u["id"] == uid), None)
    if not target:
        return False, "Usuario no encontrado"
    if target["rol"] == "admin":
        admins = [u for u in usuarios if u["rol"] == "admin" and u.get("activo", True)]
        if len(admins) == 1:
            return False, "No puedes eliminar el único administrador"
    usuarios = [u for u in usuarios if u["id"] != uid]
    _guardar_usuarios(usuarios)
    return True, "Usuario eliminado"

# ─── SESIÓN ───────────────────────────────────────────────────────────────────

def login_usuario(usuario: dict):
    """Guarda el usuario en la sesión Flask."""
    session.permanent = False  # expira al cerrar el navegador
    session["uid"]      = usuario["id"]
    session["username"] = usuario["username"]
    session["rol"]      = usuario["rol"]
    session["nombre"]   = usuario["nombre"]
    session["vinculado"] = usuario.get("empleado_vinculado")

def logout_usuario():
    session.clear()

def usuario_actual() -> dict | None:
    """Devuelve datos básicos del usuario en sesión, o None."""
    if "uid" not in session:
        return None
    return {
        "id":        session["uid"],
        "username":  session["username"],
        "rol":       session["rol"],
        "nombre":    session["nombre"],
        "vinculado": session.get("vinculado"),
    }

def tiene_permiso(permiso: str) -> bool:
    rol = session.get("rol")
    if not rol:
        return False
    return permiso in PERMISOS.get(rol, set())

# ─── DECORADORES ─────────────────────────────────────────────────────────────

def login_requerido(f):
    """Rechaza peticiones sin sesión activa."""
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if not usuario_actual():
            return jsonify({"ok": False, "error": "No autenticado", "login": True}), 401
        return f(*args, **kwargs)
    return wrapper

def permiso_requerido(permiso: str):
    """Rechaza peticiones sin el permiso necesario."""
    def decorator(f):
        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            u = usuario_actual()
            if not u:
                return jsonify({"ok": False, "error": "No autenticado", "login": True}), 401
            if not tiene_permiso(permiso):
                return jsonify({
                    "ok": False,
                    "error": f"Sin permisos. Se requiere: {permiso}",
                    "rol_actual": u["rol"]
                }), 403
            return f(*args, **kwargs)
        return wrapper
    return decorator

# ─── LOG DE ACTIVIDAD ─────────────────────────────────────────────────────────

def _cargar_log() -> list[dict]:
    if not os.path.exists(LOG_FILE):
        return []
    with open(LOG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def registrar_accion(accion: str, detalle: str = "", usuario: dict = None):
    """
    Guarda una entrada en el log de actividad.
    Si usuario=None usa el de la sesión actual.
    """
    if usuario is None:
        usuario = usuario_actual() or {"username": "sistema", "rol": "sistema"}

    log = _cargar_log()
    log.append({
        "ts":       datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "usuario":  usuario.get("username", "?"),
        "rol":      usuario.get("rol", "?"),
        "accion":   accion,
        "detalle":  detalle,
        "ip":       request.remote_addr if request else "—",
    })
    # Guardar solo los últimos 1000 registros
    if len(log) > 1000:
        log = log[-1000:]
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)

def obtener_log(limite: int = 100) -> list[dict]:
    log = _cargar_log()
    return list(reversed(log[-limite:]))
