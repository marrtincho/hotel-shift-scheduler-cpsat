"""
motor_cpsat.py — Motor de generación de horarios sobre CP-SAT (Google OR-Tools).

Es el motor ACTIVO en producción cuando `herramientas.motor_cpsat_activo()`
devuelve True (data/config_motor.json). Sustituye al motor heurístico por fases
de modelo_base.py, del que solo reutiliza los tipos y las funciones de lectura
de histórico: DeptConfig, DAYS/SHIFTS, _get_weekend_ratios, _get_shift_counts.

El catálogo completo de reglas de negocio —cuál es dura, cuál blanda, con qué
peso y por qué— vive en REGLAS.md. Este docstring describe la ARQUITECTURA;
para "¿cuál es la regla?", la fuente única es REGLAS.md.

── Modelo ───────────────────────────────────────────────────────────────────
Variable de decisión: x[persona, día, turno], booleana, con día 0..6 (Lunes a
Domingo) y turno en SHIFTS (Mañana/Tarde/Noche/Partido/Libre). Una sola llamada
a Solve() resuelve la semana entera.

Restricciones DURAS (_construir_modelo): un turno por persona y día; cobertura
mínima por turno y día; cobertura máxima (dura en Noche y Partido, blanda en
Mañana/Tarde — ver término K); descanso mínimo entre turnos de días
consecutivos; roles sin noches; días libres forzados por vacaciones y
peticiones concedidas; roles con turno fijo o con días libres fijos; y el
patrón de días libres semanales.

Términos BLANDOS del objetivo, identificados por letra tanto aquí como en
REGLAS.md: A equidad de findes libres, B continuidad de turno, C reparto
histórico, D uso de Partido, E rol excluido de equidad, F domingo del
sustituto nocturno, G cobertura de la ausencia de otro rol, H turno
complementario jefe/subjefe, I sobre-cobertura diurna, J libres previos a un
bloque de vacaciones, K exceso de cobertura, L orden de ese exceso, M días
trabajados seguidos cruzando semanas. El razonamiento de cada magnitud está
junto a su constante.

── Orquestación semanal ─────────────────────────────────────────────────────
generar_semana_cpsat_con_cobertura_nocturna() es el punto de entrada de
producción. Combina tres piezas que acaban resolviéndose juntas:

  1. QUIÉN cubre cada noche lo decide cobertura_nocturna.py ANTES de construir
     el modelo (orden fijo, configurable por el admin) y se fija como
     restricción dura. El solver no elige la cobertura nocturna.
  2. El modelo de restricciones y objetivo (_construir_modelo).
  3. Las restricciones de racha de noches (agregar_restriccion_racha_noches),
     añadidas sobre ese mismo modelo antes de resolver.

Devuelve también lo que necesita la semana siguiente: rotación nocturna
actualizada, empalme de noches y días libres arrastrados por descansos que no
caben dentro de los 7 días. Qué se persiste y qué se deriva del historial real
está en REGLAS.md §5.

── Determinismo ─────────────────────────────────────────────────────────────
El mismo input debe producir el mismo horario: num_search_workers=1 y sorted()
sobre cualquier conjunto que determine el ORDEN en que se añaden restricciones
—iterar un set() depende del hash-seed del proceso, y ante un empate de valor
óptimo eso bastaba para devolver horarios distintos con la misma entrada—. Ver
MAX_TIME_SOLVER y GRACIA_SIN_MEJORA para los límites de tiempo y la parada por
meseta.

── Diagnóstico de infactibilidad ────────────────────────────────────────────
Cuando no hay solución, _diagnosticar_infactibilidad() reconstruye un modelo
reducido con las restricciones "cedibles" (vacaciones y peticiones, cobertura
mínima puntual, patrón de libres) gateadas por asunciones, y devuelve el núcleo
irreducible del conflicto para que el encargado sepa qué ceder en lugar de
recibir un INFEASIBLE sin explicación. Solo diagnostica: nunca relaja nada por
su cuenta ni altera el resultado.
"""

import threading
import time
from datetime import date

from ortools.sat.python import cp_model

from modelo_base import (
    DAYS, SHIFTS, FUERA, NO_MORNING_AFTER, _get_weekend_ratios,
    _get_shift_counts, get_consecutive_day_pairs,
)

# Turnos de "día" — los únicos relevantes para continuidad (B) y desequilibrio
# histórico (C). Noche y Libre quedan fuera de ambos términos a propósito.
DAY_SHIFTS = ["Mañana", "Tarde", "Partido"]

# Subconjunto de DAY_SHIFTS para el término I (sobre-cobertura diurna, Fase 5
# — rediseño de prioridades): Mañana/Tarde, SIN Partido. Se deriva de
# DAY_SHIFTS (no una lista nueva desconectada) porque son el mismo concepto
# base de "turno de día" que ya usa el término B — pero Partido es aquí la
# VÁLVULA DE ESCAPE que I incentiva usar en vez de penalizar, no un turno
# más a penalizar por sobre-cobertura (ya tiene su propio coste vía D).
TURNOS_DIURNOS_SIN_PARTIDO = [s for s in DAY_SHIFTS if s != "Partido"]

# Turnos cuyo cfg.max_coverage es una preferencia BLANDA (término K) en vez
# de una restricción dura. Exactamente Mañana/Tarde — es el mismo conjunto
# que TURNOS_DIURNOS_SIN_PARTIDO, y se deriva de él a propósito para que no
# puedan divergir: son las dos caras del mismo criterio ("los turnos diurnos
# ordinarios, donde meter una persona de más es un problema de coste y no de
# servicio"). Noche y Partido quedan DUROS — ver el término K en el docstring
# del módulo para el porqué de cada uno.
TURNOS_MAX_COBERTURA_BLANDA = TURNOS_DIURNOS_SIN_PARTIDO

# ─── Pesos de los términos de objetivo (fase 2) — constantes ajustables ─────
PESO_EQUIDAD_FINDES          = 100  # término A — subido de 10 a 100 (Fase 5,
# Paso 4): con 10, la dispersión de ratio de findes libres tras 18 semanas
# encadenadas quedaba PEOR que el heurístico (0.067 vs 0.056 en recepción;
# 0.069 vs 0.056 en mozos) — el término A no tenía suficiente peso frente a
# D (Partido, 20) para imponerse en la práctica. Con 100, la dispersión baja
# a 0.042 (recepción) y 0.054 (mozos), igual o mejor que el heurístico en
# ambos, sin reintroducir Partido (sigue en 0) y con una subida marginal de
# roturas de continuidad en recepción (22→24 sobre 18 semanas) — visto el
# comparativo completo en el informe de Fase 5, la contrapartida es buena.
PESO_ROTURAS_CONTINUIDAD     = 50   # término B — REDISEÑO DE PRIORIDADES (Fase 5,
# revisión posterior a fijar num_search_workers=1/sorted()): nueva prioridad de
# negocio estricta B > I > D (continuidad primero, luego evitar sobre-cubrir
# Mañana/Tarde, luego evitar Partido) — antes B=5 era MENOR que D=20, así que
# el solver prefería romper continuidad antes que usar Partido, lo contrario
# de lo pedido. Subido a 50: claramente por encima de I(30) y D(20), y también
# por encima de F(15)/H-complementario(10) — la continuidad ya no cede ante
# ninguna otra preferencia blanda de fase 2 salvo A/E/G (equidad, rol
# excluido, cobertura de ausencia), que siguen siendo cuestiones distintas
# (agregadas o de cobertura obligatoria, no de "reparto de turnos").
PESO_DESEQUILIBRIO_HISTORICO = 1    # término C
PESO_USO_PARTIDO             = 20   # término D — sin cambios: sigue siendo la
# preferencia MÁS FÁCIL DE CEDER de las tres nuevas (B > I > D), justo lo que
# la prioridad de negocio pide. Con I=30 > D=20, el solver prefiere 1 Partido
# (coste 20) antes que 1 persona de sobra en Mañana/Tarde (coste 30) — la
# válvula de escape gana frente a la sobre-cobertura, tal como se pidió.
PESO_FINDE_ROL_EXCLUIDO      = 200  # término E — debe ser > PESO_EQUIDAD_FINDES
ESCALA_RATIO                 = 1000  # escala entera para el ratio [0,1] de A (CP-SAT exige coeficientes enteros)
PESO_DOMINGO_SUSTITUTO       = 15   # término F (ver docstring del módulo) — YA
# NO está "entre B y D" tras el rediseño de prioridades (B=50 > D=20 > F=15):
# no se ha tocado su valor porque su mecanismo real es el GATING estructural
# (_tiene_par_consecutivo_disponible), no la magnitud del peso — cuando hay
# par consecutivo disponible, F ni entra en la suma del objetivo y no compite
# con nada; cuando no lo hay, 15 sigue siendo un valor moderado razonable
# frente a G(1000)/E(200)/A(100)/B(50). Ver test_gating_prefiere_par_consecutivo...
# (Paso 3 de) para la confirmación de que el gating sigue intacto.
PESO_COBERTURA_AUSENCIA      = 1000  # término G — ver docstring
PESO_COBERTURA_DIAS_LIBRES_FIJOS = 200  # término G, segunda mitad (NUEVO,
# — confirmado como regla de negocio): el cubridor (subjefe) también
# debe ocupar el turno fijo del cubierto (jefe, Mañana) en los días libres
# FIJOS de este, no solo en sus ausencias reales — el jefe tampoco está en
# el hotel un sábado. Pero con MENOS fuerza: 200 frente a los 1000 de una
# ausencia real. Motivo: los días libres fijos se repiten TODAS las semanas,
# así que cobrarlos al peso de una ausencia real dejaría al subjefe sin
# librar un fin de semana prácticamente nunca. 200 lo pone a la altura de E
# (PESO_FINDE_ROL_EXCLUIDO, que ya empuja al subjefe a no llevarse el finde
# completo libre) y claramente por debajo de G: presiona hacia la mañana del
# sábado y del domingo, pero cede sin drama ante cobertura mínima, ante una
# ausencia real del jefe y ante la acumulación de otras preferencias.
# Se activa solo si el rol lleva cubrir_dias_libres_fijos en roles.json.
PESO_COMPLEMENTARIO_JEFATURA = 10   # término H — YA NO está "por encima de B"
# tras el rediseño de prioridades (B=50 > H=10): sigue siendo la preferencia
# más fácil de ceder de las de "reparto de turnos concretos" frente a D(20)/
# F(15), y ahora también frente a B — reflejando que emparejar jefe/subjefe
# en turnos distintos es una comodidad menor comparada con preservar la
# continuidad de alguien. No se ha subido: nada en el rediseño de prioridades
# de pedía tocar este término, y sus tests de Fase 4 siguen
# calibrados sobre su valor absoluto (10), no sobre su posición relativa a B.
PESO_SOBRECOBERTURA_DIURNA   = 30   # término I (NUEVO, Fase 5 — rediseño de
# prioridades): penaliza cubrir Mañana/Tarde por encima de cfg.min_coverage.
# Nombrado "I", no "H" — "H" ya existe (PESO_COMPLEMENTARIO_JEFATURA, turno
# complementario jefe/subjefe) y significa algo distinto; reutilizar la letra
# habría chocado con un término ya documentado. Valor 30: estrictamente entre
# B=50 y D=20 (el orden de negocio pedido es B > I > D) — con 30 > D(20), 1
# persona de sobra en Mañana/Tarde (coste I=30) sale más cara que 1 turno
# Partido (coste D=20), así que el solver prefiere Partido para el excedente,
# tal como pide el ejemplo de negocio. Con B=50 > I=30, preservar la
# continuidad de alguien sigue ganando aunque eso implique sobre-cubrir un
# turno diurno ese día (ver escenario B-vs-I, Paso 2 de).
PESO_LIBRE_DEBIDO_VACACIONES = 15   # término J (cierre
# de pendientes Fase 5, ver docstring del módulo). Mismo valor y mismo tipo
# de preferencia que F (Domingo suelto del sustituto): una colocación
# concreta para una persona concreta, no una cuestión de cobertura agregada
# (A/E/G) ni de reparto histórico (C) — cede con la misma facilidad que
# F/H frente a cobertura mínima (restricción dura #2) y frente a B
# (continuidad) si hiciera falta.
PESO_EXCESO_COBERTURA        = 5000  # término K (pedido
# explícito del usuario: cfg.max_coverage pasa de restricción DURA a blanda,
# SOLO en Mañana/Tarde — ver TURNOS_MAX_COBERTURA_BLANDA y la sección del
# término K en el docstring del módulo para por qué Noche y Partido siguen
# duros). El más alto del módulo, muy por encima de G=1000 (hasta ahora el
# máximo) — a propósito: G ya cede "solo ante max_coverage" (ver su propio
# comentario), así que si K no fuera claramente mayor, el solver podría
# preferir superar max_coverage antes que dejar sin cubrir la ausencia de
# un rol, invirtiendo esa prioridad ya establecida sin que nadie lo pidiera.
# Con 5000 (5x G), superar el máximo solo compensa cuando la alternativa es
# GENUINAMENTE no encontrar ninguna solución (INFEASIBLE) — nunca por
# ahorrarse la suma de A-J en un caso normal (esa suma rara vez pasa de unos
# pocos cientos por semana, igual que ya se documentaba para G). Lineal:
# superar el máximo en 2 personas cuesta exactamente el doble que en 1
# (sin tope superior artificial más allá de len(names), la plantilla
# completa).

PESO_ORDEN_EXCESO            = 20000  # término L (pedido
# explícito del usuario): "solo superar los máximos cuando se hayan alcanzado
# AMBOS; de nada sirve exceder el máximo de Tarde cuando Mañana aún no lo ha
# alcanzado". Penaliza, por día, que se supere el máximo de un turno mientras
# el otro turno de máximo blando TODAVÍA tiene sitio libre.
#
# POR QUÉ ES BLANDO Y NO DURO (decisión deliberada, no un descuido): con
# K=5000 por persona de más, una distribución "en mal orden" ya es SIEMPRE más
# cara que la equivalente bien ordenada, porque necesita más exceso total para
# colocar a la misma gente (p.ej. con 7 personas en Mañana/Tarde y topes 3/3:
# 3+4 cuesta 5090, mientras que 2+5 cuesta 10090). Es decir: el solver nunca
# elige el mal orden por conveniencia — solo puede caer en él cuando está
# ESTRUCTURALMENTE forzado (típicamente porque quien podría ir a Mañana viene
# de Tarde o Noche el día anterior y la regla de descanso de 9h se lo impide).
# Como restricción dura, ese caso pasaría de "horario válido con un aviso" a
# INFEASIBLE y semana entera sin generar — exactamente el fallo que el término
# K existe para evitar. Con 20000 (4x K, y por encima de G=1000) el orden
# correcto gana siempre que exista alguna alternativa legal, y cede solo
# cuando la única opción sería no generar nada.
PESO_RACHA_TRABAJO           = 300  # término M (confirmado
# con el usuario): penaliza cada día trabajado por encima de
# MAX_DIAS_TRABAJADOS_SEGUIDOS contando el arrastre de la semana anterior.
# Situado entre E(200, que empuja al subjefe a trabajar el finde) y G(1000,
# cobertura de ausencia): encadenar 8-9 días es peor que cualquier preferencia
# de reparto de turnos (B/I/D/F/H) o de colocación de findes (A/E), pero no
# debe ganar a la cobertura obligatoria de un rol ausente ni, por supuesto, al
# exceso de cobertura (K). Blando a propósito — ver el término M.
MAX_DIAS_TRABAJADOS_SEGUIDOS = 6  # tope preferido de días trabajados seguidos
# CRUZANDO semanas. Dentro de una semana el patrón de 2 libres consecutivos
# (restricción #7) ya implica un máximo de 5, pero ese patrón es POR SEMANA:
# librar Lun+Mar una semana y Sáb+Dom la siguiente deja 10 días seguidos sin
# romper ninguna regla dura. 6 deja margen para que el cruce de semana no
# obligue a recolocar los libres de todo el mundo, y corta las rachas de 8-10.

DIAS_LIBRES_SEGUIDOS_POR_SEMANA = 2  # restricción DURA (
# hallazgo de producción, ver docstring del módulo): exactamente este
# número de días "Libre" por semana — CONSECUTIVOS salvo la excepción de
# negocio confirmada (sin par consecutivo disponible, ver
# _tiene_par_consecutivo_disponible) — para cualquier persona cuyo rol no
# tenga ya una garantía estructural de días libres (cfg.fixed_days_off,
# night_fixed o cfg.fixed_roles — ver la lista completa de exclusiones en
# el docstring). Valor confirmado como regla de negocio (no asumido): 2, el mismo
# estándar que ya aplican jefe (fixed_days_off=[5,6], consecutivos) y
# night_fixed (night_fixed_days_off=[3,4], consecutivos) en la
# configuración real de Recepción — máximo 5 días trabajados por semana
# para cualquier persona, en un tramo continuo.


