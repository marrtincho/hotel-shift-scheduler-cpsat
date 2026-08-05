"""
cobertura_nocturna.py — Decide QUIÉN cubre cada noche.

Esta decisión es deliberadamente ajena al solver: motor_cpsat.py la toma como
dato de entrada y la fija en el modelo como restricción dura antes de resolver.
Así el orden de las noches es predecible y auditable para el equipo, en vez de
un subproducto de la optimización.

El mecanismo principal es un ORDEN FIJO y cíclico (empleado A, luego B, luego
C, vuelta a A) que el admin configura desde la interfaz. A quien le toque y no
esté disponible se le anota una deuda, y la recupera en cuanto vuelve. El
balance histórico solo se usa como respaldo cuando se desactiva el orden fijo
(`usar_orden_fijo: false`).

Este módulo solo decide el "quién". Qué días hay que cubrir y cómo se modela la
racha de noches es responsabilidad de quien lo invoque — hoy
motor_cpsat.generar_semana_cpsat_con_cobertura_nocturna. Ver REGLAS.md §1.9
para la regla de negocio completa y §1.10 para los límites de racha.

Importa DAYS de modelo_base y nada más: motor_cpsat.py importa de aquí, no al
revés, para evitar un ciclo de imports.

── Formato de estado_rotacion ────────────────────────────────────────────
{
    "orden": [lista de nombres, orden fijo vigente],
    "siguiente_turno": "nombre de quien le toca por defecto esta semana",
    "deudas": {"nombre": int},          # turnos saltados pendientes de recuperar
    "usar_orden_fijo": bool,            # si False, se ignora todo lo anterior
    "historico_balance": {"nombre": {"covers": int, "pending": int}},
    # mismo formato que _load_night_history en modelo_base.py — mecanismo
    # de respaldo (balance puro: menor covers-pending elige primero).

    # Campo adicional propio de este prototipo, no listado en el encargo
    # original pero necesario para la regla 4 (modo respaldo por ausencia
    # larga): cuántas semanas CONSECUTIVAS lleva night_fixed totalmente
    # ausente (forced_days_off cubriendo los 7 días). Lo mantiene
    # avanzar_semana_ausencia_night_fixed, NO seleccionar_candidato_cobertura_
    # nocturna — ver la nota de diseño en esa función sobre por qué está
    # separado.
    "ausencias_night_fixed_consecutivas": int,

    # Campo NUEVO (regla de rotación de cobertura nocturna en
    # bloques): quién está a mitad de un bloque de noches seguidas en modo
    # respaldo, y cuánto lleva cubierto. Un bloque de BLOQUE_RESPALDO_MIN(4)-
    # BLOQUE_RESPALDO_MAX(5) noches casi nunca cabe entero en una semana de
    # 7 días, así que cruza el límite de semana con frecuencia — este campo
    # es lo que permite retomarlo la semana siguiente exactamente donde se
    # quedó, en vez de volver a decidir "quién" cada lunes. Lo mantiene el
    # bucle de modo respaldo en motor_cpsat.generar_semana_cpsat_con_
    # cobertura_nocturna, no las funciones de este módulo directamente
    # (mismo patrón de separación que ausencias_night_fixed_consecutivas).
    # Vacío = {"nombre": None, "noches_totales": 0, "noches_cubiertas": 0}.
    "bloque_actual": {"nombre": str | None, "noches_totales": int, "noches_cubiertas": int},

    # Campo asociado a bloque_actual: días de descanso
    # obligatorio post-racha YA pendientes al arrancar la semana, según el
    # propio modelo CP-SAT (postweek1/postweek2 de motor_cpsat.
    # agregar_restriccion_racha_noches) — fuente autoritativa para
    # motor_cpsat._RachaTracker, que NO siempre puede reconstruir esto
    # fiablemente a partir del empalme de 5 días (noches_ultimos_5_dias) si
    # la racha de 5 terminó a mitad de dicho empalme. Lo mantiene
    # generar_semana_cpsat_con_cobertura_nocturna, no las funciones de este
    # módulo. {} o ausente = nadie con descanso pendiente.
    "descanso_pendiente_inicial": {"nombre": int},
}

── Contrato de uso (importante para la sesión que conecte esto al CP-SAT) ──
1. Una vez por semana, ANTES de cualquier llamada a
   seleccionar_candidato_cobertura_nocturna esa semana, llamar a
   avanzar_semana_ausencia_night_fixed(staff_list, estado_rotacion) y usar
   su resultado como estado_rotacion de partida. Esto actualiza el contador
   de semanas consecutivas de ausencia — se hace aparte, una vez, para que
   llamar a seleccionar_candidato_cobertura_nocturna varias veces la misma
   semana (modo respaldo, una noche a la vez) no infle el contador.
2. Modo normal (order fijo activo, night_fixed presente o ausente <2
   semanas): llamar a seleccionar_candidato_cobertura_nocturna UNA vez por
   semana — la misma persona cubre los dos días de cfg.night_fixed_days_off.
3. Modo respaldo (ausencia >=2 semanas consecutivas, o rol night_fixed
   inexistente en staff_list): llamar a seleccionar_candidato_cobertura_
   nocturna UNA vez POR CADA noche que haga falta cubrir esa semana,
   encadenando el estado_rotacion devuelto entre llamadas — así el balance
   histórico se actualiza entre noches y reparte el reparto en vez de
   asignar siempre a la misma persona. IMPORTANTE (parámetro añadido en
   Fase 5, corrección de INFEASIBLE espurio): cada una de estas llamadas
   DEBE pasar `dias_cubrir={d}` con el índice del día concreto (0=Lunes..
   6=Domingo) que se está decidiendo en esa llamada — de lo contrario la
   elegibilidad vuelve a comprobarse contra cfg.night_fixed_days_off (el
   bug original), no contra el día real, y puede devolver a alguien que
   ese día tiene una vacación/petición concedida.
"""

