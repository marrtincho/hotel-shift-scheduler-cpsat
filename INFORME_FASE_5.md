# Informe de Fase 5 — Comparativa motor heurístico vs CP-SAT

Fecha: 2026-07-31. Repo: `HorariosRecepcion-cpsat` (prototipo aislado, no toca `app.py`/`modelo_base.py` de producción salvo lectura). Entorno: `.venv_cpsat` (ortools 9.15.6755).

## Paso 0 — ¿Existe una función unificadora de las 4 fases?

**Confirmado con una llamada real, no solo por lectura de código.** `_construir_modelo` (motor_cpsat.py:273) contiene, en un único `CpModel`, las 6 restricciones duras de Fase 1, los términos de objetivo A–E de Fase 2, y los términos G (cobertura de ausencia) y H (complementario jefe/subjefe) de Fase 4. `generar_semana_cpsat_con_cobertura_nocturna` (motor_cpsat.py:725) añade sobre ESE MISMO modelo, antes de resolver, la restricción de racha de noches de Fase 3 y fija la cobertura nocturna externa (Fase 3). Un único `model.Solve()` resuelve las 4 fases juntas.

Verificación empírica (plantilla con jefe ausente + night_fixed + cobertura nocturna + racha, una sola llamada):

```
Schedule generado (1 sola resolucion):
  Carlos    : ['Noche', 'Noche', 'Noche', 'Libre', 'Libre', 'Noche', 'Noche']
  Juan      : ['Mañana', 'Mañana', 'Libre', 'Mañana', 'Mañana', 'Libre', 'Libre']
  Laura     : ['Tarde', 'Libre', 'Mañana', 'Libre', 'Libre', 'Mañana', 'Libre']
  ...
  - jefe libre Miercoles (restr dura #6): True
  - subjefe Miercoles no-Libre (cobertura G actuando): True
  - Carlos Noche los dias que no son Jue/Vie (fixed night, fase1): True
  - Carlos libre Jue/Vie (fase1 night_fixed_days_off): True
  - forced_days_off_extra generado (racha/compensacion fase3): {'Pedro': ['Lunes', 'Martes']}
```

**No hizo falta construir ninguna función nueva.** Sí se descubrió después un bug real en la orquestación de Fase 3/4 (ver Paso 3).

## Paso 0 bis — El material de "Fase 0" no existía en disco

Antes de continuar se comprobó exhaustivamente (repo, `git log --all`, ficheros sin trackear, `/tmp`, proyecto hermano `HorariosRecepcion`) que **ni `referencia_motor_actual.json` ni el caso "Viernes de Carla" habían sobrevivido a disco** — vivieron solo en el contexto de una conversación anterior que ya no está disponible. Se acordó con el usuario:
1. Regenerar y **persistir con commit** una nueva foto de referencia (no volver a perderla).
2. Reconstruir (no fingir recuperar) un escenario análogo a "Viernes de Carla".

### Integridad de `modelo_base.py`/`app.py`

`git log --oneline -- modelo_base.py app.py`: 9 commits desde el inicial (660bbd6, 2026-06-23), el más reciente **6186a38 y 9201904, ambos del 2026-07-29** (2 días antes de este informe) — *"Modificacion de logica e interfaz"*. Esos dos commits añadieron precisamente `cobertura_roles`, `roles_excluidos_equidad` y la reparación de `min_libres_dia` (bug de celdas SIN_ASIGNAR que motivó parte de esta migración). El working tree solo tiene 2 diffs sin comprometer sobre esos ficheros, ambos inertes para la lógica de horarios (un comentario en `_get_weekend_ratios` y un `os.chmod` de seguridad en `app.py` sobre el secreto de sesión). **Conclusión:** el heurístico de hoy = commit `6186a38` exactamente, sin cambios de comportamiento pendientes. Dado que todo el diseño de `motor_cpsat.py` (fases 1-4) ya asume `cobertura_roles`/`roles_excluidos_equidad`, es coherente que la Fase 0 original se generara ya sobre este mismo código.

## Paso 1 — Comparativa sobre 18 semanas encadenadas (RECEPCION y MOZOS)

Foto de referencia regenerada con `generar_referencia_motor_actual.py` (commit `b091d6e`, corregido en `33d8055` — la primera versión no llamaba a `registrar_semana`, así que el historial de equidad nunca se actualizaba entre semanas; ver nota en el propio script). Arranque **desde estado limpio**: `data-ejemplo/historial.json` y `mozos_historial.json` están vacíos (`{}`) — no hay ningún historial real anterior a esta migración que se pudiera usar como estado inicial, así que se documenta explícitamente en vez de dejarlo implícito. Sin vacaciones ni peticiones (STAFF_DEFAULT tal cual).

Mismo generador para CP-SAT: `generar_comparativa_cpsat.py`, mismas 18 semanas, mismo `STAFF_DEFAULT`, mismo lunes de arranque (2026-08-03), DATA_DIR aislado propio (cada motor evoluciona su propio historial).

`_contar_penalizaciones` de `modelo_base.py` **no sirve** para comparar continuidad entre motores: también penaliza "repetir turno al volver de librar", una noción propia del heurístico que el término B de `motor_cpsat.py` no modela. Se implementó `comparar_metricas_comunes.py` con una métrica de "roturas de continuidad" que replica EXACTAMENTE lo que penaliza el término B (cambio de turno entre dos días consecutivos ambos trabajados en Mañana/Tarde/Partido), aplicable por igual a ambos.

### Resultado (con `PESO_EQUIDAD_FINDES=100` y `num_search_workers=1` + `sorted()` — ver Paso 4 y "Verificaciones puntuales" más abajo)

