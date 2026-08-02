"""
tests/test_produccion.py — Comprueba que la app está lista para desplegarse
con gunicorn: importable sin arrancar el servidor de desarrollo, y que las
rutas de datos respetan la configuración por variables de entorno.

Aísla siempre los datos en tmp_path: nunca toca data/ real.
"""

import importlib
import os
import sys


def test_app_importable_sin_ejecutar_run(tmp_path, monkeypatch):
    """Importar app.py no debe arrancar el servidor de desarrollo (app.run()
    está aislado tras el `if __name__ == "__main__"`), condición necesaria
    para que gunicorn pueda cargar `app:app`."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SECRET_KEY", "clave-de-prueba-no-usar-en-produccion")

    # auth.py fija USERS_FILE/LOG_FILE en base a DATA_DIR en el momento de su
    # propia importación (que puede haber ocurrido antes, con el DATA_DIR
    # real). Se parchean explícitamente para no tocar datos reales al
    # reimportar app.py, igual que hace la fixture usuarios_dir en
    # test_smoke.py.
    import auth
    monkeypatch.setattr(auth, "USERS_FILE", str(tmp_path / "usuarios.json"))
    monkeypatch.setattr(auth, "LOG_FILE", str(tmp_path / "actividad.json"))

    sys.modules.pop("app", None)
    sys.modules.pop("herramientas", None)
    try:
        import app as app_module
        assert app_module.app.__class__.__name__ == "Flask"
        assert app_module.app.secret_key == "clave-de-prueba-no-usar-en-produccion"
    finally:
        sys.modules.pop("app", None)
        sys.modules.pop("herramientas", None)


def test_data_dir_respeta_variable_de_entorno(tmp_path, monkeypatch):
    """DATA_DIR debe leer la variable de entorno DATA_DIR si está definida,
    y volver al valor por defecto (BASE_DIR/data) si deja de estarlo."""
    import herramientas

    custom_dir = str(tmp_path / "datos_custom")
    monkeypatch.setenv("DATA_DIR", custom_dir)
    try:
        importlib.reload(herramientas)
        assert herramientas.DATA_DIR == custom_dir
        assert herramientas.BACKUP_DIR == os.path.join(custom_dir, "backups")
    finally:
        monkeypatch.delenv("DATA_DIR", raising=False)
        importlib.reload(herramientas)
        assert herramientas.DATA_DIR == os.path.join(herramientas.BASE_DIR, "data")