from modelo_base import DAYS

# ─── Bloques de cobertura en modo respaldo ──────────────────────────────
# Regla de negocio: en modo respaldo (sin rol night_fixed en la plantilla),
# cada persona de la rotación cubre un BLOQUE de noches seguidas, no una
# noche suelta a la vez (que es como funcionaba hasta ahora: una llamada a
# seleccionar_candidato_cobertura_nocturna por día, sin ninguna garantía de
# continuidad). BLOQUE_RESPALDO_MAX duplica a propósito el valor de
# motor_cpsat.MAX_NOCHES_VENTANA (este módulo no importa motor_cpsat.py,
# evitando un import circular — generar_semana_cpsat_con_cobertura_nocturna
# vive en motor_cpsat.py e importa de aquí, no al revés) — deben mantenerse
# sincronizadas si alguna cambia.
BLOQUE_RESPALDO_MIN = 4
BLOQUE_RESPALDO_MAX = 5


def _persona_ausente_toda_la_semana(persona: dict) -> bool:
    return set(persona.get("forced_days_off", [])) >= set(DAYS)


def _bloqueados_por_peticion_o_vacacion(staff_list: list, cfg, dias_cubrir: set = None) -> set:
    """Nombres bloqueados porque su forced_days_off cubre algún día de
    `dias_cubrir` (regla 1).

    dias_cubrir: índices de día (0=Lunes..6=Domingo) que se están decidiendo
    en ESTA llamada concreta. Si es None (por defecto), se asume el caso
    histórico de modo NORMAL: un único sustituto cubre AMBOS días de
    cfg.night_fixed_days_off, así que el bloqueo se comprueba contra esos
    dos días fijos — comportamiento sin cambios para quien no pase el
    parámetro nuevo.

    BUG corregido (Fase 5, verificación tras el arreglo del INFEASIBLE
    espurio): en modo respaldo, cada llamada decide UN día concreto (no
    necesariamente uno de cfg.night_fixed_days_off — puede no haber ni
    night_fixed en la plantilla). Antes de este parámetro, esta función
    SIEMPRE comprobaba el bloqueo contra cfg.night_fixed_days_off sin
    importar qué día se estuviera cubriendo de verdad, así que podía
    devolver como "elegible" a alguien con el día REAL que se cubre
    marcado como forced_days_off — motor_cpsat.py fijaba entonces Noche=1
    ese día sobre una persona que Fase 1 ya había forzado a Libre ese
    mismo día, produciendo un INFEASIBLE espurio (dos restricciones duras
    contradictorias), no una infactibilidad real."""
    dias_a_comprobar = set(dias_cubrir) if dias_cubrir is not None else set(cfg.night_fixed_days_off)
    dias_gap = {DAYS[i] for i in dias_a_comprobar}
    bloqueados = set()
    for p in staff_list:
        forced = set(p.get("forced_days_off", []))
        if forced & dias_gap:
            bloqueados.add(p["name"])
    return bloqueados


