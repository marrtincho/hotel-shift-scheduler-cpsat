# HorariosRecepcion

Aplicación web interna para la gestión automatizada de horarios de personal hotelero, construida con Flask y desplegada en red local. Diseñada para reemplazar la planificación manual en Excel, resolviendo restricciones de cobertura, rotaciones y legislación laboral de forma automática.

> Proyecto en producción real: los horarios que genera esta herramienta se usan actualmente en el hotel.

---

## El problema

Planificar los turnos semanales de recepción y mozos en un hotel implica manejar simultáneamente docenas de restricciones que, a mano, consumen horas cada semana y generan errores frecuentes:

- Cobertura mínima y máxima por turno (Mañana, Tarde, Noche, Partido)
- Roles con turnos o días libres fijos (jefe, fijo de noche, subjefe...)
- Rotación equitativa del sustituto nocturno, con deuda explícita si la petición de un empleado impide que le toque esa semana
- Restricción de no asignar turno de Mañana tras Tarde o Noche (también entre semanas)
- Peticiones de días libres, vacaciones y festivos trabajados
- Balance histórico de Mañana/Tarde/Partido y de fines de semana libres por empleado
- Exportación a Excel para su distribución (Recepción y Mozos en el mismo archivo)

Ninguna herramienta genérica de scheduling cubre todas estas reglas a la vez. Esta aplicación las modela explícitamente.

---

## Solución técnica

### Motor de generación de horarios (`modelo_base.py`)

El núcleo del sistema es un motor genérico y parametrizable mediante un `DeptConfig`: un dataclass que encapsula todas las reglas de un departamento (coberturas mínimas/máximas, roles con turno fijo, días libres fijos, lógica de sustitución nocturna...).

Esto permite que el mismo motor genere horarios para **Recepción** y **Mozos** con configuraciones distintas sin duplicar lógica.

El algoritmo opera en tres fases:

**1. Selección del sustituto nocturno**

Elige al empleado elegible con menor puntuación neta `(coberturas - pendientes)`. Si el candidato con menor puntuación tiene una petición de libre que coincide con los días a cubrir, se le anota una deuda (`pending += 1`) en el historial y se pasa al siguiente. Esto garantiza que quien cedió su turno por una petición tenga prioridad explícita la semana siguiente. El historial (`night_rotation_*.json`) usa un formato compatible hacia atrás con versiones anteriores.

**2. Asignación de días libres**

Respeta primero los días forzados (vacaciones, peticiones ya concedidas, compensaciones). Para la pareja de días libres restante, balancea usando un bonus proporcional al déficit de fines de semana de cada empleado respecto a la media del grupo (en lugar de un bonus fijo de una sola vez).

**3. Asignación de turnos**

Dos sub-pasadas por día:
- *Sub-pass A (déficits)*: cubre los turnos con cobertura por debajo del mínimo. El empleado elige el turno deficitario que menos ha hecho históricamente (las preferencias son una guía suave de ‑0,5, no una regla absoluta).
- *Sub-pass B (resto)*: distribuye los empleados restantes ordenados por mayor desequilibrio histórico primero, para que quien más necesita un turno lo elija antes de que se agoten los slots. El criterio es el mínimo coste ajustado por preferencias sobre los turnos ya realizados.

Este diseño reduce la desviación estándar de Mañanas entre empleados de **24,3 a 3,8** en simulaciones de 20 semanas.

**Peticiones conflictivas**: si dos personas piden el mismo día libre y conceder ambas dejaría la cobertura por debajo del mínimo, se deniega la de quien más peticiones lleva concedidas históricamente. Las peticiones denegadas se devuelven en la respuesta de la API.

### API REST (`app.py`)

Flask sirve ~30 endpoints organizados por dominio: autenticación, plantilla de empleados, generación y edición de horarios, peticiones, vacaciones, festivos, compensaciones y estadísticas.

Las semanas pasadas quedan bloqueadas automáticamente: solo el rol `admin` puede modificarlas.

La clave secreta de sesión se puede inyectar mediante la variable de entorno `SECRET_KEY`; si no existe, se genera y persiste en disco.

### Sistema de roles y permisos (`auth.py`)