def _tiene_par_consecutivo_disponible(cfg, dias_bloqueados: set) -> bool:
    """Replica la condición que en modelo_base.py (assign_days_off, rama
    night_cover_this_week) decide si hay un par de días consecutivos
    "libre-able" para el sustituto: un par (i, i+1) es válido si no toca
    ninguno de los días que ya cubre noche (cfg.night_fixed_days_off) ni
    ninguno de los días ya forzados a Libre por otro motivo (vacaciones,
    fixed_days_off — dias_bloqueados, el mismo forced_libre[nombre] que
    _construir_modelo ya calcula).

    Puramente estructural: NO mira cobertura mínima/máxima (igual que el
    heurístico tampoco lo hace en ese punto) ni depende de nada que decida
    el solver — se puede evaluar en Python antes de construir el término
    del objetivo."""
    must_work = set(cfg.night_fixed_days_off)
    return any(
        not (must_work & set(par)) and not (dias_bloqueados & set(par))
        for par in get_consecutive_day_pairs()
    )


def _dias_cobertura_ausencia(staff_list: list, cfg, rol_cubierto: str, info: dict) -> tuple:
    """Replica el cálculo de días de ausencia de _marcar_cobertura_ausencias
    en modelo_base.py, puramente en Python (no depende de ninguna variable
    del solver): devuelve
    (dias_ausencia: set[int], dias_libres_fijos: set[int], semana_completa: bool).

    "Ausente" = rol_cubierto no existe en staff_list, o la persona con ese
    rol tiene el día en forced_days_off/extra_days_off. "Semana completa" =
    el rol no existe, o la persona tiene los 7 días en forced_days_off
    (extra_days_off no cuenta para esto, igual que el original).

    Los días libres FIJOS del rol cubierto (cfg.fixed_days_off — p.ej. el
    Sábado y Domingo del jefe) se devuelven en un conjunto APARTE, y solo si
    info["dias_libres_fijos"] está activo. Van separados a propósito
    (confirmado como regla de negocio): el jefe tampoco está
    en el hotel en sus días libres fijos, así que el subjefe también debería
    ocupar esas mañanas — pero con MENOS fuerza que ante una ausencia real
    (vacaciones/peticiones), porque son días que se repiten cada semana y
    exigirlos con el peso de una ausencia real dejaría al subjefe sin librar
    un fin de semana prácticamente nunca. Ver PESO_COBERTURA_DIAS_LIBRES_
    FIJOS frente a PESO_COBERTURA_AUSENCIA en el término G.
    """
    cubierto = next((p for p in staff_list if p.get("role") == rol_cubierto), None)
    if cubierto is None:
        dias_ausencia = set(range(7))
        semana_completa = True
    else:
        forced = set(cubierto.get("forced_days_off", []))
        extra = set(cubierto.get("extra_days_off", []))
        ausentes = forced | extra
        dias_ausencia = {DAYS.index(d) for d in ausentes if d in DAYS}
        semana_completa = forced >= set(DAYS)

    dias_libres_fijos = set()
    if info.get("dias_libres_fijos"):
        # Los que ya son ausencia real no se duplican aquí: se cobran una
        # sola vez, y al peso más alto de los dos.
        dias_libres_fijos = set(cfg.fixed_days_off.get(rol_cubierto, [])) - dias_ausencia

    return dias_ausencia, dias_libres_fijos, semana_completa


def _inicio_bloque_vacaciones(person: dict):
    """Día (0=Lunes..6=Domingo) en que empieza, DENTRO de esta semana, un
    bloque de VACACIONES de `person` — o None si no aplica (término J, ver
    docstring del módulo).

    Distingue vacaciones de peticiones sueltas con el dato de entrada
    person['dias_vacaciones']: un campo que SOLO existe cuando esos días
    vienen de un registro de vacaciones.json con inicio/fin
    (herramientas.aplicar_vacaciones), nunca de una petición individual
    (herramientas.aplicar_peticiones no lo toca en absoluto) — es el mismo
    campo que ya usa la capa de presentación (app.etiquetar_celda) para
    mostrar "Vacaciones" en vez de "Libre" en el Excel. No es un campo
    inventado para: ya existe en producción con exactamente
    este propósito de distinguir origen, así que "¿puedo distinguir
    vacación de petición con el dato de entrada actual?" tiene respuesta
    SÍ: basta con mirar person.get("dias_vacaciones") — no vacío implica
    vacación real, vacío/ausente implica que cualquier forced_days_off que
    tenga esa persona viene de peticiones (o de otra fuente ajena a
    vacaciones.json).

    DECISIÓN DE DISEÑO (no asumida sin comprobar): NO se exige que
    dias_vacaciones tenga >=2 días DENTRO DE ESTA SEMANA para considerarlo
    "bloque". motor_cpsat.py solo ve una semana a la vez — un bloque de
    vacaciones que EMPIEZA el domingo de esta semana y continúa la semana
    siguiente aparece aquí con dias_vacaciones=["Domingo"] (un solo día
    visible), aunque el bloque real sea multi-día; exigir >=2 aquí
    excluiría precisamente el caso "empieza el domingo con máxima presión
    de días trabajados" que este término existe para cubrir. La protección
    real contra "no una petición suelta de un día" no viene de contar
    cuántos días de vacación se ven esta semana, sino de dos cosas ya
    garantizadas: (a) dias_vacaciones nunca lo rellena una petición (ver
    arriba), y (b) el propio conteo de días trabajados antes del inicio
    hace que una vacación pegada al lunes (0-1 días previos) no genere
    ningún libre debido de todas formas (tramo 0-1 -> 0), sea la vacación
    de 1 día o de 10.

    Condiciones para activarse:
      - dias_vacaciones no vacío.
      - Esos días forman un tramo CONSECUTIVO dentro de esta semana. Si no
        lo fueran (dato anómalo — no se espera con el mecanismo actual de
        inicio/fin, pero no se asume sin comprobar), se trata de forma
        conservadora como "no aplica" en vez de inventar un inicio.
      - El tramo no empieza en Lunes (día 0): si empezara el Lunes no
        habría ningún día anterior DENTRO de esta semana que contar, así
        que el conteo de días trabajados previos sería 0 de todas formas
        (0-1 días -> 0 libres debidos) — se omite el término entero para
        esa persona en vez de sumar aritmética que siempre da cero.
    """
    dias_vac = person.get("dias_vacaciones", [])
    indices = sorted(DAYS.index(d) for d in dias_vac if d in DAYS)
    if not indices:
        return None
    inicio, fin = indices[0], indices[-1]
    if list(range(inicio, fin + 1)) != indices:
        return None
    if inicio == 0:
        return None
    return inicio


def _v(x: dict, name: str, d: int, s: str):
    """Valor de la variable x[name, d, s] — o el entero 0 si esa celda no
    existe en el modelo. Solo faltan celdas para personas con presencia
    parcial (dias_presencia en su rol): esos días no tienen variables de
    turno, solo la variable FUERA (ver _construir_modelo). Para plantillas
    sin presencia_roles, x.get(...) SIEMPRE encuentra la variable y esto
    devuelve exactamente lo mismo que x[name, d, s] — el modelo construido
    es idéntico al de antes de este helper (requisito de no-regresión)."""
    return x.get((name, d, s), 0)