def persona_bloqueada_dia(staff_list: list, cfg, nombre: str, dia_idx: int) -> bool:
    """Wrapper delgado sobre _bloqueados_por_peticion_o_vacacion para UN
    nombre y UN día concretos (bloques de cobertura en
    modo respaldo): usado por el bucle de motor_cpsat.py para saber, día a
    día, si el titular del bloque de Noche EN CURSO sigue siendo elegible
    hoy (vacación/petición concedida a mitad de bloque) — si no lo es, el
    bloque se corta ahí (regla de negocio confirmada) y arranca uno nuevo
    con otra persona."""
    return nombre in _bloqueados_por_peticion_o_vacacion(staff_list, cfg, {dia_idx})


def _elegibles(staff_list: list, cfg, dias_cubrir: set = None,
               bloqueados_por_racha: set = None) -> list:
    """Regla 1: rol no en cfg.no_night_roles, no es night_fixed, no
    bloqueado por vacación/petición concedida en los días de `dias_cubrir`
    (ver _bloqueados_por_peticion_o_vacacion para el significado exacto de
    este parámetro y por qué None reproduce el comportamiento de modo
    normal sin cambios), y no bloqueado por descanso obligatorio post-racha
    de noches (bloqueados_por_racha — ver docstring de
    seleccionar_candidato_cobertura_nocturna, parámetro NUEVO Fase 5,
    verificación del mismo patrón de bug que dias_cubrir pero para la
    restricción de racha de 5 noches, no para vacaciones/peticiones)."""
    bloqueados = _bloqueados_por_peticion_o_vacacion(staff_list, cfg, dias_cubrir)
    bloqueados_racha = set(bloqueados_por_racha or ())
    return [
        p for p in staff_list
        if p.get("role") not in cfg.no_night_roles
        and p.get("role") != "night_fixed"
        and p["name"] not in bloqueados
        and p["name"] not in bloqueados_racha
    ]


def _en_modo_respaldo(staff_list: list, estado_rotacion: dict) -> bool:
    """Regla 4: sin night_fixed en plantilla, o >=2 semanas consecutivas de
    ausencia (contador mantenido por avanzar_semana_ausencia_night_fixed)."""
    nf = next((p for p in staff_list if p.get("role") == "night_fixed"), None)
    if nf is None:
        return True
    return estado_rotacion.get("ausencias_night_fixed_consecutivas", 0) >= 2


def modo_respaldo_activo(staff_list: list, estado_rotacion: dict) -> bool:
    """Wrapper público de _en_modo_respaldo — añadido para que quien orqueste
    la generación semanal (motor_cpsat.py) pueda decidir CUÁNTAS veces llamar
    a seleccionar_candidato_cobertura_nocturna (1 vez en modo normal, 1 vez
    por noche en modo respaldo — ver contrato de uso arriba) sin tener que
    importar un nombre "privado" (con guion bajo) de este módulo."""
    return _en_modo_respaldo(staff_list, estado_rotacion)


