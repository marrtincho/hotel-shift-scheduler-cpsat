"""
motor_cpsat.py — Prototipo AISLADO del motor de horarios sobre CP-SAT (ortools).

No toca app.py ni el motor heurístico de modelo_base.py (que sigue siendo el
usado en producción) — salvo por importar de él las dos funciones de solo
lectura de histórico reutilizadas tal cual (ver Fase 2 más abajo).

── Fase 1 — restricciones duras ─────────────────────────────────────────────
  1. Un turno por persona/día (incluye "Libre").
  2. Cobertura mín/máx por turno y día (cfg.min_coverage / cfg.max_coverage).
  3. No-Mañana-tras-Tarde/Noche, incluida la transición con el domingo de la
     semana anterior (prev_sunday_shifts).
  4. Roles con turno fijo (cfg.fixed_roles): turno fijo todos los días que
     trabajan.
  5. Roles con días libres fijos (cfg.fixed_days_off): Libre siempre en esos
     días.
  6. Vacaciones / peticiones concedidas (forced_days_off / extra_days_off en
     cada persona de staff_list): Libre siempre en esos días.

El rol "night_fixed" usa un mecanismo propio (no cfg.fixed_roles/
cfg.fixed_days_off) y se modela con el mismo patrón que #4/#5 pero anclado a
cfg.night_fixed_days_off: turno "Noche" todos los días salvo los de
cfg.night_fixed_days_off, que son "Libre". Esos días de Libre NO reciben
sustituto en esta fase — la cobertura de Noche esos días queda sujeta a
min_coverage/max_coverage como cualquier otro turno, sin tratamiento
especial (puede quedar en 0 si min_coverage lo permite, o exigir que otra
persona la cubra si min_coverage > 0, igual que con cualquier fixed_days_off).

Deliberadamente NO implementado en fase 1 (fases futuras):
  - Noches con racha o rotación por bloques (Noche se trata como un turno
    más, sin reglas de racha).
  - El sustituto de los días libres de night_fixed (select_night_covers) y
    la cobertura de jefatura (subjefe cubre a jefe).
  - Turno complementario jefe/subjefe.

── Fase 2 — términos de objetivo (preferencias blandas) ─────────────────────
Todas las restricciones duras de fase 1 siguen siendo duras; estos términos
solo desempatan entre soluciones que ya las cumplen. Pesos documentados como
constantes al principio del módulo — fáciles de ajustar sin tocar el resto
del modelo.

  A. Equidad de findes libres: prioriza dar el finde completo (Sáb+Dom
     Libre) a quien tiene menor ratio histórico de findes libres/semanas
     trabajadas (_get_weekend_ratios, reutilizada tal cual de
     modelo_base.py). El coste por persona es (ratio - ratio_medio): negativo
     (recompensa) para quien está por debajo de la media, positivo
     (penalización) para quien está por encima — no un coste solo-positivo,
     que dejaría "que nadie libre el finde" igual de barato que "lo libra
     quien menos lo ha librado" y no crearía presión real hacia la equidad.
     SOLO se calcula sobre personas cuyo rol NO está en
     cfg.roles_excluidos_equidad — las excluidas quedan fuera del conjunto
     de variables sumadas en este término, no solo con peso cero.
  B. Roturas de continuidad: penaliza cambiar de turno (Mañana/Tarde/
     Partido) entre dos días consecutivos ambos trabajados sin librar en
     medio. Noche y Libre no participan (igual que en el motor heurístico:
     "Noche nunca penaliza").
  C. Desequilibrio histórico de turnos: prefiere asignar a cada persona los
     turnos que menos ha hecho en su histórico (_get_shift_counts,
     reutilizada tal cual de modelo_base.py).
  D. Uso de "Partido": minimizarlo cuando aparece como opción.
  E. (Decisión de diseño fase 2, deliberada) Penalización ALTA — mayor que
     el peso de A — a que una persona con rol en cfg.roles_excluidos_equidad
     Y SIN entrada en cfg.fixed_days_off reciba finde completo libre. No es
     restricción dura: si esa persona tiene una vacación/petición concedida
     ese finde (restricción dura #6), el finde libre se fuerza igual, sin
     conflicto — la penalización blanda solo actúa cuando nada más lo
     justifica. Los roles cubiertos por una restricción DURA de días libres
     quedan fuera de este término (no se apilan dos mecanismos para lo
     mismo): roles en cfg.fixed_days_off (p.ej. "jefe") y el rol
     "night_fixed" (cubierto por cfg.night_fixed_days_off — ver fase 1;
     este es el único nombre de rol que se referencia literalmente en el
     modelo, porque ya es un rol estructuralmente especial en todo
     modelo_base.py, no una particularidad de un departamento concreto).
     Cualquier OTRO rol en roles_excluidos_equidad sin fixed_days_off propio
     (p.ej. "subjefe" hoy) activa este término sin que su nombre aparezca
     en ningún sitio del modelo.

── Sesión siguiente — racha de noches (ventana deslizante) ──────────────────
Añade, como restricciones DURAS adicionales, aplicadas genéricamente a
CUALQUIER persona con Noche=1 esa semana (night_fixed o quien lo cubra):

  1. Máximo 5 noches en cualquier ventana de 6 días consecutivos —
     incluyendo el empalme con la semana anterior (dato de entrada
     noches_ultimos_5_dias, ver agregar_restriccion_racha_noches).
  2. 2 días de descanso obligatorio ("Libre", no cualquier turno) tras
     agotar una racha de 5 noches consecutivas.

Quién cubre cada noche NO lo decide el solver — es un dato de entrada ya
resuelto por cobertura_nocturna.seleccionar_candidato_cobertura_nocturna
(sesión anterior). generar_semana_cpsat_con_cobertura_nocturna() conecta
ambas piezas: llama a cobertura_nocturna.py para decidir quién cubre,
fija esas celdas como restricción dura en el modelo (Noche=1), añade la
restricción de racha, resuelve, y devuelve también los datos que la
semana SIGUIENTE necesita como entrada (estado_rotacion actualizado y
noches_ultimos_5_dias actualizado — ver su docstring para el detalle del
empalme hacia adelante cuando una racha termina cerca del fin de semana).

── Esta sesión — término F: Domingo suelto del sustituto (preferencia) ──────
En departamentos con cfg.prefer_next_week_for_cover=True (mozos hoy — la
condición es sobre ese flag, no sobre el nombre literal "mozos": es la
señal ya existente en modelo_base.py de que este depto usa el mecanismo de
compensación Lun+Mar de la semana SIGUIENTE para quien cubre las noches de
night_fixed_days_off), cuando el sustituto de esta semana necesita un día
suelto (no consecutivo con las noches que cubre), el modelo PREFIERE
—no exige— que ese suelto sea el Domingo: combinado con la compensación de
Lun+Mar ya existente la semana siguiente, produce un tramo real de 3 días
libres consecutivos (Domingo-Lunes-Martes) que cruza el fin de semana.

Es una preferencia blanda (PESO_DOMINGO_SUSTITUTO, término F del
objetivo) — cede sin más si la cobertura mínima de ese Domingo exige que
el sustituto trabaje; nunca vuelve infactible el modelo. Solo se activa
para quien _construir_modelo recibe explícitamente como
nombre_sustituto_cobertura — nadie más se ve afectado, y en modo respaldo
(sin un "el sustituto de esta semana" único, ver
generar_semana_cpsat_con_cobertura_nocturna) simplemente no se activa.

CORRECCIÓN (sesión posterior): el término F solo se AÑADE al objetivo si,
para nombre_sustituto_cobertura, no existe NINGÚN par de días
consecutivos ambos disponibles como libres esa semana — misma condición
que hoy dispara la rama de "días sueltos" en modelo_base.py (dentro de
la rama `night_cover_this_week` de assign_days_off: `valid_pairs` vacío).
Es una condición de ACTIVACIÓN estructural (ver
_tiene_par_consecutivo_disponible), no una cuestión de pesos: si hay un
par consecutivo estructuralmente viable, el término F ni siquiera entra
en la suma del objetivo, así que no puede competir con el término B
(continuidad) ni con ningún otro — antes de esta corrección sí podía, y
se confirmó empíricamente que ganaba sobre B en algunos casos, algo que
contradecía la regla de negocio original.

Además, cuando hay sustituto en modo normal (cfg.prefer_next_week_for_cover
y nombre_sustituto_unico), generar_semana_cpsat_con_cobertura_nocturna
añade incondicionalmente Lunes+Martes de la semana SIGUIENTE a
forced_days_off_extra para esa persona — mismo mecanismo que
next_week_rest_days en modelo_base.py (ahí también es incondicional: no
depende de qué día suelto le tocara esta semana, ver generate_schedule).

── Esta sesión — restricción 8: cobertura de ausencia de otro rol ───────────
Traslada cfg.cobertura_roles (p.ej. subjefe cubre a jefe — ver
modelo_base._marcar_cobertura_ausencias, que es la referencia de
comportamiento) al modelo CP-SAT. Los "días de cobertura" de cada
cubridor se calculan en Python, de forma puramente determinista a partir
de staff_list/cfg (igual que _marcar_cobertura_ausencias: ausente =
rol_cubierto no existe o tiene forced_days_off/extra_days_off ese día;
los fixed_days_off del cubierto NO cuentan como ausencia; si
info["dias_libres_fijos"] está activo, se añaden además esos días de
forma permanente) — no dependen de ninguna variable del solver, así que
no hace falta modelarlos como restricción, solo como conjunto de días
sobre los que actúa el término de objetivo.

PESO_COBERTURA_AUSENCIA (término G) es DELIBERADAMENTE el peso más alto
del módulo — mayor que cualquiera de fase 2/3 (E=200 era hasta ahora el
máximo) — para que, en la práctica, ceda ÚNICAMENTE ante max_coverage
(restricción dura de fase 1), nunca ante la suma de preferencias blandas
A-F: con las plantillas y pesos habituales de este módulo, esa suma
rara vez supera unos pocos cientos por semana, así que 1000 deja margen
de sobra sin necesitar tocarlo cada vez que se añada un término nuevo.
Es EXACTAMENTE el mismo patrón que max_coverage ya impone hoy en el
motor heurístico (pass1 de assign_shifts: fuerza el turno de cobertura
solo si no rompe el máximo, si no, lo deja para que Pass 2 le busque
cualquier turno legal) — aquí, en vez de una prioridad de asignación en
dos pasadas, es un coste que el propio solver sopesa contra la única
restricción dura que puede impedirlo (#2, cobertura mín/máx).

Cuando, además, info["semana_completa"] está activo Y la ausencia
calculada es de semana completa (rol_cubierto inexistente, o con los 7
días en forced_days_off), se añade un coste EXTRA (misma constante,
aplicado solo a Sábado/Domingo) por estar Libre esos días —replica la
exclusión de fin de semana que cobertura_total_semana fuerza en
modelo_base.py (assign_days_off, Pasada B) al elegir el par de libres
del cubridor. Es la diferencia entre "Opción A" (semana_completa=False:
el cubridor puede librar el finde con normalidad, coste único de G por
no hacer el turno de cobertura) y "Opción B" (semana_completa=True:
librar el finde concretamente cuesta el doble de G, empujando con más
fuerza hacia la presencia total de mando en fin de semana).

── Esta sesión — restricción 9: turno complementario jefe/subjefe ──────────
Preferencia blanda NUEVA (no existe en modelo_base.py): en los días en
que TANTO "jefe" como "subjefe" trabajan (ninguno de los dos está
Libre), penaliza que coincidan en el mismo turno no-Libre. Se activa
solo si ambos roles existen en staff_list (comprobación genérica, no
depende del nombre de ningún departamento concreto). Mutuamente
excluyente por construcción con la restricción 8: los días en que
subjefe cubre la ausencia de jefe, jefe está Libre (ausente), así que
"ambos trabajan" ya es falso ahí sin necesitar ningún gating adicional.

PESO_COMPLEMENTARIO_JEFATURA (término H) es un peso MODERADO, menor que
PESO_COBERTURA_AUSENCIA — no debe competir con ella — y se sitúa entre
PESO_ROTURAS_CONTINUIDAD(B=5) y PESO_USO_PARTIDO(D=20): igual que F
(Domingo suelto del sustituto, 15), es una preferencia de "reparto de
turnos" entre personas concretas, no una cuestión de cobertura agregada
(A/E) ni de desequilibrio histórico (C) — 10 la deja claramente por
encima de B (para que sí tenga efecto real desempatando turnos
concretos) pero por debajo de D y F, reflejando que es la preferencia
más fácil de ceder de las nuevas si hace falta liberar margen para
cobertura mínima.

── Esta sesión — rediseño de prioridades: B > I > D ────────────────────────
Fase 5 encontró que CP-SAT eliminaba el uso de "Partido" por completo, a
costa de más roturas de continuidad que el motor heurístico (Recepción, 18
semanas: 9 roturas heurístico vs 15 CP-SAT; Partido: 10 vs 0). Causa raíz:
no existía ningún coste por cubrir un turno diurno por encima de su mínimo,
así que el solver prefería sobre-cubrir Mañana/Tarde antes que usar Partido
(que sí tenía coste, término D) — el resultado técnicamente minimizaba el
objetivo de entonces, pero no reflejaba la prioridad real de negocio.

Nueva prioridad de negocio, en este orden estricto:
  1. Continuidad (término B) — la más importante de las tres.
  2. Evitar sobre-cobertura de un turno diurno (término I, NUEVO) — si hay
     excedente de personal ese día, mandarlo a Partido es preferible a
     apilarlo de más en Mañana/Tarde.
  3. Evitar el uso de Partido en general (término D) — la de menor
     prioridad de las tres, pero sigue existiendo.

I. Sobre-cobertura de turnos diurnos: penaliza, por cada día y cada turno
   de TURNOS_DIURNOS_SIN_PARTIDO (Mañana/Tarde — subconjunto de DAY_SHIFTS,
   sin Partido), cuánto se cubre ese turno por encima de cfg.min_coverage.
   Lineal, siempre no-negativo (la restricción dura #2 de fase 1 ya
   garantiza cobertura_real >= min_coverage), no hace falta max(). No
   incluye Noche (cobertura decidida por la orquestación de fase 3, ajena
   a esta lógica) ni Partido (es la válvula de escape que este término
   empuja a usar, no algo a penalizar además por sobre-cobertura — ya
   tiene su propio coste vía D).

   Caso que se resuelve solo, sin lógica especial: si Mañana/Tarde ya
   están en su máximo (cfg.max_coverage), la restricción dura de fase 1
   ya impide meter a nadie de más ese turno — I no tiene nada que
   penalizar ahí, y Partido tampoco haría falta.

Los pesos de B/D se recalibraron y se añadió PESO_SOBRECOBERTURA_DIURNA
(I) — ver los comentarios junto a cada constante para la magnitud elegida
y por qué. Los términos F y H (complementario jefatura) no se tocaron en
valor, pero sus comentarios de posición relativa se actualizaron para no
quedar desconectados de la nueva relación B > D > F/H (ver comentarios
junto a esas constantes).
"""

