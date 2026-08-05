"""
tests/test_exportar_excel.py — Descarga del Excel con BASE_DIR NO escribible.

REGRESIÓN de producción: la exportación funcionaba en local pero fallaba
siempre a través del despliegue real con un genérico "Error al exportar".

Causa: los endpoints escribían el .xlsx en BASE_DIR y luego servían esa ruta.
En el contenedor BASE_DIR es /app, que el Containerfile llena con `COPY . .`
como root y después cambia a `USER appuser` — appuser no puede escribir ahí,
wb.save() lanzaba PermissionError, Flask devolvía su 500 en HTML y el
frontend (que espera JSON) caía en el mensaje genérico. No tenía relación con
Cloudflare; el túnel solo era la vía por la que se usaba el contenedor.

Estos tests reproducen esa condición haciendo BASE_DIR de solo lectura, que
es lo que ninguna prueba anterior cubría: en local el directorio del proyecto
siempre es escribible, así que el fallo era invisible para la suite.
"""

import os
import stat
from datetime import date

import pytest


SEMANA = date(2026, 8, 10)
NOMBRES = ("Ana", "Luis", "Marta")


@pytest.fixture
def entorno(tmp_path, monkeypatch):
    backup = tmp_path / "backups"
    backup.mkdir()
    solo_lectura = tmp_path / "app_ro"
    solo_lectura.mkdir()

    import herramientas
    import app as app_module

    monkeypatch.setattr(herramientas, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(herramientas, "BACKUP_DIR", str(backup))
    monkeypatch.setattr(herramientas, "INTERVENCIONES_FILE", str(tmp_path / "intervenciones.json"))
    monkeypatch.setattr(app_module, "DATA_DIR", str(tmp_path))
    # Réplica del contenedor: el directorio de la app NO es escribible.
    monkeypatch.setattr(app_module, "BASE_DIR", str(solo_lectura))

    from herramientas import guardar_plantilla, registrar_semana
    staff = [{"name": n, "role": "normal", "preferences": []} for n in NOMBRES]
    guardar_plantilla(staff, "recepcion")
    registrar_semana(
        SEMANA,
        {n: ["Mañana"] * 5 + ["Libre", "Libre"] for n in NOMBRES},
        staff,
        "recepcion",
    )

    os.chmod(solo_lectura, stat.S_IRUSR | stat.S_IXUSR)
    yield solo_lectura
    # Restaurado para que tmp_path se pueda limpiar.
    os.chmod(solo_lectura, stat.S_IRWXU)


@pytest.fixture
def client(entorno):
    import app as app_module
    app_module.app.config["TESTING"] = True
    c = app_module.app.test_client()
    with c.session_transaction() as sess:
        sess["uid"] = 1
        sess["username"] = "admin"
        sess["rol"] = "admin"
        sess["nombre"] = "Admin"
        sess["vinculado"] = None
    return c


def _es_xlsx(datos: bytes) -> bool:
    """Un .xlsx es un ZIP: empieza por la firma PK\\x03\\x04."""
    return datos[:4] == b"PK\x03\x04"


def test_exportar_funciona_con_base_dir_de_solo_lectura(client):
    r = client.post("/api/horario/exportar",
                    json={"dept": "recepcion", "semana": SEMANA.isoformat()})

    assert r.status_code == 200, f"debía descargar el Excel: {r.get_data()[:200]}"
    assert _es_xlsx(r.get_data()), "el cuerpo debe ser un .xlsx real"
    assert "spreadsheetml" in r.headers.get("Content-Type", "")
    assert "horario_2026-08-10.xlsx" in r.headers.get("Content-Disposition", "")


def test_exportar_semana_funciona_con_base_dir_de_solo_lectura(client):
    r = client.get(f"/api/horario/exportar-semana/{SEMANA.isoformat()}?dept=recepcion")

    assert r.status_code == 200, f"debía descargar el Excel: {r.get_data()[:200]}"
    assert _es_xlsx(r.get_data())


def test_exportar_no_escribe_ningun_fichero_en_disco(client, entorno):
    """El .xlsx se genera en memoria: además de no necesitar un directorio
    escribible, evita que dos exportaciones simultáneas de la misma semana
    se pisen sobre un nombre de fichero fijo."""
    client.post("/api/horario/exportar",
                json={"dept": "recepcion", "semana": SEMANA.isoformat()})
    assert os.listdir(entorno) == [], (
        f"no debe quedar ningún fichero en BASE_DIR: {os.listdir(entorno)}"
    )


def test_exportar_semana_inexistente_devuelve_json_no_un_500(client):
    """El frontend espera JSON en los errores: si el backend devolviera HTML
    (como hacía el 500 por PermissionError) el usuario solo vería el mensaje
    genérico 'Error al exportar' sin saber la causa."""
    r = client.get("/api/horario/exportar-semana/2099-01-05?dept=recepcion")

    assert r.status_code == 404
    assert r.is_json, f"debía ser JSON: {r.get_data()[:200]}"
    assert r.get_json()["ok"] is False