def avanzar_semana_ausencia_night_fixed(staff_list: list, estado_rotacion: dict) -> dict:
    """Actualiza el contador de semanas consecutivas de ausencia de
    night_fixed. Debe llamarse UNA vez por semana (ver contrato de uso en
    el docstring del módulo) — separado de seleccionar_candidato_cobertura_
    nocturna precisamente para que las múltiples llamadas por noche del
    modo respaldo no lo incrementen de más dentro de la misma semana.

    Si no hay ningún night_fixed en la plantilla, el contador no se toca:
    ese caso activa el modo respaldo por la condición "rol inexistente"
    (regla 4), no por rachas de ausencia — son dos disparadores distintos.
    """
    estado = dict(estado_rotacion)
    nf = next((p for p in staff_list if p.get("role") == "night_fixed"), None)
    if nf is None:
        return estado
    if _persona_ausente_toda_la_semana(nf):
        estado["ausencias_night_fixed_consecutivas"] = (
            estado.get("ausencias_night_fixed_consecutivas", 0) + 1
        )
    else:
        estado["ausencias_night_fixed_consecutivas"] = 0
    return estado


def _elegir_balance_puro(elegibles: list, estado: dict) -> tuple:
    """Regla 2 / mecanismo de respaldo: menor (covers - pending) en
    historico_balance elige primero. Desempate determinista por nombre
    (select_night_covers hoy desempata también por preferencia de
    compensación vía herramientas.cargar_compensaciones, dato externo que
    esta función no recibe — se documenta como simplificación deliberada,
    no como un olvido)."""
    balance = estado.get("historico_balance", {})

    def sort_key(p):
        info = balance.get(p["name"], {})
        return (info.get("covers", 0) - info.get("pending", 0), p["name"])

    elegido = sorted(elegibles, key=sort_key)[0]["name"]

    nuevo_balance = {n: dict(v) for n, v in balance.items()}
    info = dict(nuevo_balance.get(elegido, {"covers": 0, "pending": 0}))
    info["covers"] = info.get("covers", 0) + 1
    nuevo_balance[elegido] = info

    nuevo_estado = dict(estado)
    nuevo_estado["historico_balance"] = nuevo_balance
    return elegido, nuevo_estado


def registrar_noche_bloque(estado_rotacion: dict, nombre: str) -> dict:
    """Incrementa historico_balance[nombre]["covers"] SIN volver a elegir
    (bloques de cobertura en modo respaldo): usado por
    el bucle de motor_cpsat.py cuando el día de hoy es CONTINUACIÓN de un
    bloque ya en curso (el "quién" ya se decidió al arrancar el bloque, ver
    longitud_bloque_respaldo) — el balance histórico debe seguir reflejando
    la carga real noche a noche, igual que si _elegir_balance_puro hubiera
    vuelto a elegir a la misma persona, pero sin ejecutar de nuevo la
    selección (ya no hay nada que decidir ese día)."""
    balance = estado_rotacion.get("historico_balance", {})
    nuevo_balance = {n: dict(v) for n, v in balance.items()}
    info = dict(nuevo_balance.get(nombre, {"covers": 0, "pending": 0}))
    info["covers"] = info.get("covers", 0) + 1
    nuevo_balance[nombre] = info

    nuevo_estado = dict(estado_rotacion)
    nuevo_estado["historico_balance"] = nuevo_balance
    return nuevo_estado