def _construir_modelo(staff_list: list, week_start: date, cfg,
                       prev_sunday_shifts: dict = None,
                       nombre_sustituto_cobertura: str = None,
                       dias_bloqueados_externos: dict = None,
                       dias_trabajados_previos: dict = None):
    """Construye el CpModel completo (restricciones fase 1 + objetivo fase 2)
    sin resolverlo. Separado de generar_semana_cpsat para que los tests
    puedan añadir restricciones extra (p.ej. fijar una celda) sin duplicar
    ~150 líneas de construcción del modelo.

    nombre_sustituto_cobertura: nombre de quien cubre esta semana las noches
    de night_fixed_days_off (dato de entrada externo, igual que en
    generar_semana_cpsat_con_cobertura_nocturna — no lo decide el solver).
    Si se indica y cfg.prefer_next_week_for_cover es True, activa el
    término F (preferencia de Domingo suelto). None por defecto: no cambia
    el comportamiento de fase 1/fase 2/racha para quien no lo use.

    dias_bloqueados_externos: {nombre: {idx_dias}} — días en que `nombre`
    YA está comprometido con un turno concreto por una restricción que el
    LLAMADOR añade al modelo DESPUÉS de que esta función devuelva (hoy,
    únicamente cobertura_externa en generar_semana_cpsat_con_cobertura_
    nocturna: model.Add(x[nombre, d, "Noche"] == 1) tras esta llamada, ver
    ese código). Necesario para la restricción #7 (patrón de libres, ver
    docstring del módulo): esos días NO están forzados Libre (no van en
    forced_libre) pero TAMPOCO están disponibles para colocar el patrón de
    2 libres — sin esto, restricción #7 podía calcular "hay un par
    consecutivo disponible" usando un día que en realidad ya iba a quedar
    fijado a Noche por una restricción añadida más tarde, produciendo un
    INFEASIBLE espurio (el mismo tipo de bug que dias_cubrir ya corrigió en
    su momento para vacaciones/peticiones, ahora para el patrón de libres).
    {} por defecto: no cambia el comportamiento de quien no lo use
    (generar_semana_cpsat, y todos los tests existentes que llaman a
    _construir_modelo directamente).

    Devuelve (model, x, names, by_name).
    """
    prev_sunday_shifts = prev_sunday_shifts or {}
    dias_bloqueados_externos = dias_bloqueados_externos or {}
    # {nombre: días ya trabajados SEGUIDOS al cierre de la semana anterior}
    # (término M). {} por defecto = nadie arrastra racha, que es el
    # comportamiento de quien no lo use (tests directos, primera semana).
    dias_trabajados_previos = dias_trabajados_previos or {}
    model = cp_model.CpModel()

    names = [p["name"] for p in staff_list]
    by_name = {p["name"]: p for p in staff_list}

    # x[name, day, shift] = 1 si `name` tiene `shift` en el día `day`.
    #
    # Presencia parcial (dias_presencia, ver docstring del módulo): una
    # persona cuyo rol tiene presencia NO tiene variables de turno en sus
    # días fuera — su único valor posible ese día es FUERA, fijado a 1.
    # Los días de presencia (y todas las personas sin presencia, el caso de
    # siempre) se construyen EXACTAMENTE como antes: ninguna celda extra,
    # ninguna variable extra — modelo idéntico al anterior a este cambio
    # para cualquier plantilla sin presencia.
    x = {}
    for name in names:
        dias_presencia = cfg.presencia_roles.get(by_name[name].get("role"))
        for d in range(7):
            if dias_presencia is not None and d not in dias_presencia:
                x[name, d, FUERA] = model.NewBoolVar(f"x_{name}_{d}_{FUERA}")
                model.Add(x[name, d, FUERA] == 1)
                continue
            for s in SHIFTS:
                x[name, d, s] = model.NewBoolVar(f"x_{name}_{d}_{s}")

    # objetivo_terms: movido aquí (antes vivía solo en la sección "Fase 2"
    # más abajo) porque la restricción #2 (cobertura, justo debajo) ya
    # necesita añadir su propio término blando de exceso — ver esa
    # restricción para el porqué.
    objetivo_terms = []

    # 1. Un turno por persona/día (los días fuera de presencia quedan ya
    # fijados a FUERA arriba — no hay elección que restringir ahí).
    for name in names:
        for d in range(7):
            if (name, d, FUERA) in x:
                continue
            model.AddExactlyOne(x[name, d, s] for s in SHIFTS)

    # 2. Cobertura mínima (DURA, sin cambios) / máxima (BLANDA SOLO en
    # Mañana/Tarde, ver TURNOS_MAX_COBERTURA_BLANDA). El servicio nunca
    # puede quedar por debajo de min_coverage (sigue siendo una restricción
    # dura), y en Mañana/Tarde superar max_coverage ya no vuelve el modelo
    # INFEASIBLE — se penaliza con PESO_EXCESO_COBERTURA para que el solver
    # solo lo acepte cuando la alternativa sea genuinamente no encontrar
    # ninguna solución (p.ej. el caso real de producción: 2026-08-10, donde
    # el patrón de 2-libres-consecutivos de varias personas sin garantía
    # estructural competía entre sí de forma matemáticamente irresoluble
    # contra topes de cobertura ajustados). Motivo del cambio: antes,
    # max_coverage como restricción dura podía bloquear la generación
    # ENTERA de una semana por un desajuste de personal vs. configuración
    # de temporada, en vez de generar el mejor horario posible y avisar
    # al encargado exactamente dónde se excedió.
    #
    # Noche y Partido siguen DUROS (corrección posterior — ver término K en
    # el docstring del módulo): con TODOS los turnos blandos, Noche era el
    # destino MÁS BARATO para el personal sobrante, y una noche que el
    # solver se inventa rompe la rotación por bloques decidida fuera del
    # modelo y contamina el estado de la semana siguiente.
    #
    # sorted(...) — no basta con fijar num_search_workers=1 para
    # reproducibilidad total (Fase 5, revisión de la comparativa):
    # iterar un set() directamente depende del hash-seed aleatorio de cada
    # proceso Python (PYTHONHASHSEED, no fijo por defecto), así que el
    # ORDEN en que estas restricciones se añaden al modelo variaba entre
    # ejecuciones — verificado que eso bastaba para que, en un empate de
    # valor óptimo, el solver (ya mono-hilo) explorara la solución en
    # orden distinto y devolviera un schedule distinto sin cambiar el
    # input. sorted() elimina la dependencia del hash-seed sin depender de
    # que el entorno de despliegue fije PYTHONHASHSEED.
    turnos_con_cobertura = sorted(set(cfg.min_coverage) | set(cfg.max_coverage))
    # Guardados para el término L (orden de exceso), justo debajo del bucle.
    total_por = {}
    exceso_por = {}
    for s in turnos_con_cobertura:
        mn = cfg.min_coverage.get(s, 0)
        mx = cfg.max_coverage.get(s, len(names))
        for d in range(7):
            total = sum(_v(x, name, d, s) for name in names)
            total_por[s, d] = total
            model.Add(total >= mn)
            if s in TURNOS_MAX_COBERTURA_BLANDA:
                # exceso = max(0, total - mx) — lineal, penalización
                # proporcional (superar el máximo en 2 cuesta el doble que
                # en 1, requisito de negocio). Cota superior
                # len(names): total nunca puede superar la plantilla
                # completa.
                exceso = model.NewIntVar(0, len(names), f"exceso_cobertura_{s}_{d}")
                model.Add(exceso >= total - mx)
                exceso_por[s, d] = exceso
                objetivo_terms.append(PESO_EXCESO_COBERTURA * exceso)
            else:
                model.Add(total <= mx)

    # L. Orden del exceso (requisito de negocio): no superar el máximo
    # de un turno mientras el OTRO turno de máximo blando todavía tenga sitio.
    # "De nada sirve exceder el máximo de Tarde cuando Mañana aún no lo ha
    # alcanzado." Ver PESO_ORDEN_EXCESO para por qué es blando y no duro.
    #
    # Con exactamente dos turnos blandos (Mañana/Tarde) basta agregarlo por
    # día: "hay algún exceso" Y "queda algún hueco" es equivalente a la
    # formulación por pares, porque un turno no puede tener exceso y hueco a
    # la vez (exceso > 0 implica total > mx, luego hueco = 0). Escrito de
    # forma genérica sobre TURNOS_MAX_COBERTURA_BLANDA para que siga siendo
    # correcto si algún día se añade un tercer turno blando.
    for d in range(7):
        excesos_dia = [exceso_por[s, d] for s in TURNOS_MAX_COBERTURA_BLANDA
                       if (s, d) in exceso_por]
        if len(excesos_dia) < 2:
            continue  # con un solo turno blando no hay "orden" que imponer

        huecos_dia = []
        for s in TURNOS_MAX_COBERTURA_BLANDA:
            if (s, d) not in exceso_por:
                continue
            mx_s = cfg.max_coverage.get(s, len(names))
            # hueco = max(0, mx - cobertura_real). AddMaxEquality (igualdad
            # exacta) y no una desigualdad: el hueco entra en un AND que se
            # penaliza, no en un término con presión propia hacia abajo, así
            # que el solver podría dejarlo en 0 "gratis" y desactivar la
            # penalización sin que fuera cierto.
            hueco = model.NewIntVar(0, max(0, mx_s), f"hueco_cobertura_{s}_{d}")
            model.AddMaxEquality(hueco, [mx_s - total_por[s, d], 0])
            huecos_dia.append(hueco)

        hay_exceso = model.NewBoolVar(f"hay_exceso_{d}")
        model.Add(sum(excesos_dia) >= 1).OnlyEnforceIf(hay_exceso)
        model.Add(sum(excesos_dia) == 0).OnlyEnforceIf(hay_exceso.Not())

        hay_hueco = model.NewBoolVar(f"hay_hueco_{d}")
        model.Add(sum(huecos_dia) >= 1).OnlyEnforceIf(hay_hueco)
        model.Add(sum(huecos_dia) == 0).OnlyEnforceIf(hay_hueco.Not())

        # AND por reificación de 3 desigualdades, mismo patrón que
        # weekend_libre / break_var / both_s en el resto del módulo.
        mal_orden = model.NewBoolVar(f"exceso_fuera_de_orden_{d}")
        model.Add(mal_orden <= hay_exceso)
        model.Add(mal_orden <= hay_hueco)
        model.Add(mal_orden >= hay_exceso + hay_hueco - 1)
        objetivo_terms.append(PESO_ORDEN_EXCESO * mal_orden)

    # no_night_roles: nunca Noche.
    for name in names:
        role = by_name[name].get("role")
        if role in cfg.no_night_roles:
            for d in range(7):
                model.Add(_v(x, name, d, "Noche") == 0)

    # 3. No-Mañana-tras-Tarde/Noche, MÁS (regla de
    # rotación de cobertura nocturna) tras Noche solo puede seguir Noche o
    # Libre: Tarde y Partido también quedan bloqueados el día siguiente a
    # un Noche (no solo Mañana, como ya bloqueaba NO_MORNING_AFTER). Esta
    # restricción es EXCLUSIVA de motor_cpsat.py — no se toca NO_MORNING_
    # AFTER (modelo_base.py), compartida con el motor heurístico, para
    # mantener el aislamiento que el módulo declara desde su docstring.
    for name in names:
        for d in range(7):
            if d == 0:
                prev_shift = prev_sunday_shifts.get(name)
                if prev_shift in NO_MORNING_AFTER:
                    model.Add(_v(x, name, 0, "Mañana") == 0)
                if prev_shift == "Noche":
                    model.Add(_v(x, name, 0, "Tarde") == 0)
                    model.Add(_v(x, name, 0, "Partido") == 0)
            else:
                model.Add(
                    _v(x, name, d, "Mañana") + _v(x, name, d - 1, "Tarde")
                    + _v(x, name, d - 1, "Noche") <= 1
                )
                model.Add(_v(x, name, d, "Tarde") + _v(x, name, d - 1, "Noche") <= 1)
                model.Add(_v(x, name, d, "Partido") + _v(x, name, d - 1, "Noche") <= 1)

    # 6. Vacaciones / peticiones concedidas → Libre forzado.
    forced_libre = {name: set() for name in names}
    for name in names:
        person = by_name[name]
        forced_days = list(person.get("forced_days_off", [])) + list(person.get("extra_days_off", []))
        for day_name in forced_days:
            if day_name in DAYS:
                forced_libre[name].add(DAYS.index(day_name))

    # 5. Roles con días libres fijos → Libre siempre.
    for name in names:
        role = by_name[name].get("role")
        for d in cfg.fixed_days_off.get(role, []):
            forced_libre[name].add(d)

    # night_fixed: mismo patrón que #5, anclado a cfg.night_fixed_days_off.
    for name in names:
        role = by_name[name].get("role")
        if role == "night_fixed":
            for d in cfg.night_fixed_days_off:
                forced_libre[name].add(d)

    for name in names:
        for d in forced_libre[name]:
            if (name, d, "Libre") not in x:
                continue  # vacación sobre un día ya fuera de presencia: no-op
            model.Add(x[name, d, "Libre"] == 1)

    # 6b. Presencia parcial: en un día de presencia que no está forzado
    # Libre por vacación/petición (forced_libre, arriba), el solver NO
    # puede elegir Libre — contrato "trabaja siempre" (ver docstring del
    # módulo). Combinado con #1 (un turno por día sobre SHIFTS, donde
    # Libre es uno más) esto deja a la persona con un turno de trabajo ese
    # día, obligatoriamente.
    for name in names:
        dias_presencia = cfg.presencia_roles.get(by_name[name].get("role"))
        if dias_presencia is None:
            continue
        for d in dias_presencia:
            if d in forced_libre[name]:
                continue
            model.Add(x[name, d, "Libre"] == 0)

    # 4. Roles con turno fijo: ese turno todos los días que trabajan (los
    # días forzados a Libre por #5/#6 ya quedaron fijados arriba y no se
    # tocan aquí).
    for name in names:
        role = by_name[name].get("role")
        turno_fijo = cfg.fixed_roles.get(role)
        if turno_fijo is None:
            continue
        for d in range(7):
            if d in forced_libre[name]:
                continue
            if (name, d, turno_fijo) not in x:
                continue  # día fuera de presencia: no hay variables de turno
            model.Add(x[name, d, turno_fijo] == 1)

    # night_fixed: mismo patrón que #4, turno fijo "Noche" (los días de
    # night_fixed_days_off ya quedaron fijados a Libre arriba).
    for name in names:
        role = by_name[name].get("role")
        if role != "night_fixed":
            continue
        for d in range(7):
            if d in forced_libre[name]:
                continue
            model.Add(x[name, d, "Noche"] == 1)

    # 7. Patrón de libres semanales (hallazgo de
    # producción, ver docstring del módulo): cada persona cuyo rol no tenga
    # ya una garantía estructural de días libres (fixed_days_off,
    # night_fixed, turno fijo) recibe EXACTAMENTE DIAS_LIBRES_SEGUIDOS_
    # POR_SEMANA días "Libre" — ni más ni menos — entre los días que no
    # están YA forzados Libre por vacaciones/peticiones/LA (forced_libre,
    # calculado arriba en #5/#6), y esos días deben ser CONSECUTIVOS
    # (índices de día adyacentes) siempre que exista al menos un par
    # consecutivo disponible entre los días no forzados. Cuando ningún par
    # consecutivo es estructuralmente posible (p.ej. el sustituto de
    # cobertura nocturna cuyos días fijos de cobertura no dejan ningún par
    # libre — el mismo caso que ya activa el término blando F, ver
    # _tiene_par_consecutivo_disponible), se exige igualmente el conteo
    # exacto pero SIN exigir consecutividad (2 días sueltos).
    #
    # Los días ya forzados Libre (vacaciones/peticiones/LA) quedan FUERA de
    # este conteo — se apilan aparte, sin alterar ni sustituir el patrón de
    # 2 seguidos (confirmado como regla de negocio): alguien con una petición
    # suelta un miércoles sigue necesitando su propio par de 2 seguidos en
    # el resto de la semana, además de ese miércoles.
    #
    # Si a una persona le quedan menos de 2 días NO forzados en la semana
    # (una vacación/baja larga que ya cubre 6-7 días), no hay sitio para
    # exigir nada más — se omite la restricción entera para ella esa
    # semana en vez de forzar una contradicción sobre los 0-1 días que le
    # quedan.
    for name in names:
        role = by_name[name].get("role")
        if (role == "night_fixed" or cfg.fixed_days_off.get(role)
                or cfg.fixed_roles.get(role) or role in cfg.presencia_roles):
            # Presencia parcial: la excepción deliberada a esta restricción
            # (ver docstring del módulo) — el patrón de 2 libres no aplica a
            # quien no está contratado toda la semana; sus días fuera ya
            # quedan fijados a FUERA arriba y los de presencia no pueden
            # ser Libre (bloque 6b).
            continue

        dias_bloqueados_persona = dias_bloqueados_externos.get(name, set())

        # Caso especial confirmado como regla de negocio (NUEVO —
        # regla de rotación de cobertura nocturna): si el compromiso
        # externo de Noche de esta persona termina exactamente el Sábado
        # (día 5) sin cubrir también el Domingo, el Domingo queda
        # estructuralmente forzado a Libre para ella — tras Noche el
        # sábado, la restricción de transición exige Noche o Libre el
        # domingo, y Noche ahí competiría con night_fixed (o cualquier
        # otra cobertura ya fija ese día) por encima de max_coverage. Este
        # razonamiento SIGUE siendo válido tras el término K: el máximo de
        # Noche es uno de los dos que se mantienen DUROS a propósito (ver
        # TURNOS_MAX_COBERTURA_BLANDA), justamente para que el solver no
        # pueda añadir noches que nadie planificó.
        # El
        # Domingo CUENTA como uno de sus DIAS_LIBRES_SEGUIDOS_POR_SEMANA(2)
        # — no se apila aparte como una vacación — y el que queda
        # pendiente (1) se coloca donde haga falta, SIN exigir que sea
        # consecutivo con nada (el mozo que cubre noches puede tener sus 2
        # libres semanales separados; el domingo ya forma, junto con el
        # Lunes+Martes incondicional de la semana siguiente —
        # cfg.prefer_next_week_for_cover—, un tramo de 3 libres seguidos
        # aparte).
        if 5 in dias_bloqueados_persona and 6 not in dias_bloqueados_persona:
            model.Add(x[name, 6, "Libre"] == 1)
            dias_no_forzados = [
                d for d in range(7)
                if d not in forced_libre[name] and d not in dias_bloqueados_persona and d != 6
            ]
            if len(dias_no_forzados) >= DIAS_LIBRES_SEGUIDOS_POR_SEMANA - 1:
                model.Add(
                    sum(x[name, d, "Libre"] for d in dias_no_forzados)
                    == DIAS_LIBRES_SEGUIDOS_POR_SEMANA - 1
                )
            continue

        # Petición concedida (CORRECCIÓN: reportado por el
        # usuario): a diferencia de vacaciones/LA (que SÍ se apilan aparte,
        # sin cambios), un día suelto de PETICIÓN no es un libre extra —
        # cuenta como uno de los DIAS_LIBRES_SEGUIDOS_POR_SEMANA(2), la
        # petición solo fija DÓNDE cae ese libre. Se distingue de vacación
        # con person['dias_vacaciones'] (solo lo rellena herramientas.
        # aplicar_vacaciones, nunca aplicar_peticiones — ver _inicio_bloque_
        # vacaciones arriba, mismo dato ya usado con este propósito).
        # Confirmado con el usuario: con UNA petición esta semana, se
        # intenta formar el par de 2 seguidos incluyéndola (el otro día
        # debe ser el adyacente, si hay alguno disponible); con DOS
        # peticiones la misma semana ya no se exige que sean consecutivas
        # entre sí — el conteo de 2 queda cubierto por ambas, sueltas.
        person = by_name[name]
        dias_vacaciones_idx = {
            DAYS.index(d) for d in person.get("dias_vacaciones", []) if d in DAYS
        }
        dias_peticion = (forced_libre[name] - dias_vacaciones_idx) - dias_bloqueados_persona

        no_disponibles = (forced_libre[name] - dias_peticion) | dias_bloqueados_persona
        dias_no_forzados_totales = [d for d in range(7) if d not in no_disponibles]

        if len(dias_peticion) > DIAS_LIBRES_SEGUIDOS_POR_SEMANA:
            # Más peticiones concedidas que el propio patrón admite: el
            # patrón ya está más que cubierto, así que no se puede exigir
            # nada más — pero SÍ hay que impedir libres ADICIONALES.
            # (CORRECCIÓN) Antes esto era un `continue` seco,
            # que dejaba los días restantes sin ninguna cota: alguien con 3
            # peticiones concedidas podía acabar con los 7 días Libre, el
            # mismo fallo silencioso ("7 libres / 0 días trabajados") que
            # esta restricción existe para evitar.
            resto = [d for d in dias_no_forzados_totales if d not in dias_peticion]
            if resto:
                model.Add(sum(x[name, d, "Libre"] for d in resto) == 0)
            continue

        if dias_peticion:
            faltan = DIAS_LIBRES_SEGUIDOS_POR_SEMANA - len(dias_peticion)
            dias_no_forzados = [d for d in dias_no_forzados_totales if d not in dias_peticion]
            if faltan > 0 and len(dias_no_forzados) < faltan:
                # No quedan días donde colocar lo que falta del patrón. No
                # hace falta cota adicional aquí: esta rama solo se alcanza
                # con faltan==1 y dias_no_forzados vacío (con 2 peticiones
                # faltan==0 y se sigue por el camino normal de abajo), así
                # que no hay ningún día suelto que el solver pudiera poner
                # Libre de más.
                continue
            # faltan == 0 (dos peticiones ya completan el patrón): el resto
            # de días NO forzados deben quedar SIN Libre — sin este "== 0"
            # explícito (en vez de omitir la restricción), esos días
            # quedarían totalmente libres para el solver, sin ningún límite
            # de cuántos más podría poner Libre esa semana.
            if faltan == 1 and len(dias_peticion) == 1:
                p = next(iter(dias_peticion))
                candidatos_par = [d for d in (p - 1, p + 1) if 0 <= d <= 6 and d in dias_no_forzados]
                if candidatos_par:
                    indicadores_par = []
                    for d in candidatos_par:
                        ind = model.NewBoolVar(f"par_libre_peticion_{name}_{d}")
                        model.Add(x[name, d, "Libre"] == 1).OnlyEnforceIf(ind)
                        indicadores_par.append(ind)
                    model.AddExactlyOne(indicadores_par)
            model.Add(sum(x[name, d, "Libre"] for d in dias_no_forzados) == faltan)
            continue

        dias_no_forzados = dias_no_forzados_totales
        if len(dias_no_forzados) < DIAS_LIBRES_SEGUIDOS_POR_SEMANA:
            # Vacación/baja larga que ya cubre 6-7 días: no hay sitio para
            # exigir el patrón. Se deja SIN cota adicional a propósito: el
            # único día suelto que pudiera quedar ya está rodeado de días de
            # vacación, así que ponerlo Libre no es el fallo de reparto que
            # esta restricción persigue (esa persona no está acaparando los
            # libres de nadie), y forzarla a trabajar justo ese día sí
            # podría volver la semana infactible sin ninguna ganancia real.
            continue

        no_forzados_set = set(dias_no_forzados)
        pares_disponibles = [
            (d, d + 1) for d in range(6)
            if d in no_forzados_set and d + 1 in no_forzados_set
        ]
        if pares_disponibles:
            indicadores_par = []
            for d1, d2 in pares_disponibles:
                ind = model.NewBoolVar(f"par_libre_{name}_{d1}_{d2}")
                model.Add(x[name, d1, "Libre"] == 1).OnlyEnforceIf(ind)
                model.Add(x[name, d2, "Libre"] == 1).OnlyEnforceIf(ind)
                indicadores_par.append(ind)
            model.AddExactlyOne(indicadores_par)
        # Con o sin par disponible: el total de Libre entre los días no
        # forzados debe ser exactamente el tamaño del patrón — cuando hay
        # par, esto además impide que sobren libres fuera del par elegido;
        # cuando no hay par, es la única exigencia posible (2 sueltos).
        model.Add(
            sum(x[name, d, "Libre"] for d in dias_no_forzados) == DIAS_LIBRES_SEGUIDOS_POR_SEMANA
        )

    # ─── Fase 2: términos de objetivo ────────────────────────────────────
    # (objetivo_terms ya se inicializó antes de la restricción #1 — la
    # restricción #2, cobertura máxima, ya necesitaba empezar a añadir
    # términos blandos antes de llegar aquí.)

    # Variable auxiliar compartida por A y E: finde completo libre.
    weekend_libre = {}
    for name in names:
        v = model.NewBoolVar(f"weekend_libre_{name}")
        model.Add(v <= _v(x, name, 5, "Libre"))
        model.Add(v <= _v(x, name, 6, "Libre"))
        model.Add(v >= _v(x, name, 5, "Libre") + _v(x, name, 6, "Libre") - 1)
        weekend_libre[name] = v

    # A. Equidad de findes — solo personas cuyo rol NO está en
    # cfg.roles_excluidos_equidad entran en la suma; las demás no aparecen
    # aquí en absoluto (ver docstring del módulo).
    ratios = _get_weekend_ratios(cfg.dept_id, staff_list)

    # La media se calcula SOLO sobre las personas que de verdad entran en
    # este término (CORRECCIÓN). Antes se calculaba sobre todo
    # `ratios`, incluidas las que tienen el fin de semana libre GARANTIZADO
    # por una restricción dura — el jefe (fixed_days_off=[5,6]) y el
    # night_fixed, cuyo ratio histórico tiende a 1.0 por construcción. Eso
    # inflaba la media y desplazaba el coeficiente (ratio - avg_ratio) de
    # todas las demás hacia el lado negativo, es decir: convertía "dar un
    # finde libre a cualquiera" en una recompensa sistemática en vez de en
    # el mecanismo de equidad relativa que este término pretende ser.
    def _entra_en_equidad(nombre: str) -> bool:
        rol = by_name[nombre].get("role")
        if rol in cfg.presencia_roles:
            # Presencia parcial: exclusión ESTRUCTURAL, además del flag
            # excluir_de_equidad forzado a True en herramientas.py — el
            # ratio de findes de quien no trabaja toda la semana no mide
            # equidad (sus días fuera ni siquiera son "Libre").
            return False
        if rol in cfg.roles_excluidos_equidad:
            return False
        # Finde libre garantizado por restricción dura: su ratio no mide
        # equidad, mide su propia configuración. Se comprueba que los días
        # libres fijos cubran REALMENTE el finde entero (5 y 6) — un rol con
        # fixed_days_off entre semana (p.ej. [0,1]) sí compite por los
        # findes como cualquier otro y debe seguir contando.
        dias_fijos = set(cfg.night_fixed_days_off if rol == "night_fixed"
                         else cfg.fixed_days_off.get(rol, []))
        return not {5, 6} <= dias_fijos

    ratios_validos = [
        d["ratio"] for nombre, d in ratios.items()
        if nombre in by_name and _entra_en_equidad(nombre)
    ]
    avg_ratio = sum(ratios_validos) / len(ratios_validos) if ratios_validos else 0.0
    for name in names:
        if not _entra_en_equidad(name):
            continue
        ratio = ratios.get(name, {}).get("ratio", avg_ratio)
        # Coste relativo a la media: negativo (recompensa) si está por
        # debajo, positivo (penalización) si está por encima. Un coste
        # solo-positivo (p.ej. ratio*escala) haría "que nadie libre el
        # finde" tan barato como "que lo libre quien menos lo ha librado" —
        # sin presión real hacia la equidad.
        coef = int(round((ratio - avg_ratio) * ESCALA_RATIO))
        objetivo_terms.append(PESO_EQUIDAD_FINDES * coef * weekend_libre[name])

    # E. Penalización alta: rol excluido de equidad SIN fixed_days_off propio
    # (ni el caso especial night_fixed, cubierto por restricción dura fase 1)
    # que recibe finde completo libre.
    roles_con_libre_duro = set(cfg.fixed_days_off) | {"night_fixed"}
    roles_incentivo_trabajar_finde = {
        role for role in cfg.roles_excluidos_equidad
        if role not in roles_con_libre_duro
    }
    for name in names:
        role = by_name[name].get("role")
        if role in roles_incentivo_trabajar_finde:
            objetivo_terms.append(PESO_FINDE_ROL_EXCLUIDO * weekend_libre[name])

    # B. Roturas de continuidad: penaliza cambiar de turno entre dos días
    # consecutivos ambos trabajados (Mañana/Tarde/Partido, sin librar en
    # medio). Noche y Libre no participan.
    work_var = {}
    for name in names:
        for d in range(7):
            wv = model.NewBoolVar(f"work_{name}_{d}")
            model.Add(wv == sum(_v(x, name, d, s) for s in DAY_SHIFTS))
            work_var[name, d] = wv

    for name in names:
        for d in range(1, 7):
            work_both = model.NewBoolVar(f"work_both_{name}_{d}")
            model.Add(work_both <= work_var[name, d - 1])
            model.Add(work_both <= work_var[name, d])
            model.Add(work_both >= work_var[name, d - 1] + work_var[name, d] - 1)

            keep_terms = []
            for s in DAY_SHIFTS:
                keep = model.NewBoolVar(f"keep_{name}_{d}_{s}")
                model.Add(keep <= _v(x, name, d - 1, s))
                model.Add(keep <= _v(x, name, d, s))
                model.Add(keep >= _v(x, name, d - 1, s) + _v(x, name, d, s) - 1)
                keep_terms.append(keep)

            break_var = model.NewBoolVar(f"break_{name}_{d}")
            model.Add(break_var == work_both - sum(keep_terms))
            objetivo_terms.append(PESO_ROTURAS_CONTINUIDAD * break_var)

    # B (cruce de semana) — NUEVO: el bucle de arriba empieza en
    # d=1, así que compara 0-1, 1-2 … 5-6 pero NUNCA el domingo de la semana
    # anterior con el lunes de esta. Medido antes de este arreglo sobre la
    # plantilla real (4 semanas encadenadas): 100% de continuidad DENTRO de la
    # semana frente a solo 42% en el cruce Domingo→Lunes — apenas por encima
    # del azar entre tres turnos diurnos (33%), porque nada lo penalizaba.
    #
    # No era una decisión de diseño sino un hueco: la regla de negocio es "no
    # cambiar de turno entre dos días TRABAJADOS consecutivos", y Domingo→Lunes
    # es exactamente ese caso cuando la persona trabaja ambos. Es distinto del
    # cambio a través de los DÍAS LIBRES, que sigue siendo gratis a propósito
    # (ver el test de continuidad entre libres): si el domingo anterior fue
    # Libre, prev_shift no está en DAY_SHIFTS y este término ni se añade.
    #
    # El domingo anterior es un DATO constante (prev_sunday_shifts), no una
    # variable del modelo, así que no hace falta el patrón work_both/keep del
    # bucle de arriba: basta con
    #   rotura = trabaja_el_lunes − mantiene_el_mismo_turno
    # que vale 1 solo si el lunes trabaja un turno diurno distinto, y 0 si
    # mantiene el turno, si libra o si hace Noche.
    for name in names:
        prev_shift = prev_sunday_shifts.get(name)
        if prev_shift not in DAY_SHIFTS:
            continue
        break_cruce = model.NewBoolVar(f"break_cruce_semana_{name}")
        model.Add(break_cruce == work_var[name, 0] - _v(x, name, 0, prev_shift))
        objetivo_terms.append(PESO_ROTURAS_CONTINUIDAD * break_cruce)

    # M. Días trabajados seguidos cruzando semanas (NUEVO —
    # confirmado como regla de negocio). El patrón de libres (restricción #7) es POR
    # SEMANA, así que nada impedía librar Lun+Mar una semana y Sáb+Dom la
    # siguiente: 10 días trabajados seguidos sin romper ninguna regla. Medido
    # sobre la plantilla real en 4 semanas encadenadas: Maira 9 días seguidos,
    # Carmen 8.
    #
    # Se modela como ventana deslizante sobre la secuencia
    # [días ya trabajados al cierre de la semana anterior] + [los 7 de esta],
    # penalizando cada ventana de MAX_DIAS_TRABAJADOS_SEGUIDOS+1 días que esté
    # entera trabajada. Es BLANDO (igual que el orden de exceso, y por el mismo
    # motivo): como restricción dura podría volver INFEASIBLE una semana entera
    # por un arrastre de la anterior que ya no se puede cambiar.
    for name in names:
        r = min(int(dias_trabajados_previos.get(name, 0) or 0), 7)
        secuencia = [1] * r + [work_var[name, d] for d in range(7)]
        ventana = MAX_DIAS_TRABAJADOS_SEGUIDOS + 1
        for g in range(0, len(secuencia) - ventana + 1):
            tramo = secuencia[g:g + ventana]
            if all(isinstance(v, int) for v in tramo):
                # Ventana enteramente dentro de la semana ANTERIOR: son todo
                # constantes, ya está decidida y no hay nada que penalizar
                # aquí (penalizarla solo añadiría una constante al objetivo).
                # isinstance(v, int) y no `v == 1`: sobre una BoolVar, `== 1`
                # construye una expresión del modelo, no un booleano de Python.
                continue
            exceso_racha = model.NewIntVar(0, ventana, f"exceso_racha_trabajo_{name}_{g}")
            model.Add(exceso_racha >= sum(tramo) - MAX_DIAS_TRABAJADOS_SEGUIDOS)
            objetivo_terms.append(PESO_RACHA_TRABAJO * exceso_racha)

    # C. Desequilibrio histórico de turnos: prefiere el turno menos hecho.
    shift_counts = _get_shift_counts(cfg.dept_id)
    for name in names:
        counts = shift_counts.get(name, {})
        for s in DAY_SHIFTS:
            c = counts.get(s, 0)
            if c:
                for d in range(7):
                    objetivo_terms.append(PESO_DESEQUILIBRIO_HISTORICO * c * _v(x, name, d, s))

    # D. Uso de "Partido".
    objetivo_terms.append(
        PESO_USO_PARTIDO * sum(_v(x, name, d, "Partido") for name in names for d in range(7))
    )

    # F. Preferencia de Domingo suelto para el sustituto de cobertura
    # nocturna (mozos y cualquier depto con el mismo mecanismo — ver
    # docstring del módulo). Solo se activa si cfg.prefer_next_week_for_cover
    # es True (la señal existente de este mecanismo, no el nombre literal
    # "mozos"), se indicó explícitamente quién es el sustituto esta semana,
    # Y (gating estructural, corrección posterior) no existe NINGÚN par de
    # días consecutivos disponible para esa persona (_tiene_par_consecutivo_
    # disponible) — si lo hay, el término ni se añade a objetivo_terms: no
    # puede competir con B (continuidad) ni con nada, gane o pierda, porque
    # sencillamente no está en la suma.
    if cfg.prefer_next_week_for_cover and nombre_sustituto_cobertura in names:
        # Presencia parcial: si el Domingo de esa persona cae fuera de
        # presencia no existe su variable Libre — el término ni se añade
        # (no hay preferencia que expresar sobre un día que ya no trabaja).
        if ((nombre_sustituto_cobertura, 6, "Libre") in x
                and not _tiene_par_consecutivo_disponible(
                    cfg, forced_libre[nombre_sustituto_cobertura])):
            objetivo_terms.append(
                PESO_DOMINGO_SUSTITUTO * (1 - x[nombre_sustituto_cobertura, 6, "Libre"])
            )

    # G. Cobertura de ausencia de otro rol (cfg.cobertura_roles, p.ej.
    # subjefe cubre a jefe — ver docstring del módulo). Peso muy alto, pero
    # SIEMPRE blando: cede ante max_coverage (restricción dura #2) sin
    # volver el modelo infactible, igual que el motor heurístico nunca
    # rompe max_coverage por este motivo.
    for rol_cubridor, info in cfg.cobertura_roles.items():
        rol_cubierto = info.get("cubre")
        if not rol_cubierto:
            continue
        cubridores = [name for name in names if by_name[name].get("role") == rol_cubridor]
        if not cubridores:
            continue
        turno_cobertura = cfg.fixed_roles.get(rol_cubierto)
        if not turno_cobertura:
            continue

        dias_ausencia, dias_libres_fijos, semana_completa = _dias_cobertura_ausencia(
            staff_list, cfg, rol_cubierto, info
        )
        cobertura_total_semana = semana_completa and info.get("semana_completa")

        for name in cubridores:
            for d in dias_ausencia:
                objetivo_terms.append(PESO_COBERTURA_AUSENCIA * (1 - _v(x, name, d, turno_cobertura)))
            # Días libres FIJOS del cubierto: mismo mecanismo, peso menor
            # (ver _dias_cobertura_ausencia y la constante).
            for d in dias_libres_fijos:
                objetivo_terms.append(
                    PESO_COBERTURA_DIAS_LIBRES_FIJOS * (1 - _v(x, name, d, turno_cobertura))
                )
            if cobertura_total_semana:
                for d in (5, 6):
                    objetivo_terms.append(PESO_COBERTURA_AUSENCIA * _v(x, name, d, "Libre"))

    # H. Turno complementario jefe/subjefe: en los días que ambos trabajan,
    # penaliza (blando) que coincidan en el mismo turno no-Libre. Solo se
    # activa si ambos roles existen en staff_list. Mutuamente excluyente
    # con G por construcción: los días de cobertura de ausencia, jefe está
    # Libre, así que nunca hay "ambos trabajan" ahí.
    jefe_name = next((name for name in names if by_name[name].get("role") == "jefe"), None)
    subjefe_name = next((name for name in names if by_name[name].get("role") == "subjefe"), None)
    if jefe_name and subjefe_name:
        for d in range(7):
            coincidencias = []
            for s in SHIFTS:
                if s == "Libre":
                    continue
                both_s = model.NewBoolVar(f"complementario_{s}_{d}")
                model.Add(both_s <= _v(x, jefe_name, d, s))
                model.Add(both_s <= _v(x, subjefe_name, d, s))
                model.Add(both_s >= _v(x, jefe_name, d, s) + _v(x, subjefe_name, d, s) - 1)
                coincidencias.append(both_s)
            objetivo_terms.append(PESO_COMPLEMENTARIO_JEFATURA * sum(coincidencias))

    # I. Sobre-cobertura de turnos diurnos (Fase 5 — rediseño de
    # prioridades, ver docstring del módulo): penaliza, por cada día y cada
    # turno de TURNOS_DIURNOS_SIN_PARTIDO (Mañana/Tarde), cuánto se cubre
    # ese turno POR ENCIMA de cfg.min_coverage. Lineal y siempre no-negativo
    # sin necesitar max(): la restricción dura #2 (fase 1) ya garantiza
    # cobertura_real >= min_coverage para cualquier solución factible, así
    # que (cobertura_real - min_coverage) nunca puede ser negativo — no hay
    # riesgo de que este término "recompense" nada.
    # No incluye Noche (su cobertura la decide la orquestación de fase 3,
    # ajena a esta lógica de Partido como alternativa) ni Partido (es la
    # válvula de escape que este término empuja a usar, no un turno más a
    # penalizar por exceso).
    for turno in TURNOS_DIURNOS_SIN_PARTIDO:
        min_turno = cfg.min_coverage.get(turno, 0)
        for d in range(7):
            cobertura_real = sum(_v(x, name, d, turno) for name in names)
            objetivo_terms.append(PESO_SOBRECOBERTURA_DIURNA * (cobertura_real - min_turno))

    # J. Libre(s) debido(s) antes de que empiecen las vacaciones (NUEVO,
    # — cierre de pendientes Fase 5, ver docstring del módulo
    # para el diseño completo). Solo se procesa para quien tiene un bloque
    # de vacaciones multi-día que empieza a mitad de ESTA semana
    # (_inicio_bloque_vacaciones distingue vacación de petición suelta y
    # devuelve None si no aplica).
    for name in names:
        inicio = _inicio_bloque_vacaciones(by_name[name])
        if inicio is None:
            continue

        # Conteo de días trabajados antes del inicio — expresión del
        # modelo, no un dato precalculado: depende de qué resuelva el
        # solver para esos días (Libre o no).
        dias_previos = list(range(inicio))
        cuenta_trabajados = len(dias_previos) - sum(_v(x, name, d, "Libre") for d in dias_previos)

        # step1 <-> cuenta>=2 (activa el 1er libre debido).
        # step2 <-> cuenta>=5 (activa el 2º libre debido, ADEMÁS del 1º).
        # step1+step2 reproduce exactamente la escalera 0/1/2 pedida:
        # 0,1->0; 2,3,4->1; 5,6->2. Reificación completa (ambas
        # direcciones) porque ninguna otra restricción ata a estas
        # variables — sin la igualdad exacta el solver podría dejarlas en
        # 1 sin motivo real y el término penalizaría de más.
        step1 = model.NewBoolVar(f"vac_step1_{name}")
        model.Add(cuenta_trabajados >= 2).OnlyEnforceIf(step1)
        model.Add(cuenta_trabajados <= 1).OnlyEnforceIf(step1.Not())

        step2 = model.NewBoolVar(f"vac_step2_{name}")
        model.Add(cuenta_trabajados >= 5).OnlyEnforceIf(step2)
        model.Add(cuenta_trabajados <= 4).OnlyEnforceIf(step2.Not())

        # falta1/falta2: AND de "el tramo exige este libre" y "ese día
        # concreto no quedó Libre" — mismo patrón de AND por 3
        # desigualdades que weekend_libre/break_var/both_s más arriba.
        # Solo penalizan la AUSENCIA del libre debido; nunca fuerzan
        # x[...]==1 (preferencia blanda de verdad, cede si hace falta).
        pos1 = inicio - 1
        falta1 = model.NewBoolVar(f"vac_falta1_{name}")
        model.Add(falta1 <= step1)
        model.Add(falta1 <= 1 - _v(x, name, pos1, "Libre"))
        model.Add(falta1 >= step1 + (1 - _v(x, name, pos1, "Libre")) - 1)
        objetivo_terms.append(PESO_LIBRE_DEBIDO_VACACIONES * falta1)

        if inicio >= 2:
            pos2 = inicio - 2
            falta2 = model.NewBoolVar(f"vac_falta2_{name}")
            model.Add(falta2 <= step2)
            model.Add(falta2 <= 1 - _v(x, name, pos2, "Libre"))
            model.Add(falta2 >= step2 + (1 - _v(x, name, pos2, "Libre")) - 1)
            objetivo_terms.append(PESO_LIBRE_DEBIDO_VACACIONES * falta2)

    model.Minimize(sum(objetivo_terms))

    return model, x, names, by_name