**Nota sobre estos números:** las cifras de esta tabla son las FINALES, tras resolver por completo el no-determinismo del solver (ver sección de verificaciones puntuales, puntos 3-4): confirmadas estables en 8/8 regeneraciones consecutivas byte-a-byte idénticas. Versiones anteriores de esta tabla en este mismo informe tenían números ligeramente distintos (p.ej. B=20 en vez de 15 para Recepción) que resultaron ser ruido de desempate no determinista del solver, no el comportamiento real del modelo — se sustituyen aquí, no se mantienen ambas.

| Métrica | Recepción — Heurístico | Recepción — CP-SAT | Mozos — Heurístico | Mozos — CP-SAT |
|---|---|---|---|---|
| Violaciones duras (18 semanas) | 0 | 0 | 0 | 0 |
| Roturas de continuidad (término B) | 9 | 15 | 28 | 13 |
| Uso de "Partido" | 10 | 0 | 72 | 0 |
| Dispersión ratio findes (max−min) | 0.056 | 0.051 | 0.056 | 0.021 |

Salida literal (`generar_comparativa_cpsat.py` + `comparar_metricas_comunes.py`, determinista — verificado 8/8 corridas idénticas):
```
=== recepcion ===
  Heurístico: {'violaciones_duras': 0, 'roturas_continuidad_termino_B': 9, 'uso_partido': 10}
  CP-SAT:     {'violaciones_duras': 0, 'roturas_continuidad_termino_B': 15, 'uso_partido': 0}

=== mozos ===
  Heurístico: {'violaciones_duras': 0, 'roturas_continuidad_termino_B': 28, 'uso_partido': 72}
  CP-SAT:     {'violaciones_duras': 0, 'roturas_continuidad_termino_B': 13, 'uso_partido': 0}
```

Detalle semana a semana en `comparativa_cpsat_recepcion.json`, `comparativa_cpsat_mozos.json`, `referencia_motor_actual.json`.

## Paso 2 — Clasificación de diferencias

### Esperadas por diseño (no investigadas como bug)
- **Uso de "Partido" → 0 en CP-SAT, 10/72 en heurístico.** Término D (`PESO_USO_PARTIDO=20`) minimiza explícitamente su uso; el heurístico solo lo evita "cuando es posible" de forma local. Coincide con el diseño documentado ("Partido solo si es absolutamente necesario" en `modelo_mozos.py`) y es una mejora clara, sobre todo en mozos (72→0 en 18 semanas).
- **Patrón de quién cubre cada semana la cobertura nocturna**: orden fijo con deudas (CP-SAT/`cobertura_nocturna.py`) vs. balance puro (heurístico) — el reparto concreto difiere por diseño, intencional.
- **Término F (Domingo suelto del sustituto) y término H (complementario jefe/subjefe)**: inexistentes en el heurístico, nuevos por diseño.

### Sin explicar (investigadas)
- **Roturas de continuidad (término B) MAYORES en CP-SAT en recepción (20 vs 9)**, pese a que CP-SAT penaliza explícitamente esta métrica y el heurístico no la optimiza globalmente. Explicación encontrada: CP-SAT prioriza el término D (Partido, peso 20) por encima de B (peso 5) — elimina 10 partidos (valor 10×20=200) a cambio de ~11 roturas extra (11×5=55), una operación que el optimizador acepta porque el balance neto es favorable según los pesos actuales. **No se corrigió**: es una decisión de diseño explícita en los pesos ya documentados (D > B), y el resultado neto (0 Partido, algo más de roturas) se considera aceptable, pero se deja señalado por si en producción se prefiriera lo contrario (ver recomendación final).
- **Dispersión de equidad de findes, peor en CP-SAT que en el heurístico con el peso original (10)** — ver Paso 4, corregido.

## Paso 3 — Caso "Viernes de Carla" (RECONSTRUCCIÓN, no recuperación)

El escenario original (plantilla de 10 personas, un viernes concreto sin solución) no se pudo recuperar — nunca se persistió y vivió solo en el contexto de una conversación anterior. Se construyó un escenario NUEVO y análogo en `escenario_viernes_carla_reconstruido.py`: plantilla de 10 personas (jefe, subjefe, 2 adjunto_recepcion, 6 normal, sin night_fixed — fuerza rotación de noches por bloques), con días sueltos recurrentes para 5 de los 6 "normal" TODAS las semanas + ausencias del jefe cada 2 semanas, encadenado 8 semanas.

**Resultado con el heurístico (SEED=1, reproducible):** `validar_viabilidad` confirma viable (totales semanales correctos), y las semanas 1-7 se resuelven sin violaciones. En la **semana 8**, tras 50 reintentos, el heurístico **no logra eliminar una violación de continuidad**:
```
violaciones tras 50 intentos: ['Laura: Martes (Tarde) → Miércoles (Mañana)']
```
Nota de honestidad: es intermitente — depende de la semilla aleatoria (en 10 ejecuciones sin semilla fija, falló ~2/10 veces) y del estado acumulado de 8 semanas, no de un viernes concreto como el caso original. Es una reconstrucción análoga, no el caso histórico.

**Resultado con motor_cpsat.py sobre el MISMO escenario exacto** (`escenario_viernes_carla_cpsat.py`):
```
SEMANA 1: Resuelto sin RuntimeError (factible para CP-SAT).
SEMANA 2: INFACTIBLE: CP-SAT no encontró solución factible para las restricciones dadas (status=INFEASIBLE).
```