def longitud_bloque_respaldo(staff_list: list, nombre: str, dia_inicio: int) -> int:
    """Decide cuántas noches seguidas (BLOQUE_RESPALDO_MIN(4)-BLOQUE_
    RESPALDO_MAX(5)) cubre `nombre` a partir de `dia_inicio` (NUEVO, esta
    sesión — bloques de cobertura en modo respaldo).

    Por defecto BLOQUE_RESPALDO_MAX(5) (confirmado como regla de negocio: "siempre
    5 salvo que la rotación exija menos"). Se acorta a un valor menor SOLO
    si `nombre` ya tiene una vacación/petición concedida (forced_days_off)
    dentro de la semana visible que truncaría el bloque de 5 antes de
    completarlo — en ese caso, se elige la longitud más larga que quepa
    ANTES de ese día bloqueado, sin bajar de BLOQUE_RESPALDO_MIN(4). Si ni
    siquiera 4 caben antes del bloqueo, se deja en BLOQUE_RESPALDO_MAX(5) de
    todas formas: el bloque se interrumpirá (mecanismo ya previsto en el
    bucle de motor_cpsat.py, ver persona_bloqueada_dia) en vez de intentar
    evitarlo a toda costa — esta función solo evita la interrupción cuando
    es sencillo hacerlo (acortando dentro del rango 4-5), no en todos los
    casos posibles.

    Solo mira DENTRO de la semana visible (los 7 días de `staff_list` tal
    como llega a esta llamada) — no hay forma de ver vacaciones de semanas
    futuras desde aquí, límite ya existente en el resto de la arquitectura
    (cada semana se genera con su propio staff_list)."""
    persona = next((p for p in staff_list if p["name"] == nombre), None)
    forced = set(persona.get("forced_days_off", [])) if persona else set()
    dias_bloqueados = sorted(
        DAYS.index(d) for d in forced if d in DAYS and DAYS.index(d) > dia_inicio
    )
    if not dias_bloqueados:
        return BLOQUE_RESPALDO_MAX
    primer_bloqueo = dias_bloqueados[0]
    cabe = primer_bloqueo - dia_inicio
    if BLOQUE_RESPALDO_MIN <= cabe < BLOQUE_RESPALDO_MAX:
        return cabe
    return BLOQUE_RESPALDO_MAX


def _elegir_orden_fijo(elegibles: list, estado: dict) -> tuple:
    """Reglas 3.a-3.d."""
    orden = list(estado.get("orden", []))
    if not orden:
        raise RuntimeError("estado_rotacion['orden'] está vacío: no hay a quién asignar.")
    deudas = dict(estado.get("deudas", {}))
    elegibles_nombres = {p["name"] for p in elegibles}

    # 3.a: alguien con deuda pendiente. Empate = primero en "orden".
    con_deuda = [n for n in orden if deudas.get(n, 0) > 0 and n in elegibles_nombres]
    if con_deuda:
        max_deuda = max(deudas[n] for n in con_deuda)
        elegido = next(n for n in con_deuda if deudas[n] == max_deuda)
        deudas[elegido] = deudas[elegido] - 1
    else:
        # 3.b: recorrido cíclico desde "siguiente_turno".
        siguiente = estado.get("siguiente_turno")
        idx_inicio = orden.index(siguiente) if siguiente in orden else 0
        n = len(orden)
        elegido = None
        for offset in range(n):
            candidato = orden[(idx_inicio + offset) % n]
            if candidato in elegibles_nombres:
                elegido = candidato
                break
            deudas[candidato] = deudas.get(candidato, 0) + 1
        if elegido is None:
            raise RuntimeError(
                "Nadie en 'orden' es elegible esta semana (todos bloqueados o "
                "fuera de cfg.no_night_roles)."
            )

    # 3.c: siguiente_turno pasa a quien sigue al elegido en "orden",
    # independientemente de si ganó por deuda (3.a) o por turno (3.b).
    idx_elegido = orden.index(elegido)
    nuevo_siguiente = orden[(idx_elegido + 1) % len(orden)]

    # 3.d: actualizar historico_balance aunque no se use para decidir.
    balance = estado.get("historico_balance", {})
    nuevo_balance = {n: dict(v) for n, v in balance.items()}
    info = dict(nuevo_balance.get(elegido, {"covers": 0, "pending": 0}))
    info["covers"] = info.get("covers", 0) + 1
    nuevo_balance[elegido] = info

    nuevo_estado = dict(estado)
    nuevo_estado["deudas"] = deudas
    nuevo_estado["siguiente_turno"] = nuevo_siguiente
    nuevo_estado["historico_balance"] = nuevo_balance
    return elegido, nuevo_estado