# ═══════════════════════════════════════════════════════════════════════════
# Diagnóstico de infactibilidad (Tema 3, cierre de pendientes
# Fase 5)
# ═══════════════════════════════════════════════════════════════════════════
#
# API confirmada disponible en la versión de ortools instalada (9.15.6755):
# CpModel.AddAssumptions(lits) / CpSolver.SufficientAssumptionsForInfeasibility()
# — el mecanismo estándar de ortools para encontrar un subconjunto MÍNIMO de
# literales ("assumptions") que, impuestos junto al resto del modelo, no
# tienen solución. Se construye una versión REDUCIDA del modelo real (solo
# las restricciones duras de fase 1, sin fase 2/objetivo ni fase 3/racha —
# la racha nunca es la única causa de una infactibilidad que fase 1 ya no
# tuviera, porque agregar_restriccion_racha_noches solo AÑADE restricciones
# sobre un modelo que fase 1 ya construyó) donde las piezas potencialmente
# "cedibles" — vacaciones/peticiones (agrupadas por persona en tramos
# contiguos: un bloque de vacación multi-día es UN solo motivo, no N) y
# cobertura mínima por día/turno — están controladas por variables de
# asunción en vez de fijadas incondicionalmente. Las piezas estructurales
# del departamento (cobertura MÁXIMA, no_night_roles, turno fijo de rol,
# no-Mañana-tras-Tarde/Noche) se dejan siempre activas: no son algo que un
# encargado pueda "conceder" para resolver un conflicto puntual de una
# semana, así que diagnosticar sobre ellas no ayudaría a decidir qué ceder
# — si el conflicto real depende de alguna de ellas, el diagnóstico lo dice
# explícitamente en vez de inventar una explicación con lo que sí puede ver.
#
# Solo diagnostica — nunca relaja nada automáticamente ni cambia el
# resultado devuelto al llamador; la decisión de qué ceder la toma el
# encargado manualmente después de leer el mensaje.