from datetime import date

from ortools.sat.python import cp_model

from modelo_base import (
    DAYS, SHIFTS, NO_MORNING_AFTER, _get_weekend_ratios, _get_shift_counts,
    get_consecutive_day_pairs,
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
# sesión posterior a fijar num_search_workers=1/sorted()): nueva prioridad de
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
# (Paso 3 de esta sesión) para la confirmación de que el gating sigue intacto.
PESO_COBERTURA_AUSENCIA      = 1000  # término G — el más alto del módulo, ver docstring
PESO_COMPLEMENTARIO_JEFATURA = 10   # término H — YA NO está "por encima de B"
# tras el rediseño de prioridades (B=50 > H=10): sigue siendo la preferencia
# más fácil de ceder de las de "reparto de turnos concretos" frente a D(20)/
# F(15), y ahora también frente a B — reflejando que emparejar jefe/subjefe
# en turnos distintos es una comodidad menor comparada con preservar la
# continuidad de alguien. No se ha subido: nada en el rediseño de prioridades
# de esta sesión pedía tocar este término, y sus tests de Fase 4 siguen
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
# turno diurno ese día (ver escenario B-vs-I, Paso 2 de esta sesión).


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
    del solver): devuelve (dias_ausencia: set[int], semana_completa: bool).

    "Ausente" = rol_cubierto no existe en staff_list, o la persona con ese
    rol tiene el día en forced_days_off/extra_days_off. Los días libres
    FIJOS del rol cubierto (cfg.fixed_days_off) NO cuentan como ausencia.
    "Semana completa" = el rol no existe, o la persona tiene los 7 días en
    forced_days_off (extra_days_off no cuenta para esto, igual que el
    original). Si info["dias_libres_fijos"] está activo, se añaden además
    (siempre, sea o no ausencia real) los días libres fijos del cubierto.
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

    if info.get("dias_libres_fijos"):
        dias_ausencia |= set(cfg.fixed_days_off.get(rol_cubierto, []))

    return dias_ausencia, semana_completa