def seleccionar_candidato_cobertura_nocturna(staff_list: list, cfg, estado_rotacion: dict,
                                              dias_cubrir: set = None,
                                              bloqueados_por_racha: set = None) -> tuple:
    """Elige quién cubre la próxima noche/gap de night_fixed. Ver el
    contrato de uso en el docstring del módulo para cuántas veces llamarla
    por semana según el modo activo.

    dias_cubrir: índices de día (0=Lunes..6=Domingo) que esta llamada
    concreta está decidiendo — se reenvía tal cual a _elegibles (ver su
    docstring). Parámetro NUEVO (Fase 5, corrección del INFEASIBLE
    espurio de modo respaldo):
      - Modo NORMAL (una llamada/semana, el mismo elegido cubre AMBOS días
        de cfg.night_fixed_days_off): se puede omitir (None) — el
        comportamiento por defecto sigue comprobando el bloqueo contra
        cfg.night_fixed_days_off, que es exactamente lo correcto aquí.
      - Modo RESPALDO (una llamada POR CADA día de la semana, ver
        generar_semana_cpsat_con_cobertura_nocturna en motor_cpsat.py):
        el llamador DEBE pasar `dias_cubrir={d}` con el día concreto que
        esa llamada está decidiendo — omitirlo reintroduce el bug
        original (bloqueo comprobado contra el día equivocado).

    bloqueados_por_racha: nombres que NO deben proponerse para el día que
    decide esta llamada porque están en su descanso obligatorio de 2 días
    tras agotar una racha de 5 noches (restricción de Fase 3,
    agregar_restriccion_racha_noches en motor_cpsat.py). Parámetro NUEVO
    (Fase 5, verificación del mismo tipo de bug que dias_cubrir, esta vez
    para la racha en vez de vacaciones/peticiones — ver docstring de
    motor_cpsat.generar_semana_cpsat_con_cobertura_nocturna, sección
    "seguimiento de racha en modo respaldo", para cómo se calcula este
    conjunto día a día). Solo tiene sentido en modo RESPALDO: es donde
    esta función se llama varias veces dentro de la MISMA semana y puede
    volver a proponer, sin saberlo, a alguien que el propio bucle del
    llamador ya hizo encadenar 5 noches seguidas. En modo normal (una sola
    llamada/semana, el sustituto solo cubre 2 días fijos no consecutivos
    de por sí con el resto de la semana) no hace falta: se puede omitir
    (None) sin cambiar el comportamiento.

    Devuelve (nombre_elegido, estado_rotacion_actualizado).
    """
    elegibles = _elegibles(staff_list, cfg, dias_cubrir, bloqueados_por_racha)
    if not elegibles:
        raise RuntimeError("Nadie en staff_list es elegible para cubrir la noche esta semana.")

    if not estado_rotacion.get("usar_orden_fijo", True):
        return _elegir_balance_puro(elegibles, estado_rotacion)

    # El ORDEN FIJO se aplica también en modo respaldo (CAMBIO de,
    # requisito de negocio: "debería ser fijo, más allá de si existe el
    # rol de noche fijo o no"). Antes, el modo respaldo caía en
    # _elegir_balance_puro; como la plantilla real de Recepción no tiene
    # night_fixed, el modo respaldo está SIEMPRE activo, así que el orden fijo
    # era código muerto en producción.
    #
    # El ciclo que producía _elegir_balance_puro era correcto por accidente:
    # tras un bloque de 5 noches el contador `covers` sube 5 y esa persona se va
    # al final de la cola, lo que con 3 personas equivalentes da un ciclo
    # perfecto (medido: Carlos→Martin→Seba, huecos de 10 días). Pero se deshacía
    # en cuanto algo rompía la simetría — comprobado: un empleado nuevo
    # (covers=0) frente a veteranos (covers=30) encadenaba 12 bloques seguidos,
    # y a quien se saltaba por vacaciones no se le anotaba deuda, así que perdía
    # el turno sin compensación. _elegir_orden_fijo sí recorre el ciclo desde
    # siguiente_turno y anota deudas[c] += 1 por cada persona saltada, que la
    # recupera en cuanto vuelve a estar disponible.
    return _elegir_orden_fijo(elegibles, estado_rotacion)


