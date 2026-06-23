"""
modelo_mozos.py — Configuración del departamento de Mozos.
Reglas propias:
- 5 personas, sin jefe ni subjefe
- night_fixed libra Vie(4)+Sáb(5)
- Sustituto cubre Vie+Sáb noche y recibe Lun+Mar de la SIGUIENTE semana
- 1 persona por turno (Mañana/Tarde/Noche), Partido solo si es absolutamente necesario
- Todos los roles son "normal" o "night_fixed"
"""
from modelo_base import DeptConfig, generate_schedule, export_to_excel, DAYS

MOZOS_CONFIG = DeptConfig(
    dept_id    = "mozos",
    dept_label = "Mozos",
    # night_fixed libra Vie(4)+Sáb(5)
    night_fixed_days_off      = [4, 5],
    # compensación: Lun+Mar de la semana siguiente (gestionado como petición)
    night_cover_compensation  = [0, 1],
    min_coverage = {"Mañana": 1, "Tarde": 1, "Noche": 1, "Partido": 0},
    max_coverage = {"Mañana": 1, "Tarde": 1, "Noche": 1, "Partido": 2},
    no_night_roles = set(),          # todos pueden hacer noche
    fixed_roles    = {},             # sin roles fijos de turno
    fixed_days_off = {},             # sin roles con días fijos
    cover_consecutive          = False,  # libres pueden no ser consecutivos
    night_rotation_key         = "mozos",
    allow_non_consecutive_cover= True,
    prefer_next_week_for_cover = True,   # Lun+Mar de semana siguiente
)

STAFF_DEFAULT = [
    {"name": "Fran",    "role": "night_fixed", "preferences": ["Noche"]},
    {"name": "Rubén",   "role": "normal",      "preferences": ["Mañana"]},
    {"name": "Gloria",  "role": "normal",      "preferences": ["Tarde"]},
    {"name": "Tomás",   "role": "normal",      "preferences": []},
    {"name": "Inés",    "role": "normal",      "preferences": ["Mañana"]},
]
