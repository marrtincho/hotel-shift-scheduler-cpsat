"""
modelo_recepcion.py — Configuración del departamento de Recepción.
Importa el motor genérico de modelo_base.py.
"""
from modelo_base import DeptConfig, generate_schedule, export_to_excel, DAYS

RECEPCION_CONFIG = DeptConfig(
    dept_id    = "recepcion",
    dept_label = "Recepción",
    # night_fixed libra Jue(3)+Vie(4)
    night_fixed_days_off      = [3, 4],
    # sustituto recibe Sáb(5)+Dom(6)
    night_cover_compensation  = [5, 6],
    min_coverage = {"Mañana": 2, "Tarde": 2, "Noche": 1, "Partido": 0},
    max_coverage = {"Mañana": 3, "Tarde": 3, "Noche": 1, "Partido": 1},
    no_night_roles = {"jefe", "subjefe", "adjunto_recepcion"},
    fixed_roles    = {"jefe": "Mañana"},
    fixed_days_off = {"jefe": [5, 6]},
    cover_consecutive          = True,
    night_rotation_key         = "recepcion",
    allow_non_consecutive_cover= False,
    prefer_next_week_for_cover = False,
)

STAFF_DEFAULT = [
    {"name": "Carlos",  "role": "night_fixed",       "preferences": ["Noche"]},
    {"name": "Juan",    "role": "jefe",               "preferences": ["Mañana"]},
    {"name": "Laura",   "role": "subjefe",            "preferences": ["Tarde"]},
    {"name": "Pedro",   "role": "normal",             "preferences": ["Mañana", "Partido"]},
    {"name": "Ana",     "role": "normal",             "preferences": []},
    {"name": "Miguel",  "role": "normal",             "preferences": ["Tarde"]},
    {"name": "Sofía",   "role": "adjunto_recepcion",  "preferences": ["Mañana"]},
    {"name": "Diego",   "role": "normal",             "preferences": []},
]