def _construir_modelo(staff_list: list, week_start: date, cfg,
                       prev_sunday_shifts: dict = None,
                       nombre_sustituto_cobertura: str = None):
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

    Devuelve (model, x, names, by_name).
    """
    prev_sunday_shifts = prev_sunday_shifts or {}
    model = cp_model.CpModel()

    names = [p["name"] for p in staff_list]
    by_name = {p["name"]: p for p in staff_list}

    # x[name, day, shift] = 1 si `name` tiene `shift` en el día `day`.
    x = {}
    for name in names:
        for d in range(7):
            for s in SHIFTS:
                x[name, d, s] = model.NewBoolVar(f"x_{name}_{d}_{s}")

    # 1. Un turno por persona/día.
    for name in names:
        for d in range(7):
            model.AddExactlyOne(x[name, d, s] for s in SHIFTS)

    # 2. Cobertura mín/máx por turno y día.
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
    for s in turnos_con_cobertura:
        mn = cfg.min_coverage.get(s, 0)
        mx = cfg.max_coverage.get(s, len(names))
        for d in range(7):
            total = sum(x[name, d, s] for name in names)
            model.Add(total >= mn)
            model.Add(total <= mx)

    # no_night_roles: nunca Noche.
    for name in names:
        role = by_name[name].get("role")
        if role in cfg.no_night_roles:
            for d in range(7):
                model.Add(x[name, d, "Noche"] == 0)

    # 3. No-Mañana-tras-Tarde/Noche.
    for name in names:
        for d in range(7):
            if d == 0:
                prev_shift = prev_sunday_shifts.get(name)
                if prev_shift in NO_MORNING_AFTER:
                    model.Add(x[name, 0, "Mañana"] == 0)
            else:
                model.Add(
                    x[name, d, "Mañana"] + x[name, d - 1, "Tarde"] + x[name, d - 1, "Noche"] <= 1
                )

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
            model.Add(x[name, d, "Libre"] == 1)

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

    # ─── Fase 2: términos de objetivo ────────────────────────────────────
    objetivo_terms = []

    # Variable auxiliar compartida por A y E: finde completo libre.
    weekend_libre = {}
    for name in names:
        v = model.NewBoolVar(f"weekend_libre_{name}")
        model.Add(v <= x[name, 5, "Libre"])
        model.Add(v <= x[name, 6, "Libre"])
        model.Add(v >= x[name, 5, "Libre"] + x[name, 6, "Libre"] - 1)
        weekend_libre[name] = v

    # A. Equidad de findes — solo personas cuyo rol NO está en
    # cfg.roles_excluidos_equidad entran en la suma; las demás no aparecen
    # aquí en absoluto (ver docstring del módulo).
    ratios = _get_weekend_ratios(cfg.dept_id, staff_list)
    ratios_validos = [d["ratio"] for d in ratios.values()]
    avg_ratio = sum(ratios_validos) / len(ratios_validos) if ratios_validos else 0.0
    for name in names:
        role = by_name[name].get("role")
        if role in cfg.roles_excluidos_equidad:
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
            model.Add(wv == sum(x[name, d, s] for s in DAY_SHIFTS))
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
                model.Add(keep <= x[name, d - 1, s])
                model.Add(keep <= x[name, d, s])
                model.Add(keep >= x[name, d - 1, s] + x[name, d, s] - 1)
                keep_terms.append(keep)

            break_var = model.NewBoolVar(f"break_{name}_{d}")
            model.Add(break_var == work_both - sum(keep_terms))
            objetivo_terms.append(PESO_ROTURAS_CONTINUIDAD * break_var)

    # C. Desequilibrio histórico de turnos: prefiere el turno menos hecho.
    shift_counts = _get_shift_counts(cfg.dept_id)
    for name in names:
        counts = shift_counts.get(name, {})
        for s in DAY_SHIFTS:
            c = counts.get(s, 0)
            if c:
                for d in range(7):
                    objetivo_terms.append(PESO_DESEQUILIBRIO_HISTORICO * c * x[name, d, s])

    # D. Uso de "Partido".
    objetivo_terms.append(
        PESO_USO_PARTIDO * sum(x[name, d, "Partido"] for name in names for d in range(7))
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
        if not _tiene_par_consecutivo_disponible(cfg, forced_libre[nombre_sustituto_cobertura]):
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

        dias_ausencia, semana_completa = _dias_cobertura_ausencia(staff_list, cfg, rol_cubierto, info)
        cobertura_total_semana = semana_completa and info.get("semana_completa")

        for name in cubridores:
            for d in dias_ausencia:
                objetivo_terms.append(PESO_COBERTURA_AUSENCIA * (1 - x[name, d, turno_cobertura]))
            if cobertura_total_semana:
                for d in (5, 6):
                    objetivo_terms.append(PESO_COBERTURA_AUSENCIA * x[name, d, "Libre"])

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
                model.Add(both_s <= x[jefe_name, d, s])
                model.Add(both_s <= x[subjefe_name, d, s])
                model.Add(both_s >= x[jefe_name, d, s] + x[subjefe_name, d, s] - 1)
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
            cobertura_real = sum(x[name, d, turno] for name in names)
            objetivo_terms.append(PESO_SOBRECOBERTURA_DIURNA * (cobertura_real - min_turno))

    model.Minimize(sum(objetivo_terms))

    return model, x, names, by_name


def _resolver_y_extraer(model: cp_model.CpModel, x: dict, names: list) -> dict:
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 20.0  # 90.0 -> 20.0 (Fase 5,
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
    # ver INFORME_FASE_5.md para las mediciones completas.
    solver.parameters.num_search_workers = 1
    status = solver.Solve(model)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise RuntimeError(
            "CP-SAT no encontró solución factible para las restricciones dadas "
            f"(status={solver.StatusName(status)})."
        )

    schedule = {name: ["" for _ in range(7)] for name in names}
    for name in names:
        for d in range(7):
            for s in SHIFTS:
                if solver.Value(x[name, d, s]) == 1:
                    schedule[name][d] = s
                    break

    return schedule


def generar_semana_cpsat(staff_list: list, week_start: date, cfg,
                          prev_sunday_shifts: dict = None) -> dict:
    model, x, names, by_name = _construir_modelo(staff_list, week_start, cfg, prev_sunday_shifts)
    return _resolver_y_extraer(model, x, names)


# ═══════════════════════════════════════════════════════════════════════════
# Racha de noches — ventana deslizante (esta sesión)
# ═══════════════════════════════════════════════════════════════════════════

MAX_NOCHES_VENTANA = 5   # máximo de noches en cualquier ventana de...
VENTANA_NOCHES = 6       # ...esta cantidad de días consecutivos.
DIAS_DESCANSO_TRAS_RACHA = 2


def _resolver_y_extraer_con_extra(model: cp_model.CpModel, x: dict, names: list,
                                   extra_vars: dict) -> tuple:
    """Igual que _resolver_y_extraer, pero además lee el valor resuelto de
    variables auxiliares del modelo (p.ej. las de racha_postweek). Función
    NUEVA y separada — _resolver_y_extraer no se toca, para no romper el
    tipo de retorno que ya usan las fases 1/2 y sus tests.

    extra_vars: {clave: BoolVar}. Devuelve (schedule, {clave: 0|1}).
    """
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 20.0  # 90.0 -> 20.0 (Fase 5,
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
    # ver INFORME_FASE_5.md para las mediciones completas.
    solver.parameters.num_search_workers = 1
    status = solver.Solve(model)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise RuntimeError(
            "CP-SAT no encontró solución factible para las restricciones dadas "
            f"(status={solver.StatusName(status)})."
        )

    schedule = {name: ["" for _ in range(7)] for name in names}
    for name in names:
        for d in range(7):
            for s in SHIFTS:
                if solver.Value(x[name, d, s]) == 1:
                    schedule[name][d] = s
                    break

    valores_extra = {k: solver.Value(v) for k, v in extra_vars.items()}
    return schedule, valores_extra


def agregar_restriccion_racha_noches(model: cp_model.CpModel, x: dict, names: list,
                                      noches_ultimos_5_dias: dict) -> dict:
    """Añade, sobre un modelo ya construido por _construir_modelo (llamar
    ANTES de resolver — CP-SAT permite seguir añadiendo restricciones tras
    fijar el objetivo con model.Minimize), las dos restricciones duras de
    racha de Noche, para TODAS las personas en `names` genéricamente (no
    hace falta distinguir night_fixed de su sustituto: la restricción
    opera sobre x[name, d, "Noche"] sea quien sea):

      1. Máximo MAX_NOCHES_VENTANA(5) noches en cualquier ventana de
         VENTANA_NOCHES(6) días consecutivos.
      2. DIAS_DESCANSO_TRAS_RACHA(2) días de Libre obligatorio inmediatamente
         tras una racha de exactamente 5 noches consecutivas.

    noches_ultimos_5_dias: {nombre: [bool, bool, bool, bool, bool]} — empalme
    con la semana anterior. índice 0 = 5 días antes del lunes de esta semana
    (miércoles de la semana pasada), índice 4 = domingo de la semana pasada
    (el día inmediatamente anterior al lunes nuevo). Quien no aparezca en el
    dict se asume sin noches en los últimos 5 días ([False]*5) — cubre el
    caso de un empleado nuevo sin histórico de racha.

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
        secuencia += [x[name, d, "Noche"] for d in range(7)]  # índices 5..11
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
                    model.Add(x[name, d, "Libre"] == 1).OnlyEnforceIf(racha5)
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

    return postweek_vars


