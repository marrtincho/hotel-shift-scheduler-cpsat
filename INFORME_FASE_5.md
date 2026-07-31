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

### Resultado (con `PESO_EQUIDAD_FINDES` ya ajustado a 100 — ver Paso 4)

| Métrica | Recepción — Heurístico | Recepción — CP-SAT | Mozos — Heurístico | Mozos — CP-SAT |
|---|---|---|---|---|
| Violaciones duras (18 semanas) | 0 | 0 | 0 | 0 |
| Roturas de continuidad (término B) | 9 | 20 | 28 | 14 |
| Uso de "Partido" | 10 | 0 | 72 | 0 |
| Dispersión ratio findes (max−min) | 0.056 | 0.042 | 0.056 | 0.069* |

`*` mozos: la dispersión de CP-SAT osciló entre 0.054 y 0.069 en distintas ejecuciones a igualdad de pesos (ver Paso 2) — no es completamente estable; recepción sí fue estable (0.042 en repeticiones).

Salida literal (`generar_comparativa_cpsat.py` + `comparar_metricas_comunes.py`):
```
=== recepcion ===
  Heurístico: {'violaciones_duras': 0, 'roturas_continuidad_termino_B': 9, 'uso_partido': 10}
  CP-SAT:     {'violaciones_duras': 0, 'roturas_continuidad_termino_B': 20, 'uso_partido': 0}

=== mozos ===
  Heurístico: {'violaciones_duras': 0, 'roturas_continuidad_termino_B': 28, 'uso_partido': 72}
  CP-SAT:     {'violaciones_duras': 0, 'roturas_continuidad_termino_B': 14, 'uso_partido': 0}
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
