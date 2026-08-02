#!/usr/bin/env python3
"""
migrar_night_rotation.py — Convierte los ficheros de estado antiguos
night_rotation_{key}.json / night_rotation_{key}_bloques.json (formato de
producción, modelo_base.py) al nuevo formato de empalme que usa la
restricción de racha de motor_cpsat.py (agregar_restriccion_racha_noches):

    {"nombre": {"noches_ultimos_5_dias": [bool, bool, bool, bool, bool]}}
    # índice 0 = 5 días antes del lunes de la próxima semana a generar
    # índice 4 = domingo inmediatamente anterior a ese lunes

No toca app.py ni modelo_base.py — es un script standalone del prototipo
aislado. No escribe sobre los ficheros antiguos ni sobre night_rotation.json
de producción: genera un fichero NUEVO (night_racha_{key}.json) para no
mezclar formatos ni arriesgar los datos reales.

── De dónde sale cada dato ───────────────────────────────────────────────

Hay DOS formatos antiguos distintos, y solo uno de ellos contiene
información de racha reciente:

1. night_rotation_{key}.json ("plain", usado por select_night_covers para
   el sustituto de gap corto — 2 noches/semana en recepción):
     {"nombre": {"covers": int, "pending": int}}   (o int suelto, formato legacy)
   Esto es un CONTADOR ACUMULADO histórico de cuántas veces cubrió, sin
   ninguna noción de qué días concretos ni de racha reciente. El propio
   mecanismo de gap corto nunca deja que el sustituto se acerque a una
   racha real (cubre como mucho 2 noches seguidas), así que no hay racha
   que migrar desde aquí — se traduce a [False]*5 para todas las personas
   que solo aparecen en este fichero.

2. night_rotation_{key}_bloques.json (usado por _plan_noches_bloques
   cuando night_fixed está ausente — bloques rotatorios de hasta
   MAX_NOCHES_BLOQUE=5 noches consecutivas):
     {"_actual": {"nombre": str|None, "hechas": int}, "<persona>": {"covers": int}}
   AQUÍ SÍ hay información de racha: "_actual.hechas" es cuántas noches
   CONSECUTIVAS lleva la persona "_actual.nombre" en su bloque actual, en
   el momento en que se guardó el estado (el domingo de la última semana
   generada con el motor antiguo). Se traduce así:
     noches_ultimos_5_dias["_actual.nombre"] = [False]*(5-h) + [True]*h
     (h = min(hechas, 5); las noches más RECIENTES ocupan las últimas
     posiciones del array, que es exactamente lo que representa el índice
     4 = "ayer" / domingo anterior).
   Cualquier OTRA persona nombrada en este fichero (con solo "covers"
   acumulado, sin ser "_actual") tiene noches_ultimos_5_dias = [False]*5:
   el modelo de bloques garantiza que solo una persona está en racha
   activa a la vez — cualquier otra ya cerró su bloque y descansó.

Si una persona aparece en AMBOS ficheros, gana el de "_bloques" (tiene
información de racha real; el "plain" nunca la tiene).

── Campos del formato antiguo SIN equivalente en el nuevo (documentados,
   no descartados en silencio) ──────────────────────────────────────────

- "covers" / "pending" (ambos ficheros): son el HISTÓRICO ACUMULADO de
  cuántas noches cubrió cada persona — siguen siendo relevantes, pero para
  el mecanismo de RESPALDO de cobertura_nocturna.py (historico_balance),
  no para la racha. Este script NO los migra (no es su alcance: el pedido
  es específicamente noches_ultimos_5_dias) — si se quiere poblar
  historico_balance con este mismo dato de entrada, es una migración
  APARTE y trivial (el formato ya coincide: {"nombre": {"covers":...,
  "pending":...}} es literalmente el formato de historico_balance).

- night_fixed: NO aparece en NINGUNO de los dos ficheros antiguos, en
  ningún departamento. Motivo: en el sistema de producción, night_fixed
  no tiene límite de racha — trabaja Noche indefinidamente en su patrón
  fijo semanal (todos los días salvo cfg.night_fixed_days_off), sin
  descanso obligatorio tras N noches. El concepto de "racha de 5 con
  descanso obligatorio" es NUEVO en este prototipo CP-SAT y no existía
  para night_fixed en producción. Este script NO PUEDE reconstruir su
  noches_ultimos_5_dias desde datos que nunca se registraron — lo deja en
  [False]*5 (asume arranque limpio) y EMITE UN AVISO explícito, porque es
  una suposición, no un dato migrado. Si night_fixed llevaba de verdad
  varios días seguidos de Noche en el momento de migrar, un operador debe
  revisarlo y corregirlo a mano.
"""

import argparse
import json
import os
import sys

MAX_NOCHES_BLOQUE = 5  # mismo valor que modelo_base.MAX_NOCHES_BLOQUE


def _leer_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        contenido = f.read().strip()
    return json.loads(contenido) if contenido else {}


def _covers_de(valor) -> int:
    """Soporta el formato legacy (int suelto) además de {"covers":...}."""
    if isinstance(valor, int):
        return valor
    return valor.get("covers", 0) if isinstance(valor, dict) else 0