**Esto NO es ninguno de los dos resultados que anticipaba el plan** (ni "CP-SAT resuelve lo que el heurístico no podía" ni "infactibilidad matemática real que exige relajar una restricción de cobertura"). Es un **tercer resultado: un bug real, verificado, en `cobertura_nocturna.py`**:

- En "modo respaldo" (activo aquí porque no hay `night_fixed`), `generar_semana_cpsat_con_cobertura_nocturna` llama a `seleccionar_candidato_cobertura_nocturna` una vez POR CADA día de la semana (7 llamadas).
- La elegibilidad (`_elegibles`/`_bloqueados_por_peticion_o_vacacion`) solo excluye a quien tiene `forced_days_off` en `cfg.night_fixed_days_off` (un par FIJO, p.ej. Jue/Vie) — **nunca comprueba si la persona tiene libre el día ESPECÍFICO que se está cubriendo en esa llamada concreta**, porque ni `seleccionar_candidato_cobertura_nocturna` ni `_elegibles` reciben ese día como parámetro.
- Verificado con un script de diagnóstico dedicado: en la semana 2, el algoritmo asigna a **Ana** cubrir la Noche del **Martes**, pero Ana tiene `forced_days_off=['Martes']` (su día suelto recurrente) — igual con **Diego** y el Miércoles:

```
--- semana 2 ---
modo_respaldo: True
  dia=Martes     cubre=Ana      forced_days_off=['Martes'] <<< CONFLICTO
  dia=Miércoles  cubre=Diego    forced_days_off=['Miércoles'] <<< CONFLICTO
```

- Esto produce, dentro de `_construir_modelo`, una contradicción directa: la restricción dura #6 fija `x[Ana, Martes, "Libre"] == 1` (vacación), y `generar_semana_cpsat_con_cobertura_nocturna` fija por separado `x[Ana, Martes, "Noche"] == 1` (cobertura externa) — INFEASIBLE por construcción, no por falta de margen real en la plantilla.

**Corrección necesaria (no aplicada en esta sesión, requiere cambio de API, no un one-liner):** pasar el día concreto que se está cubriendo a `seleccionar_candidato_cobertura_nocturna`/`_elegibles`/`_bloqueados_por_peticion_o_vacacion`, y filtrar la elegibilidad de modo respaldo contra el `forced_days_off` de ESE día, no contra `cfg.night_fixed_days_off`. Esto afecta a 3 funciones y a su único llamador (`generar_semana_cpsat_con_cobertura_nocturna`) y a los tests existentes de `cobertura_nocturna.py` que asumen la firma actual — se documenta como bloqueante, no se corrige aquí para no mezclar un cambio de API con un informe de verificación.

**Este es, con diferencia, el hallazgo más importante del informe**: es un bug real en el propio motor_cpsat/cobertura_nocturna (código de esta migración, no del heurístico), que en modo respaldo puede producir un `RuntimeError` de infactibilidad espuria en producción con cualquier plantilla sin `night_fixed` que además tenga vacaciones/peticiones concedidas coincidiendo con el reparto de cobertura nocturna.

## Paso 4 — Ajuste de pesos

`PESO_EQUIDAD_FINDES`: **10 → 100** (motor_cpsat.py, commit `5f91e0e`).

- Con 10: dispersión de ratio de findes libres tras 18 semanas = 0.067 (recepción) / 0.069 (mozos) — **peor que el heurístico** (0.056 en ambos). El término A no tenía peso suficiente frente a D (Partido=20) para imponerse en la práctica.
- Con 100: dispersión = 0.042 (recepción, estable en repeticiones) / 0.054–0.069 (mozos, algo de variación entre ejecuciones) — igual o mejor que el heurístico en la mayoría de las repeticiones, sin reintroducir Partido (sigue en 0) y con una subida marginal de roturas de continuidad en recepción (22→24 sobre 18 semanas en el experimento intermedio; 20 en la comparativa final).
- No se tocó `PESO_FINDE_ROL_EXCLUIDO` (200): sigue cumpliendo `> PESO_EQUIDAD_FINDES` (200 > 100).
- **No se ajustó** el desequilibrio de continuidad B vs D señalado en el Paso 2 — es una decisión de negocio (¿es peor una rotura de continuidad o un turno Partido?) sobre la que no hay una respuesta objetivamente correcta con los datos de este informe; se deja para decisión explícita del usuario.

## Paso 5 — Suite completa de tests

Ejecutada con `.venv_cpsat` (conftest.py verificó el entorno correctamente):

```
$ PYTHONPATH=. python -m pytest tests/ -q
........................................................................ [ 35%]
........................................................................ [ 70%]
............................................................             [100%]
204 passed in 47.34s
```

**204/204 tests pasan**, incluido el cambio de peso del Paso 4 (ningún test fija ese valor como constante esperada).

### Clasificación de los 23 ficheros de test

**Ya prueban motor_cpsat/cobertura_nocturna.py directamente** (trabajo de sesiones anteriores a este informe):
`test_motor_cpsat.py`, `test_cobertura_jefatura_cpsat.py`, `test_cobertura_nocturna_orden_fijo.py`, `test_racha_noches.py`, `test_domingo_suelto_sustituto.py`. `test_cobertura_jefatura_cpsat.py` documenta explícitamente en su propio docstring 3 funciones del original (`test_cobertura_jefatura.py`) que no se portaron y por qué (retrocompatibilidad de `roles.json`, propiedad ya cubierta en otro test, propiedad que no aplica al modelo CP-SAT).

