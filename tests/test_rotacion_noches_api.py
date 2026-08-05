"""
tests/test_rotacion_noches_api.py — Endpoints de la ronda de noches
(/api/rotacion-noches), la pantalla con la que el admin fija en qué orden se
turnan los empleados para cubrir las noches.

El orden es una LISTA FIJA: la generación semanal no la reordena, solo avanza
el puntero `siguiente_turno` por ella (ver cobertura_nocturna._elegir_orden_fijo
y REGLAS.md §1.9).

Mismo patrón que tests/test_permisos.py: DATA_DIR temporal y sesión forjada en
el test client de Flask, sin login real.
"""

import pytest


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    backup = tmp_path / "backups"
    backup.mkdir()

    import herramientas
    import app as app_module

    monkeypatch.setattr(herramientas, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(herramientas, "BACKUP_DIR", str(backup))
    monkeypatch.setattr(herramientas, "INTERVENCIONES_FILE", str(tmp_path / "intervenciones.json"))
    monkeypatch.setattr(app_module, "DATA_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def client(data_dir):
    import app as app_module
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def _sesion(client, rol):
    with client.session_transaction() as sess:
        sess["uid"] = 1
        sess["username"] = f"u_{rol}"
        sess["rol"] = rol
        sess["nombre"] = rol.capitalize()
        sess["vinculado"] = None


def _seed(dept_id="recepcion"):
    """Plantilla con roles mezclados: solo los 'normal' pueden cubrir noches
    (jefe/subjefe/adjunto llevan sin_noches en la config de Recepción)."""
    from herramientas import guardar_plantilla
    guardar_plantilla([
        {"name": "Jose", "role": "jefe", "preferences": []},
        {"name": "Martin", "role": "normal", "preferences": []},
        {"name": "Carlos", "role": "normal", "preferences": []},
        {"name": "Vero", "role": "subjefe", "preferences": []},
        {"name": "Seba", "role": "normal", "preferences": []},
    ], dept_id)


# ─── Permisos: solo admin ────────────────────────────────────────────────

@pytest.mark.parametrize("rol", ["encargado", "empleado"])
def test_solo_admin_puede_ver_la_ronda(client, rol):
    _seed()
    _sesion(client, rol)
    assert client.get("/api/rotacion-noches?dept=recepcion").status_code == 403


@pytest.mark.parametrize("rol", ["encargado", "empleado"])
def test_solo_admin_puede_reordenar(client, rol):
    _seed()
    _sesion(client, rol)
    r = client.put("/api/rotacion-noches",
                   json={"dept": "recepcion", "orden": ["Carlos", "Martin", "Seba"]})
    assert r.status_code == 403


def test_sin_sesion_no_se_puede_ver(client):
    _seed()
    r = client.get("/api/rotacion-noches?dept=recepcion")
    assert r.get_json().get("login") is True


# ─── GET: solo los elegibles, en orden de plantilla ──────────────────────

def test_get_devuelve_solo_empleados_que_pueden_cubrir_noches(client):
    _seed()
    _sesion(client, "admin")

    d = client.get("/api/rotacion-noches?dept=recepcion").get_json()["data"]
    nombres = [e["nombre"] for e in d["orden"]]

    assert nombres == ["Martin", "Carlos", "Seba"], (
        "jefe/subjefe llevan sin_noches y no entran en la ronda; el orden "
        "inicial es el de la plantilla"
    )
    assert d["siguiente_turno"] == "Martin"
    assert all("noches_cubiertas" in e and "deuda" in e for e in d["orden"])


def test_get_incorpora_altas_al_final_sin_perder_el_orden_guardado(client):
    """Un fichaje nuevo aparece AL FINAL de la ronda, sin alterar el orden que
    el admin había fijado para los demás (reconciliar_orden)."""
    _seed()
    _sesion(client, "admin")

    client.put("/api/rotacion-noches",
               json={"dept": "recepcion", "orden": ["Seba", "Martin", "Carlos"]})

    from herramientas import cargar_plantilla, guardar_plantilla
    staff = cargar_plantilla("recepcion")
    staff.append({"name": "Nuevo", "role": "normal", "preferences": []})
    guardar_plantilla(staff, "recepcion")

    d = client.get("/api/rotacion-noches?dept=recepcion").get_json()["data"]
    assert [e["nombre"] for e in d["orden"]] == ["Seba", "Martin", "Carlos", "Nuevo"]


# ─── PUT: guarda y valida ────────────────────────────────────────────────

def test_put_guarda_el_orden_y_lo_devuelve_el_get(client):
    _seed()
    _sesion(client, "admin")

    r = client.put("/api/rotacion-noches",
                   json={"dept": "recepcion", "orden": ["Seba", "Carlos", "Martin"]})
    assert r.status_code == 200

    d = client.get("/api/rotacion-noches?dept=recepcion").get_json()["data"]
    assert [e["nombre"] for e in d["orden"]] == ["Seba", "Carlos", "Martin"]


def test_put_conserva_siguiente_turno_si_sigue_en_la_lista(client):
    """Reordenar la ronda no debe saltarse ni repetir a quien ya le tocaba."""
    _seed()
    _sesion(client, "admin")

    client.put("/api/rotacion-noches",
               json={"dept": "recepcion", "orden": ["Martin", "Carlos", "Seba"]})
    # Se fuerza el puntero a Carlos, como si ya se hubieran generado semanas.
    import app as app_module
    estado = app_module._cargar_estado_cpsat("recepcion")["estado_rotacion"]
    estado["siguiente_turno"] = "Carlos"
    app_module._guardar_estado_cpsat("recepcion", estado)

    r = client.put("/api/rotacion-noches",
                   json={"dept": "recepcion", "orden": ["Seba", "Carlos", "Martin"]})
    assert r.get_json()["data"]["siguiente_turno"] == "Carlos"


@pytest.mark.parametrize("orden,motivo", [
    (["Martin", "Carlos"], "falta un elegible"),
    (["Martin", "Carlos", "Seba", "Jose"], "incluye a alguien que no puede cubrir noches"),
    (["Martin", "Martin", "Carlos"], "repite un empleado"),
    (["Martin", "Carlos", "Fantasma"], "nombre inexistente"),
    ("Martin,Carlos", "no es una lista"),
])
def test_put_rechaza_ordenes_invalidos(client, orden, motivo):
    """Se valida en vez de reconciliar en silencio: un orden incompleto
    enviado por error dejaría gente fuera de la ronda sin que nadie lo note."""
    _seed()
    _sesion(client, "admin")

    r = client.put("/api/rotacion-noches", json={"dept": "recepcion", "orden": orden})
    assert r.status_code == 400, f"debía rechazarse ({motivo}): {r.get_json()}"

    # Y el orden guardado no se toca.
    d = client.get("/api/rotacion-noches?dept=recepcion").get_json()["data"]
    assert [e["nombre"] for e in d["orden"]] == ["Martin", "Carlos", "Seba"]


# ─── El orden fijado es el que usa la generación ─────────────────────────

def test_el_orden_fijado_por_el_admin_es_el_que_se_usa_al_generar(client, data_dir):
    """Extremo a extremo: se fija un orden distinto al de plantilla y la
    ronda de noches lo respeta, sin reordenarse por cantidad de noches."""
    _seed()
    _sesion(client, "admin")
    client.put("/api/rotacion-noches",
               json={"dept": "recepcion", "orden": ["Seba", "Carlos", "Martin"]})

    import app as app_module
    from cobertura_nocturna import seleccionar_candidato_cobertura_nocturna

    cfg = app_module._get_dept_config("recepcion")
    from herramientas import cargar_plantilla
    staff = cargar_plantilla("recepcion")
    estado = app_module._cargar_estado_cpsat("recepcion")["estado_rotacion"]

    elegidos = []
    for _ in range(6):
        nombre, estado = seleccionar_candidato_cobertura_nocturna(staff, cfg, estado)
        elegidos.append(nombre)

    # La ronda es un CICLO y se entra por `siguiente_turno`, que se conserva al
    # reordenar (ver test_put_conserva_siguiente_turno_...), así que la
    # secuencia es una ROTACIÓN del orden configurado — el mismo anillo, no
    # necesariamente empezando por el primero de la lista.
    orden = ["Seba", "Carlos", "Martin"]
    inicio = orden.index(elegidos[0])
    esperado = [orden[(inicio + i) % len(orden)] for i in range(6)]
    assert elegidos == esperado, (
        f"la ronda debe recorrer linealmente el orden fijado por el admin "
        f"{orden} (entrando por {elegidos[0]}): {elegidos}"
    )