def _agrupar_en_tramos(dias_ordenados: list) -> list:
    """Agrupa una lista ORDENADA de índices de día en tramos de días
    consecutivos: [0,1,2,4,6] -> [[0,1,2],[4],[6]]. Vacía -> []."""
    if not dias_ordenados:
        return []
    tramos = [[dias_ordenados[0]]]
    for d in dias_ordenados[1:]:
        if d == tramos[-1][-1] + 1:
            tramos[-1].append(d)
        else:
            tramos.append([d])
    return tramos


def _describir_bloque_libre(name: str, bloque: list) -> str:
    if len(bloque) == 1:
        return f"{name}: {DAYS[bloque[0]]} forzado libre (vacación/petición)"
    return f"{name}: {DAYS[bloque[0]]}-{DAYS[bloque[-1]]} forzado libre (vacación/petición)"


def _construir_modelo_diagnostico(staff_list: list, cfg) -> tuple:
    """Versión reducida de _construir_modelo (solo restricciones duras de
    fase 1) donde las piezas "cedibles" quedan controladas por variables de
    asunción — ver la nota de diseño arriba para el alcance exacto (qué se
    gatea y qué se deja siempre activo, y por qué).

    Devuelve (model, x, names, asunciones: {BoolVar: descripción legible}).
    Nunca se resuelve como modelo de producción real — solo se usa dentro
    de _diagnosticar_infactibilidad.
    """
    model = cp_model.CpModel()
    names = [p["name"] for p in staff_list]
    by_name = {p["name"]: p for p in staff_list}

    x = {}
    for name in names:
        dias_presencia = cfg.presencia_roles.get(by_name[name].get("role"))
        for d in range(7):
            if dias_presencia is not None and d not in dias_presencia:
                x[name, d, FUERA] = model.NewBoolVar(f"dx_{name}_{d}_{FUERA}")
                model.Add(x[name, d, FUERA] == 1)
                continue
            for s in SHIFTS:
                x[name, d, s] = model.NewBoolVar(f"dx_{name}_{d}_{s}")
    for name in names:
        for d in range(7):
            if (name, d, FUERA) in x:
                continue
            model.AddExactlyOne(x[name, d, s] for s in SHIFTS)

    asunciones = {}

    # Cobertura: la máxima se modela SOLO para los turnos donde sigue
    # siendo dura (Noche y Partido — todo lo que no está en
    # TURNOS_MAX_COBERTURA_BLANDA). En Mañana/Tarde dejó de ser dura en
    # _construir_modelo (ver PESO_EXCESO_COBERTURA) y por tanto no puede
    # causar un INFEASIBLE genuino, así que reproducirla aquí haría que el
    # diagnóstico señalara como "conflicto" algo que el modelo real ya no
    # trata como bloqueante. Se deja siempre activa, sin gatear, igual que
    # antes: sigue siendo una política estructural del departamento, no
    # algo que un encargado conceda para una semana concreta.
    #
    # Mínima sigue gateada por asunción, agrupada por (día, turno) — es la
    # unidad más pequeña con sentido para un encargado ("la cobertura
    # mínima de Noche el Sábado").
    turnos_con_cobertura = sorted(set(cfg.min_coverage) | set(cfg.max_coverage))
    for s in turnos_con_cobertura:
        mn = cfg.min_coverage.get(s, 0)
        mx = cfg.max_coverage.get(s, len(names))
        for d in range(7):
            total = sum(_v(x, name, d, s) for name in names)
            if s not in TURNOS_MAX_COBERTURA_BLANDA:
                model.Add(total <= mx)
            if mn > 0:
                a = model.NewBoolVar(f"asum_mincov_{s}_{d}")
                model.Add(total >= mn).OnlyEnforceIf(a)
                asunciones[a] = f"Cobertura mínima Noche={mn} el {DAYS[d]}" if s == "Noche" \
                    else f"Cobertura mínima {s}={mn} el {DAYS[d]}"

    # no_night_roles: siempre activa.
    for name in names:
        role = by_name[name].get("role")
        if role in cfg.no_night_roles:
            for d in range(7):
                model.Add(_v(x, name, d, "Noche") == 0)

    # No-Mañana-tras-Tarde/Noche, MÁS tras Noche solo Noche o Libre (NUEVO,
    # — ver la restricción #3 de _construir_modelo): siempre
    # activa (empalme con la semana anterior no se conoce aquí — el
    # diagnóstico no lo necesita para el tipo de conflicto que existe para
    # detectar, ver docstring del módulo).
    for name in names:
        for d in range(1, 7):
            model.Add(
                _v(x, name, d, "Mañana") + _v(x, name, d - 1, "Tarde")
                + _v(x, name, d - 1, "Noche") <= 1
            )
            model.Add(_v(x, name, d, "Tarde") + _v(x, name, d - 1, "Noche") <= 1)
            model.Add(_v(x, name, d, "Partido") + _v(x, name, d - 1, "Noche") <= 1)

    # Vacaciones/peticiones — agrupadas por persona en tramos contiguos,
    # gateadas por asunción (un bloque multi-día = un solo motivo).
    forced_libre = {name: set() for name in names}
    for name in names:
        person = by_name[name]
        forced_days = list(person.get("forced_days_off", [])) + list(person.get("extra_days_off", []))
        for day_name in forced_days:
            if day_name in DAYS:
                forced_libre[name].add(DAYS.index(day_name))

    for name in names:
        role = by_name[name].get("role")
        for d in cfg.fixed_days_off.get(role, []):
            forced_libre[name].add(d)
        if role == "night_fixed":
            for d in cfg.night_fixed_days_off:
                forced_libre[name].add(d)

    for name in names:
        for bloque in _agrupar_en_tramos(sorted(forced_libre[name])):
            dias_con_libre = [d for d in bloque if (name, d, "Libre") in x]
            if not dias_con_libre:
                # Bloque entero sobre días fuera de presencia: no hay nada
                # que gatear — no se crea la asunción (crearla sin
                # restricciones daría un falso positivo en el núcleo).
                continue
            a = model.NewBoolVar(f"asum_libre_{name}_{bloque[0]}_{bloque[-1]}")
            for d in dias_con_libre:
                model.Add(x[name, d, "Libre"] == 1).OnlyEnforceIf(a)
            asunciones[a] = _describir_bloque_libre(name, bloque)

    # Presencia parcial (6b de _construir_modelo): Libre imposible en los
    # días de presencia no forzados — el diagnóstico reproduce el mismo
    # cierre estructural para no señalar como cedible algo que el contrato
    # "trabaja siempre" no permite.
    for name in names:
        dias_presencia = cfg.presencia_roles.get(by_name[name].get("role"))
        if dias_presencia is None:
            continue
        for d in dias_presencia:
            if d in forced_libre[name]:
                continue
            model.Add(x[name, d, "Libre"] == 0)

    # Turno fijo de rol (incluye night_fixed): política estructural, se
    # aplica siempre en los días que no forman parte de ningún bloque
    # gateado — aproximación deliberada: el diagnóstico solo necesita
    # distinguir "vacación/petición" de "cobertura mínima" como motivos
    # cedibles, no fragmentar además el turno fijo en función de qué
    # asunciones concretas terminen activas.
    for name in names:
        role = by_name[name].get("role")
        turno_fijo = cfg.fixed_roles.get(role)
        if turno_fijo is None:
            continue
        for d in range(7):
            if d in forced_libre[name]:
                continue
            if (name, d, turno_fijo) not in x:
                continue  # día fuera de presencia: no hay variables de turno
            model.Add(x[name, d, turno_fijo] == 1)

    for name in names:
        role = by_name[name].get("role")
        if role != "night_fixed":
            continue
        for d in range(7):
            if d in forced_libre[name]:
                continue
            model.Add(x[name, d, "Noche"] == 1)

    # Patrón de libres semanales (restricción #7 de _construir_modelo, ver
    # docstring del módulo): gateado por asunción, igual que la cobertura
    # mínima — es una pieza CEDIBLE (un encargado puede aceptar romper el
    # patrón de 2 seguidos para alguien concreto ante un conflicto real),
    # no una política estructural del departamento. Un único booleano por
    # persona gatea el patrón COMPLETO (par consecutivo si lo hay, conteo
    # exacto siempre) — el diagnóstico solo necesita saber SI el patrón de
    # esa persona formó parte del conflicto, no fragmentar además en "el
    # conteo" vs "la consecutividad".
    for name in names:
        role = by_name[name].get("role")
        if (role == "night_fixed" or cfg.fixed_days_off.get(role)
                or cfg.fixed_roles.get(role) or role in cfg.presencia_roles):
            continue

        dias_no_forzados = [d for d in range(7) if d not in forced_libre[name]]
        if len(dias_no_forzados) < DIAS_LIBRES_SEGUIDOS_POR_SEMANA:
            continue

        a = model.NewBoolVar(f"asum_patronlibres_{name}")
        no_forzados_set = set(dias_no_forzados)
        pares_disponibles = [
            (d, d + 1) for d in range(6)
            if d in no_forzados_set and d + 1 in no_forzados_set
        ]
        if pares_disponibles:
            indicadores_par = []
            for d1, d2 in pares_disponibles:
                ind = model.NewBoolVar(f"dasum_par_libre_{name}_{d1}_{d2}")
                model.Add(x[name, d1, "Libre"] == 1).OnlyEnforceIf(ind)
                model.Add(x[name, d2, "Libre"] == 1).OnlyEnforceIf(ind)
                indicadores_par.append(ind)
            model.Add(sum(indicadores_par) == 1).OnlyEnforceIf(a)
        model.Add(
            sum(x[name, d, "Libre"] for d in dias_no_forzados) == DIAS_LIBRES_SEGUIDOS_POR_SEMANA
        ).OnlyEnforceIf(a)
        asunciones[a] = f"Patrón de {DIAS_LIBRES_SEGUIDOS_POR_SEMANA} días libres semanales de {name}"

    return model, x, names, asunciones


TIEMPO_MAXIMO_DIAGNOSTICO_SEGUNDOS = 10.0  # límite propio del diagnóstico —
# ver nota extensa junto a _resolver_y_extraer (max_time_in_seconds=20.0)
# sobre por qué el modelo PRINCIPAL necesita un tope frente al timeout de
# 60s de gunicorn.conf.py. El diagnóstico corre DESPUÉS de que ese modelo
# ya haya agotado hasta sus 20s concluyendo INFEASIBLE — sin límite
# propio, medido empíricamente (Fase 5,): un caso complejo
# pero en realidad FACTIBLE del modelo reducido (60 personas, vacaciones
# solapadas al azar, sin infactibilidad real que encontrar) tardó ~90s en
# que CP-SAT terminara de decidir SAT/UNSAT — sumado a los hasta 20s del
# modelo principal, se comería el timeout de 60s de gunicorn sin devolver
# nada al encargado, ni siquiera el RuntimeError original. Un caso
# genuinamente infactible a la misma escala (60 personas), en cambio, se
# resolvió en ~0.07s — la lentitud no está en encontrar el conflicto real
# (SufficientAssumptionsForInfeasibility converge rápido una vez que
# Solve() ya confirmó INFEASIBLE), sino en el propio Solve() inicial
# cuando el modelo reducido resulta ser factible pero difícil de decidir.
# 10s dejan margen bajo el timeout de 60s incluso en el peor caso (20s
# modelo principal + 10s diagnóstico + margen para el resto de la
# petición HTTP), y son coherentes con el propósito del diagnóstico
# (una ayuda rápida, no una prueba exhaustiva): si no concluye en 10s, se
# informa explícitamente en vez de colgar la respuesta (ver más abajo).


def _reducir_a_nucleo_irreducible(model: cp_model.CpModel, nucleo_inicial: set,
                                   tiempo_limite: float) -> tuple:
    """SufficientAssumptionsForInfeasibility() devuelve un subconjunto
    SUFICIENTE para la infactibilidad (documentado así por ortools), no uno
    IRREDUCIBLE — puede contener motivos redundantes que, si se quitaran,
    el resto seguiría siendo infactible igual (o sea, no eran realmente
    necesarios para el conflicto). Esta función lo reduce con el algoritmo
    estándar "deletion-based" (uno de los métodos habituales para calcular
    un IIS — Irreducible Infeasible Subset): por cada asunción del núcleo,
    prueba a quitarla y researcar infactibilidad con las demás — si SIGUE
    infactible sin ella, sobraba (se descarta definitivamente); si se
    vuelve factible, era necesaria (se conserva).

    UNA SOLA PASADA sobre el núcleo inicial basta: es la garantía que da el
    filtro de borrado clásico. Cuando se procesa una asunción, se prueba
    contra el conjunto que queda tras haber descartado ya todas las
    anteriores, así que al terminar la pasada cada asunción conservada es
    necesaria para el conjunto FINAL, no solo para el de su momento — que
    es justo la definición de irreducible. Una versión anterior de esta
    función iteraba hasta punto fijo (`while cambiado`), lo que multiplica
    el número de resoluciones sin poder quitar ni una asunción más.
    Medido en un escenario de 36 asunciones (9 personas, 5 de vacaciones
    simultáneas): la pasada única tarda ~0.08s sobre los ~0.02s del
    diagnóstico sin reducción, así que el punto fijo tampoco llegaba a
    agotar TIEMPO_MAXIMO_DIAGNOSTICO_SEGUNDOS a esta escala — el motivo
    del cambio es que las vueltas extra son trabajo demostrablemente
    inútil, no un incendio de rendimiento. El presupuesto de tiempo sigue
    ahí para escalas mayores.

    Reutiliza el MISMO objeto `model` para cada resolución de prueba
    (ClearAssumptions + AddAssumptions con el subconjunto candidato en
    cada vuelta) en vez de reconstruirlo desde cero — las variables de
    asunción y sus OnlyEnforceIf ya están fijadas en el modelo; lo único
    que cambia entre pruebas es QUÉ subconjunto se le pasa como
    "asumido" a esta resolución en concreto.

    Devuelve (nucleo_final: set[BoolVar], verificado_completo: bool) —
    verificado_completo=False si el presupuesto de tiempo se agotó a
    mitad del barrido (el núcleo devuelto SIGUE siendo suficiente e
    infactible — nunca se pierde esa garantía — pero no se llegó a
    confirmar que ya no se puede reducir más, así que podría seguir
    conteniendo algún motivo redundante sin detectar)."""
    nucleo = set(nucleo_inicial)
    t0 = time.time()
    for var in sorted(nucleo_inicial, key=lambda v: v.Index()):
        # sorted() por Index: el orden de barrido determina cuál de dos
        # motivos intercambiables se conserva, así que iterar un set() daría
        # mensajes distintos entre ejecuciones (mismo motivo que el sorted()
        # de la restricción #2 — el hash-seed de Python no es fijo).
        restante = tiempo_limite - (time.time() - t0)
        if restante <= 0:
            return nucleo, False
        candidato = nucleo - {var}
        if not candidato:
            continue  # no tiene sentido researcar sobre el conjunto vacío
        solver_prueba = cp_model.CpSolver()
        solver_prueba.parameters.num_search_workers = 1
        solver_prueba.parameters.max_time_in_seconds = restante
        model.ClearAssumptions()
        model.AddAssumptions(list(candidato))
        status = solver_prueba.Solve(model)
        if status == cp_model.INFEASIBLE:
            nucleo = candidato  # var era redundante -- descartada
        elif status == cp_model.UNKNOWN:
            # No se pudo concluir a tiempo si var era o no redundante —
            # postura conservadora: se conserva (nunca se descarta algo
            # sin confirmar que de verdad sobraba) y se corta el barrido
            # aquí (el presupuesto ya se está agotando).
            return nucleo, False
        # FEASIBLE/OPTIMAL sin var -> var era necesaria, se queda en
        # nucleo tal cual, seguir probando con la siguiente.
    return nucleo, True


