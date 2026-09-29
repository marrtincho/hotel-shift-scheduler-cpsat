# REGLAS.md — Fuente única de verdad del motor de horarios

> **Generado leyendo el código real** de `motor_cpsat.py`, `cobertura_nocturna.py`,
> `app.py` y `modelo_base.py`. Este documento es la fuente única de las reglas de
> negocio: los docstrings de los módulos describen su arquitectura, no las reglas.
> Antes de tocar el motor, ver también
> [§6 Limitaciones conocidas](#6-limitaciones-conocidas-y-trampas-de-mantenimiento).
>
> **Si vas a modificar el motor, lee primero [§4 Reglas no negociables](#4-reglas-no-negociables).**
>
> Motor activo en producción: `data/config_motor.json` = `{"activo": true}`
> (`herramientas.motor_cpsat_activo`, `herramientas.py:1180`). La variable de entorno
> `USAR_MOTOR_CPSAT` tiene prioridad sobre el fichero si está definida.

Convenciones: días `0=Lunes … 6=Domingo` (`modelo_base.DAYS`).
Turnos: `Mañana, Tarde, Noche, Partido, Libre` (`modelo_base.SHIFTS`).
Horas reales (`data/horarios_turnos.json`): Mañana 07–15, Tarde 15–23,
Noche 23–07, Partido 10–18.

Estado adicional: `Fuera` (`modelo_base.FUERA`, **deliberadamente NO está en `SHIFTS`**)
— aparece solo en horarios que tienen categorías con presencia parcial
(`dias_presencia`, ver [§1.8](#18-patrón-de-días-libres-semanales-2-consecutivos)) y marca
los días en que esa persona no está contratada. No computa en nada: ni cobertura, ni
equidad, ni libres, ni festivos trabajados.

---

## 1. Restricciones DURAS

Todas viven en `motor_cpsat._construir_modelo` salvo donde se indique. Si alguna no
se puede satisfacer, el modelo devuelve INFEASIBLE y la generación falla con
`RuntimeError` → HTTP 422 (nunca produce un horario que las viole).

### 1.1 Un turno por persona y día
**Regla:** cada persona tiene exactamente un estado cada día, incluido `Libre`.
**Código:** `_construir_modelo` → `model.AddExactlyOne(x[name, d, s] for s in SHIFTS)`.
**Parámetros:** ninguno.
**Excepciones:** los días fuera de presencia de una categoría con `dias_presencia`
(ver [§1.8](#18-patrón-de-días-libres-semanales-2-consecutivos)): ahí no hay variables de
turno — la única opción es `Fuera`, fijada al construir el modelo.

### 1.2 Cobertura mínima por turno y día
**Regla:** cada turno necesita un número mínimo de personas cada día. Es lo que
garantiza que el servicio esté cubierto.
**Código:** `_construir_modelo`, bucle `turnos_con_cobertura` → `model.Add(total >= mn)`.
**Parámetros:** `cfg.min_coverage`. Valor efectivo desde
`data/coverage_config.json` (perfil de temporada activo), no desde
`modelo_recepcion.py`. Recepción, perfil `normal`: Mañana 2, Tarde 2, Noche 1, Partido 0.
**Excepciones:** ninguna. Ver [§4](#4-reglas-no-negociables).

### 1.3 Cobertura máxima — DURA solo en Noche y Partido
**Regla:** Noche y Partido nunca pueden superar su tope. Mañana y Tarde sí pueden
superarlo, con penalización (término K, ver [§2.12](#212-k-exceso-de-cobertura-en-mañanatarde)).
**Código:** mismo bucle; `if s in TURNOS_MAX_COBERTURA_BLANDA:` → término blando,
`else:` → `model.Add(total <= mx)`.
**Parámetros:** `cfg.max_coverage`; `TURNOS_MAX_COBERTURA_BLANDA = ["Mañana", "Tarde"]`
(`motor_cpsat.py:503`, derivado de `TURNOS_DIURNOS_SIN_PARTIDO`).
Recepción `normal`: Mañana 3, Tarde 3, Noche 1, Partido 1.
**Por qué Noche es dura:** quién cubre cada noche **no lo decide el solver** — lo
decide `cobertura_nocturna.py` y se fija después (ver [§1.9](#19-cobertura-nocturna-fijada-externamente)).
Con el máximo de Noche blando, Noche era el destino más barato del personal
sobrante (K=5000 pelado vs K+I=5030 en Mañana/Tarde y K+D=5020 en Partido), y una
noche inventada rompe la rotación por bloques **y contamina la semana siguiente**,
porque `noches_ultimos_5_dias` se re-deriva del horario guardado ([§5](#5-estado-entre-semanas)).
**Excepciones:** ninguna.

### 1.4 Descanso mínimo de 9 horas entre turnos de días consecutivos
**Regla:** entre el fin de un turno y el comienzo del turno del día siguiente deben
pasar al menos 9 horas. Con los horarios reales eso prohíbe exactamente cuatro
transiciones: `Tarde→Mañana` (8h), `Noche→Mañana` (0h), `Noche→Tarde` (8h),
`Noche→Partido` (3h). Dicho de otro modo: **tras una Noche solo puede venir Noche o Libre.**
**Código:** `_construir_modelo`, restricción #3 (tres `model.Add(... <= 1)` por día).
Incluye el empalme con el domingo anterior vía `prev_sunday_shifts`.
**Parámetros:** `herramientas.DESCANSO_MINIMO_HORAS = 9` y
`herramientas.transiciones_prohibidas()`, derivada de `data/horarios_turnos.json`.
**Ojo:** el modelo tiene las cuatro transiciones **escritas a mano**, no las lee de
`transiciones_prohibidas()`. La equivalencia la garantiza
`tests/test_revision_reglas.py::test_transiciones_prohibidas_derivadas_coinciden_con_el_modelo`.
Si cambias los horarios de un turno, ese test es el que avisa.
**Nota:** `modelo_base.NO_MORNING_AFTER` solo conoce las dos transiciones que
terminan en Mañana — es del motor heurístico y **no** es la regla completa.
**Excepciones:** ninguna.

### 1.5 Roles sin noches
**Regla:** ciertos roles nunca hacen Noche.
**Código:** `_construir_modelo` → `if role in cfg.no_night_roles: model.Add(x[name, d, "Noche"] == 0)`.
**Parámetros:** `cfg.no_night_roles`. En la app se reconstruye desde el flag
`sin_noches` de `data/roles.json` (`app._get_dept_config`); `night_fixed` queda
siempre excluido de esa lista. Recepción: `jefe`, `subjefe`, `adjunto_recepcion`.
**Excepciones:** ninguna.

### 1.6 Días libres forzados (vacaciones, peticiones concedidas, LA)
**Regla:** los días ya concedidos son `Libre`, sin discusión.
**Código:** `_construir_modelo` → `forced_libre`, luego `model.Add(x[name, d, "Libre"] == 1)`.
**Parámetros:** `person["forced_days_off"]` + `person["extra_days_off"]` (LA,
"libres abonables"). `person["dias_vacaciones"]` **no** fuerza nada por sí mismo:
solo distingue el ORIGEN (vacaciones vs petición) — ver [§1.8](#18-patrón-de-días-libres-semanales-2-consecutivos) y [§2.11](#211-j-libres-debidos-antes-de-vacaciones).
**Excepciones:** ninguna. Ver [§4](#4-reglas-no-negociables).

### 1.7 Roles con turno fijo y con días libres fijos
**Regla:** el jefe hace Mañana todos los días que trabaja y libra Sábado y Domingo.
El `night_fixed` hace Noche todos los días que trabaja y libra sus dos días fijos.
**Código:** `_construir_modelo`, restricciones #4 y #5 (+ los dos bloques gemelos
para `night_fixed`). Los días forzados a Libre tienen prioridad: el turno fijo solo
se aplica `if d not in forced_libre[name]`.
**Parámetros:** `cfg.fixed_roles` (`{"jefe": "Mañana"}`), `cfg.fixed_days_off`
(`{"jefe": [5, 6]}`), `cfg.night_fixed_days_off` (`[3, 4]` en Recepción).
Todos parcheados desde `data/roles.json` por `app._get_dept_config`.
**Excepciones:** ninguna.

### 1.8 Patrón de días libres semanales — 2 consecutivos
**Regla:** cada persona sin garantía estructural propia de libres recibe
**exactamente 2 días `Libre`, consecutivos**. Ni más ni menos.
**Código:** `_construir_modelo`, restricción #7 (`motor_cpsat.py` ~línea 852 en adelante).
**Parámetros:** `DIAS_LIBRES_SEGUIDOS_POR_SEMANA = 2`.
**Roles excluidos** (ya tienen su propia garantía dura, no se apila otra):
`role == "night_fixed"`, `cfg.fixed_days_off.get(role)`, `cfg.fixed_roles.get(role)`,
`role in cfg.presencia_roles` (ver la excepción de presencia parcial más abajo).
Hoy en Recepción eso es solo `jefe` y `night_fixed`; aplica a `normal`,
`subjefe` y `adjunto_recepcion`.

Esta restricción tiene **cuatro caminos distintos** según la situación de la persona,
y es la parte del motor donde más fácil es asumir algo falso:

| Situación | Qué exige el código |
|---|---|
| Caso normal (sin peticiones ni bloqueos) | Exactamente 2 Libres entre los días no forzados, y consecutivos si existe algún par consecutivo disponible; si no existe ninguno, 2 sueltos. |
| **1 petición concedida** | La petición **cuenta como uno de los 2**. El otro debe ser un día **adyacente** a la petición si hay alguno disponible. Total exigido en días no forzados: 1. |
| **2 peticiones concedidas** | Ambas cuentan como los 2. **No** se exige que sean consecutivas entre sí. Total exigido en días no forzados: 0 (los demás días se trabajan). |
| **3 o más peticiones** | El patrón ya está cubierto de sobra; el código solo impide libres ADICIONALES (`sum(Libre en el resto) == 0`). |
| Bloqueo externo de Noche que acaba el Sábado | El Domingo queda forzado `Libre` y **cuenta como uno de los 2**; el otro se coloca donde sea, sin exigir consecutividad. |
| Menos de 2 días no forzados (vacación de 6–7 días) | La restricción se omite entera para esa persona esa semana. |

**Vacaciones vs peticiones — la distinción crítica:** los días de **vacaciones**
(`dias_vacaciones` no vacío) se apilan APARTE y no consumen el patrón: quien tenga
vacaciones sigue necesitando sus 2 libres consecutivos en el resto de la semana.
Los días de **petición** SÍ consumen el patrón. El código separa ambos con
`dias_peticion = (forced_libre - dias_vacaciones_idx) - dias_bloqueados_persona`.
⚠️ Es la parte del motor donde más fácil es asumir algo falso: que una petición se
apila aparte de los 2 libres, como sí hacen las vacaciones. No es así.

**Excepción deliberada — presencia parcial (`dias_presencia` → `cfg.presencia_roles`):**
una categoría con días de presencia (p.ej. refuerzo de finde `[5, 6]`, cobertura de
3 días sueltos) tiene **trabajo garantizado** en esos días y queda `Fuera` el resto
de la semana. No recibe el patrón de 2 libres — puede acabar la semana con 0. Es el
contrato "trabaja siempre" confirmado con el usuario: **solo una vacación/petición
concedida (`forced_libre`) puede librar un día de presencia**; el solver no puede
elegir `Libre` ahí (`model.Add(x[name, d, "Libre"] == 0)`). Configuración en
`data/roles.json` (`herramientas.py`, MÓDULO 6); excluida estructuralmente de la
equidad de findes y del registro de festivos trabajados. Invariantes cubiertos en
`tests/test_presencia_cpsat.py`.

### 1.9 Cobertura nocturna fijada externamente
**Regla:** quién hace Noche cada día **no lo decide el solver**. Lo decide
`cobertura_nocturna.py` antes de construir el modelo, y se clava como restricción dura.
**Código:** `generar_semana_cpsat_con_cobertura_nocturna` →
`model.Add(x[nombre, d, "Noche"] == 1)` para cada `(día, nombre)` de `cobertura_externa`.
**Orden de rotación — SIEMPRE fijo y cíclico** (confirmado con el usuario):
`empleado A → B → C → A…`, recorriendo `estado_rotacion["orden"]` desde
`siguiente_turno`, **con independencia de que exista o no el rol `night_fixed`**
(`_elegir_orden_fijo`). Quien queda saltado por vacación/petición acumula
`deudas[n] += 1` y recupera turno en cuanto vuelve a estar disponible.

- **Configuración:** la lista `orden` vive en `data/cpsat_estado_{dept}.json`. Se siembra
  con el orden de la plantilla, restringida a los empleados que pueden cubrir noches (ni
  `night_fixed` ni ningún rol de `cfg.no_night_roles`) — hoy en Recepción los 3 `normal`.
  Se edita desde la **GUI, sección "Ronda de noches", solo para admin**
  (permiso `gestionar_rotacion_noches`, exclusivo del rol admin en `auth.PERMISOS`):
  `GET/PUT /api/rotacion-noches`. El PUT exige una permutación EXACTA de los elegibles
  —ni faltas, ni repetidos, ni quien no pueda hacer noches— en vez de reconciliar en
  silencio, para que un orden incompleto enviado por error no deje a nadie fuera de la
  ronda sin que se note. Al reordenar se **conserva `siguiente_turno`** si sigue en la
  lista: cambiar el anillo no debe saltarse ni repetir a quien ya le tocaba.
- **Altas y bajas:** `cobertura_nocturna.reconciliar_orden`, llamada en cada generación
  desde la orquestación. El alta entra **al final del ciclo y sin recuperar atrasos**;
  la baja sale de la lista y recoloca `siguiente_turno`.
- **Escape:** `usar_orden_fijo: false` vuelve al reparto por balance histórico puro.

Con 3 personas y bloques de 5 noches el ciclo da **10 días de descanso entre bloque y
bloque** para todos — el máximo posible (ciclo de 15 días: 5 trabajando + 10 libres).

> **Antes de esta sesión el orden fijo era código muerto en producción.** Como la
> plantilla real no tiene `night_fixed`, el modo respaldo está siempre activo y caía en
> `_elegir_balance_puro`. El ciclo que se observaba era correcto *por accidente*: tras
> un bloque de 5 noches el contador `covers` sube 5 y esa persona pasa al final de la
> cola. Se deshacía en cuanto algo rompía la simetría — verificado: un empleado nuevo
> (`covers=0`) frente a veteranos (`covers=30`) encadenaba **12 bloques seguidos**, y a
> quien se saltaba por vacaciones **no se le anotaba deuda**, así que perdía el turno
> sin compensación. Además `estado_rotacion["orden"]` se ignoraba (el orden real salía
> alfabético) y `incorporar_empleado_orden`/`dar_de_baja_empleado_orden` no las llamaba
> nadie, así que la lista se quedaba obsoleta al cambiar la plantilla.

**Dos modos** (`cobertura_nocturna.modo_respaldo_activo`) — afectan a la LONGITUD de los
bloques y a cuántas veces se selecciona por semana, **ya no al criterio de selección**:
- **Normal** — existe un `night_fixed` en plantilla y no lleva ≥2 semanas ausente.
  Una sola llamada de selección por semana; el elegido cubre los
  `cfg.night_fixed_days_off` del titular.
- **Respaldo** — no hay `night_fixed`, **o** `ausencias_night_fixed_consecutivas >= 2`.
  Se asignan **bloques de 4–5 noches seguidas** (`BLOQUE_RESPALDO_MIN = 4`,
  `BLOQUE_RESPALDO_MAX = 5`), una llamada de selección por día, con el bloque
  persistido entre semanas en `estado_rotacion["bloque_actual"]`.
  **La plantilla real de Recepción no tiene `night_fixed`, así que el modo respaldo
  está SIEMPRE activo en producción.**
**Excepciones:** un bloque se corta a mitad si su titular queda bloqueado ese día por
vacación/petición (`persona_bloqueada_dia`) o por descanso post-racha.

**Bloque obsoleto:** al reanudar `bloque_actual` de la semana anterior se comprueba que
esa persona tuviera Noche el domingo pasado (`noches_ultimos_5_dias[nombre][-1]`); si
no, el bloque se descarta. `bloque_actual` solo lo mantiene la rama de modo respaldo, así
que unas semanas de modo normal en medio lo dejan **congelado** — y reanudarlo clava una
noche suelta a alguien cuya última noche fue semanas atrás, lo que con `MIN_NOCHES_RACHA=2`
vuelve la semana entera **INFEASIBLE**. Traza real del escenario de alternancia que lo
destapó: `sem 5 → bloque_actual={N6,5,4}` … `sem 8 → sigue {N6,5,4}` aunque N6 no hizo
ninguna noche en las semanas 6-8.

### 1.10 Racha de noches
**Regla:** tres límites sobre las noches de cualquier persona (no solo del `night_fixed`):
1. Máximo **5 noches en cualquier ventana de 6 días** consecutivos, incluido el
   empalme con la semana anterior.
2. Tras agotar una racha de 5 noches, **2 días de `Libre` obligatorio** (no "otro
   turno cualquiera": `Libre`).
3. **Mínimo 2 noches seguidas** — nadie cubre una noche suelta aislada.
**Código:** `agregar_restriccion_racha_noches` (`motor_cpsat.py:2074`), llamada
DESPUÉS de `_construir_modelo` y del `Minimize`.
**Parámetros:** `MAX_NOCHES_VENTANA = 5`, `VENTANA_NOCHES = 6`,
`DIAS_DESCANSO_TRAS_RACHA = 2`, `MIN_NOCHES_RACHA = 2`.
**Excepciones:**
- El descanso de 2 días que no cabe dentro de la semana se traslada a la siguiente vía
  las variables `post1/post2` → `forced_days_off_extra` ([§5](#5-estado-entre-semanas)).
- La regla conservadora "el Domingo no puede arrancar un bloque nuevo aislado"
  (`x[name,6,"Noche"] <= x[name,5,"Noche"]`) **no** se aplica a quien el llamador
  sabe que continúa bloque la semana siguiente (`personas_bloque_continua`), ni a
  ningún `night_fixed` (se exime en `generar_semana_cpsat_con_cobertura_nocturna`).

---

## 2. Términos BLANDOS del objetivo

El solver minimiza la suma de todos. Solo desempatan entre soluciones que ya cumplen
todas las restricciones duras. Orden de prioridad **por peso real**, de mayor a menor:

| | Término | Peso | Qué prefiere |
|---|---|---|---|
| **L** | `PESO_ORDEN_EXCESO` | **20000** | No exceder un máximo mientras el otro turno tenga sitio |
| **K** | `PESO_EXCESO_COBERTURA` | **5000** | No superar el máximo de Mañana/Tarde |
| **G** | `PESO_COBERTURA_AUSENCIA` | **1000** | Que el cubridor ocupe el turno del ausente |
| **M** | `PESO_RACHA_TRABAJO` | **300** | No encadenar más de 6 días trabajados, cruzando semanas |
| **E** | `PESO_FINDE_ROL_EXCLUIDO` | **200** | Que un rol excluido de equidad no libre el finde entero |
| **G′** | `PESO_COBERTURA_DIAS_LIBRES_FIJOS` | **200** | Cubrir también los días libres FIJOS del cubierto |
| **A** | `PESO_EQUIDAD_FINDES` | **100** (×coef) | Findes libres para quien menos ha tenido |
| **B** | `PESO_ROTURAS_CONTINUIDAD` | **50** | No cambiar de turno a mitad de semana |
| **I** | `PESO_SOBRECOBERTURA_DIURNA` | **30** | No apilar gente en Mañana/Tarde por encima del mínimo |
| **D** | `PESO_USO_PARTIDO` | **20** | Usar Partido lo menos posible |
| **F** | `PESO_DOMINGO_SUSTITUTO` | **15** | Que el día suelto del sustituto sea el Domingo |
| **J** | `PESO_LIBRE_DEBIDO_VACACIONES` | **15** | Libres pegados al inicio de un bloque de vacaciones |
| **H** | `PESO_COMPLEMENTARIO_JEFATURA` | **10** | Que jefe y subjefe no coincidan de turno |
| **C** | `PESO_DESEQUILIBRIO_HISTORICO` | **1** (×conteo) | El turno que menos ha hecho cada persona |

La prioridad de negocio declarada para el trío diurno es **B > I > D**: preservar la
continuidad gana sobre evitar sobre-cobertura, que a su vez gana sobre evitar Partido.
Con I(30) > D(20), el solver prefiere mandar el excedente a Partido antes que apilarlo
en Mañana/Tarde — mientras Partido tenga hueco, porque su máximo es duro.

### 2.1 A — Equidad de findes libres
Coste `(ratio − ratio_medio) × 1000 × 100` sobre la variable "finde completo libre".
Negativo (recompensa) para quien está por debajo de la media. Datos de
`modelo_base._get_weekend_ratios`.
**Quién entra:** función local `_entra_en_equidad` — quedan fuera los roles de
`cfg.roles_excluidos_equidad` **y** los que tienen el finde garantizado por
restricción dura (`{5,6} ⊆ días fijos`), tanto del término como del cálculo de la media.
⚠️ Son **dos** criterios de exclusión, no uno: el rol excluido de equidad y el finde
garantizado por restricción dura.

### 2.2 B — Roturas de continuidad
Penaliza cambiar de turno entre **dos días consecutivos ambos trabajados**. Solo
participan `Mañana/Tarde/Partido` (`DAY_SHIFTS`): Noche y Libre no.
**Decisión de negocio confirmada:** cambiar de turno **a través del bloque de días
libres es GRATIS**. El bloque de libres es exactamente donde se permite rotar.
Anclado por `tests/test_revision_reglas.py::test_cambio_de_turno_a_traves_de_los_libres_no_se_penaliza`.

**Cruza la frontera de semana** (añadido en esta sesión). El bucle principal va de
`d=1` a `d=6`, así que no compara el domingo anterior con el lunes actual; un bloque
aparte lo cubre usando `prev_sunday_shifts`, con el **mismo peso 50**. Como el domingo
anterior es un dato constante, la rotura se calcula sin el patrón `work_both/keep`:
`rotura = trabaja_el_lunes − mantiene_el_mismo_turno`.
Si el domingo previo fue `Libre` o `Noche`, el término **no se añade** — volver de los
días libres con otro turno sigue siendo gratis.

Antes del arreglo, medido sobre la plantilla real en 4 semanas encadenadas:

| | Antes | Después |
|---|---|---|
| Continuidad dentro de la semana | 100 % (103/103) | 99 % (104/105) |
| Continuidad Domingo→Lunes | **42 %** (5/12) | **91 %** (10/11) |

42 % estaba apenas por encima del azar entre tres turnos diurnos (33 %), porque nada lo
penalizaba. Anclado por `::test_continuidad_cruza_la_frontera_de_semana` y
`::test_el_cruce_de_semana_es_gratis_si_el_domingo_anterior_fue_libre`.

### 2.2b M — Días trabajados seguidos cruzando semanas
**Regla:** preferir no encadenar más de `MAX_DIAS_TRABAJADOS_SEGUIDOS = 6` días
trabajados, **contando el arrastre de la semana anterior**.
**Por qué hace falta:** el patrón de 2 libres ([§1.8](#18-patrón-de-días-libres-semanales-2-consecutivos))
es **por semana**, así que librar Lun+Mar una semana y Sáb+Dom la siguiente encadena
hasta 10 días trabajados sin romper ninguna regla dura. Medido antes del arreglo sobre
la plantilla real en 4 semanas: **Maira 9 días seguidos, Carmen 8**. Después: **máximo 6**.
**Código:** `_construir_modelo`, ventana deslizante sobre
`[1]*r + [work_var[name, d] for d in range(7)]`, donde `r` = días trabajados seguidos al
cierre de la semana anterior. Penaliza cada ventana de 7 días enteramente trabajada.
Las ventanas que caen íntegramente en la semana anterior se saltan (son constantes).
**Dato de entrada:** `dias_trabajados_previos`, derivado del historial real en
`app._derivar_estado_racha_cpsat_desde_historial` — contando hacia atrás desde el
domingo hasta el primer día no trabajado, igual que `noches_ultimos_5_dias` y por el
mismo motivo (una edición manual queda reflejada sin recalcular nada).
**Peso:** `PESO_RACHA_TRABAJO = 300`, entre E(200) y G(1000).
**Blando a propósito:** el arrastre de la semana anterior ya no se puede cambiar, así
que como restricción dura podría volver INFEASIBLE una semana entera por algo ya
decidido. Anclado por `::test_racha_de_dias_trabajados_no_cruza_de_6_entre_semanas` y
`::test_racha_de_trabajo_es_blanda_no_vuelve_infactible`.

### 2.3 C — Desequilibrio histórico
`peso × conteo_histórico × x[...]` por turno diurno. Datos de `_get_shift_counts`.

### 2.4 D — Uso de Partido
Coste plano por cada celda de Partido.

### 2.5 E — Rol excluido de equidad que libra el finde entero
Solo para roles en `cfg.roles_excluidos_equidad` que **no** tengan libres garantizados
por regla dura (`cfg.fixed_days_off` o `night_fixed`). Hoy: `subjefe`.

### 2.6 F — Domingo suelto del sustituto nocturno
Solo si `cfg.prefer_next_week_for_cover` (mozos) **y** se indicó el sustituto único
**y** (gating estructural) no existe ningún par de días consecutivos disponible para
esa persona (`_tiene_par_consecutivo_disponible`). Si hay par disponible, el término
**ni se añade** a la suma: no compite con nada.

### 2.7 G — Cobertura de ausencia de otro rol
Penaliza que el cubridor no esté en el turno fijo del cubierto durante sus ausencias.
"Ausente" = el rol no existe, o tiene ese día en `forced_days_off`/`extra_days_off`.
**Parámetros:** `cfg.cobertura_roles` desde `data/roles.json` (`cubre_ausencia_de`).
Hoy: `subjefe` cubre a `jefe` en Mañana.
Si además `semana_completa` está activo y la ausencia es de semana entera, se añade
un coste extra de 1000 por librar Sábado o Domingo.

### 2.8 G′ — Días libres FIJOS del cubierto
Los días libres fijos del jefe (Sáb+Dom) se cubren también, pero a **200**, no a 1000:
se repiten todas las semanas y cobrarlos como una ausencia real dejaría al subjefe sin
librar un fin de semana casi nunca. Se activa con `cubrir_dias_libres_fijos` en
`data/roles.json` (hoy `true` para `subjefe`). Devuelto en un conjunto aparte por
`_dias_cobertura_ausencia`, que **excluye** los días que ya son ausencia real (no se
cobra dos veces).

### 2.9 H — Turno complementario jefe/subjefe
En los días en que ambos trabajan, penaliza que coincidan en el mismo turno no-Libre.
Solo si ambos roles existen en la plantilla. Mutuamente excluyente con G por
construcción: los días que el subjefe cubre, el jefe está Libre.

### 2.10 I — Sobre-cobertura diurna
`30 × (cobertura_real − min_coverage)` por día para Mañana y Tarde. No incluye Noche
(la decide la orquestación) ni Partido (es la válvula de escape que I empuja a usar).

### 2.11 J — Libres debidos antes de vacaciones
Cuando un bloque de **vacaciones** (no una petición: se distingue con
`dias_vacaciones`, ver `_inicio_bloque_vacaciones`) empieza a mitad de semana,
prefiere colocar libres pegados a su inicio, según los días trabajados antes:
`0-1 → 0 libres`, `2-4 → 1 libre`, `5-6 → 2 libres`. Modelado con dos booleanos de
umbral (`step1`, `step2`) porque el conteo depende de lo que resuelva el solver.
No aplica si el bloque empieza en Lunes.

### 2.12 K — Exceso de cobertura en Mañana/Tarde
`exceso = max(0, cobertura_real − max_coverage)`, penalizado a 5000 por persona de más.
Lineal: 2 de más cuesta el doble que 1. Solo Mañana y Tarde ([§1.3](#13-cobertura-máxima-dura-solo-en-noche-y-partido)).
Superar el máximo debe compensar solo cuando la alternativa es no encontrar ninguna
solución. Cuando ocurre, `app._calcular_advertencias` lo reporta con el detalle de
cuántas personas sobran.

### 2.13 L — Orden del exceso
**Regla de negocio:** no se supera el máximo de un turno mientras el **otro** turno de
máximo blando todavía tenga sitio. *"De nada sirve exceder el máximo de Tarde cuando
Mañana aún no lo ha alcanzado."* Hay que llenar Mañana y Tarde **hasta sus dos
máximos** antes de que ninguno de los dos pueda pasarse.
**Código:** `_construir_modelo`, justo tras el bucle de cobertura. Por día construye
`hueco[s,d] = max(0, mx − total)` con `AddMaxEquality` (igualdad exacta, no
desigualdad: el hueco entra en un AND penalizado, sin presión propia hacia abajo), los
booleanos `hay_exceso[d]` / `hay_hueco[d]`, y penaliza su AND.
**Peso:** `PESO_ORDEN_EXCESO = 20000` — 4× K y por encima de G, así que el orden
correcto gana siempre que exista alternativa legal.

**Por qué es BLANDO y no duro** (decisión deliberada, documentada para que nadie lo
"endurezca" luego): con K=5000 por persona de más, una distribución en mal orden ya es
**siempre más cara** que la equivalente bien ordenada, porque necesita más exceso total
para colocar a la misma gente. Medido en el escenario de 12 personas (topes 3/3):

| Lunes M/T | Coste | Orden |
|---|---|---|
| 3+4, 4+3, 4+4 | 20 680 | ok |
| 2+5 | 45 780 | mal |
| 2+4 | 45 800 | mal |

Es decir: **el solver nunca elige el mal orden por conveniencia**; solo puede caer en
él cuando está *estructuralmente forzado* — típicamente porque quien podría ir a Mañana
viene de Tarde o Noche el día anterior y la regla de descanso de 9 h ([§1.4](#14-descanso-mínimo-de-9-horas-entre-turnos-de-días-consecutivos))
se lo impide. Como restricción dura, ese caso pasaría de *"horario válido con un aviso"*
a *INFEASIBLE y semana entera sin generar* — exactamente el fallo que el término K
existe para evitar. Anclado por
`tests/test_revision_reglas.py::test_el_exceso_respeta_el_orden_ambos_al_maximo_antes_de_exceder`
y `::test_orden_del_exceso_es_preferencia_dominante_no_restriccion_dura`.

---

## 3. Mecanismos del solver

| Mecanismo | Valor | Dónde |
|---|---|---|
| Límite de tiempo del modelo principal | `MAX_TIME_SOLVER = 20.0` s | `motor_cpsat.py:1758` |
| Parada por meseta | `GRACIA_SIN_MEJORA = 3.0` s sin mejora | `_resolver_con_parada_por_meseta` |
| Determinismo | `num_search_workers = 1` | `_resolver_y_extraer`, `_resolver_y_extraer_con_extra` |
| Determinismo (orden de restricciones) | `sorted()` sobre los sets de turnos y sobre el núcleo del diagnóstico | restricción #2, `_reducir_a_nucleo_irreducible` |
| Límite del diagnóstico de infactibilidad | `TIEMPO_MAXIMO_DIAGNOSTICO_SEGUNDOS = 10.0` s | `motor_cpsat.py:1559` |
| Timeout HTTP del despliegue | 60 s | `gunicorn.conf.py` |

**Parada por meseta:** `Solve()` corre en un hilo aparte; en cuanto pasan 3 s sin una
solución mejor se llama a `StopSearch()`. `max_time_in_seconds` sigue siendo la red de
seguridad si la meseta nunca llega. El peor caso (20 s modelo + 10 s diagnóstico) cabe
bajo los 60 s de gunicorn.

**Determinismo:** `num_search_workers=1` **no basta** por sí solo. Iterar un `set()`
depende del hash-seed aleatorio del proceso, lo que cambiaba el ORDEN en que se añaden
las restricciones y, ante un empate de valor óptimo, devolvía horarios distintos con la
misma entrada. Por eso los `sorted()`. No dependas de `PYTHONHASHSEED`.

**Rendimiento medido** (plantilla real, 9 personas, 6 semanas encadenadas):
media 4,5 s por semana, peor caso 6,9 s.

**Diagnóstico de infactibilidad:** cuando el modelo sale INFEASIBLE,
`_diagnosticar_infactibilidad` reconstruye un modelo reducido con las piezas
"cedibles" gateadas por asunciones (vacaciones/peticiones agrupadas en tramos,
cobertura mínima por día/turno, patrón de libres por persona), usa
`SufficientAssumptionsForInfeasibility()` y reduce el resultado a un núcleo
**irreducible** con una pasada del filtro de borrado
(`_reducir_a_nucleo_irreducible`). Las piezas estructurales (cobertura máxima de
Noche/Partido, roles sin noche, turno fijo, descanso de 9 h) se dejan siempre activas:
no son algo que un encargado pueda conceder. Solo diagnostica — nunca relaja nada.
Medido: 36 asunciones → 0,08 s.

---

## 4. REGLAS NO NEGOCIABLES

> **Esta sección existe para que ninguna sesión futura las "flexibilice" por su cuenta.
> Ninguna de estas cuatro se toca sin aprobación explícita del usuario en esa misma
> conversación. No basta con que "resolvería un INFEASIBLE": un INFEASIBLE es
> información legítima sobre un conflicto real, no un bug a silenciar relajando una
> de estas reglas.**

### 4.1 Los 2 días libres naturales consecutivos — DERECHO CONTRACTUAL
Cada persona sin garantía estructural propia recibe **exactamente 2 días libres, y
consecutivos**. No es una preferencia ni un objetivo de calidad: es un derecho
contractual del trabajador. La única excepción admitida, ya en el código, es que **no
exista ningún par consecutivo estructuralmente posible** esa semana, en cuyo caso se
exigen igualmente 2 días, sueltos.
**Nunca:** convertirla en término blando, bajarla a "al menos 1", ni permitir que el
solver dé 0 libres a alguien para cuadrar la cobertura.
`DIAS_LIBRES_SEGUIDOS_POR_SEMANA = 2`, restricción #7.

### 4.2 `max_coverage` de Noche y Partido — NUNCA superables
El tope de **Noche** y de **Partido** es duro y debe seguir siéndolo.
Ya se intentó una vez hacerlos blandos (junto con Mañana/Tarde) y produjo una
regresión seria: el solver se inventaba noches para colocar personal sobrante,
rompiendo la rotación por bloques y propagando "noches fantasma" a la semana
siguiente. Si vuelve a aparecer un INFEASIBLE por capacidad, la salida es
Mañana/Tarde (que ya son blandas), no estas dos.
Anclado por `tests/test_revision_reglas.py::test_excedente_de_plantilla_nunca_genera_noches_extra`
y `::test_max_coverage_blando_solo_en_manana_y_tarde`.

### 4.3 `min_coverage` de todos los turnos — DURA sin excepción
El servicio nunca puede quedar por debajo del mínimo en ningún turno ni ningún día.
Puede quedar por encima (problema de coste), nunca por debajo (problema de servicio).
Esto **no cambió** cuando el máximo pasó a blando en Mañana/Tarde, y no debe cambiar.
Anclado por `tests/test_max_coverage_blando.py::test_min_coverage_sigue_siendo_dura_reporta_infeasible`.

### 4.4 Vacaciones y peticiones ya concedidas — INTOCABLES
Un día ya concedido (`forced_days_off`, `extra_days_off`) es `Libre` y punto. El motor
nunca revoca una concesión para cuadrar cobertura. La decisión de denegar una petición
se toma ANTES, en `aplicar_peticiones_con_prioridad` (`app.py`), donde sí hay una
política explícita de prioridad y las denegadas se devuelven en la respuesta de la API.

### 4.5 Adicional: el descanso de 9 horas
No estaba en la lista pedida, pero se documenta aquí porque es de la misma naturaleza
(legal, no de calidad): las cuatro transiciones de [§1.4](#14-descanso-mínimo-de-9-horas-entre-turnos-de-días-consecutivos)
no se relajan. Si se cambian los horarios de un turno, hay que recalcular la tabla, no
borrar la restricción.

---

## 5. Estado entre semanas

### 5.1 Qué se PERSISTE en disco
| Qué | Dónde | Escrito por |
|---|---|---|
| `estado_rotacion` (cola de turnos de cobertura nocturna) + `semanas` (estado inicial/final de cada semana generada) | `data/cpsat_estado_{dept}.json` | `app._guardar_estado_cpsat` (`guardar_json_atomico`) |
| Horario de cada semana | `data/historial*.json` | `herramientas.registrar_semana` |
| Plantilla, roles, vacaciones, peticiones, festivos, cupos | `data/*.json` | `herramientas.py` |

Contenido de `estado_rotacion` (`cobertura_nocturna.py:20-62`):
- `orden` — orden fijo de rotación; `siguiente_turno` — a quién le toca.
- `deudas` — `{nombre: int}`, turnos saltados pendientes de recuperar.
- `usar_orden_fijo` — si es `False`, se ignora todo lo anterior y se usa balance puro.
- `historico_balance` — `{nombre: {covers, pending}}`.
- `ausencias_night_fixed_consecutivas` — dispara el modo respaldo a partir de 2.
- `bloque_actual` — `{nombre, noches_totales, noches_cubiertas}`, bloque de respaldo en
  curso que cruza el fin de semana.
- `descanso_pendiente_inicial` — `{nombre: int}`, días de descanso post-racha que
  arrastra la semana nueva.

Los tres últimos los mantiene **`motor_cpsat`**, no `cobertura_nocturna`.

**La ronda avanza una vez por SEMANA, no una vez por clic en "Generar".** El fichero
guarda además `semanas: {"YYYY-MM-DD": {"inicial", "final"}}`: el estado con el que se
generó cada semana y el que quedó al terminarla. Al generar `week_start` se parte, por
este orden, de (1) el `inicial` ya registrado para esa semana, (2) el `final` de la
semana anterior, (3) el `estado_rotacion` vigente o la semilla
(`app._estado_rotacion_base_semana`). Así **regenerar la misma semana da el mismo
resultado**: si esta semana les toca a dos compañeros concretos, siguen siendo ellos por
muchas veces que se pulse Generar. Antes solo existía un `estado_rotacion` "el último",
y cada regeneración volvía a avanzar `siguiente_turno` y a sumar `historico_balance`, así
que la ronda corría sola. Excepción deliberada: `orden` y `usar_orden_fijo` se toman
siempre del estado vigente —son configuración del admin (`PUT /api/rotacion-noches`) y
deben aplicarse en la próxima generación—; lo que rebobina son los contadores. Se
conservan las últimas `MAX_SEMANAS_ESTADO_ROTACION` (120) semanas.

### 5.2 Qué se DERIVA del historial real en cada generación
`app._derivar_estado_racha_cpsat_desde_historial` recalcula **en cada llamada**, desde
el horario realmente guardado de la semana anterior (`week_start − 7 días`):

- **`noches_ultimos_5_dias`** — `[turnos[d] == "Noche" for d in range(2, 7)]`, es decir
  Miércoles→Domingo de la semana anterior. Es el empalme de la restricción de racha.
- **`forced_days_off_extra`** — descanso post-racha que se desborda:
  `turnos[1:6] == ["Noche"]*5` (Mar–Sáb) → Lunes libre;
  `turnos[2:7] == ["Noche"]*5` (Mié–Dom) → Lunes y Martes libres.
- **Compensación Lun+Mar** del sustituto, solo si `cfg.prefer_next_week_for_cover` y
  **exactamente una** persona cubrió todos los `cfg.night_fixed_days_off`.
- **`dias_trabajados_previos`** — días trabajados seguidos al cierre de la semana
  anterior, contando hacia atrás desde el domingo hasta el primer día no trabajado.
  Entrada del término M ([§2.2b](#22b-m-días-trabajados-seguidos-cruzando-semanas)).

Además, `app.api_generar` calcula **`prev_sunday_shifts`** (turno del domingo anterior
por persona) desde el mismo historial y lo pasa al motor. Alimenta dos cosas: la regla
dura de descanso de 9 h ([§1.4](#14-descanso-mínimo-de-9-horas-entre-turnos-de-días-consecutivos))
y el cruce de continuidad del término B ([§2.2](#22-b-roturas-de-continuidad)).

**Por qué se deriva en vez de cachearse:** estos valores SÍ se persistían antes, y se
desincronizaban en cuanto alguien editaba un turno a mano (`api_editar_turno`): el
caché no se enteraba y la semana siguiente heredaba una "Noche fantasma" inexistente.
Derivándolos del historial real —que la edición manual sí actualiza— la clase entera
de bug desaparece. **No reintroduzcas un caché de estos dos valores.**
`estado_rotacion` sí se persiste porque es una cola de turnos, no un hecho observable
en el horario.

### 5.3 Configuración efectiva
`app._get_dept_config(dept_id)` parte de `modelo_recepcion.RECEPCION_CONFIG` /
`modelo_mozos.MOZOS_CONFIG` y los parchea con:
1. `fixed_roles`, `fixed_days_off`, `night_fixed_days_off`, `night_cover_compensation`,
   `no_night_roles`, `cobertura_roles`, `roles_excluidos_equidad` ← `data/roles*.json`
2. `min_coverage`, `max_coverage` ← `data/coverage_config.json` (perfil de temporada activo)

**Los valores de `modelo_recepcion.py` / `modelo_mozos.py` son solo fallback.** Quien
llame a `motor_cpsat` directamente (tests, scripts) **no** pasa por este parcheo y
usará los valores estáticos, que pueden diferir de producción.

---

## 6. Limitaciones conocidas y trampas de mantenimiento

Puntos donde el código es correcto pero frágil, o donde una lectura ingenua lleva a
conclusiones falsas. Es lo primero que conviene mirar antes de tocar el motor.

### 6.1 Los docstrings ya no narran la historia del módulo
Durante el desarrollo por fases, `motor_cpsat.py` y `cobertura_nocturna.py` acumularon
cabeceras que eran un registro cronológico de cambios, y varias de sus afirmaciones
habían quedado desmentidas por el propio código —entre otras, que `motor_cpsat` era «un
prototipo aislado que no toca app.py» cuando es el motor de producción—. Esas cabeceras
se reescribieron como descripción de arquitectura y **este documento es la fuente única
de las reglas de negocio**. Si cambias una regla, actualiza aquí: no vuelvas a
convertir los docstrings en un changelog.

### 6.2 Las cuatro transiciones de descanso están escritas a mano
`_construir_modelo` codifica literalmente `Tarde→Mañana`, `Noche→Mañana`, `Noche→Tarde`
y `Noche→Partido`; **no** las lee de `herramientas.transiciones_prohibidas()`. Si
cambias el horario de un turno en `data/horarios_turnos.json`, el modelo no se entera
solo. Lo que impide que ambas definiciones diverjan es
`tests/test_revision_reglas.py::test_transiciones_prohibidas_derivadas_coinciden_con_el_modelo`.

### 6.3 `BLOQUE_RESPALDO_MAX` duplica un valor a mano
`cobertura_nocturna.BLOQUE_RESPALDO_MAX` (5) repite el valor de
`motor_cpsat.MAX_NOCHES_VENTANA`. No se puede importar sin crear un ciclo
(`motor_cpsat` importa de `cobertura_nocturna`, no al revés), así que hay que
mantenerlos sincronizados manualmente.

### 6.4 El modelo de diagnóstico es una aproximación
`_construir_modelo_diagnostico` reproduce las restricciones duras de fase 1, pero:
- **no** incluye las noches ya fijadas por la orquestación (`dias_bloqueados_externos`),
  así que un INFEASIBLE originado ahí cae en el mensaje genérico "no se pudo reproducir
  la infactibilidad";
- modela el patrón de libres en su forma simple (conteo exacto + par consecutivo), sin
  replicar las ramas de peticiones de [§1.8](#18-patrón-de-días-libres-semanales-2-consecutivos).

### 6.5 `validar_viabilidad` depende del motor activo
`app.api_generar` la llama **antes** de elegir motor, y su fórmula de capacidad trata
`max_coverage` como techo duro. Como en CP-SAT el máximo de Mañana/Tarde es blando, hay
que pasarle `turnos_sin_techo`; si no, rechaza con "Personal excesivo" semanas que el
motor sí resuelve. `api_generar` solo lo pasa cuando `motor_cpsat_activo()`, para que el
motor heurístico conserve su comportamiento. Si algún día se añaden más turnos blandos,
esta llamada hay que revisarla.

### 6.6 La configuración efectiva no está en el código
`modelo_recepcion.py` / `modelo_mozos.py` son **solo fallback**. Los valores reales
salen de `data/roles*.json` y `data/coverage_config.json` vía `app._get_dept_config`
(ver [§5.3](#53-configuración-efectiva)). Quien llame a `motor_cpsat` directamente
—tests, scripts— no pasa por ese parcheo y usa los estáticos, que pueden diferir de
producción. Además `data/` **no está versionado**: los ajustes hechos en una máquina no
viajan con el repositorio.

### 6.7 El orden de las restricciones afecta al resultado
Con empates en el valor óptimo, el orden en que se añaden las restricciones decide qué
solución se devuelve. Por eso hay `sorted()` sobre conjuntos que de otro modo se
iterarían en orden dependiente del hash-seed del proceso. No sustituyas esos `sorted()`
por iteración directa de un `set()` aunque parezca equivalente.