def migrar_dept(dir_origen: str, rotation_key: str) -> tuple[dict, list[str]]:
    """Migra los ficheros de una rotation_key concreta. Devuelve
    (nuevo_estado, avisos) — avisos es una lista de strings a mostrar al
    operador (campos sin equivalente, night_fixed sin datos, etc.), no
    silenciados dentro del propio JSON de salida."""
    path_plain = os.path.join(dir_origen, f"night_rotation_{rotation_key}.json")
    path_bloques = os.path.join(dir_origen, f"night_rotation_{rotation_key}_bloques.json")

    datos_plain = _leer_json(path_plain)
    datos_bloques = _leer_json(path_bloques)

    nuevo_estado: dict = {}
    avisos: list[str] = []

    # 1. Formato "plain": todas las personas nombradas -> [False]*5 (no hay
    # racha migrable, ver docstring del módulo).
    for nombre in datos_plain:
        nuevo_estado[nombre] = {"noches_ultimos_5_dias": [False] * 5}

    if datos_plain:
        avisos.append(
            f"[{rotation_key}] {path_plain}: {len(datos_plain)} persona(s) con "
            f"histórico 'covers/pending' pero SIN racha migrable (ese fichero "
            f"nunca trackeó días concretos, solo un contador acumulado). Se "
            f"inicializan en [False]*5. Ese 'covers/pending' sigue siendo dato "
            f"válido para historico_balance de cobertura_nocturna.py — no se "
            f"migra aquí (fuera del alcance de este script)."
        )

    # 2. Formato "_bloques": prioridad sobre "plain" si hay solape (SÍ tiene
    # racha real).
    actual = datos_bloques.get("_actual", {})
    nombre_actual = actual.get("nombre")
    hechas = actual.get("hechas", 0)

    for nombre, valor in datos_bloques.items():
        if nombre == "_actual":
            continue
        # Cualquier persona nombrada en _bloques que NO sea la actual: sin
        # racha activa (el modelo de bloques solo permite una a la vez).
        nuevo_estado[nombre] = {"noches_ultimos_5_dias": [False] * 5}

    if nombre_actual:
        h = max(0, min(int(hechas), MAX_NOCHES_BLOQUE))
        secuencia = [False] * (5 - h) + [True] * h
        nuevo_estado[nombre_actual] = {"noches_ultimos_5_dias": secuencia}
        avisos.append(
            f"[{rotation_key}] {path_bloques}: '{nombre_actual}' llevaba "
            f"{hechas} noche(s) consecutivas en su bloque actual -> migrado a "
            f"noches_ultimos_5_dias={secuencia}."
        )

    if datos_bloques and not nombre_actual:
        avisos.append(
            f"[{rotation_key}] {path_bloques}: sin bloque activo ('_actual.nombre' "
            f"vacío) — nadie con racha reciente que migrar desde este fichero."
        )

    if not datos_plain and not datos_bloques:
        avisos.append(
            f"[{rotation_key}] no se encontró {path_plain} ni {path_bloques} "
            f"(o ambos vacíos) — nada que migrar, se genera un estado vacío."
        )

    avisos.append(
        f"[{rotation_key}] night_fixed: SIN datos migrables en ningún fichero "
        f"antiguo (nunca tuvo límite de racha en producción — concepto nuevo "
        f"de este prototipo). Si al migrar night_fixed llevaba días recientes "
        f"trabajando Noche, un operador debe añadir su entrada manualmente "
        f"antes de generar la primera semana con el nuevo motor; de lo "
        f"contrario se asume arranque limpio ([False]*5, sin entrada en el "
        f"JSON de salida)."
    )

    return nuevo_estado, avisos


def migrar(dir_origen: str, dir_destino: str, rotation_keys: list) -> None:
    os.makedirs(dir_destino, exist_ok=True)
    for key in rotation_keys:
        nuevo_estado, avisos = migrar_dept(dir_origen, key)
        path_salida = os.path.join(dir_destino, f"night_racha_{key}.json")
        with open(path_salida, "w", encoding="utf-8") as f:
            json.dump(nuevo_estado, f, ensure_ascii=False, indent=2, sort_keys=True)

        print(f"\n=== {key} -> {path_salida} ===")
        print(json.dumps(nuevo_estado, ensure_ascii=False, indent=2, sort_keys=True))
        for aviso in avisos:
            print(f"  AVISO: {aviso}")


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Migra night_rotation_*.json (formato antiguo) al nuevo "
                     "formato de empalme de racha (noches_ultimos_5_dias)."
    )
    parser.add_argument(
        "--origen", default="data-ejemplo",
        help="Directorio con los night_rotation_*.json antiguos (default: data-ejemplo/)."
    )
    parser.add_argument(
        "--destino", default="data-ejemplo",
        help="Directorio donde escribir night_racha_*.json (default: data-ejemplo/)."
    )
    parser.add_argument(
        "--keys", nargs="+", default=["recepcion", "mozos", "recepcion_semanal"],
        help="rotation_keys a migrar (default: recepcion mozos recepcion_semanal)."
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args()
    migrar(args.origen, args.destino, args.keys)