def reconciliar_orden(estado_rotacion: dict, staff_list: list, cfg) -> dict:
    """Sincroniza 'orden' con quién puede cubrir noches HOY (NUEVO, esta
    sesión). Añade al FINAL a quien sea elegible y no esté todavía en la
    lista, y da de baja a quien ya no está en la plantilla — reutilizando
    incorporar_empleado_orden / dar_de_baja_empleado_orden (reglas 5 y 6).
    Respeta el orden relativo de quienes ya estaban: reordenar la lista a
    mano en cpsat_estado_{dept}.json sigue siendo la forma de configurarla.

    Existe porque el orden fijo pasó a usarse también en modo respaldo y
    _elegir_orden_fijo solo mira dentro de 'orden': alguien elegible pero
    ausente de la lista NUNCA haría noches, y si nadie de 'orden' fuera
    elegible lanzaría RuntimeError aunque hubiera gente disponible. Las dos
    funciones de alta/baja existían desde el principio pero NADIE las
    llamaba fuera de sus tests, así que la lista se quedaba obsoleta en
    cuanto cambiaba la plantilla.

    "Elegible" = mismo criterio que _elegibles y que
    app._estado_rotacion_inicial_cpsat: ni night_fixed (cubre sus propias
    noches) ni ningún rol de cfg.no_night_roles. Deliberadamente NO se filtra
    por vacaciones/peticiones ni por descanso de racha: esos son bloqueos de
    UNA semana concreta, y quien esté de vacaciones hoy debe conservar su
    sitio en el ciclo para cuando vuelva.

    Alta = al FINAL del ciclo, sin recuperar atrasos (confirmado con el
    usuario): nadie encadena decenas de noches seguidas para "ponerse al
    día" con el histórico de los demás.
    """
    estado = dict(estado_rotacion)
    elegibles_hoy = [
        p["name"] for p in staff_list
        if p.get("role") != "night_fixed" and p.get("role") not in cfg.no_night_roles
    ]
    for nombre in elegibles_hoy:
        if nombre not in estado.get("orden", []):
            estado = incorporar_empleado_orden(estado, nombre)

    en_plantilla = {p["name"] for p in staff_list}
    for nombre in list(estado.get("orden", [])):
        if nombre not in en_plantilla:
            estado = dar_de_baja_empleado_orden(estado, nombre)

    # siguiente_turno puede haber quedado a None (baja del último) o apuntar a
    # alguien que ya no está: se recoloca al principio del ciclo.
    orden = estado.get("orden", [])
    if orden and estado.get("siguiente_turno") not in orden:
        estado["siguiente_turno"] = orden[0]

    return estado


def incorporar_empleado_orden(estado_rotacion: dict, nombre: str) -> dict:
    """Regla 5: se añade al final de 'orden'. No toca 'siguiente_turno'."""
    estado = dict(estado_rotacion)
    orden = list(estado.get("orden", []))
    if nombre not in orden:
        orden.append(nombre)
    estado["orden"] = orden
    return estado


def dar_de_baja_empleado_orden(estado_rotacion: dict, nombre: str) -> dict:
    """Regla 6: se elimina de 'orden'. Si era 'siguiente_turno', pasa a
    quien estaba inmediatamente después (o al primero si era el último)."""
    estado = dict(estado_rotacion)
    orden = list(estado.get("orden", []))
    if nombre not in orden:
        return estado

    idx = orden.index(nombre)
    n = len(orden)
    siguiente_tras_baja = orden[(idx + 1) % n] if n > 1 else None

    orden.remove(nombre)
    estado["orden"] = orden

    if estado.get("siguiente_turno") == nombre:
        estado["siguiente_turno"] = siguiente_tras_baja

    deudas = dict(estado.get("deudas", {}))
    deudas.pop(nombre, None)
    estado["deudas"] = deudas

    return estado
