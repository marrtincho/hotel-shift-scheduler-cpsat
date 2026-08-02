#!/bin/bash
# backup.sh — Copia de seguridad diaria de los datos de Horarios Recepcion.
# Comprime %h/horarios-data y conserva solo los últimos 14 backups.
set -euo pipefail

ORIGEN_DIR="horarios-data"
ORIGEN="$HOME/$ORIGEN_DIR"
DESTINO="$HOME/backups-horarios"
RETENER=14
FECHA=$(date +%F)
ARCHIVO="$DESTINO/horarios-data-$FECHA.tar.gz"

mkdir -p "$DESTINO"

if [ ! -d "$ORIGEN" ]; then
    echo "[ERROR] No existe $ORIGEN, nada que respaldar" >&2
    exit 1
fi

tar -czf "$ARCHIVO" -C "$HOME" "$ORIGEN_DIR"
echo "Backup creado: $ARCHIVO"

# Conserva solo los últimos $RETENER backups, borra los más antiguos
cd "$DESTINO"
# shellcheck disable=SC2012
ls -1t horarios-data-*.tar.gz 2>/dev/null | tail -n "+$((RETENER + 1))" | xargs -r rm -f

# Subida externa opcional a almacenamiento remoto (descomentar y configurar
# un remoto rclone previamente con `rclone config`):
# rclone copy "$ARCHIVO" remoto:hotel-backups/horarios/
