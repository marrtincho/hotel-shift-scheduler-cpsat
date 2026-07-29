"""
scripts/generar_datos_ejemplo.py
Genera data-ejemplo/ con datos FICTICIOS para poder mostrar el proyecto
sin exponer datos reales de producción.
Usa las PLANTILLA_DEFAULT y ROLES_DEFAULT definidas en herramientas.py.
"""

import copy
import hashlib
import json
import os
import secrets
import sys

# Añadir el directorio raíz al path para poder importar el proyecto
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from herramientas import PLANTILLA_DEFAULT, ROLES_DEFAULT  # noqa: E402

DESTINO = os.path.join(ROOT, "data-ejemplo")


def guardar(path: str, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  Creado: {os.path.relpath(path, ROOT)}")


def _hash_password(password: str):
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 260000)
    return dk.hex(), salt


def main():
    print(f"\nGenerando datos de ejemplo en: {DESTINO}\n")

    # Plantillas ficticias (copiadas de PLANTILLA_DEFAULT, ya son ficticias)
    for dept_id, plantilla in PLANTILLA_DEFAULTa.items():
        nombre_archivo = "plantilla.json" if dept_id == "recepcion" else f"{dept_id}_plantilla.json"
        guardar(os.path.join(DESTINO, nombre_archivo), copy.deepcopy(plantilla))

    # Roles
    for dept_id, roles in ROLES_DEFAULT.items():
        nombre_archivo = "roles.json" if dept_id == "recepcion" else f"{dept_id}_roles.json"
        guardar(os.path.join(DESTINO, nombre_archivo), copy.deepcopy(roles))

    # Datos vacíos
    for nombre_archivo in [
        "historial.json", "mozos_historial.json",
        "peticiones.json", "mozos_peticiones.json",
        "vacaciones.json", "mozos_vacaciones.json",
        "compensaciones.json", "mozos_compensaciones.json",
        "festivos_trabajados.json",
        "actividad.json",
        "night_rotation_recepcion.json",
        "night_rotation_mozos.json",
        "night_covers_recepcion.json",
        "night_covers_mozos.json",
    ]:
        valor_vacio = [] if nombre_archivo in ("vacaciones.json", "mozos_vacaciones.json", "actividad.json") else {}
        guardar(os.path.join(DESTINO, nombre_archivo), valor_vacio)

    # Usuario administrador ficticio (contraseña: ejemplo1234)
    pw_hash, salt = _hash_password("ejemplo1234")
    usuarios = [{
        "id":                "1",
        "username":          "admin",
        "nombre":            "Administrador Ejemplo",
        "rol":               "admin",
        "hash":              pw_hash,
        "salt":              salt,
        "activo":            True,
        "empleado_vinculado": None,
    }]
    guardar(os.path.join(DESTINO, "usuarios.json"), usuarios)

    # Compensaciones iniciales (saldo 0 para todos)
    for dept_id, plantilla in PLANTILLA_DEFAULT.items():
        nombre_archivo = "compensaciones.json" if dept_id == "recepcion" else f"{dept_id}_compensaciones.json"
        comp = {p["name"]: {"modalidad": "cobrar", "la_saldo": 0} for p in plantilla}
        guardar(os.path.join(DESTINO, nombre_archivo), comp)

    # Carpeta de backups vacía
    os.makedirs(os.path.join(DESTINO, "backups"), exist_ok=True)
    print(f"  Creado: data-ejemplo/backups/")

    print(f"\nListo. Para usar estos datos en la app:")
    print(f"  cp -r {DESTINO}/* {os.path.join(ROOT, 'data')}/")
    print(f"  (o bien apunta DATA_DIR a '{DESTINO}' antes de arrancar)\n")


if __name__ == "__main__":
    main()
