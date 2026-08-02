"""
Varios tests de este directorio (motor_cpsat.py y lo que dependa de él)
requieren `ortools`, que solo está instalado en el entorno virtual
.venv_cpsat/ del proyecto (ver README.md) — el .venv/ por defecto de
HorariosRecepcion/ no lo trae. Sin este check, ejecutar pytest con el
entorno equivocado activo produce un ModuleNotFoundError genérico dentro
de la traza de import de cada test, indistinguible a simple vista de un
fallo real del código.
"""
try:
    import ortools  # noqa: F401
except ModuleNotFoundError:
    import pytest

    pytest.exit(
        "ortools no está disponible en este entorno de Python. "
        "Activa el entorno virtual .venv_cpsat antes de correr los tests "
        "(ver README.md):\n\n"
        "  cd HorariosRecepcion-cpsat && source .venv_cpsat/bin/activate\n",
        returncode=1,
    )