def generar_semana_cpsat_con_cobertura_nocturna(
    staff_list: list, week_start: date, cfg,
    estado_rotacion: dict, noches_ultimos_5_dias: dict,
    prev_sunday_shifts: dict = None,
) -> dict:
    """Orquesta una semana completa conectando cobertura_nocturna.py (QUIÉN
    cubre — dato de entrada, no lo decide el solver) con las restricciones
    de racha de Noche de esta sesión (CUÁNDO/cuánto puede encadenarse). No
    reimplementa nada de fase 1/fase 2/sesión anterior — solo las combina.

    Modo normal (night_fixed presente o ausente <2 semanas consecutivas):
    llama a seleccionar_candidato_cobertura_nocturna UNA vez, y esa misma
    persona cubre AMBOS días de cfg.night_fixed_days_off (igual que
    select_night_covers hoy en producción).

    Modo respaldo (>=2 semanas consecutivas ausente, o rol night_fixed
    inexistente en staff_list): llama a seleccionar_candidato_cobertura_
    nocturna UNA vez POR CADA uno de los 7 días de la semana (encadenando
    el estado_rotacion entre llamadas), porque night_fixed normalmente
    cubriría los 7 y ahora ninguno tiene automáticamente asignado quién lo
    hace.

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
    )

    estado_rotacion = avanzar_semana_ausencia_night_fixed(staff_list, estado_rotacion)

    cobertura_externa = {}  # day_idx -> nombre
    # nombre_sustituto_unico: solo tiene sentido en modo normal (una misma
    # persona cubre AMBOS días de cfg.night_fixed_days_off) — el término F
    # (domingo suelto) asume ese caso clásico de sustituto único (mozos:
    # cubre Vie+Sáb). En modo respaldo no hay "el sustituto de esta semana"
    # (pueden ser hasta 7 personas distintas, una por noche), así que el
    # término F simplemente no se activa ahí.
    nombre_sustituto_unico = None
    if modo_respaldo_activo(staff_list, estado_rotacion):
        for d in range(7):
            # dias_cubrir={d} (Fase 5, corrección de INFEASIBLE espurio):
            # cada llamada de modo respaldo decide un día CONCRETO, no los
            # dos días fijos de cfg.night_fixed_days_off (que en modo
            # respaldo pueden ser irrelevantes, p.ej. si no hay night_fixed
            # en la plantilla) — sin este parámetro, la elegibilidad podía
            # devolver a alguien con el día `d` real ya forzado Libre por
            # vacación/petición, y la línea de abajo fijaba Noche=1 sobre
            # esa misma celda: dos restricciones duras contradictorias,
            # INFEASIBLE que no reflejaba una imposibilidad real.
            nombre, estado_rotacion = seleccionar_candidato_cobertura_nocturna(
                staff_list, cfg, estado_rotacion, dias_cubrir={d}
            )
            cobertura_externa[d] = nombre
    else:
        nombre, estado_rotacion = seleccionar_candidato_cobertura_nocturna(
            staff_list, cfg, estado_rotacion
        )
        nombre_sustituto_unico = nombre
        for d in cfg.night_fixed_days_off:
            cobertura_externa[d] = nombre

    model, x, names, by_name = _construir_modelo(
        staff_list, week_start, cfg, prev_sunday_shifts,
        nombre_sustituto_cobertura=nombre_sustituto_unico,
    )

    for d, nombre in cobertura_externa.items():
        model.Add(x[nombre, d, "Noche"] == 1)

    postweek_vars = agregar_restriccion_racha_noches(model, x, names, noches_ultimos_5_dias)
    extra_vars_planas = {}
    for name, (post1, post2) in postweek_vars.items():
        extra_vars_planas[f"{name}__lunes"] = post1
        extra_vars_planas[f"{name}__martes"] = post2

    schedule, valores_extra = _resolver_y_extraer_con_extra(model, x, names, extra_vars_planas)

    forced_days_off_extra = {}
    for name in names:
        dias = []
        if valores_extra[f"{name}__lunes"]:
            dias.append(DAYS[0])
        if valores_extra[f"{name}__martes"]:
            dias.append(DAYS[1])
        if dias:
            forced_days_off_extra[name] = dias

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

    return {
        "schedule": schedule,
        "estado_rotacion": estado_rotacion,
        "noches_ultimos_5_dias": noches_siguiente,
        "forced_days_off_extra": forced_days_off_extra,
    }