def _diagnosticar_infactibilidad(staff_list: list, cfg) -> str:
    """Reconstruye el modelo reducido con asunciones
    (_construir_modelo_diagnostico), usa
    solver.SufficientAssumptionsForInfeasibility() para encontrar un
    subconjunto SUFICIENTE de motivos "cedibles" (vacaciones/peticiones,
    cobertura mínima puntual) que, combinados, no tienen solución, y
    luego lo reduce a un núcleo IRREDUCIBLE de verdad con
    _reducir_a_nucleo_irreducible (ver esa función — SufficientAssumptions
    ForInfeasibility por sí sola NO garantiza minimalidad, solo
    suficiencia, así que sin este segundo paso el mensaje podía incluir
    motivos que en realidad no hacían falta para el conflicto real).

    Nunca lanza excepción por sí misma (solo diagnostica, ver nota de
    diseño arriba): si no logra reproducir la infactibilidad con las
    piezas que sabe gatear, si el conflicto resulta ser puramente
    estructural (no depende de ninguna asunción), o si se agota
    TIEMPO_MAXIMO_DIAGNOSTICO_SEGUNDOS sin llegar a una conclusión, lo
    dice explícitamente en vez de colgar la respuesta o inventar una
    explicación.
    """
    t0 = time.time()
    model, x, names, asunciones = _construir_modelo_diagnostico(staff_list, cfg)
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    solver.parameters.max_time_in_seconds = TIEMPO_MAXIMO_DIAGNOSTICO_SEGUNDOS
    lits = list(asunciones.keys())
    model.AddAssumptions(lits)
    status = solver.Solve(model)

    if status == cp_model.UNKNOWN:
        return (
            "El horario sigue siendo INFACTIBLE (ver status arriba); el "
            "diagnóstico detallado de qué restricciones concretas chocan "
            f"no estuvo disponible a tiempo (límite propio de "
            f"{TIEMPO_MAXIMO_DIAGNOSTICO_SEGUNDOS:.0f}s agotado sin que el "
            "modelo reducido de diagnóstico terminara de decidirse — no es "
            "una segunda comprobación de si hay problema, solo de POR QUÉ). "
            "Revisar manualmente."
        )

    if status != cp_model.INFEASIBLE:
        return (
            "Diagnóstico automático: no se pudo reproducir la infactibilidad "
            "usando solo vacaciones/peticiones y cobertura mínima puntual — "
            "el conflicto real depende de alguna restricción estructural "
            "(cobertura máxima de Noche/Partido, roles sin Noche, turno "
            "fijo de rol, no-Mañana-tras-Tarde/Noche) no cubierta por este diagnóstico "
            "automático. Revisar manualmente."
        )

    indices_core = set(solver.SufficientAssumptionsForInfeasibility())
    nucleo_suficiente = {var for var in asunciones if var.Index() in indices_core}

    tiempo_restante = TIEMPO_MAXIMO_DIAGNOSTICO_SEGUNDOS - (time.time() - t0)
    if tiempo_restante > 0 and nucleo_suficiente:
        nucleo_final, verificado = _reducir_a_nucleo_irreducible(
            model, nucleo_suficiente, tiempo_restante
        )
    else:
        nucleo_final, verificado = nucleo_suficiente, False

    razones = sorted({asunciones[var] for var in nucleo_final})

    if not razones:
        return (
            "Diagnóstico automático: la infactibilidad no depende de ninguna "
            "vacación/petición ni de ningún mínimo de cobertura puntual — es "
            "puramente estructural (roles fijos, turno fijo u otra "
            "restricción de fase 1 no cedible). Revisar manualmente."
        )

    mensaje = (
        ("Diagnóstico automático — combinación mínima irreducible en conflicto: "
         if verificado else
         "Diagnóstico automático — combinación suficiente en conflicto "
         "(no se confirmó a tiempo que ya no se pudiera reducir más, "
         "puede incluir algún motivo redundante): ")
        + "; ".join(razones) + "."
    )
    return mensaje


def _mensaje_infactible(solver: cp_model.CpSolver, status, staff_list: list = None, cfg=None) -> str:
    """Mensaje de RuntimeError para status INFEASIBLE/etc. — compartido por
    _resolver_y_extraer y _resolver_y_extraer_con_extra. Si se dispone de
    staff_list/cfg (los callers de producción siempre los pasan; las
    llamadas directas de tests con solo model/x/names, sin ellos, no),
    añade el diagnóstico automático (Tema 3, ver más arriba). El
    diagnóstico nunca lanza: si falla por cualquier motivo, se anota el
    fallo en vez de perder el RuntimeError original."""
    mensaje = (
        "CP-SAT no encontró solución factible para las restricciones dadas "
        f"(status={solver.StatusName(status)})."
    )
    if staff_list is not None and cfg is not None:
        try:
            mensaje += " " + _diagnosticar_infactibilidad(staff_list, cfg)
        except Exception as exc:  # el diagnóstico es solo informativo
            mensaje += f" (el diagnóstico automático falló: {exc!r})"
    return mensaje


MAX_TIME_SOLVER = 20.0  # red de seguridad (ver razonamiento completo más abajo)
GRACIA_SIN_MEJORA = 3.0  # NUEVO (investigación de rendimiento):
# tras encontrar al menos una solución, si pasan GRACIA_SIN_MEJORA(3)
# segundos sin que aparezca una mejor, se detiene la búsqueda ahí en vez de
# agotar MAX_TIME_SOLVER. Verificado en 3 escenarios reales
# (plantilla_holgada de cobertura, Recepción real 2026-08-10, mozos en modo
# respaldo permanente): en los 3, la solución encontrada dentro de los
# primeros 3-7s ya era EXACTAMENTE la misma (mismo ObjectiveValue()) que la
# resolución completa de 20s — los ~13-17s restantes se gastaban sin éxito
# tratando de PROBAR optimalidad (mejorando el bound, nunca el objetivo),
# no en encontrar nada mejor. Esto no es una relajación de la garantía ya
# documentada (216 comparaciones, ver docstring de _resolver_y_extraer): esa
# garantía es sobre el resultado (10-20s trunca igual que 90s), no sobre
# CUÁNDO dentro de la ventana aparece ese resultado — aquí solo se deja de
# esperar en vano una vez que la mejora se estancó.


class _RastreadorDeMejora(cp_model.CpSolverSolutionCallback):
    """Registra, con marca de tiempo, cada solución mejorada que CP-SAT
    encuentra — usado por _resolver_con_parada_por_meseta para saber, desde
    OTRO hilo (ver esa función), cuánto tiempo lleva sin mejorar el
    objetivo. CP-SAT solo invoca on_solution_callback cuando aparece una
    NUEVA solución (nunca periódicamente), así que un simple contador de
    "tiempo transcurrido" no puede vivir dentro del callback — de ahí que
    la detección de meseta viva en el hilo que llama a Solve, sondeando
    este estado compartido (protegido por lock, escrito desde el hilo de
    resolución y leído desde el principal)."""

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()
        self.ultima_mejora = time.time()
        self.mejor_valor = None

    def on_solution_callback(self):
        valor = self.ObjectiveValue()
        ahora = time.time()
        with self._lock:
            if self.mejor_valor is None or valor < self.mejor_valor - 1e-9:
                self.mejor_valor = valor
                self.ultima_mejora = ahora

    def segundos_sin_mejora(self) -> float:
        with self._lock:
            tiene_solucion = self.mejor_valor is not None
            transcurrido = time.time() - self.ultima_mejora
        return transcurrido if tiene_solucion else -1.0


def _resolver_con_parada_por_meseta(solver: cp_model.CpSolver, model: cp_model.CpModel,
                                     gracia: float = GRACIA_SIN_MEJORA):
    """Ejecuta solver.Solve(model) en un hilo aparte y lo detiene (StopSearch,
    documentada como segura de llamar desde otro hilo) en cuanto pasan
    `gracia` segundos sin que aparezca una solución mejor — sin tocar
    solver.parameters.max_time_in_seconds, que sigue actuando como red de
    seguridad si la meseta nunca llega (p.ej. un escenario genuinamente sin
    ninguna solución factible durante mucho tiempo). Devuelve el mismo
    status que solver.Solve(model) habría devuelto."""
    callback = _RastreadorDeMejora()
    resultado = {}

    def _resolver():
        resultado["status"] = solver.Solve(model, callback)

    hilo = threading.Thread(target=_resolver)
    hilo.start()
    while hilo.is_alive():
        time.sleep(0.05)
        if callback.segundos_sin_mejora() > gracia:
            solver.StopSearch()
            break
    hilo.join()
    return resultado["status"]


def _resolver_y_extraer(model: cp_model.CpModel, x: dict, names: list,
                         staff_list: list = None, cfg=None) -> dict:
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = MAX_TIME_SOLVER  # 90.0 -> 20.0 (Fase 5,
    # decisión final tras el rediseño B>I>D): con PESO_ROTURAS_CONTINUIDAD=50
    # y el término I, la búsqueda mono-hilo (num_search_workers=1, fijado por
    # determinismo) puede tardar hasta 90s en PROBAR optimalidad a escala
    # real de producción — pero gunicorn.conf.py fija timeout=60 en el
    # despliegue real, así que un límite de 90s aquí es inútil en la
    # práctica: el proceso worker muere primero, sin devolver nada.
    # Verificado con 216 comparaciones (18 semanas x Recepción/Mozos x topes
    # de 10s/20s x 3 repeticiones cada una, escala real 9/5 personas): la
    # solución truncada a 10-20s tuvo SIEMPRE (216/216, sin excepción) el
    # mismo ObjectiveValue() que la solución óptima confirmada con hasta
    # 90s — el tiempo extra se gastaba enteramente en demostrar que no
    # existía nada mejor (status OPTIMAL), nunca en encontrar algo mejor.
    # 20s deja margen bajo el timeout de 60s de gunicorn (encadenar varias
    # llamadas al solver dentro de una misma petición HTTP, p.ej. modo
    # respaldo con 7 llamadas/semana, debe caber cómodamente por debajo de
    # 60s). status FEASIBLE se acepta como resultado válido más abajo
    # (ver "if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE)" más abajo) —
    # ya lo era antes de este cambio, no hizo falta tocar esa condición.
    # num_search_workers=1 (Fase 5, revisión de la comparativa): el valor
    # por defecto (0 = portafolio paralelo) hace que, ante empates en el
    # valor óptimo, el desempate dependa de qué hilo termina antes —
    # verificado con el mismo input exacto produciendo schedules distintos
    # entre corridas. El horario generado debe ser reproducible ante una
    # regeneración (motivo de negocio original de la migración: el
    # heurístico con random.seed no fijo ya tenía este problema y era
    # parte de lo que se quería mejorar, no repetir). El coste en tiempo
    # es despreciable a la escala real de hoy (5-9 personas/depto) y solo
    # se vuelve notable (varios segundos) a partir de ~20-25 personas —
    # ver docs/INFORME_FASE_5.md para las mediciones completas.
    solver.parameters.num_search_workers = 1
    status = _resolver_con_parada_por_meseta(solver, model)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise RuntimeError(_mensaje_infactible(solver, status, staff_list, cfg))

    schedule = {name: ["" for _ in range(7)] for name in names}
    for name in names:
        for d in range(7):
            for s in SHIFTS + [FUERA]:
                if (name, d, s) in x and solver.Value(x[name, d, s]) == 1:
                    schedule[name][d] = s
                    break

    return schedule


def generar_semana_cpsat(staff_list: list, week_start: date, cfg,
                          prev_sunday_shifts: dict = None) -> dict:
    model, x, names, by_name = _construir_modelo(staff_list, week_start, cfg, prev_sunday_shifts)
    return _resolver_y_extraer(model, x, names, staff_list, cfg)


# ═══════════════════════════════════════════════════════════════════════════
# Racha de noches — ventana deslizante
# ═══════════════════════════════════════════════════════════════════════════

MAX_NOCHES_VENTANA = 5   # máximo de noches en cualquier ventana de...
VENTANA_NOCHES = 6       # ...esta cantidad de días consecutivos.
DIAS_DESCANSO_TRAS_RACHA = 2
MIN_NOCHES_RACHA = 2  # NUEVO (regla de rotación de cobertura
# nocturna): ninguna racha de Noche puede ser más corta que esto — nadie
# cubre una noche suelta aislada. Se combina con MAX_NOCHES_VENTANA(5) para
# que toda racha de Noche caiga en [MIN_NOCHES_RACHA, MAX_NOCHES_VENTANA].
# Aplica genéricamente a CUALQUIER persona con Noche=1 (igual que el resto
# de esta restricción), no solo a quien rota cobertura nocturna.


def _racha_inicial_por_nombre(names: list, noches_ultimos_5_dias: dict) -> dict:
    """Racha de noches consecutivas que cada persona trae de antes de esta
    semana, mirando noches_ultimos_5_dias (mismo formato que recibe
    agregar_restriccion_racha_noches) desde el día más cercano al lunes
    (índice 4) hacia atrás, hasta el primer False.

    Usada por el bucle de modo respaldo de
    generar_semana_cpsat_con_cobertura_nocturna para saber, ANTES de fijar
    ninguna cobertura de esta semana, con cuántas noches seguidas entra ya
    cada candidato — sin esto, una persona que entra con p.ej. 4/5 ya
    encadenadas podría recibir una 5ª noche el lunes y quedar sin que el
    bucle sepa que le toca descanso el martes/miércoles."""
    racha = {}
    for name in names:
        empalme = list(noches_ultimos_5_dias.get(name, [False] * 5))
        streak = 0
        for v in reversed(empalme):
            if v:
                streak += 1
            else:
                break
        racha[name] = streak
    return racha


