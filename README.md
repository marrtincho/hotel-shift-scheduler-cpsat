# HorariosRecepcion

Aplicación web interna para la gestión automatizada de horarios de personal hotelero, construida con Flask y desplegada en red local. Diseñada para reemplazar la planificación manual en Excel, resolviendo restricciones de cobertura, rotaciones y legislación laboral de forma automática.

> Proyecto en producción real: los horarios que genera esta herramienta se usan actualmente en el hotel.

---

## El problema

Planificar los turnos semanales de recepción y mozos en un hotel implica manejar simultáneamente docenas de restricciones que, a mano, consumen horas cada semana y generan errores frecuentes:

- Cobertura mínima y máxima por turno (Mañana, Tarde, Noche, Partido)
- Roles con turnos o días libres fijos (jefe, fijo de noche, subjefe...)
- Rotación justa del sustituto nocturno y compensación de días libres que puede cruzar semanas
- Restricción de no asignar turno de Mañana tras Tarde o Noche (también entre semanas)
- Peticiones de días libres, vacaciones y festivos trabajados
- Balance histórico de fines de semana libres por empleado
- Exportación a Excel para su distribución

Ninguna herramienta genérica de scheduling cubre todas estas reglas a la vez. Esta aplicación las modela explícitamente.

---

## Solución técnica

### Motor de generación de horarios (`modelo_base.py`)

El núcleo del sistema es un motor genérico y parametrizable mediante un `DeptConfig`: un dataclass que encapsula todas las reglas de un departamento (coberturas mínimas/máximas, roles con turno fijo, días libres fijos, lógica de sustitución nocturna...).

Esto permite que el mismo motor genere horarios para **Recepción** y **Mozos** con configuraciones distintas sin duplicar lógica.

El algoritmo opera en tres fases:

1. **Selección del sustituto nocturno**: elige al empleado elegible con menos coberturas históricas (rotación equitativa), respetando preferencias de compensación (cobrar vs. días LA). Si la compensación cae fuera de la semana actual, la persiste para aplicarla la semana siguiente.

2. **Asignación de días libres**: respeta primero los días forzados (vacaciones, peticiones, compensaciones), luego balancea las parejas de días libres restantes usando conteo histórico de fines de semana.

3. **Asignación de turnos**: dos sub-pasadas por día: primero cubre déficits de cobertura mínima, luego distribuye el resto respetando preferencias individuales y la restricción de no-Mañana-tras-Noche/Tarde, incluyendo el cruce de semana.

### API REST (`app.py`)

Flask sirve ~30 endpoints organizados por dominio: autenticación, plantilla de empleados, generación y edición de horarios, peticiones, vacaciones, festivos, compensaciones y estadísticas.

Las semanas pasadas quedan bloqueadas automáticamente: solo el rol `admin` puede modificarlas.

### Sistema de roles y permisos (`auth.py`)

Autenticación por sesión con clave secreta persistente en disco. Permisos granulares por rol: `gestionar_usuarios`, `editar_plantilla`, `generar_horario`, `exportar_horario`, `editar_vacaciones`, `editar_compensaciones`, `gestionar_roles`. Los decoradores `@login_requerido` y `@permiso_requerido` protegen cada endpoint.

Log de actividad persistente para todas las acciones relevantes.

### Estadísticas y trazabilidad

- Distribución histórica de turnos por empleado
- Fines de semana libres acumulados
- Tasa de peticiones concedidas
- Festivos trabajados y saldo de días LA pendientes
- Advertencias automáticas de violaciones de cobertura o restricciones

---

## Stack

- **Backend**: Python 3, Flask
- **Persistencia**: JSON en disco (sin base de datos externa; el sistema corre en la red local del hotel)
- **Exportación**: openpyxl (Excel con colores por turno, fórmulas de cobertura, freeze panes)
- **Frontend**: HTML/CSS/JS vanilla servido por Flask

---

## Estructura del proyecto

```
├── app.py               # Servidor Flask, API REST (~30 endpoints)
├── modelo_base.py       # Motor genérico de scheduling + exportación Excel
├── modelo_recepcion.py  # DeptConfig específico de Recepción
├── modelo_mozos.py      # DeptConfig específico de Mozos
├── herramientas.py      # Persistencia: plantilla, historial, vacaciones, peticiones...
├── auth.py              # Autenticación, sesiones, permisos, log de actividad
├── static/              # Frontend (index.html, JS, CSS)
├── data/                # JSONs de estado (plantilla, historial, rotaciones...)
└── instalar.bat         # Script de instalación para Windows (entorno del hotel)
```

---

## Decisiones de diseño destacadas

**Sin base de datos**: el sistema corre en la red local de un hotel con un equipo no técnico. SQLite o Postgres añadirían fricción de mantenimiento sin aportar ventajas reales para el volumen de datos manejado.

**Motor parametrizable por departamento**: en lugar de dos motores separados para Recepción y Mozos, un único `generate_schedule()` recibe un `DeptConfig`. Añadir un nuevo departamento es crear una nueva configuración, no nuevo código.

**Compensación nocturna entre semanas**: la lógica de sustitución detecta si los días de compensación del sustituto caen fuera de la semana actual y los persiste para aplicarlos en la siguiente generación. Esto evita violaciones del descanso mínimo sin intervención manual.

**Bloqueo de semanas pasadas**: una vez que una semana queda en el pasado, sus horarios se congelan. Solo el administrador puede modificarlos, previniendo cambios accidentales en registros históricos que afectarían las estadísticas.

---

## Instalación

```bash
# Clonar el repositorio
git clone https://github.com/marrtincho/HorariosRecepcion.git
cd HorariosRecepcion

# Crear entorno virtual e instalar dependencias
python -m venv venv
source venv/bin/activate  # En Windows: venv\Scripts\activate
pip install -r requirements.txt

# Arrancar el servidor
python app.py
```

Abrir en el navegador: `http://localhost:5000`

En Windows, el script `instalar.bat` automatiza la instalación del entorno.

**Credenciales por defecto**: usuario `admin`, contraseña `admin` (cambiar tras el primer login).