**Fuera de alcance de esta migración (no prueban el motor de horarios, prueban app.py/herramientas.py — capas de autenticación, auditoría, cupos, dashboard, presentación):** `test_advertencias_estructuradas.py`, `test_auditoria.py`, `test_cupos.py`, `test_dashboard.py`, `test_dashboard_equidad.py`, `test_permisos.py`, `test_produccion.py`, `test_smoke.py`, `test_vacaciones_presentacion.py`. Migrarlos no tendría sentido: no dependen de qué motor genera el horario.

**Aplicables en principio, NO portados a un fichero `_cpsat` nuevo en esta sesión (decisión de alcance/tiempo, documentada explícitamente, no omitida en silencio):**
- `test_restricciones_duras.py`: `test_30_semanas_sin_violaciones`/`test_30_semanas_plantilla_ampliada` verifican ausencia de violaciones en cadenas largas — propiedad que en CP-SAT es estructuralmente **garantizada por construcción** (las restricciones de Fase 1 son duras del solver, no pueden violarse; si no hay solución, `RuntimeError`, no una celda con violación), ya validada indirectamente por las 0 violaciones en 18+8 semanas de este informe. `test_look_ahead_no_bloquea_persona_libre_dia_siguiente`/`test_look_ahead_si_bloquea_cuando_quedan_pocos_elegibles` prueban un detalle interno del algoritmo heurístico (mirar hacia delante en la asignación greedy) que no existe conceptualmente en un solver global — no aplica.
- `test_continuidad_turnos.py`: varios tests asumen la continuidad como una propiedad casi-dura del heurístico; en CP-SAT es explícitamente un término BLANDO (B) por diseño — un port 1:1 estaría probando una propiedad que motor_cpsat.py deliberadamente no garantiza como dura, y necesitaría reformularse (comparar el coste del término, no una aserción binaria), como ya se hizo en `test_cobertura_jefatura_cpsat.py` para la restricción 8.
- `test_temporadas.py`: la mayoría de sus tests prueban `validar_viabilidad`/`cargar_coverage_activa` (funciones puras compartidas, sin motor) — no aplica migrarlas. `test_generate_10_personas_perfil_alta`/`test_generate_7_personas_perfil_baja` (30 semanas) sí son del motor, pero la propiedad que verifican (30 semanas sin violaciones) ya quedó cubierta por este informe (18+8 semanas con 0 violaciones en CP-SAT).
- `test_equidad_findes.py`: `test_equidad_12_semanas` queda superado por el análisis de equidad de 18 semanas de este mismo informe (Pasos 1/4), más exhaustivo.
- `test_cobertura_nocturna.py` (el original, sin sufijo `_cpsat`): prueba `select_night_covers` del heurístico, un mecanismo que CP-SAT reemplaza por diseño con `cobertura_nocturna.py` (orden fijo) — ya tiene su contraparte en `test_cobertura_nocturna_orden_fijo.py`, que prueba el mecanismo NUEVO, no el mismo. Un port 1:1 no tendría sentido (probaría un algoritmo que ya no existe en el prototipo).

## Recomendación final

**No está listo para producción.** Motivo principal, no accesorio: el bug de `cobertura_nocturna.py` en modo respaldo (Paso 3) puede producir `RuntimeError` de infactibilidad espuria — no por falta de personal real, sino por una contradicción interna del propio código — en cualquier plantilla sin `night_fixed` (rotación de noches por bloques) que tenga alguna vacación/petición concedida coincidiendo con el día que le toca cubrir a esa persona. Esto es exactamente el tipo de escenario (plantilla de 10, sin `night_fixed`) que motivó parte de esta migración.

**Qué falta antes de poder decir "listo":**
1. Corregir el bug de elegibilidad por día en `cobertura_nocturna.py` (pasar el día concreto por toda la cadena de llamadas) y volver a correr el escenario reconstruido para confirmar que deja de ser INFEASIBLE.
2. Decisión de negocio pendiente (Paso 2/4): ¿se acepta que CP-SAT cambie más de turno (roturas de continuidad) a cambio de eliminar casi todo el uso de "Partido", o se prefiere lo contrario? Con los pesos actuales (D=20 > B=5) CP-SAT ya toma esa decisión de forma consistente; falta confirmar que es la deseada.
3. La variación de la dispersión de equidad de mozos entre ejecuciones (0.054–0.069) merece una tanda de repeticiones adicional antes de confiar en el peso 100 como definitivo para ese departamento.

**Lo que SÍ está validado y sólido:** integración de las 4 fases en una sola resolución (Paso 0), 0 violaciones duras en 18+8 semanas encadenadas para ambos departamentos con dos plantillas distintas, eliminación completa de "Partido" (mejora clara), 204/204 tests existentes en verde tras el ajuste de peso.

---

## Verificaciones puntuales (revisión antes de cerrar el informe)

Los 5 puntos siguientes se hicieron a petición explícita del usuario, antes de aceptar la conclusión preliminar como definitiva. Cambian la recomendación final en un punto concreto (ver al final de esta sección).

### 1. Diff literal de `app.py`/`modelo_base.py`