class _RachaTracker:
    """Seguimiento determinista, en Python, de la racha de Noche y el
    descanso obligatorio post-racha — usado por el bucle de modo respaldo
    de generar_semana_cpsat_con_cobertura_nocturna (Tema 1, Fase 5, cierre
    de pendientes) para saber, ANTES de fijar cada día, a quién NO debe
    proponer porque ya le toca descanso.

    Extraída como clase independiente (antes vivía inline dentro del
    bucle) por dos motivos: (1) evitar duplicar la aritmética si en el
    futuro hace falta en más de un sitio, y (2) — el motivo de esta
    sesión — poder REPRODUCIRLA de forma aislada en tests y compararla,
    día a día, contra lo que agregar_restriccion_racha_noches calcula de
    verdad DENTRO del modelo CP-SAT para las mismas personas/semanas (ver
    tests/test_racha_respaldo_multisemana.py). Si esta clase alguna vez
    diverge de esa restricción — por ejemplo un día de diferencia en
    cómo cuenta cada una — ese test debe detectarlo directamente, no solo
    por la ausencia de un INFEASIBLE espurio (que solo detecta el caso de
    sub-predicción, no el de sobre-predicción ni un desfase que la
    holgura de personal disimule).

    La aritmética es EXACTAMENTE la misma que agregar_restriccion_racha_
    noches aplica dentro del modelo (MAX_NOCHES_VENTANA noches seguidas ->
    DIAS_DESCANSO_TRAS_RACHA de descanso obligatorio), solo que evaluada
    día a día en Python, antes de resolver, sobre la persona que el propio
    bucle de modo respaldo va fijando como cobertura externa — no sobre
    variables del solver.
    """

    def __init__(self, names: list, noches_ultimos_5_dias: dict,
                 descanso_pendiente_inicial: dict = None):
        self.names = list(names)
        self.racha_actual = _racha_inicial_por_nombre(self.names, noches_ultimos_5_dias)
        self.descanso_pendiente = {n: 0 for n in self.names}

        # descanso_pendiente_inicial (CORRECCIÓN: expuesta por
        # los bloques de 4-5 noches, mucho más propensos que la selección
        # día a día anterior a que una racha de 5 termine cerca del límite
        # de semana): días de descanso YA pendientes al arrancar esta
        # semana, según el propio modelo CP-SAT (postweek1/postweek2 de
        # agregar_restriccion_racha_noches — ver generar_semana_cpsat_con_
        # cobertura_nocturna, que los persiste en estado_rotacion tras cada
        # semana). Es la fuente AUTORITATIVA — el empalme de 5 días por sí
        # solo NO basta para reconstruir esto en todos los casos: si la
        # racha de 5 terminó a mitad del empalme (p.ej. Mar-Sáb de la
        # semana pasada, con 1 día de descanso —Domingo— ya tomado DENTRO
        # de esa semana y 1 más pendiente hoy Lunes), el empalme (Mié-Dom)
        # nunca ve el Martes en que empezó la racha, así que la
        # reconstrucción por conteo de _racha_inicial_por_nombre no puede
        # distinguir ese caso de "sin racha en absoluto" — ambos dan
        # streak=0 al llegar a un Domingo en False. Con descanso_pendiente_
        # inicial no hace falta reconstruir nada: el dato ya viene resuelto.
        for n, restantes in (descanso_pendiente_inicial or {}).items():
            if restantes > 0:
                self.descanso_pendiente[n] = restantes
                self.racha_actual[n] = 0

    def bloqueados(self) -> set:
        """Nombres que, a fecha de HOY (antes de decidir quién cubre hoy),
        siguen en su descanso obligatorio post-racha pendiente de días
        anteriores."""
        return {n for n, restantes in self.descanso_pendiente.items() if restantes > 0}

    def registrar(self, elegido: str) -> None:
        """El día de hoy ya se decidió: `elegido` cubrió Noche. Consume un
        día de descanso pendiente para quien lo tuviera (aunque no sea
        `elegido` — el descanso transcurre igual, se haya cubierto o no la
        noche con otra persona); avanza la racha de `elegido` y la rompe
        para el resto (solo una cobertura externa por día en modo
        respaldo); si la racha de `elegido` alcanza el máximo, activa su
        descanso obligatorio."""
        for n in self.names:
            if self.descanso_pendiente.get(n, 0) > 0:
                self.descanso_pendiente[n] -= 1

        self.racha_actual[elegido] = self.racha_actual.get(elegido, 0) + 1
        for n in self.names:
            if n != elegido:
                self.racha_actual[n] = 0
        if self.racha_actual[elegido] >= MAX_NOCHES_VENTANA:
            self.descanso_pendiente[elegido] = DIAS_DESCANSO_TRAS_RACHA
            self.racha_actual[elegido] = 0


def _resolver_y_extraer_con_extra(model: cp_model.CpModel, x: dict, names: list,
                                   extra_vars: dict,
                                   staff_list: list = None, cfg=None) -> tuple:
    """Igual que _resolver_y_extraer, pero además lee el valor resuelto de
    variables auxiliares del modelo (p.ej. las de racha_postweek). Función
    NUEVA y separada — _resolver_y_extraer no se toca, para no romper el
    tipo de retorno que ya usan las fases 1/2 y sus tests.

    extra_vars: {clave: BoolVar}. Devuelve (schedule, {clave: 0|1}).
    """
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = MAX_TIME_SOLVER  # 90.0 -> 20.0 (Fase 5,
    # antes era el literal 20.0 duplicado aquí: el mismo valor
    # que MAX_TIME_SOLVER pero sin referenciarlo, así que subir la constante
    # no habría afectado a esta función.
    # decisión final tras el rediseño B>I>D): con PESO_ROTURAS_CONTINUIDAD=50
    # y el término I, la búsqueda mono-hilo (num_search_workers=1, fijado por
    # determinismo) puede tardar hasta 90s en PROBAR optimalidad a escala
    # real de producción — pero gunicorn.conf.py fija timeout=60 en el
    # despliegue real, así que un límite de 90s aquí es inútil en la
    # práctica: el proceso worker muere primero, sin devolver nada.
    # Verificado con 216 comparaciones (18 semanas x Recepción/Mozos x topes
    # de 10s/20s x 3 repeticiones cada una, escala real 9/5 personas): la
    # solución truncada a 10-20s tuvo SIEMPRE (216/216, sin excepción) el
    # mismo ObjectiveValue() que la solución óptima confirmada con hasta
    # 90s — el tiempo extra se gastaba enteramente en demostrar que no
    # existía nada mejor (status OPTIMAL), nunca en encontrar algo mejor.
    # 20s deja margen bajo el timeout de 60s de gunicorn (encadenar varias
    # llamadas al solver dentro de una misma petición HTTP, p.ej. modo
    # respaldo con 7 llamadas/semana, debe caber cómodamente por debajo de
    # 60s). status FEASIBLE se acepta como resultado válido más abajo
    # (ver "if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE)" más abajo) —
    # ya lo era antes de este cambio, no hizo falta tocar esa condición.
    # num_search_workers=1 (Fase 5, revisión de la comparativa): el valor
    # por defecto (0 = portafolio paralelo) hace que, ante empates en el
    # valor óptimo, el desempate dependa de qué hilo termina antes —
    # verificado con el mismo input exacto produciendo schedules distintos
    # entre corridas. El horario generado debe ser reproducible ante una
    # regeneración (motivo de negocio original de la migración: el
    # heurístico con random.seed no fijo ya tenía este problema y era
    # parte de lo que se quería mejorar, no repetir). El coste en tiempo
    # es despreciable a la escala real de hoy (5-9 personas/depto) y solo
    # se vuelve notable (varios segundos) a partir de ~20-25 personas —
    # ver docs/INFORME_FASE_5.md para las mediciones completas.
    solver.parameters.num_search_workers = 1
    status = _resolver_con_parada_por_meseta(solver, model)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise RuntimeError(_mensaje_infactible(solver, status, staff_list, cfg))

    schedule = {name: ["" for _ in range(7)] for name in names}
    for name in names:
        for d in range(7):
            for s in SHIFTS + [FUERA]:
                if (name, d, s) in x and solver.Value(x[name, d, s]) == 1:
                    schedule[name][d] = s
                    break

    valores_extra = {k: solver.Value(v) for k, v in extra_vars.items()}
    return schedule, valores_extra


def agregar_restriccion_racha_noches(model: cp_model.CpModel, x: dict, names: list,
                                      noches_ultimos_5_dias: dict,
                                      personas_bloque_continua: set = None) -> dict:
    """Añade, sobre un modelo ya construido por _construir_modelo (llamar
    ANTES de resolver — CP-SAT permite seguir añadiendo restricciones tras
    fijar el objetivo con model.Minimize), las restricciones duras de racha
    de Noche, para TODAS las personas en `names` genéricamente (no hace
    falta distinguir night_fixed de su sustituto: la restricción opera
    sobre x[name, d, "Noche"] sea quien sea):

      1. Máximo MAX_NOCHES_VENTANA(5) noches en cualquier ventana de
         VENTANA_NOCHES(6) días consecutivos.
      2. DIAS_DESCANSO_TRAS_RACHA(2) días de Libre obligatorio inmediatamente
         tras una racha de exactamente 5 noches consecutivas.
      3. MIN_NOCHES_RACHA(2) — NUEVO, regla de rotación de
         cobertura nocturna: ninguna racha de Noche puede ser más corta que
         esto (nadie cubre una noche suelta aislada). Se implementa
         prohibiendo que cualquier día de Noche quede sin NINGÚN vecino
         (anterior o siguiente) también en Noche — técnica estándar para
         forbid un "1 aislado" en una secuencia binaria, sin necesitar
         variables de racha adicionales. Para el día 0 el "vecino anterior"
         usa el empalme (índice 4, constante); para el día 6 (Domingo) NO
         hay forma de mirar "el lunes que viene" (no está modelado en esta
         llamada) — se aplica la regla conservadora de prohibir que
         Domingo sea un arranque nuevo aislado (Sábado=0 y Domingo=1),
         salvo para quien el llamador YA SABE que continúa su bloque la
         semana que viene (`personas_bloque_continua` — ver
         cobertura_nocturna.py / generar_semana_cpsat_con_cobertura_
         nocturna, mecanismo de bloques de modo respaldo). Si en el futuro
         un escenario sin ese conocimiento previo necesitara genuinamente
         un arranque aislado el domingo, esto lo reportaría como
         INFEASIBLE — correcto (refleja el conflicto real), no un bug.

    noches_ultimos_5_dias: {nombre: [bool, bool, bool, bool, bool]} — empalme
    con la semana anterior. índice 0 = 5 días antes del lunes de esta semana
    (miércoles de la semana pasada), índice 4 = domingo de la semana pasada
    (el día inmediatamente anterior al lunes nuevo). Quien no aparezca en el
    dict se asume sin noches en los últimos 5 días ([False]*5) — cubre el
    caso de un empleado nuevo sin histórico de racha.

    personas_bloque_continua: nombres exentos de la regla conservadora de
    Domingo (punto 3 arriba) porque el llamador ya garantiza externamente
    que su bloque continúa el Lunes de la semana siguiente. {} por defecto.

    Modelo de índices interno (por persona), secuencia de 14 posiciones:
      0..4   → empalme (constante 0/1, ya conocido, no es variable)
      5..11  → x[name, d, "Noche"] para d=0..6 (esta semana, variable)
      12,13  → "racha_postweek" (variables auxiliares SIN turno real —
               representan "¿la semana SIGUIENTE debe forzar Libre el
               Lunes/Martes por una racha que termina muy cerca del fin
               de esta semana y no deja margen dentro de ella?")

    Devuelve {nombre: (postweek_lunes_var, postweek_martes_var)}. El
    llamador debe leer su valor tras resolver (con
    _resolver_y_extraer_con_extra) y, si están en 1, trasladarlos como
    forced_days_off a la generación de la semana SIGUIENTE — exactamente
    el mismo patrón que next_week_rest_days ya usa en modelo_base.py para
    casos análogos (compensación de cobertura nocturna que cruza el fin
    de semana). Sin esto, una racha de 5 que termina en sábado o domingo
    de esta semana no podría garantizar sus 2 días de descanso completos,
    porque parte de ellos cae fuera de los 7 días que este modelo conoce.
    """
    postweek_vars = {}
    personas_bloque_continua = personas_bloque_continua or set()

    for name in names:
        empalme = list(noches_ultimos_5_dias.get(name, [False] * 5))
        if len(empalme) != 5:
            raise ValueError(
                f"noches_ultimos_5_dias[{name!r}] debe tener exactamente 5 valores, "
                f"tiene {len(empalme)}"
            )

        post1 = model.NewBoolVar(f"racha_postweek1_{name}")
        post2 = model.NewBoolVar(f"racha_postweek2_{name}")
        postweek_vars[name] = (post1, post2)

        secuencia = [int(bool(v)) for v in empalme]        # índices 0..4
        secuencia += [_v(x, name, d, "Noche") for d in range(7)]  # índices 5..11
        secuencia += [post1, post2]                          # índices 12,13

        # 1. Ventana deslizante: máximo 5 en cualquier tramo de 6 consecutivos
        # dentro de empalme+semana (índices 0..11 — post1/post2 no son
        # turnos "Noche" reales, quedan fuera de esta restricción).
        for g in range(0, (5 + 7) - VENTANA_NOCHES + 1):  # g = 0..6
            ventana = secuencia[g:g + VENTANA_NOCHES]
            model.Add(sum(ventana) <= MAX_NOCHES_VENTANA)

        # 2. Descanso obligatorio tras racha de exactamente 5. g cubre las
        # 8 posiciones de inicio posibles de un tramo de 5 dentro de
        # empalme+semana (0..7); los 2 índices de descanso (g+5, g+6)
        # siempre caen dentro de 5..13 (esta semana o postweek).
        activadores_post1 = []
        activadores_post2 = []
        for g in range(0, (5 + 7) - MAX_NOCHES_VENTANA + 1):  # g = 0..7
            tramo = secuencia[g:g + MAX_NOCHES_VENTANA]
            racha5 = model.NewBoolVar(f"racha5_{name}_{g}")
            # Reificación completa (no solo unidireccional): racha5 <-> los
            # 5 días del tramo son todos Noche. Necesaria para que el AND
            # quede determinado con precisión (ver más abajo por qué
            # post1/post2 sí necesitan esto, a diferencia del Libre forzado
            # dentro de la semana).
            model.Add(sum(tramo) >= MAX_NOCHES_VENTANA).OnlyEnforceIf(racha5)
            model.Add(sum(tramo) <= MAX_NOCHES_VENTANA - 1).OnlyEnforceIf(racha5.Not())

            for offset in (VENTANA_NOCHES - 1, VENTANA_NOCHES):  # 5, 6
                idx_descanso = g + offset
                if idx_descanso <= 11:
                    d = idx_descanso - 5
                    # Aquí SÍ basta con la implicación unidireccional: Libre
                    # es un turno real ya acotado por AddExactlyOne y el
                    # resto de restricciones de esa persona ese día — no hay
                    # riesgo de "falso positivo" como con post1/post2 (que
                    # son variables puramente informativas sin ninguna otra
                    # restricción que las ate, y necesitan la igualdad
                    # exacta para no quedar en 1 sin motivo real).
                    model.Add(
                        _v(x, name, d, "Libre") + _v(x, name, d, FUERA) == 1
                    ).OnlyEnforceIf(racha5)
                elif idx_descanso == 12:
                    activadores_post1.append(racha5)
                elif idx_descanso == 13:
                    activadores_post2.append(racha5)

        # post1/post2 == OR(activadores) exactamente — no solo "==1 si algún
        # activador es cierto", también "==0 si ninguno lo es". Sin esta
        # igualdad exacta, al no aparecer en la función objetivo, el solver
        # sería libre de dejarlas en 1 sin que hiciera falta, y el llamador
        # forzaría de más un día libre la semana siguiente sin motivo real.
        if activadores_post1:
            model.AddMaxEquality(post1, activadores_post1)
        else:
            model.Add(post1 == 0)
        if activadores_post2:
            model.AddMaxEquality(post2, activadores_post2)
        else:
            model.Add(post2 == 0)

        # 3. Mínimo de MIN_NOCHES_RACHA(2) noches seguidas (NUEVO, esta
        # sesión — ver docstring arriba para el razonamiento completo).
        # Días interiores (0..5 → índices 5..10): ningún "1 aislado", el
        # empalme (índice 4) cubre el vecino anterior del día 0 sin caso
        # especial.
        for d in range(0, 6):
            idx = d + 5
            if (name, d, "Noche") not in x:
                continue  # día fuera de presencia: nada que encadenar
            model.Add(secuencia[idx - 1] + secuencia[idx + 1] >= 1).OnlyEnforceIf(x[name, d, "Noche"])

        # Día 6 (Domingo, índice 11): regla conservadora — no arrancar un
        # bloque nuevo aislado ese día, salvo continuidad ya garantizada
        # externamente.
        if name not in personas_bloque_continua:
            model.Add(_v(x, name, 6, "Noche") <= _v(x, name, 5, "Noche"))

    return postweek_vars


