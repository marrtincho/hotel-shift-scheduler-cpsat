# HorariosRecepcion

Aplicación web para la planificación automática de turnos de personal hotelero.
Modela las reglas reales de un departamento —convenio, cobertura de servicio,
vacaciones, rotación nocturna— como un problema de satisfacción de restricciones
y resuelve la semana completa con **CP-SAT** (Google OR-Tools).

> Proyecto en producción real: los horarios que genera se usan actualmente en el hotel.

---

## El problema

Planificar los turnos semanales de recepción y mozos implica satisfacer a la vez
decenas de restricciones que, a mano, consumen horas cada semana y generan errores:

- Cobertura mínima y máxima por turno (Mañana, Tarde, Noche, Partido) y por día.
- Descanso mínimo legal de 9 horas entre turnos de días consecutivos.
- Exactamente 2 días libres consecutivos por semana y persona (derecho contractual).
- Rotación nocturna en bloques, con orden fijo y máximo de noches encadenadas.
- Roles con turno fijo o días libres fijos (jefe, fijo de noche) y roles que cubren
  la ausencia de otros (el subjefe cubre al jefe).
- Vacaciones, peticiones de días libres con prioridad y cupos, y festivos trabajados.
- Equidad a largo plazo: reparto de fines de semana libres y de turnos entre personas.

Todo esto además tiene que encajar **entre semanas**: el descanso de una racha de
noches puede desbordar al lunes siguiente, y el turno del domingo condiciona el del
lunes.

## Enfoque

El núcleo es un modelo de programación por restricciones. La variable de decisión es
`x[persona, día, turno]` booleana; una sola llamada al solver resuelve la semana
entera.

- Lo que **nunca** puede violarse (convenio, cobertura mínima, descanso) son
  restricciones **duras**.
- Lo que es preferible pero negociable (equidad, continuidad de turno, evitar
  turnos partidos) son **términos del objetivo** con pesos calibrados y
  justificados.

Esa separación es la decisión de diseño central: permite que el sistema degrade con
elegancia. Ante una semana imposible produce el mejor horario alcanzable y avisa de
dónde se excedió, en lugar de fallar sin más — y cuando algo es genuinamente
irresoluble, un **diagnóstico de infactibilidad** devuelve el subconjunto irreducible
de reglas en conflicto, para que el encargado sepa exactamente qué ceder.

**Las reglas completas —cuál es dura, cuál blanda, con qué peso y por qué— están
documentadas en [`REGLAS.md`](REGLAS.md)**, que se mantiene como fuente única de
verdad frente al código.

### Determinismo

El mismo input produce siempre el mismo horario: el solver corre mono-hilo y las
restricciones se añaden en orden estable. No es un detalle menor — sin ello, ante un
empate de valor óptimo, regenerar una semana devolvía un horario distinto y el
equipo perdía la confianza en la herramienta.

---

## Arquitectura

```
app.py                   Servidor Flask, API REST (~58 endpoints)
├── motor_cpsat.py       Motor de horarios (CP-SAT): restricciones, objetivo,
│                        orquestación semanal y diagnóstico de infactibilidad
├── cobertura_nocturna.py  Decide QUIÉN cubre cada noche (orden fijo cíclico)
├── modelo_base.py       Motor heurístico anterior + tipos compartidos y
│                        exportación a Excel
├── modelo_recepcion.py  Configuración por departamento (DeptConfig)
├── modelo_mozos.py
├── herramientas.py      Persistencia JSON, plantilla, vacaciones, peticiones
├── cupos.py             Cupos mensuales de peticiones por empleado
├── estadisticas.py      Métricas de reparto e histórico
├── auth.py              Autenticación, roles y permisos, log de actividad
└── static/index.html    Frontend (HTML/CSS/JS sin framework)

REGLAS.md                Reglas de negocio del motor — fuente única de verdad
docs/                    Informe histórico de validación del motor
deploy/                  Podman Quadlet + Cloudflare Tunnel + backups
tests/                   297 tests (pytest)
```

Quién cubre cada noche **no lo decide el solver**: lo resuelve `cobertura_nocturna.py`
antes de construir el modelo y se fija como restricción dura. Así la rotación nocturna
es predecible y auditable para el equipo, en vez de un subproducto de la optimización.
El admin configura ese orden desde la interfaz.

### Motor configurable

`modelo_base.py` conserva el motor heurístico por fases con el que arrancó el
proyecto. El motor activo se elige en caliente (`data/config_motor.json` o la
variable de entorno `USAR_MOTOR_CPSAT`), lo que permitió migrar a CP-SAT
comparando ambos sobre semanas reales antes de conmutar.

### Sistema de roles y permisos

Tres roles —`admin`, `encargado`, `empleado`— con permisos granulares y decoradores
`@login_requerido` / `@permiso_requerido` en cada endpoint. Contraseñas con
PBKDF2-SHA256, sesiones firmadas y log de actividad persistente.

Las semanas pasadas se congelan automáticamente: solo el admin puede modificarlas,
para que el histórico que alimenta las métricas de equidad no cambie bajo los pies.

---

## Stack

- **Backend**: Python 3, Flask
- **Solver**: Google OR-Tools (CP-SAT)
- **Persistencia**: JSON en disco con escritura atómica (`write-to-tmp` + `os.replace`)
- **Exportación**: openpyxl, generado en memoria
- **Frontend**: HTML/CSS/JS vanilla
- **Despliegue**: Podman rootless + systemd (Quadlet) + Cloudflare Tunnel
- **Tests**: pytest

**Sin base de datos**: el sistema corre en la red local de un hotel con un equipo no
técnico. Para este volumen de datos, SQLite o Postgres añadirían mantenimiento sin
aportar nada; los ficheros JSON son inspeccionables y respaldables con `tar`.

---

## Instalación

```bash
git clone https://github.com/marrtincho/HorariosRecepcion.git
cd HorariosRecepcion

python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt

python app.py                     # http://localhost:5000
```

Credenciales por defecto: `admin` / `admin1234` (cambiar tras el primer acceso).
En Windows, `instalar.bat` automatiza el proceso.

Con `data/` vacío la aplicación arranca sin datos; `scripts/generar_datos_ejemplo.py`
crea una plantilla de prueba a partir de `data-ejemplo/`.

### Tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

La suite completa tarda unos 12 minutos: la mayoría de los tests resuelven modelos
CP-SAT reales, incluidas cadenas de 12-18 semanas encadenadas para verificar
propiedades que solo fallan por acumulación entre semanas.

### Despliegue

Ver [`deploy/DESPLIEGUE.md`](deploy/DESPLIEGUE.md) para la guía completa con Podman
rootless, Quadlet y Cloudflare Tunnel.

```bash
SECRET_KEY=una_clave_larga_aleatoria python app.py
```

Si no se define `SECRET_KEY`, se genera y persiste en `data/.secret_key`.

---

## Licencia y uso

Este repositorio se publica con fines de portfolio y demostración técnica.
Todos los derechos reservados © 2026 Martín Ropero.

No se autoriza el uso, copia, modificación, distribución o despliegue de este código,
total o parcial, con fines comerciales o de producción sin permiso expreso y por
escrito del autor.

Si tu empresa está interesada en usar esta herramienta o en contratar servicios de
desarrollo relacionados: mropero21@gmail.com
