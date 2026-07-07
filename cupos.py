"""
cupos.py — Sistema de cupos mensuales para peticiones de días libres de empleados.

Reglas:
  - Cada empleado dispone de CUPO_MENSUAL_DEFAULT peticiones "propias" (origen
    empleado) por mes natural, ampliable con cupos_extra.json.
  - Las peticiones creadas por jefe/admin (origen="jefe") nunca computan en el
    cupo: son gestión manual, no autoservicio.
  - El mes que consume una petición es el mes del DÍA PEDIDO (fecha real),
    no el de la semana en la que se registra ni el de creación.
  - Solo se pueden crear o cancelar peticiones sobre semanas que aún no
    tengan horario generado (historial.json).
"""

import os
from datetime import date, timedelta

import herramientas
from herramientas import (
    cargar_peticiones, cargar_historial, _cargar_json, guardar_json_atomico,
    DIAS_SEMANA, _semana_str,
)

CUPO_MENSUAL_DEFAULT = 2

CUPOS_EXTRA_FILE = os.path.join(herramientas.DATA_DIR, "cupos_extra.json")


# ─── CUPO EXTRA ───────────────────────────────────────────────────────────────

def cargar_cupos_extra() -> dict:
    return _cargar_json(CUPOS_EXTRA_FILE, {})


def guardar_cupos_extra(cupos: dict):
    guardar_json_atomico(CUPOS_EXTRA_FILE, cupos)


def cupo_extra(nombre: str, yyyy_mm: str) -> int:
    return cargar_cupos_extra().get(nombre, {}).get(yyyy_mm, 0)


def cupo_total(nombre: str, yyyy_mm: str) -> int:
    return CUPO_MENSUAL_DEFAULT + cupo_extra(nombre, yyyy_mm)


def validar_valor_cupo_extra(valor) -> tuple:
    """
    Valida el valor recibido para el cupo extra de un empleado/mes: debe ser
    un entero mayor o igual a 0. Devuelve (ok, valor_normalizado, mensaje_error).
    Si ok es False, valor_normalizado es None.
    """
    if isinstance(valor, bool):
        return False, None, "El cupo extra debe ser un número entero"
    try:
        entero = int(valor)
        if isinstance(valor, float) and valor != entero:
            raise ValueError
        if isinstance(valor, str) and not valor.strip().lstrip("-").isdigit():
            raise ValueError
    except (TypeError, ValueError):
        return False, None, "El cupo extra debe ser un número entero"
    if entero < 0:
        return False, None, "El cupo extra debe ser un entero mayor o igual a 0"
    return True, entero, None


# ─── SEMANA GENERADA ──────────────────────────────────────────────────────────

def semana_generada(semana: date, dept_id: str = "recepcion") -> bool:
    """True si esa semana (lunes) ya tiene horario publicado en el historial."""
    return _semana_str(semana) in cargar_historial(dept_id)


# ─── CUPO USADO ───────────────────────────────────────────────────────────────

def cupo_usado(nombre: str, yyyy_mm: str, dept_id: str = "recepcion",
               excluir_semana: str | None = None) -> int:
    """
    Cuenta los días de peticiones con origen='empleado' y estado='activa'
    cuyo día pedido (fecha real, no la semana de registro) cae en yyyy_mm.
    'excluir_semana' permite ignorar la entrada de una semana concreta (se usa
    al editar/sobrescribir la propia petición de esa semana).
    """
    total = 0
    peticiones = cargar_peticiones(dept_id)
    for semana_str, entradas in peticiones.items():
        if semana_str == "_prioridad" or semana_str == excluir_semana:
            continue
        if not isinstance(entradas, dict):
            continue
        entrada = entradas.get(nombre)
        if not isinstance(entrada, dict):
            continue
        if entrada.get("origen") != "empleado" or entrada.get("estado", "activa") != "activa":
            continue
        try:
            semana = date.fromisoformat(semana_str)
        except ValueError:
            continue
        for dia in entrada.get("dias", []):
            if dia not in DIAS_SEMANA:
                continue
            fecha = semana + timedelta(days=DIAS_SEMANA.index(dia))
            if fecha.strftime("%Y-%m") == yyyy_mm:
                total += 1
    return total


def peticiones_activas_de(nombre: str, dept_id: str = "recepcion") -> list[dict]:
    """Lista de peticiones activas (cualquier origen) de un empleado, para
    mostrar en la tabla de transparencia."""
    activas = []
    peticiones = cargar_peticiones(dept_id)
    for semana_str, entradas in sorted(peticiones.items()):
        if semana_str == "_prioridad" or not isinstance(entradas, dict):
            continue
        entrada = entradas.get(nombre)
        if not isinstance(entrada, dict):
            continue
        if entrada.get("estado", "activa") != "activa":
            continue
        activas.append({
            "semana":    semana_str,
            "dias":      entrada.get("dias", []),
            "origen":    entrada.get("origen", "jefe"),
            "creada_en": entrada.get("creada_en"),
            "motivo":    entrada.get("motivo"),
        })
    return activas


# ─── VALIDACIÓN DE NUEVAS PETICIONES ──────────────────────────────────────────

def validar_nueva_peticion(nombre: str, semana: date, dias_pedidos: list,
                           rol_usuario: str, dept_id: str = "recepcion") -> tuple:
    """
    Valida si se puede crear/actualizar una petición de 'nombre' para los días
    'dias_pedidos' (nombres de día Lunes..Domingo) de la semana dada.

    - jefe/admin (rol_usuario != 'empleado'): siempre ok, salvo semana ya
      publicada. El jefe queda excluido del cupo: sus propios días se
      gestionan con modificación manual, no con peticiones.
    - empleado: se agrupan los días por mes natural y se comprueba, para cada
      mes, que el cupo usado + los nuevos no supere el cupo total. Si algún
      mes no cabe, se rechaza la petición COMPLETA (no parcialmente).
    """
    if not dias_pedidos:
        return False, "Debes seleccionar al menos un día"
    for dia in dias_pedidos:
        if dia not in DIAS_SEMANA:
            return False, f"Día inválido: {dia}"

    if semana_generada(semana, dept_id):
        return False, "Esa semana ya está publicada; habla con el encargado para un cambio manual."

    if rol_usuario != "empleado":
        return True, ""

    nuevos_por_mes: dict = {}
    for dia in dias_pedidos:
        fecha = semana + timedelta(days=DIAS_SEMANA.index(dia))
        mes = fecha.strftime("%Y-%m")
        nuevos_por_mes[mes] = nuevos_por_mes.get(mes, 0) + 1

    excluir = _semana_str(semana)
    for mes, nuevos in nuevos_por_mes.items():
        usadas = cupo_usado(nombre, mes, dept_id, excluir_semana=excluir)
        total = cupo_total(nombre, mes)
        if usadas + nuevos > total:
            faltan = usadas + nuevos - total
            return False, (
                f"No puedes solicitar esos días: en {mes} ya tienes {usadas} petición(es) "
                f"pedida(s) de un cupo total de {total}. "
                f"Necesitarías {faltan} petición(es) extra ese mes."
            )
    return True, ""
