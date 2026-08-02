FROM python:3.12-slim

WORKDIR /app
RUN useradd -m appuser

COPY requirements-prod.txt .
RUN pip install --no-cache-dir -r requirements-prod.txt

COPY . .
# Datos reales, entorno de desarrollo y ficheros generados no deben viajar
# dentro de la imagen (ya excluidos por .containerignore; se repite aquí
# como red de seguridad).
RUN rm -rf data data-ejemplo tests .git .idea *.xlsx

USER appuser
EXPOSE 8000
CMD ["gunicorn", "-c", "gunicorn.conf.py", "app:app"]
