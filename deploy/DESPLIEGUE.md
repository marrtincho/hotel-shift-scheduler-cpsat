# Despliegue en producción — Podman rootless + Quadlet + Cloudflare Tunnel

Guía para desplegar Horarios Recepción en un servidor Fedora usando Podman
rootless, gestionado por systemd de usuario mediante Quadlet, con acceso
externo a través de un túnel de Cloudflare (sin abrir puertos en el router).

Todos los comandos se ejecutan como el usuario normal (rootless), **no**
como root ni con `sudo`, salvo que se indique lo contrario.

---

## 0. Requisitos previos

- Fedora con `podman` instalado (incluye Quadlet desde Podman 4.4+).
- Un túnel de Cloudflare ya creado en el dashboard de Cloudflare Zero Trust,
  con su `TUNNEL_TOKEN`.
- Habilitar que los servicios de usuario sigan corriendo tras cerrar sesión:

  ```bash
  loginctl enable-linger "$USER"
  ```

  Sin esto, systemd detiene los servicios de usuario al cerrar la sesión SSH.

---

## 1. Construir la imagen

Desde la raíz del proyecto:

```bash
podman build -t localhost/horarios:latest -f Containerfile .
```

Verifica que arranca correctamente antes de continuar:

```bash
podman run --rm -p 127.0.0.1:8000:8000 \
  -e SECRET_KEY=prueba \
  localhost/horarios:latest
```

Abre `http://localhost:8000` y confirma que la app responde. Detén el
contenedor con `Ctrl+C`.

---

## 2. Preparar directorios de datos y configuración

```bash
mkdir -p ~/horarios-data
mkdir -p ~/.config/horarios
mkdir -p ~/.config/containers/systemd
```

El contenedor corre como `appuser` (usuario no root dentro del contenedor,
creado con `useradd -m` en el `Containerfile`). Con Podman rootless, el UID
de `appuser` dentro del contenedor se traduce a un UID distinto en el host
mediante el mapeo de subuids. El volumen se monta con la opción `:Z` para
que SELinux etiquete el contenido correctamente; si al arrancar ves errores
de permisos al escribir en `/app/data`, ajusta la propiedad del volumen con:

```bash
podman unshare chown -R $(podman run --rm localhost/horarios:latest id -u appuser):$(podman run --rm localhost/horarios:latest id -g appuser) ~/horarios-data
```

---

## 3. Crear `~/.config/horarios/env` con datos reales

Copia la plantilla y rellénala:

```bash
cp deploy/env.example ~/.config/horarios/env
chmod 600 ~/.config/horarios/env
```

Edita `~/.config/horarios/env`:

```
SECRET_KEY=<genera una clave larga aleatoria, p.ej. con: openssl rand -hex 32>
TUNNEL_TOKEN=<token del túnel de Cloudflare>
```

Este fichero contiene secretos: no lo subas al repositorio (ya está fuera
del control de versiones al vivir en `~/.config`, no en el proyecto).

---

## 4. Copiar los Quadlets

```bash
cp deploy/horarios.container deploy/cloudflared.container \
   ~/.config/containers/systemd/
```

Recarga systemd para que genere las unidades a partir de los Quadlets:

```bash
systemctl --user daemon-reload
```

---

## 5. Arrancar los servicios

Para probar antes de comprometerte a que arranquen automáticamente en cada
reinicio, usa `start` (no `enable`):

```bash
systemctl --user start horarios
systemctl --user start cloudflared
```

Verifica el estado:

```bash
systemctl --user status horarios cloudflared
```

---

## 6. Verificación

Sigue los logs en vivo:

```bash
journalctl --user -u horarios -f
```

En otra terminal, para el túnel:

```bash
journalctl --user -u cloudflared -f
```

Confirma que la app responde localmente:

```bash
curl -I http://127.0.0.1:8000
```

Y que el dominio del túnel de Cloudflare (configurado en el dashboard de
Cloudflare Zero Trust) sirve la misma app desde fuera de la red local.

---

## 7. Pasar de `start` a `enable` (producción permanente)

Una vez verificado que todo funciona, para que los servicios arranquen
automáticamente en cada arranque del sistema (recuerda el paso 0:
`loginctl enable-linger`):

```bash
systemctl --user enable --now horarios
systemctl --user enable --now cloudflared
```

`enable --now` habilita el arranque automático y arranca el servicio en el
mismo paso; si ya estaban corridos con `start`, `--now` los reinicia con la
configuración ya "enabled".

Para desactivar el arranque automático sin detener el servicio en curso:

```bash
systemctl --user disable horarios cloudflared
```

---

## 8. Actualizar a una nueva versión

```bash
git pull
podman build -t localhost/horarios:latest -f Containerfile .
systemctl --user restart horarios
```

---

## 9. Copias de seguridad automáticas

Instala el script de backup y las unidades de systemd (ver
`deploy/backup.sh`, `deploy/backup.service`, `deploy/backup.timer`):

```bash
mkdir -p ~/.local/bin
cp deploy/backup.sh ~/.local/bin/horarios-backup.sh
chmod +x ~/.local/bin/horarios-backup.sh

cp deploy/backup.service deploy/backup.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now backup.timer
```

Comprueba la próxima ejecución programada:

```bash
systemctl --user list-timers backup.timer
```

Los backups se guardan comprimidos en `~/backups-horarios/`, conservando
los últimos 14. La línea de subida externa vía `rclone` está comentada en
`backup.sh`; descoméntala y configura un remoto (`rclone config`) si quieres
además una copia fuera del servidor.

---

## Resumen de rutas usadas

| Ruta | Contenido |
|---|---|
| `~/horarios-data` | Datos persistentes de la app (montados en `/app/data`) |
| `~/.config/horarios/env` | Secretos: `SECRET_KEY`, `TUNNEL_TOKEN` |
| `~/.config/containers/systemd/` | Quadlets (`.container`) |
| `~/.config/systemd/user/` | Unidades de backup (`.service`, `.timer`) |
| `~/backups-horarios/` | Backups comprimidos (`.tar.gz`), últimos 14 |