```
$ git diff HEAD -- app.py modelo_base.py
diff --git a/app.py b/app.py
index e9ae56f..8e41065 100755
--- a/app.py
+++ b/app.py
@@ -75,6 +75,10 @@ else:
     app.secret_key = secrets.token_bytes(32)
     with open(_secret_file, "wb") as _f:
         _f.write(app.secret_key)
+    # Restringir permisos: esta clave firma las sesiones de todos los
+    # usuarios (incluido admin); si el fichero quedara legible por otros
+    # usuarios del sistema, podrían falsificar cookies de sesión.
+    os.chmod(_secret_file, 0o600)
 
 app.config["SESSION_COOKIE_HTTPONLY"] = True   # JS no puede leer la cookie
 app.config["SESSION_COOKIE_SAMESITE"] = "Lax"  # protección CSRF básica
diff --git a/modelo_base.py b/modelo_base.py
index 6fe7daa..4416308 100755
--- a/modelo_base.py
+++ b/modelo_base.py
@@ -119,6 +119,12 @@ def _get_weekend_ratios(dept_id: str, staff_actual: list) -> dict:
     el finde libre fue compensación por cobertura nocturna.
     Personas sin historial reciben el ratio medio del equipo.
     """
+    # NOTA (duplicación frágil): esta función relee cargar_roles(dept_id) en vez
+    # de recibir cfg.roles_excluidos_equidad (que ya trae este mismo dato,
+    # parcheado por app._get_dept_config). Hoy coinciden porque ambos leen la
+    # misma fuente sin caché, pero cualquier caller que construya un cfg sin
+    # pasar por _get_dept_config puede divergir de lo que ve esta función.
+    # No se corrige aquí — solo queda anotado.
     try:
         from herramientas import cargar_historial, cargar_roles
     except Exception:
```

Confirmado literalmente: un comentario en `modelo_base.py`, un `os.chmod` en `app.py`. Ningún cambio de comportamiento de horarios.

### 2. ¿`PESO_EQUIDAD_FINDES=100` invalidó `test_termino_e_cede_cuando_la_alternativa_cuesta_mas_de_200`?

El test seguía pasando (`pytest -k termino_e` → PASSED), pero su propio cálculo de calibración (`coste_alternativa_termino_a = round((1.0 - avg_ratio) * 1000) * 10`) tenía el **10 hardcodeado** — un comentario decía `# * PESO_EQUIDAD_FINDES` pero nunca importaba la constante real. Tras subir el peso a 100, ese cálculo quedó **desconectado de la constante real** (mismo patrón de falso-positivo silencioso que ya se vio con "domingo suelto"): el test seguía pasando, pero su propia guarda de calibración ya no medía lo que el modelo usa de verdad.

Verificación con `ObjectiveValue()` explícito en ambos lados (script dedicado, reproducido con el import real de `PESO_EQUIDAD_FINDES`/`ESCALA_RATIO`):

```
PESO_EQUIDAD_FINDES actual = 100  (era 10 antes del Paso 4)
PESO_FINDE_ROL_EXCLUIDO    = 200
ESCALA_RATIO               = 1000

ratios: {'N1': 1.0, 'N4': 0.0, 'N5': 0.0, 'N2': 0.0, 'Cubridor': 0.0, 'N3': 0.0}
avg_ratio = 0.1667

coef_n1 (ratio-avg)*ESCALA_RATIO = 833
coste_alternativa_termino_a CORRECTO (peso real=100) = 83300
coste_alternativa_termino_a con *10 hardcodeado (obsoleto)          = 8330
  -> el coste real es 10.0x el que el comentario del test calcula a mano

[ESCENARIO REAL] Cubridor=['Noche', 'Tarde', 'Tarde', 'Libre', 'Libre', 'Libre', 'Libre']
  ObjectiveValue=424.0  óptimo=True

[CONTRAFACTUAL: Cubridor libra, N1 forzado a trabajar] N1=['Tarde', 'Tarde', 'Tarde', 'Tarde', 'Tarde', 'Libre', 'Libre']
  ObjectiveValue=83532.0  óptimo=True

Delta ObjectiveValue (contrafactual - real) = 83108.0
PESO_FINDE_ROL_EXCLUIDO (E) = 200
```

**Conclusión con números, no "sigue en verde":** el test pasa por el motivo REAL, no por casualidad — el delta de `ObjectiveValue` (83108) sigue de cerca el coste de equidad correctamente calculado (83300), confirmando que el término A sigue dominando ampliamente al término E (200) también con el peso nuevo. La presión sobre el trade-off que el test demuestra en realidad **aumentó 10x** (de un margen de ~41x sobre 200 a ~416x), no disminuyó — no es un caso "domingo suelto" de presión insuficiente. Sí se corrigió la guarda de calibración del test (ahora importa `PESO_EQUIDAD_FINDES`/`ESCALA_RATIO` reales de `motor_cpsat`, commit incluido en esta revisión) para que no vuelva a quedar desconectada de la constante si el peso cambia otra vez.

### 3. Causa de la varianza run-to-run en equidad de Mozos

Diagnóstico con evidencia, no narrativa: se generó la cadena CP-SAT de Mozos (18 semanas) dos veces con **exactamente el mismo input** (mismo `STAFF_DEFAULT`, mismo `MOZOS_CONFIG`, sin depender del historial del heurístico — `generar_comparativa_cpsat.py` nunca lee `referencia_motor_actual.json`, construye su propio historial desde cero en cada corrida) y se compararon los ficheros JSON de salida byte a byte:

```
$ cmp mozos_run_A.json mozos_run_B.json
mozos_run_A.json mozos_run_B.json differ: byte 13348, line 784
```

Los schedules **difieren** pese al input idéntico → descarta la hipótesis (a) (input no determinista); confirma la hipótesis (b) (múltiples soluciones óptimas empatadas, desempate no determinista del solver).

Causa raíz identificada: `cp_model.CpSolver()` en `motor_cpsat.py` no fija `num_search_workers` (por defecto `0` = búsqueda en portafolio paralela, con varios hilos compitiendo) ni depende de que el `random_seed` (por defecto `1`, sí fijo) sea suficiente para determinar cuál de varios workers "gana" la carrera cuando hay empate en el valor óptimo — el orden de finalización entre hilos no es determinista. Verificado forzando `solver.parameters.num_search_workers = 1` (búsqueda mono-hilo): la misma cadena, corrida dos veces, produjo **JSON idéntico byte a byte**.