def generar_semana_cpsat_con_cobertura_nocturna(
    staff_list: list, week_start: date, cfg,
    estado_rotacion: dict, noches_ultimos_5_dias: dict,
    prev_sunday_shifts: dict = None,
    dias_trabajados_previos: dict = None,
) -> dict:
    """Orquesta una semana completa conectando cobertura_nocturna.py (QUIÉN
    cubre — dato de entrada, no lo decide el solver) con las restricciones
    de racha de Noche de (CUÁNDO/cuánto puede encadenarse). No
    reimplementa nada de fase 1/fase 2/revisión anterior — solo las combina.

    Modo normal (night_fixed presente o ausente <2 semanas consecutivas):
    llama a seleccionar_candidato_cobertura_nocturna UNA vez, y esa misma
    persona cubre AMBOS días de cfg.night_fixed_days_off (igual que
    select_night_covers hoy en producción).

    Modo respaldo (>=2 semanas consecutivas ausente, o rol night_fixed
    inexistente en staff_list): cubre por BLOQUES de 4-5 noches SEGUIDAS
    por persona (regla de rotación de cobertura
    nocturna; antes se llamaba a seleccionar_candidato_cobertura_nocturna
    una vez por cada uno de los 7 días sin ninguna garantía de bloque). El
    bloque en curso se retoma de la semana anterior (estado_rotacion
    ["bloque_actual"]) si lo había — casi nunca cabe entero en 7 días, así
    que cruza el límite de semana con normalidad — y solo se llama a
    seleccionar_candidato_cobertura_nocturna para arrancar un bloque NUEVO
    (al terminar el anterior, o si se interrumpe porque su titular pasa a
    estar bloqueado por una vacación/petición a mitad de bloque — se corta
    ahí, con lo ya cubierto, y arranca uno nuevo con otra persona).

    Devuelve un dict:
      "schedule": {nombre: [turno]*7}
      "estado_rotacion": actualizado, para pasar a la llamada de la semana
        siguiente.
      "noches_ultimos_5_dias": empalme para la semana siguiente (derivado
        de los últimos 5 días de la semana recién generada: miércoles a
        domingo de ESTA semana).
      "forced_days_off_extra": {nombre: [nombres de día, p.ej. "Lunes"]} —
        días libres forzados para la semana SIGUIENTE, de dos fuentes que
        se fusionan aquí:
          1. Descansos de racha que no cupieron dentro de esta semana
             (racha que termina en sábado o domingo).
          2. Compensación Lun+Mar del sustituto de cobertura nocturna en
             modo normal (cfg.prefer_next_week_for_cover) — incondicional,
             igual que next_week_rest_days en modelo_base.py.
        El llamador debe fusionar esto con el forced_days_off propio de
        cada persona (vacaciones/peticiones reales) en el staff_list de
        la semana SIGUIENTE, antes de volver a llamar a esta función.
    """
    from cobertura_nocturna import (
        avanzar_semana_ausencia_night_fixed,
        seleccionar_candidato_cobertura_nocturna,
        modo_respaldo_activo,
        persona_bloqueada_dia,
        longitud_bloque_respaldo,
        registrar_noche_bloque,
        reconciliar_orden,
    )

    estado_rotacion = avanzar_semana_ausencia_night_fixed(staff_list, estado_rotacion)
    # Sincroniza la lista 'orden' del ciclo fijo con quién puede cubrir noches
    # esta semana (altas al final, bajas fuera). Necesario desde que el orden
    # fijo se usa también en modo respaldo: _elegir_orden_fijo solo mira dentro
    # de 'orden', así que sin esto un empleado nuevo nunca haría noches.
    estado_rotacion = reconciliar_orden(estado_rotacion, staff_list, cfg)

    cobertura_externa = {}  # day_idx -> nombre
    # nombre_sustituto_unico: solo tiene sentido en modo normal (una misma
    # persona cubre AMBOS días de cfg.night_fixed_days_off) — el término F
    # (domingo suelto) asume ese caso clásico de sustituto único (mozos:
    # cubre Vie+Sáb). En modo respaldo no hay "el sustituto de esta semana"
    # (pueden ser hasta 7 personas distintas, una por noche), así que el
    # término F simplemente no se activa ahí.
    nombre_sustituto_unico = None
    # personas_bloque_continua (regla de rotación de
    # cobertura nocturna en bloques): quien sigue a mitad de un bloque de
    # modo respaldo al terminar esta semana queda exento, en la restricción
    # de racha (más abajo), de la regla conservadora de Domingo — su
    # continuidad hacia el Lunes que viene ya está garantizada por
    # construcción (cobertura_externa de la semana siguiente arrancará
    # retomando el mismo bloque, ver bloque_actual).
    personas_bloque_continua = set()
    if modo_respaldo_activo(staff_list, estado_rotacion):
        # Seguimiento de racha en modo respaldo (Fase 5, verificación del
        # mismo tipo de bug ya corregido para dias_cubrir, esta vez para el
        # descanso obligatorio post-racha de 5 noches, restricción de Fase
        # 3 en agregar_restriccion_racha_noches): las 7 llamadas de este
        # bucle fijan cobertura_externa[d] ANTES de que esa restricción se
        # añada al modelo (más abajo). Si la selección de candidato no sabe
        # nada de la racha que ella misma va encadenando día a día dentro
        # de esta misma semana, puede volver a proponer a la misma persona
        # una 6ª noche seguida, o proponerla para un día que ya le tocaría
        # de descanso obligatorio tras su 5ª — produciendo el mismo tipo de
        # INFEASIBLE espurio que dias_cubrir corrigió para vacaciones/
        # peticiones, pero ahora por chocar con la racha en vez de con una
        # ausencia real. Se replica en Python, día a día, exactamente la
        # misma lógica de "racha de 5 → 2 de descanso obligatorio" que
        # agregar_restriccion_racha_noches aplicará luego dentro del
        # modelo — determinista, sin variables del solver, igual que
        # _tiene_par_consecutivo_disponible hace para el término F.
        names_racha = [p["name"] for p in staff_list]
        tracker = _RachaTracker(
            names_racha, noches_ultimos_5_dias,
            descanso_pendiente_inicial=estado_rotacion.get("descanso_pendiente_inicial", {}),
        )

        # bloque (regla de rotación de cobertura
        # nocturna en bloques): retoma el bloque en curso de la semana
        # anterior (si lo había) en vez de decidir "quién" cada día desde
        # cero — un bloque de 4-5 noches casi nunca cabe entero en 7 días,
        # así que cruza el límite de semana con normalidad (ver
        # cobertura_nocturna.py, formato de estado_rotacion).
        bloque = dict(estado_rotacion.get("bloque_actual") or {
            "nombre": None, "noches_totales": 0, "noches_cubiertas": 0,
        })

        # Un bloque solo CONTINÚA si de verdad venía encadenado: esa persona
        # tuvo Noche el último día de la semana anterior (noches_ultimos_5_
        # dias[nombre][-1] = domingo pasado). Si no, el bloque quedó OBSOLETO
        # y hay que descartarlo (CORRECCIÓN).
        #
        # Bug preexistente que destapó el paso a orden fijo en modo respaldo:
        # bloque_actual solo lo mantiene la rama de modo RESPALDO, así que
        # unas semanas de modo NORMAL en medio (night_fixed presente otra vez)
        # lo dejan congelado. Traza real del escenario de alternancia:
        #   sem 5 (respaldo) N6 cubre 4 de 5 -> bloque_actual={N6,5,4}
        #   sem 6-7 (normal) N6 no hace ninguna noche, bloque_actual intacto
        #   sem 8-9 (respaldo) se reanuda el bloque de N6 como si siguiera
        # Reanudarlo clava una noche SUELTA (el lunes) a alguien cuya última
        # noche fue tres semanas antes: sin Noche el domingo anterior y con el
        # martes ya ocupado por otro bloque, la regla de MIN_NOCHES_RACHA(2)
        # es imposible de cumplir y la semana entera sale INFEASIBLE.
        if bloque["nombre"] is not None:
            empalme_bloque = noches_ultimos_5_dias.get(bloque["nombre"]) or []
            if not (empalme_bloque and empalme_bloque[-1]):
                bloque = {"nombre": None, "noches_totales": 0, "noches_cubiertas": 0}

        for d in range(7):
            # ¿el titular del bloque en curso sigue siendo elegible hoy?
            # (regla de interrupción confirmada: una vacación/petición a
            # mitad de bloque lo corta ahí, con lo ya cubierto — siempre
            # ≥2 gracias al mínimo general de racha — y arranca uno nuevo
            # con otra persona.)
            if bloque["nombre"] is not None and (
                persona_bloqueada_dia(staff_list, cfg, bloque["nombre"], d)
                or bloque["nombre"] in tracker.bloqueados()
            ):
                bloque = {"nombre": None, "noches_totales": 0, "noches_cubiertas": 0}

            if bloque["nombre"] is None:
                # dias_cubrir={d} (Fase 5, corrección de INFEASIBLE
                # espurio): cada arranque de bloque decide un día CONCRETO,
                # no los dos días fijos de cfg.night_fixed_days_off (que en
                # modo respaldo pueden ser irrelevantes, p.ej. si no hay
                # night_fixed en la plantilla) — sin este parámetro, la
                # elegibilidad podía devolver a alguien con el día `d` real
                # ya forzado Libre por vacación/petición, y la línea de
                # abajo fijaba Noche=1 sobre esa misma celda: dos
                # restricciones duras contradictorias, INFEASIBLE que no
                # reflejaba una imposibilidad real.
                #
                # Se incluye TAMBIÉN d+1 en dias_cubrir cuando cae dentro de
                # esta semana (verificado empíricamente
                # necesario en "Viernes de Carla" 12 semanas): sin esto, se
                # podía elegir como titular de un bloque NUEVO a alguien con
                # una vacación/petición propia justo al día siguiente,
                # interrumpiendo el bloque tras 1 sola noche — violando el
                # mínimo general de racha (MIN_NOCHES_RACHA=2,
                # agregar_restriccion_racha_noches) de forma que ni siquiera
                # el mecanismo de interrupción confirmado (que asume ≥2
                # noches ya cubiertas) contempla. Exigir elegibilidad para
                # {d, d+1} garantiza que quien arranca un bloque nuevo puede
                # cubrir, como mínimo, las 2 noches que el mínimo general
                # exige antes de cualquier interrupción posible.
                #
                # bloqueados_por_racha: nombres que a fecha de HOY (antes
                # de decidir el día d) todavía tienen descanso obligatorio
                # post-racha pendiente de días anteriores de esta misma
                # semana — ver _RachaTracker.
                dias_arranque = {d, d + 1} if d < 6 else {d}
                nombre, estado_rotacion = seleccionar_candidato_cobertura_nocturna(
                    staff_list, cfg, estado_rotacion, dias_cubrir=dias_arranque,
                    bloqueados_por_racha=tracker.bloqueados(),
                )
                bloque = {
                    "nombre": nombre,
                    "noches_totales": longitud_bloque_respaldo(staff_list, cfg, nombre, d),
                    "noches_cubiertas": 0,
                }
            else:
                # Continuación de bloque: el "quién" ya se decidió al
                # arrancarlo — no se vuelve a llamar a seleccionar_
                # candidato_cobertura_nocturna (nada que decidir hoy), solo
                # se refleja la carga en historico_balance.
                estado_rotacion = registrar_noche_bloque(estado_rotacion, bloque["nombre"])

            cobertura_externa[d] = bloque["nombre"]
            tracker.registrar(bloque["nombre"])
            bloque["noches_cubiertas"] += 1
            if bloque["noches_cubiertas"] >= bloque["noches_totales"]:
                bloque = {"nombre": None, "noches_totales": 0, "noches_cubiertas": 0}

        estado_rotacion["bloque_actual"] = bloque
        if bloque["nombre"] is not None:
            personas_bloque_continua.add(bloque["nombre"])
    else:
        nombre, estado_rotacion = seleccionar_candidato_cobertura_nocturna(
            staff_list, cfg, estado_rotacion
        )
        nombre_sustituto_unico = nombre
        for d in cfg.night_fixed_days_off:
            cobertura_externa[d] = nombre

    # dias_bloqueados_externos (restricción #7, ver docstring de
    # _construir_modelo): invierte cobertura_externa ({día: nombre}) a
    # {nombre: {días}} — los días en que cada persona YA está comprometida
    # con Noche por cobertura_externa (fijado más abajo, DESPUÉS de esta
    # llamada) no pueden contarse como "disponibles" para el patrón de 2
    # libres de esa persona, sea el modo normal (un único sustituto, ambos
    # días de cfg.night_fixed_days_off) o el modo respaldo (hasta 7
    # personas distintas, un día cada una).
    dias_bloqueados_externos = {}
    for d, nombre in cobertura_externa.items():
        dias_bloqueados_externos.setdefault(nombre, set()).add(d)

    model, x, names, by_name = _construir_modelo(
        staff_list, week_start, cfg, prev_sunday_shifts,
        nombre_sustituto_cobertura=nombre_sustituto_unico,
        dias_bloqueados_externos=dias_bloqueados_externos,
        dias_trabajados_previos=dias_trabajados_previos,
    )

    for d, nombre in cobertura_externa.items():
        if (nombre, d, "Noche") not in x:
            # Presencia parcial: no existe la variable Noche en un día
            # fuera — la elegibilidad fina por presencia llega en el paso 4;
            # aquí se evita fijar una celda inexistente y la celda queda
            # como FUERA (fijada al construir el modelo).
            continue
        model.Add(x[nombre, d, "Noche"] == 1)

    # night_fixed queda siempre exento de la regla conservadora de Domingo
    # (agregar_restriccion_racha_noches, punto 3): su patrón de Noche es
    # enteramente estructural (cfg.night_fixed_days_off, restricción #4/#5),
    # no una elección libre del solver, y su racha real es continua entre
    # semanas por diseño (dos noches sueltas de descanso). La regla
    # "Domingo Noche <= Sábado Noche" asume que un Domingo trabajado es un
    # arranque nuevo que necesita verificarse — para night_fixed eso es
    # falso siempre que cfg.night_fixed_days_off termine en Sábado (ej.
    # mozos, [4,5]): night_fixed libra Sábado por turno fijo pero SÍ trabaja
    # Noche el Domingo por el mismo turno fijo, lo que sin esta excepción
    # queda contradicho por la regla conservadora → INFEASIBLE espurio.
    personas_bloque_continua_racha = set(personas_bloque_continua)
    for name, person in by_name.items():
        if person.get("role") == "night_fixed":
            personas_bloque_continua_racha.add(name)

    postweek_vars = agregar_restriccion_racha_noches(
        model, x, names, noches_ultimos_5_dias,
        personas_bloque_continua=personas_bloque_continua_racha,
    )
    extra_vars_planas = {}
    for name, (post1, post2) in postweek_vars.items():
        extra_vars_planas[f"{name}__lunes"] = post1
        extra_vars_planas[f"{name}__martes"] = post2

    schedule, valores_extra = _resolver_y_extraer_con_extra(
        model, x, names, extra_vars_planas, staff_list, cfg
    )

    forced_days_off_extra = {}
    # descanso_pendiente_inicial (ver _RachaTracker):
    # misma fuente (post1/post2) que forced_days_off_extra, pero como
    # CONTEO de días de descanso aún pendientes al arrancar la semana
    # siguiente (0, 1 o 2) — post2=1 implica post1=1 por construcción
    # (nunca hay "solo martes" sin lunes), así que 2 si post2, si no 1 si
    # post1, si no 0. Es la entrada que _RachaTracker necesita para saber,
    # sin tener que reconstruirlo del empalme de 5 días (que no siempre
    # alcanza a ver el inicio completo de la racha), a quién sigue
    # bloqueado por descanso al principio de la semana siguiente.
    descanso_pendiente_inicial = {}
    for name in names:
        dias = []
        if valores_extra[f"{name}__lunes"]:
            dias.append(DAYS[0])
        if valores_extra[f"{name}__martes"]:
            dias.append(DAYS[1])
        if dias:
            forced_days_off_extra[name] = dias
        if valores_extra[f"{name}__martes"]:
            descanso_pendiente_inicial[name] = 2
        elif valores_extra[f"{name}__lunes"]:
            descanso_pendiente_inicial[name] = 1

    # Compensación Lun+Mar de la semana siguiente para el sustituto de
    # cobertura nocturna en modo normal (mecanismo de mozos —
    # prefer_next_week_for_cover). Incondicional, igual que
    # next_week_rest_days en modelo_base.py: no depende de qué día suelto
    # le haya tocado esta semana, solo de haber sido el sustituto. Se
    # fusiona (no reemplaza) con cualquier descanso de racha ya pendiente
    # para la misma persona.
    if cfg.prefer_next_week_for_cover and nombre_sustituto_unico is not None:
        existentes = forced_days_off_extra.get(nombre_sustituto_unico, [])
        forced_days_off_extra[nombre_sustituto_unico] = list(
            dict.fromkeys(existentes + [DAYS[0], DAYS[1]])
        )

    noches_siguiente = {
        name: [schedule[name][d] == "Noche" for d in range(2, 7)]
        for name in names
    }

    estado_rotacion = dict(estado_rotacion)
    estado_rotacion["descanso_pendiente_inicial"] = descanso_pendiente_inicial

    return {
        "schedule": schedule,
        "estado_rotacion": estado_rotacion,
        "noches_ultimos_5_dias": noches_siguiente,
        "forced_days_off_extra": forced_days_off_extra,
    }
