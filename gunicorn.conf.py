"""Configuración de gunicorn para producción (Podman + Quadlet)."""

bind = "0.0.0.0:8000"
# Un solo worker y un solo hilo: el almacenamiento es JSON con lectura +
# modificación + escritura (no hay locking entre peticiones concurrentes),
# así que solo un proceso/hilo puede ejecutar una petición a la vez para
# evitar condiciones de carrera (una petición podría pisar los cambios de
# otra al leer el fichero antes de que la primera termine de escribirlo).
# Con threads > 1 esto ya no está garantizado dentro del mismo proceso.
workers = 1
threads = 1
timeout = 60
accesslog = "-"
errorlog = "-"