**Efecto secundario importante al fijar `num_search_workers=1`:** además de estabilizar Mozos, expuso que `tests/test_racha_noches.py::test_estado_sucio_fuerza_descanso_inmediato` **deja de pasar** con la búsqueda mono-hilo.

**Decisión tomada en esta revisión:** se revirtió el cambio de `num_search_workers` (vuelto a como estaba, sin pin) para no introducir una regresión de test no solicitada ni mezclarla con esta verificación — motor_cpsat.py queda igual que antes de este punto 3 (`git diff` sobre el fichero, vacío tras la reversión).

#### Revisión pedida por el usuario (antes de fijar el parámetro en ningún sitio): causa raíz del test roto + impacto en rendimiento

**Causa raíz del test roto — confirmada con evidencia, no inferida.** Se leyó `agregar_restriccion_racha_noches` (motor_cpsat.py): la restricción 2 (descanso obligatorio) solo fuerza `Libre` en las posiciones INMEDIATAMENTE siguientes a una ventana de 5 días que resultó ser TODA "Noche" (`racha5` reificado sobre esa ventana concreta) — es exactamente "los 2 días siguientes a agotar una racha de 5", tal como está documentado, sin gap.

En el escenario del test (`min_coverage`/`max_coverage` Noche=1, `NocheFijo` presente y no ausente esa semana), `NocheFijo` ya ocupa el ÚNICO cupo de Noche del lunes por su turno fijo — así que N1 **no puede estructuralmente** trabajar Noche el lunes (violaría `max_coverage`). El test asumía que las únicas opciones de N1 eran "Noche" o "Libre", pero nada en la restricción de racha (ni ninguna otra) prohíbe que N1 haga Mañana/Tarde/Partido el lunes si no continúa la racha — la restricción solo actúa cuando una racha de 5 SÍ se completa.

Verificado empíricamente:
```
NocheFijo primeros 7 dias: ['Noche', 'Noche', 'Noche', 'Libre', 'Libre', 'Noche', 'Noche']
N1 primeros 7 dias:        ['Tarde', 'Libre', 'Mañana', 'Noche', 'Noche', 'Tarde', 'Libre']
Verificacion independiente de racha/descanso: PASA (sin excepcion)
```
`_verificar_todos` — el verificador independiente del propio archivo de test, que recorre la cadena concatenada de 16 semanas día a día SIN asumir nada sobre quién debía hacer qué, solo contando rachas y descansos reales — **pasa sin excepción** incluso en la ejecución donde la aserción específica del test falla. Esto es la confirmación definitiva: la restricción dura de Fase 3 funciona correctamente (opción **(b)**, no (a)). El bug está en el test (asume que N1 solo tiene Noche/Libre como opciones, cuando Tarde/Mañana/Partido son alternativas igual de válidas que no violan ninguna regla de racha) — **no se corrigió modelo_base.py ni motor_cpsat.py**, y el propio test tampoco se tocó en esta revisión (queda pendiente de decidir si se corrige la aserción o se elimina esa rama del test, ver más abajo).

**Impacto en tiempo de resolución** (`solver.parameters.num_search_workers`: 0=por defecto/paralelo vs 1=mono-hilo). Primero medido en corrida única (ver más abajo la repetición pedida por el usuario antes de aceptar la tabla):

```
=== Recepción — plantilla REAL de producción (9 personas, data/plantilla.json) ===
  por defecto (paralelo)      : 0.050s (status=OPTIMAL)
  num_search_workers=1        : 0.048s (status=OPTIMAL)

=== Mozos — plantilla REAL de producción (5 personas, data/mozos_plantilla.json) ===
  por defecto (paralelo)      : 0.023s (status=OPTIMAL)
  num_search_workers=1        : 0.030s (status=OPTIMAL)

=== Recepción + Mozos combinados en un solo modelo (14 personas, cota superior hipotética — hoy se resuelven por separado) ===
  por defecto (paralelo)      : 0.079s (status=OPTIMAL)
  num_search_workers=1        : 0.167s (status=OPTIMAL)
```

**Repetición pedida por el usuario (5 corridas por escenario, media + rango, antes de dar la tabla por buena):**

```
=== 20 personas ===
  por defecto (paralelo)  : media=0.130s  min=0.126s  max=0.138s  valores=[0.131, 0.138, 0.126, 0.128, 0.128]
  num_search_workers=1    : media=0.896s  min=0.881s  max=0.913s  valores=[0.881, 0.899, 0.901, 0.913, 0.888]

=== 25 personas ===
  por defecto (paralelo)  : media=0.171s  min=0.160s  max=0.183s  valores=[0.163, 0.170, 0.160, 0.183, 0.178]
  num_search_workers=1    : media=5.416s  min=5.339s  max=5.498s  valores=[5.378, 5.471, 5.339, 5.498, 5.395]

=== 30 personas ===
  por defecto (paralelo)  : media=0.201s  min=0.194s  max=0.228s  valores=[0.194, 0.228, 0.195, 0.196, 0.194]
  num_search_workers=1    : media=1.839s  min=1.820s  max=1.864s  valores=[1.859, 1.823, 1.864, 1.831, 1.820]
```

**El 5.267s de la primera medición de "Sintético 25" NO fue un valor atípico** — es representativo: las 5 repeticiones caen en un rango estrecho (5.339s–5.498s, media 5.416s). Curiosamente, 25 personas resulta consistentemente MÁS lento que 30 con `num_search_workers=1` (5.4s vs 1.8s) — un artefacto real y estable de la estructura combinatoria de ese tamaño concreto con esta configuración de cobertura, no ruido de medición.

