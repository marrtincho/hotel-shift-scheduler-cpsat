# Carpeta `data/`

Esta carpeta **no se versiona** (está en `.gitignore`).

Se crea y puebla automáticamente al arrancar la aplicación con datos por defecto
(plantilla de ejemplo, roles y usuario admin).

## Archivos que genera la app

| Archivo | Contenido |
|---|---|
| `plantilla.json` / `mozos_plantilla.json` | Empleados |
| `roles.json` / `mozos_roles.json` | Categorías de empleado |
| `historial.json` / `mozos_historial.json` | Horarios generados |
| `peticiones.json` / `mozos_peticiones.json` | Peticiones de libre |
| `vacaciones.json` / `mozos_vacaciones.json` | Periodos de vacaciones |
| `compensaciones.json` / `mozos_compensaciones.json` | Saldo LA y modalidad |
| `festivos_trabajados.json` | Registro de festivos trabajados |
| `actividad.json` | Log de acciones de usuario |
| `usuarios.json` | Usuarios del sistema (hashes de contraseña) |
| `.secret_key` | Clave secreta para firmar sesiones Flask |
| `backups/` | Copias automáticas fechadas antes de cada escritura |

## Secret key

Si la clave `.secret_key` ha sido expuesta, bórrala o define `SECRET_KEY`
como variable de entorno (tiene prioridad):

```bash
export SECRET_KEY=$(python -c "import secrets; print(secrets.token_hex(32))")
```

## Datos de ejemplo

Para explorar el proyecto sin datos reales:

```bash
python scripts/generar_datos_ejemplo.py
```

Genera `data-ejemplo/` con plantilla, roles y usuarios ficticios.