Autenticación por sesión con clave secreta persistente. Permisos granulares por rol: `gestionar_usuarios`, `editar_plantilla`, `generar_horario`, `exportar_horario`, `editar_vacaciones`, `editar_compensaciones`, `gestionar_roles`. Los decoradores `@login_requerido` y `@permiso_requerido` protegen cada endpoint.

Log de actividad persistente para todas las acciones relevantes.

### Estadísticas y trazabilidad

- Distribución histórica de turnos por empleado
- Fines de semana libres acumulados y balance respecto a la media del grupo
- Tasa de peticiones concedidas vs. denegadas
- Festivos trabajados y saldo de días LA pendientes
- Advertencias automáticas de violaciones de cobertura o restricciones

---

## Stack

- **Backend**: Python 3, Flask
- **Persistencia**: JSON en disco con escritura atómica (write-to-tmp + `os.replace`), sin base de datos externa
- **Exportación**: openpyxl (Excel combinado Recepción + Mozos, colores por turno, fórmulas de cobertura, freeze panes)
- **Frontend**: HTML/CSS/JS vanilla servido por Flask
- **Tests**: pytest, con datos en memoria y semanas en futuro lejano para no tocar datos de producción

---

## Estructura del proyecto

```
├── app.py                  # Servidor Flask, API REST (~30 endpoints)
├── modelo_base.py          # Motor genérico de scheduling + exportación Excel
├── modelo_recepcion.py     # DeptConfig específico de Recepción
├── modelo_mozos.py         # DeptConfig específico de Mozos
├── herramientas.py         # Persistencia: plantilla, historial, vacaciones, peticiones...
├── auth.py                 # Autenticación, sesiones, permisos, log de actividad
├── static/                 # Frontend (index.html, JS, CSS)
├── tests/                  # Pruebas de humo con pytest
├── scripts/                # Generador de datos de ejemplo
├── data/                   # JSONs de estado (plantilla, historial, rotaciones...)
├── requirements.txt        # Flask, openpyxl
├── requirements-dev.txt    # + pytest
└── instalar.bat            # Script de instalación para Windows (entorno del hotel)
```

---

## Decisiones de diseño destacadas

**Sin base de datos**: el sistema corre en la red local de un hotel con un equipo no técnico. SQLite o Postgres añadirían fricción de mantenimiento sin aportar ventajas reales para el volumen de datos manejado.

**Motor parametrizable por departamento**: en lugar de dos motores separados para Recepción y Mozos, un único `generate_schedule()` recibe un `DeptConfig`. Añadir un nuevo departamento es crear una nueva configuración, no nuevo código.

**Preferencias como guía suave**: las preferencias de turno de cada empleado influyen con un peso de ‑0,5 sobre el conteo histórico. Esto permite que un empleado que ha acumulado muchos turnos de Tarde por preferencia sea reorientado gradualmente hacia Mañana sin contradecir su preferencia de forma brusca.

**Deuda nocturna explícita**: si la petición de un empleado impide que cubra el turno nocturno cuando le toca en la rotación, se le anota `pending += 1` en el historial. La clave de ordenación es `covers - pending`, lo que lo sitúa automáticamente como primero en la rotación de la semana siguiente.

**Compensación nocturna entre semanas**: la lógica de sustitución detecta si los días de compensación del sustituto caen fuera de la semana actual y los persiste para aplicarlos en la siguiente generación, evitando violaciones del descanso mínimo sin intervención manual.

**Bloqueo de semanas pasadas**: una vez que una semana queda en el pasado, sus horarios se congelan. Solo el administrador puede modificarlos, previniendo cambios accidentales en registros históricos que afectarían las estadísticas.

---

## Licencia y uso

Este repositorio se publica con fines de portfolio y demostración técnica.
Todos los derechos reservados © 2026 Martín Ropero.

No se autoriza el uso, copia, modificación, distribución o despliegue de este
código, total o parcial, con fines comerciales o de producción sin permiso
expreso y por escrito del autor.

Si tu empresa está interesada en usar esta herramienta o en contratar
servicios de desarrollo relacionados, contáctame: mropero21@gmail.com.

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

**Credenciales por defecto**: usuario `admin`, contraseña `admin1234` (cambiar tras el primer login).

### Tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -v
```

### SECRET_KEY personalizada

```bash
SECRET_KEY=mi_clave_secreta python app.py
```

Si no se especifica, se genera automáticamente y se persiste en `data/.secret_key`.