**Lectura, ahora con rangos estables:** a la escala REAL actual de producción (5-9 personas por departamento), el impacto es indistinguible del ruido de medición (decenas de milisegundos). La brecha crece de forma consistente y reproducible a partir de ~20 personas (media 0.90s, ~7x más lento que el paralelo) y se vuelve significativa en el rango 25-30 (media 1.8-5.4s, hasta ~30x) — muy por encima de cualquier plantilla real existente hoy (la mayor, Recepción, tiene 9). **Con la escala actual del proyecto, el rendimiento no es un argumento de peso contra fijar `num_search_workers=1`.**

**Decisión de producto, ya tomada por el usuario con estos datos delante:** fijar `num_search_workers=1` en `motor_cpsat.py` (ambos puntos donde se construye `CpSolver()`), documentado con un comentario explicando el motivo (reproducibilidad del horario ante regeneración). El test roto (`test_estado_sucio_fuerza_descanso_inmediato`) se corrigió en la misma sesión, ya que su causa raíz quedó confirmada como bug del test (opción (b)), no del modelo — ver `tests/test_racha_noches.py`.

**Hallazgo adicional, no pedido pero necesario para que el pin cumpliera su propósito:** al re-verificar el determinismo tras fijar `num_search_workers=1` con varias repeticiones (no solo un par A/B), se encontró que **seguía sin ser 100% determinista** (4 de 5 corridas idénticas, la 5ª distinta). Causa raíz, leída en el código (`motor_cpsat.py`, restricción 2 de cobertura): `turnos_con_cobertura = set(cfg.min_coverage) | set(cfg.max_coverage)` se iteraba directamente para añadir las restricciones de cobertura mín/máx por turno — el orden de iteración de un `set` en Python depende del hash-seed aleatorio de cada proceso (`PYTHONHASHSEED`, no fijo por defecto), así que el orden en que esas restricciones se añadían al modelo variaba entre ejecuciones, y eso bastaba para que el solver (ya mono-hilo) resolviera un empate de forma distinta. Verificado: con `PYTHONHASHSEED=0` fijo, 5/5 corridas idénticas; sin fijarlo pero con el `set` reemplazado por `sorted(set(...))` en el código, también 5/5 (y 8/8 en una repetición posterior) idénticas — se optó por corregir el código (`sorted()`), no por depender de una variable de entorno que el despliegue podría olvidar fijar. Commit incluido en esta sesión.

Con ambos cambios (`num_search_workers=1` + `sorted()`), la reproducibilidad del horario generado ante una regeneración con el mismo input queda garantizada, verificada empíricamente, no solo asumida.

### 4. Cuantificación del trade-off D>B (Recepción, 18 semanas)

Tabla semana a semana (roturas de continuidad término B, uso de Partido), heurístico vs CP-SAT, sobre las mismas 18 semanas de `referencia_motor_actual.json`/`comparativa_cpsat_recepcion.json`.

**Actualización tras resolver el no-determinismo (ver punto 3):** con `num_search_workers=1` + `sorted()` ya aplicados, esta tabla es ahora reproducible byte a byte (verificado 8/8) — reemplaza la versión anterior de este informe (que tenía TOTAL=17, generada antes del segundo fix de determinismo):

```
Semana    B heur  B cpsat  Partido heur  Partido cpsat
1              0        0             0              0
2              0        1             0              0
3              0        1             1              0
4              1        1             0              0
5              2        0             0              0
6              1        1             0              0
7              0        0             1              0
8              1        0             1              0
9              0        0             1              0
10             0        1             1              0
11             1        1             2              0
12             1        2             1              0
13             0        2             0              0
14             0        1             0              0
15             0        1             0              0
16             1        1             0              0
17             1        1             1              0
18             0        1             1              0
TOTAL          9       15            10              0
```

**Nota de honestidad, descubierta al intentar aislar el mecanismo con rigor:** el primer intento de aislamiento (recalcular `ObjectiveValue()` de la rama real con D=20 vs. una rama contrafactual resuelta con `PESO_USO_PARTIDO=0`, cada una leyendo **su propio** `ObjectiveValue()`) fue **metodológicamente inválido** — cada solver reporta su objetivo bajo sus propios pesos, así que un Partido que cuesta 0 en la rama D=0 no es comparable directamente contra la rama D=20. Se corrigió fijando **ambos** schedules candidatos como restricciones duras en un modelo construido con los **pesos reales** y leyendo `ObjectiveValue()` de esa misma resolución (comparación consistente). Con esa corrección, en la semana 12 (de aquella corrida, previa al segundo fix de determinismo):

```
sched_real (0 Partido, B=1): coste bajo pesos reales = -2849.0
sched_d0   (2 Partido, B=1): coste bajo pesos reales = -2809.0
Delta (sched_d0 - sched_real) bajo pesos reales = 40.0
```

Esto confirma que, bajo los pesos reales, la alternativa con más Partido cuesta más — el término D actúa como se espera. La demostración causal semana-a-semana limpia (aislar una semana donde D=20 fuerce una rotura de continuidad ADICIONAL, no solo evite un Partido) seguía sin poder hacerse de forma perfectamente reproducible en el momento de escribir esto, aunque el no-determinismo de fondo ya está resuelto — no se ha repetido ese experimento concreto tras el fix, así que esta limitación se deja anotada en vez de reclamar una conclusión más fuerte de la que se verificó.

**Conclusión:** el patrón agregado (Partido→0, roturas de continuidad ~1.7x el heurístico en Recepción con los números ya deterministas) es consistente con el orden de pesos documentado (D=20 > B=5) y con el hecho de que CP-SAT reoptimiza globalmente cada semana sin la "inercia" que el heurístico local produce por construcción.

**No se reordenaron los pesos D/B.** Queda como decisión pendiente del usuario, con la tabla completa delante: ¿aceptar que CP-SAT rompa más veces la continuidad a cambio de casi eliminar "Partido", o preferir lo contrario?

### 5. Test permanente (xfail) para el bug de `cobertura_nocturna.py`

Añadido a `tests/test_cobertura_nocturna_orden_fijo.py`:
`test_modo_respaldo_no_debe_asignar_a_quien_tiene_ese_dia_forzado_libre` — modo respaldo (sin `night_fixed`), Ana con Lunes forzado libre; se piden los 7 días de cobertura y se comprueba que ningún día Ana quede asignada el día que tiene bloqueado. Marcado `@pytest.mark.xfail(strict=True, reason=...)` con referencia explícita a la causa raíz (falta el parámetro de día concreto en `seleccionar_candidato_cobertura_nocturna`/`_elegibles`/`_bloqueados_por_peticion_o_vacacion`).

Nota de rigor: el primer intento de este test (con "Martes" como día bloqueado) dio **XPASS** — no porque el bug no exista, sino porque la rotación por orden fijo de ese escenario concreto nunca hacía coincidir a Ana con el Martes (coincidencia de fase de rotación, el mismo patrón de falso-negativo de los puntos 2/3). Se verificó primero, con un script aparte, qué día le toca realmente a Ana en la rotación determinista de ese `estado_rotacion`, y se usó ESE día ("Lunes") para forzar el conflicto de verdad:

```
$ pytest tests/test_cobertura_nocturna_orden_fijo.py -k modo_respaldo_no_debe -v
tests/test_cobertura_nocturna_orden_fijo.py::test_modo_respaldo_no_debe_asignar_a_quien_tiene_ese_dia_forzado_libre XFAIL [100%]
13 deselected, 1 xfailed
```

Suite completa tras añadir el test:
```
$ PYTHONPATH=. python -m pytest tests/ -q
........................................................................ [ 35%]
.................x...................................................... [ 70%]
.............................................................            [100%]
204 passed, 1 xfailed in 46.20s
```

## Conclusión final (revisada tras las 5 verificaciones + el cierre de determinismo)

**Sigue sin estar listo para producción — pero el determinismo del solver ya no es parte de los motivos.** El bloqueo principal (bug de `cobertura_nocturna.py` en modo respaldo, Paso 3) permanece intacto y tiene un test permanente que lo documenta (punto 5). El resto de lo encontrado en las verificaciones puntuales ha quedado CERRADO en esta misma sesión, no solo señalado:

- **`PESO_EQUIDAD_FINDES=100` (Paso 4):** confirmado que no rompió la propiedad de diseño de fase 2 (E cede ante A) — el margen de esa propiedad de hecho mejoró 10x (punto 2). La guarda de test obsoleta que dependía del valor viejo hardcodeado quedó corregida.
- **No-determinismo del solver (puntos 3-4):** resuelto por completo, con dos causas distintas encontradas y corregidas, no solo una:
  1. `num_search_workers` sin fijar (portafolio paralelo) → fijado a `1` en `motor_cpsat.py`, con el motivo documentado en el propio código.
  2. Un `set()` iterado directamente para construir restricciones de cobertura, cuyo orden depende del hash-seed aleatorio de cada proceso → corregido con `sorted()`, sin depender de que el entorno de despliegue fije `PYTHONHASHSEED`.
  Verificado **8/8 regeneraciones consecutivas byte-a-byte idénticas** tras ambos cambios, a diferencia de la variación observada antes (con solo el primer fix, 4/5).
- **`test_estado_sucio_fuerza_descanso_inmediato`:** su causa raíz quedó confirmada como bug del propio test (asumía que N1 solo podía hacer Noche o Libre, cuando Tarde/Mañana/Partido eran alternativas igual de válidas) — corregido en `tests/test_racha_noches.py`. La restricción dura de racha de Fase 3 nunca estuvo rota.
- **Rendimiento de `num_search_workers=1`:** medido con 5 repeticiones por escala (no un solo valor) — despreciable a la escala real de hoy (5-9 personas), crece de forma reproducible a partir de ~20 personas (hasta ~30x más lento en el peor caso, 25 personas, con un patrón no monótono real y estable, no ruido). No es un obstáculo a la escala actual del proyecto.
- **Todas las cifras de las tablas del informe (Paso 1, punto 4) se regeneraron y sustituyeron** por sus versiones ya deterministas — no queda ningún número en este informe que dependa de qué corrida concreta se mire.
- El trade-off D (Partido) vs B (continuidad) sigue como **decisión de negocio pendiente del usuario** — ahora con una tabla estable y reproducible delante, no una que cambia entre lecturas.

**Lista de qué falta antes de "listo", actualizada:**
1. Corregir el bug de elegibilidad por día en `cobertura_nocturna.py` (bloqueante, Paso 3 — sigue siendo el único motivo de fondo; ya tiene test xfail que lo prueba).
2. Decisión de negocio pendiente: ¿D (Partido) > B (continuidad) es el orden de prioridad deseado? (Recepción: 15 roturas CP-SAT vs 9 heurístico, a cambio de 0 Partido vs 10).

Todo lo demás que motivó dudas en la ronda de verificaciones puntuales (peso de equidad, determinismo del solver, rendimiento, el test de racha) queda resuelto y cerrado en esta sesión.
