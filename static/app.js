// ═══════════════════════════════════════════════════════════════════════
// HOTEL MANAGER — lógica de la interfaz
// ───────────────────────────────────────────────────────────────────────
// Se carga con `defer` desde index.html: se ejecuta con el DOM ya
// completo, así que todos los document.getElementById() de nivel superior
// encuentran su elemento. Los onclick="…" del marcado se resuelven en el
// momento del clic, no al analizar la página, por lo que no necesitan que
// este fichero esté cargado antes que el body.
//
// Sin build step: es JavaScript de navegador tal cual, servido por Flask
// desde /static. No hay bundler, ni imports, ni transpilación.
// ═══════════════════════════════════════════════════════════════════════
// Cierra un modal por id. Vive aquí (y no en un <script> del <head> como
// antes) porque los onclick="closeModal(...)" del marcado se resuelven al
// pulsar, no al analizar la página: con `defer` ya está definida de sobra.
function closeModal(id){
  var el = document.getElementById(id);
  if(el) el.classList.add('hidden');
}

// ROLES_BY_DEPT se carga dinámicamente desde /api/roles (ver
// cargarRolesDept/cargarRolesTodosDepts más abajo) — antes estaban
// hardcodeados aquí. Es la ÚNICA fuente de verdad para resolver
// etiqueta/icono de un rol: NO existe un mapa plano compartido entre
// departamentos, porque el mismo id de rol (p.ej. "normal") tiene labels
// distintos en recepción y en mozos — un mapa plano hace que el último
// departamento cargado pise la etiqueta del otro. Toda resolución de
// etiqueta debe pasar por etiquetaRol(dept, rolId) más abajo, indicando
// explícitamente el departamento del dato que se está pintando.
// ── ICONOS ────────────────────────────────────────────────────────────────
// Devuelve un <svg> que apunta al sprite Lucide incrustado al principio del
// body. Se usa desde las plantillas de render igual que ya se usa en el HTML
// estático, para que no haya dos formas distintas de pintar un icono.
//   ic('trash')            -> icono al tamaño heredado
//   ic('plus','ic-16')     -> icono a 16px
function ic(nombre, cls=''){
  return `<svg class="ic ${cls}" aria-hidden="true"><use href="#i-${nombre}"></use></svg>`;
}

// ── ESTADOS VACÍOS ────────────────────────────────────────────────────────
// Una sección sin datos nunca se queda en blanco: icono, título y una línea
// que explique qué falta o qué hacer a continuación.
function vacio({icono='clipboard', titulo, texto='', accion='', pequeno=false}){
  return `<div class="empty${pequeno?' empty-sm':''}">
    <div class="empty-ic">${ic(icono, pequeno?'ic-20':'ic-24')}</div>
    <div class="empty-title">${titulo}</div>
    ${texto ? `<div class="empty-txt">${texto}</div>` : ''}
    ${accion ? `<div class="empty-actions">${accion}</div>` : ''}
  </div>`;
}

// Igual, pero dentro de una tabla (ocupa toda la fila).
function vacioFila(colspan, opciones){
  return `<tr><td colspan="${colspan}">${vacio({...opciones, pequeno:true})}</td></tr>`;
}

let ROLES_BY_DEPT = {};
let rolesCache = {};

// Única función de resolución de etiqueta de rol. Requiere el dept del
// dato que se está pintando (nunca currentDept implícito, que puede haber
// cambiado). Si el id no existe en ese departamento, devuelve el propio id
// como fallback visible — nunca la etiqueta de otro departamento.
function etiquetaRol(dept, rolId){
  const info = (ROLES_BY_DEPT[dept] || []).find(r => r.v === rolId);
  return info ? info.l : rolId;
}
const COMP  = {cobrar:'Cobra festivo',librar_mismo_dia:'Libra ese día',dia_libre_eleccion:'Acumula LA'};
const DAYS  = ['Lunes','Martes','Miércoles','Jueves','Viernes','Sábado','Domingo'];
const SECS  = {dashboard:['Mi panel','Tu semana, festivos y estadísticas'],horario:['Horario semanal',''],empleados:['Empleados','Gestión de plantilla'],categorias:['Categorías','Configuración de roles de empleado'],festivos:['Festivos','Valencia ciudad 2026'],compensaciones:['Compensaciones (LA)','Libres adicionales y festivos trabajados'],peticiones:['Peticiones','Días libres solicitados por empleado'],vacaciones:['Vacaciones','Periodos registrados'],estadisticas:['Estadísticas','Acumulado histórico por empleado'],historial:['Historial','Semanas guardadas'],asistente:['Asistente IA','Consultas sobre el equipo'],auditoria:['Auditoría','Intervenciones manuales sobre horario y plantilla']};

async function cargarRolesDept(dept){
  const r = await api('GET', `/roles?dept=${dept}`);
  if(!r.ok) return;
  rolesCache[dept] = r.data;
  ROLES_BY_DEPT[dept] = Object.entries(r.data).map(([v,info])=>(
    {v, l: `${info.label}${info.icon?' '+info.icon:''}`}
  ));
}

async function cargarRolesTodosDepts(){
  await Promise.all([cargarRolesDept('recepcion'), cargarRolesDept('mozos')]);
}

let weekOffset = 0;
let lastSchedule = null;
let lastStaff = null;
let lastFestivos = [];
let lastAdvertencias = [];
let editEmpIdx = null;
let laNombre = null;
let staffCache = [];

// ── WEEK ──────────────────────────────────────────────────────────────────
function getMonday(off=0){
  const today = new Date();
  const day = today.getDay();
  const diff = today.getDate() - (day===0?6:day-1);
  const mon = new Date(today.setDate(diff));
  mon.setDate(mon.getDate() + off*7);
  return mon;
}
function weekDates(off=0){
  const mon = getMonday(off);
  return Array.from({length:7},(_,i)=>{const d=new Date(mon);d.setDate(d.getDate()+i);return d;});
}
function isoDate(d){ return d.toISOString().split('T')[0]; }
function fmtDate(d){ return d.toLocaleDateString('es-ES',{day:'2-digit',month:'short'}); }
function initials(name){ return name.trim().split(' ').slice(0,2).map(w=>w[0]).join('').toUpperCase();}
// Escapa texto libre (motivos, notas...) antes de insertarlo con innerHTML.
// Los nombres de empleado/usuario ya se validan en el servidor para no
// contener estos caracteres, pero motivos/notas son texto libre real.
function escapeHtml(s){
  return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function updateWeekLabel(){
  const dates = weekDates(weekOffset);
  const mon = dates[0], sun = dates[6];
  document.getElementById('week-lbl').textContent =
    `${fmtDate(mon)} – ${fmtDate(sun)} ${sun.getFullYear()}`;
  document.getElementById('topbar-sub').textContent =
    `Semana del ${mon.toLocaleDateString('es-ES',{day:'2-digit',month:'long'})} al ${sun.toLocaleDateString('es-ES',{day:'2-digit',month:'long',year:'numeric'})}`;
}


// ── TOAST ─────────────────────────────────────────────────────────────────
function toast(msg, type='ok'){
  const t = document.createElement('div');
  t.className = `toast ${type}`;
  // El mensaje va por textContent (puede venir del servidor); el icono se
  // añade aparte para no tener que escapar nada.
  t.innerHTML = ic(type==='ok' ? 'check-simple' : 'x', 'ic-16');
  t.appendChild(document.createTextNode(' ' + msg));
  document.body.appendChild(t);
  setTimeout(()=>{t.style.opacity='0';setTimeout(()=>t.remove(),300)},3000);
}

// ── NAVIGATION — handled in auth section below ────────────────────────────

// ── HORARIO ───────────────────────────────────────────────────────────────
function isPastWeek(off){ return getMonday(off) < getMonday(0); }

// La semana queda fija una vez pasada: solo el admin puede regenerarla.
function semanaBloqueadaParaGenerar(off){
  return isPastWeek(off) && currentUser?.rol !== 'admin';
}

// Etiqueta en reposo de cada botón de acción de la parrilla. ÚNICA fuente de
// verdad: los handlers ponen "Generando…/Vaciando…/Borrando…" con
// marcarBotonOcupado() y updateGenerarState() las repone TODAS.
//
// Antes, updateGenerarState() reponía el texto de "Generar"
// pero de "Asignar a mano" y "Borrar" solo el `disabled` — así que se
// quedaban en "Vaciando…"/"Borrando…" para siempre, incluso al cambiar de
// semana (que llama a esta misma función). Solo era estético, el botón
// seguía funcionando, pero daba la sensación de que la acción no había
// terminado.
const ETIQUETA_BOTON_ACCION = {
  'btn-generar':       () => ic('refresh','ic-16') + ' Generar',
  'btn-vaciar':        () => ic('pencil','ic-16') + ' Asignar a mano',
  'btn-borrar-semana': () => ic('trash','ic-16') + ' Borrar',
};

// Red de seguridad: si una petición se queda colgada (corte de red, pestaña
// suspendida, backend caído a media respuesta) el `finally` del handler puede
// tardar en ejecutarse o no ejecutarse nunca. Pasado este tiempo se repone el
// botón igualmente. 60s = el timeout de gunicorn (gunicorn.conf.py): más allá
// de eso la petición está muerta de todas formas, y por debajo cortaría
// generaciones legítimas (el solver admite hasta 20s + 10s de diagnóstico).
const ESPERA_MAXIMA_BOTON_MS = 60000;
let watchdogBotonAccion = null;

function marcarBotonOcupado(id, texto){
  const btn = document.getElementById(id);
  if(!btn) return null;
  // El spinner sustituye al icono en su sitio: el botón no cambia de forma
  // y se ve claramente que la acción está en curso (el motor puede tardar
  // hasta ~20 s en generar).
  btn.innerHTML = `<span class="spinner"></span> ${texto}`;
  btn.classList.add('is-loading');
  btn.disabled = true;
  clearTimeout(watchdogBotonAccion);
  watchdogBotonAccion = setTimeout(updateGenerarState, ESPERA_MAXIMA_BOTON_MS);
  return btn;
}

function updateGenerarState(){
  clearTimeout(watchdogBotonAccion);
  const btnGen    = document.getElementById('btn-generar');
  const btnExp    = document.getElementById('btn-exportar');
  const btnVaciar = document.getElementById('btn-vaciar');
  const btnBorrar = document.getElementById('btn-borrar-semana');
  if(!btnGen || !btnExp || !btnVaciar || !btnBorrar) return;

  btnGen.classList.toggle('hidden',    !tienePermiso('generar_horario'));
  btnExp.classList.toggle('hidden',     !tienePermiso('exportar_horario'));
  btnVaciar.classList.toggle('hidden',  !tienePermiso('vaciar_horario_manual'));
  btnBorrar.classList.toggle('hidden',  !tienePermiso('eliminar_horario_semana'));

  // Repone la etiqueta de reposo de los botones que marca
  // marcarBotonOcupado(), no solo la de "Generar": es lo que garantiza que
  // ninguno se quede con el texto de progreso, tanto al terminar la acción
  // como al cambiar de semana. Reasignar innerHTML entero elimina además el
  // <span class="spinner">.
  //
  // "Exportar" queda FUERA a propósito: se gestiona solo, con su propio
  // try/catch/finally que cubre también el fallo de red, y no pasa por
  // marcarBotonOcupado. Reponerlo aquí lo dejaría con la etiqueta de reposo
  // en mitad de una exportación si el usuario cambia de semana mientras se
  // descarga el Excel.
  btnVaciar.innerHTML = ETIQUETA_BOTON_ACCION['btn-vaciar']();
  btnBorrar.innerHTML = ETIQUETA_BOTON_ACCION['btn-borrar-semana']();
  btnVaciar.classList.remove('is-loading');
  btnBorrar.classList.remove('is-loading');
  btnGen.classList.remove('is-loading');

  const bloqueada = semanaBloqueadaParaGenerar(weekOffset);
  if(bloqueada){
    btnGen.disabled = true;
    btnGen.innerHTML = ic('lock','ic-16') + ' Semana fija';
    btnGen.title = 'Esta semana ya pasó. Solo un administrador puede regenerarla.';
  } else {
    btnGen.disabled = false;
    btnGen.innerHTML = ETIQUETA_BOTON_ACCION['btn-generar']();
    btnGen.title = '';
  }
  btnVaciar.disabled = bloqueada;
  btnBorrar.disabled = bloqueada || !(lastStaff && lastStaff.length);
}

// ── MI PANEL (EMPLEADO) — mobile-first, bloques 1-6 ──────────────────────
let dashData = null;
let dashDetalleAbierto = null;

const DASH_COLOR = {Mañana:'var(--mañana)',Tarde:'var(--tarde)',Noche:'var(--noche)',Partido:'var(--partido)',Libre:'var(--libre)',Vacaciones:'var(--vacaciones)'};
const DASH_TEXT  = {Mañana:'var(--mañana-t)',Tarde:'var(--tarde-t)',Noche:'var(--noche-t)',Partido:'var(--partido-t)',Libre:'var(--libre-t)',Vacaciones:'var(--vacaciones-t)'};
const DASH_DIA_CORTO = {Lunes:'L',Martes:'M',Miércoles:'X',Jueves:'J',Viernes:'V',Sábado:'S',Domingo:'D'};
// "Libre" y "Vacaciones" son ambos días sin trabajar de cara al dashboard;
// "Vacaciones" es solo la etiqueta de presentación para un "Libre" por vacaciones.
const DASH_LIBRE_LIKE = new Set(['Libre','Vacaciones']);

function dashHorarioTxt(tramos){
  if(!tramos || !tramos.length) return '';
  return tramos.map(t=>`${t.inicio}–${t.fin}`).join(' y ');
}

async function cargarDashboard(){
  const r = await api('GET','/dashboard');
  if(!r.ok){
    document.getElementById('dash-ahora').innerHTML =
      `<div class="p-16"><div class="alert err m-0">${ic('error','ic-16')}<span>${r.error}</span></div></div>`;
    ['dash-semana-tira','dash-semana-detalle','dash-contadores','dash-peticiones-cupo','dash-peticiones-lista','dash-historico-extra','dash-equidad-body'].forEach(id=>{
      document.getElementById(id).innerHTML = '';
    });
    document.getElementById('dash-chart').innerHTML = '';
    return;
  }
  dashData = r.data;
  dashDetalleAbierto = null;
  renderDashAhora();
  renderDashSemana();
  renderDashContadores();
  renderDashPeticiones();
  renderDashHistorico();
  renderDashEquidad();
}

// 1. AHORA MISMO
function renderDashAhora(){
  const d = dashData.ahora_mismo;
  const el = document.getElementById('dash-ahora');
  if(!d.existe_horario){
    el.innerHTML = `<div class="p-16">
      <div class="dash-eyebrow">Hoy · ${d.dia}</div>
      ${vacio({icono:'calendar-off', pequeno:true,
               titulo:'Aún no hay horario esta semana',
               texto:'En cuanto el encargado lo genere verás aquí tu turno de hoy y de mañana.'})}
    </div>`;
    return;
  }
  const esLibre = DASH_LIBRE_LIKE.has(d.turno_hoy);
  let cuerpo;
  if(esLibre){
    const esVacaciones = d.turno_hoy === 'Vacaciones';
    const prefijo = esVacaciones ? 'De vacaciones — vuelves' : 'Vuelves';
    const msg = d.regreso
      ? `${prefijo} el ${d.regreso.dia.toLowerCase()} de ${d.regreso.turno}`
      : (esVacaciones ? 'De vacaciones — todavía no hay horario para saber cuándo vuelves' : 'Todavía no hay horario para saber cuándo vuelves');
    cuerpo = `<span class="sc ${d.turno_hoy} dash-turno-badge">${d.turno_hoy}</span>
      <div class="t-sm muted mt-8">${msg}</div>`;
  }else{
    const companeros = d.companeros_hoy.length
      ? `Hoy trabajas con: <strong>${d.companeros_hoy.join(', ')}</strong>`
      : 'Trabajas solo/a hoy';
    cuerpo = `<span class="sc ${d.turno_hoy} dash-turno-badge">${d.turno_hoy}</span>
      <div class="t-base muted mt-8">${dashHorarioTxt(d.horario_hoy)}</div>
      <div class="t-sm muted-3 mt-8">${companeros}</div>`;
  }
  const mananaTxt = d.turno_manana === null
    ? '<span class="muted-3 t-sm">Sin horario todavía</span>'
    : `<span class="sc ${d.turno_manana} sc-mini">${d.turno_manana}</span>
       ${!DASH_LIBRE_LIKE.has(d.turno_manana) ? `<span class="t-xs muted">${dashHorarioTxt(d.horario_manana)}</span>` : ''}`;

  el.innerHTML = `
    <div class="dash-ahora-hoy-wrap">
      <div class="dash-ahora-hoy-col">
        <div class="dash-eyebrow">Hoy · ${d.dia}</div>
        <div class="mt-8">${cuerpo}</div>
      </div>
      <div class="dash-ahora-manana-col wrap">
        <span class="dash-eyebrow nowrap">Mañana:</span> ${mananaTxt}
      </div>
    </div>`;
}

// 2. MI SEMANA
function renderDashSemana(){
  document.getElementById('dash-semana-lbl').textContent = dashData.semana.key;
  const tira = document.getElementById('dash-semana-tira');
  tira.innerHTML = dashData.semana.dias.map((dia,i)=>{
    const corto = DASH_DIA_CORTO[dia.dia] || dia.dia.substring(0,1);
    const sinTurno = !dia.turno;
    const horarioLine = (!sinTurno && !DASH_LIBRE_LIKE.has(dia.turno))
      ? `<div class="dash-day-horario">${dashHorarioTxt(dia.horario)}</div>` : '';
    const cortoTurno = dia.turno==='Libre' ? 'Lib' : (dia.turno==='Vacaciones' ? 'Vac' : dia.turno.substring(0,3));
    return `<div class="dash-day${dia.hoy?' hoy':''}" data-idx="${i}">
      <div class="dash-day-lbl">${corto}${dia.es_regreso?' ↩':''}</div>
      <span class="sc ${sinTurno?'SIN_ASIGNAR':dia.turno} dash-day-sc">${sinTurno?'?':cortoTurno}</span>
      ${horarioLine}
    </div>`;
  }).join('');
  tira.querySelectorAll('.dash-day').forEach(elDay=>{
    elDay.onclick = ()=> toggleDashDetalle(parseInt(elDay.dataset.idx, 10));
  });
  document.getElementById('dash-semana-detalle').innerHTML = '';
}

function toggleDashDetalle(idx){
  const cont = document.getElementById('dash-semana-detalle');
  if(dashDetalleAbierto === idx){ dashDetalleAbierto = null; cont.innerHTML = ''; return; }
  dashDetalleAbierto = idx;
  const dia = dashData.semana.dias[idx];
  if(!dia.turno){
    cont.innerHTML = `<div class="row-sep-top muted t-sm">${dia.dia}: sin horario generado todavía.</div>`;
    return;
  }
  const companeros = DASH_LIBRE_LIKE.has(dia.turno) ? '—' : (dia.companeros.length ? dia.companeros.join(', ') : 'trabajas solo/a');
  cont.innerHTML = `<div class="row-sep-top">
    <div class="row wrap mb-6">
      <strong class="t-base">${dia.dia}</strong>
      <span class="sc ${dia.turno}">${dia.turno}</span>
      ${dia.es_regreso ? '<span class="badge info">Vuelves de tus libres</span>' : ''}
    </div>
    ${!DASH_LIBRE_LIKE.has(dia.turno) ? `<div class="t-sm muted">Horario: ${dashHorarioTxt(dia.horario)}</div>` : ''}
    <div class="t-sm muted mt-4">Compañeros: ${companeros}</div>
  </div>`;
}

// 3. MIS CONTADORES
function renderDashContadores(){
  const c = dashData.contadores;
  document.getElementById('dash-contadores').innerHTML = `
    <div class="stat"><div class="stat-val">${c.peticiones_mes.usadas}/${c.peticiones_mes.total}</div><div class="stat-lbl">Peticiones ${c.peticiones_mes.mes}</div></div>
    <div class="stat"><div class="stat-val">${c.vacaciones.restantes}</div><div class="stat-lbl">Vacaciones restantes</div><div class="stat-sub">de ${c.vacaciones.dotacion} días ${c.vacaciones.año}</div></div>
    <div class="stat"><div class="stat-val">${c.festivos_trabajados_anio}</div><div class="stat-lbl">Festivos trabajados</div><div class="stat-sub">este año</div></div>
    <div class="stat"><div class="stat-val">${c.la_saldo}</div><div class="stat-lbl">Días LA pendientes</div><div class="stat-sub">${c.modalidad_compensacion==='cobrar'?'modalidad: cobrar':'modalidad: LA'}</div></div>`;
}

// 4. MIS PETICIONES
function renderDashPeticiones(){
  const p = dashData.peticiones;
  document.getElementById('dash-peticiones-cupo').textContent =
    `Cupo ${p.cupo.mes_actual.mes}: ${p.cupo.mes_actual.usadas}/${p.cupo.mes_actual.total} · ${p.cupo.mes_siguiente.mes}: ${p.cupo.mes_siguiente.usadas}/${p.cupo.mes_siguiente.total}`;
  const lista = document.getElementById('dash-peticiones-lista');
  if(!p.activas.length){
    lista.innerHTML = vacio({icono:'inbox', pequeno:true,
      titulo:'No tienes peticiones activas',
      texto:'Cuando pidas un día libre aparecerá aquí, con su estado.'});
    return;
  }
  lista.innerHTML = p.activas.map(a=>`
    <div class="row-sep-top row between">
      <div>
        <div class="t-sm w-500">${a.semana} — ${a.dias.join(', ')}</div>
        <div class="t-xs muted">${a.origen==='empleado'?'Solicitada por ti':'Gestionada por el encargado'}${a.motivo?' · '+escapeHtml(a.motivo):''}</div>
      </div>
      <button class="btn sm" onclick="cancelarPeticionDash('${a.semana}')">Cancelar</button>
    </div>`).join('');
}

async function cancelarPeticionDash(semana){
  if(!confirm('¿Cancelar esta petición?')) return;
  const r = await api('DELETE', `/peticiones/${semana}/${encodeURIComponent(dashData.nombre)}?dept=${dashData.dept}`);
  if(r.ok){ toast('Petición cancelada'); cargarDashboard(); }
  else toast(r.error || 'Error al cancelar', 'err');
}

document.getElementById('dash-btn-nueva-peticion').onclick = ()=>{
  document.getElementById('btn-add-pet').click();
};

// 5. MI HISTÓRICO — barras SVG a mano, sin librerías nuevas
function renderDashHistorico(){
  const periodo = document.getElementById('dash-periodo').value;
  const h = dashData.historico[periodo];
  const turnos  = ['Mañana','Tarde','Noche','Partido'];
  const valores = turnos.map(t=>h.turnos[t]||0);
  const max = Math.max(1, ...valores);
  const W=300, H=130, padBottom=20, padTop=16, barW=46, gap=20;
  const barsHTML = turnos.map((t,i)=>{
    const v = valores[i];
    const barH = Math.round((v/max) * (H-padBottom-padTop));
    const x = gap + i*(barW+gap);
    const y = H - padBottom - barH;
    return `<rect x="${x}" y="${y}" width="${barW}" height="${Math.max(barH,1)}" rx="4" class="bar-turno ${t}"></rect>
      <text x="${x+barW/2}" y="${Math.max(y-6,12)}" text-anchor="middle" font-size="12" font-weight="600" fill="var(--text)">${v}</text>
      <text x="${x+barW/2}" y="${H-5}" text-anchor="middle" font-size="10" fill="var(--text-2)">${t}</text>`;
  }).join('');
  const svg = document.getElementById('dash-chart');
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.innerHTML = barsHTML;

  document.getElementById('dash-historico-extra').innerHTML = `
    <div class="stat"><div class="stat-val">${h.findes}</div><div class="stat-lbl">Findes libres</div></div>
    <div class="stat"><div class="stat-val">${h.noches}</div><div class="stat-lbl">Noches cubiertas</div></div>
    <div class="stat"><div class="stat-val">${h.festivos}</div><div class="stat-lbl">Festivos</div></div>`;
}
document.getElementById('dash-periodo').onchange = ()=>{ if(dashData) renderDashHistorico(); };

// 5b. MI EQUIDAD — nunca muestra nombres ni datos de otros compañeros, solo
// el propio dato y la media agregada del equipo (ver equidad_de en
// estadisticas.py). Tono neutro y explicativo, nunca competitivo.
const EQUIDAD_POSICION_TXT = {
  en_media:   'Estás en la media del equipo.',
  por_encima: 'Estás por encima de la media del equipo.',
  por_debajo: 'Estás por debajo de la media del equipo.',
};

function _fmtEquidad(n, decimales=2){
  return Number(n).toLocaleString('es-ES', {minimumFractionDigits:decimales, maximumFractionDigits:decimales});
}

function _equidadContadoresSinComparar(eq){
  return `<div class="g3">
    <div class="stat"><div class="stat-val">${eq.findes.propios}</div><div class="stat-lbl">Findes libres</div></div>
    <div class="stat"><div class="stat-val">${eq.noches.propias}</div><div class="stat-lbl">Noches cubiertas</div></div>
    <div class="stat"><div class="stat-val">${eq.festivos.propios}</div><div class="stat-lbl">Festivos trabajados</div></div>
  </div>`;
}

function _renderEquidadBarra(lbl, valorMostrado, propio, media, posicion, texto){
  const tope = Math.max(propio, media, 0.0001) * 1.25;
  const pctPropio = Math.min(100, (propio / tope) * 100);
  const pctMedia  = Math.min(100, (media  / tope) * 100);
  return `<div class="equidad-metric">
    <div class="equidad-metric-hd">
      <span class="equidad-metric-lbl">${lbl}</span>
      <span class="equidad-metric-val">${valorMostrado}</span>
    </div>
    <div class="equidad-bar-track">
      <div class="equidad-bar-fill" style="width:${pctPropio}%"></div>
      <div class="equidad-bar-media" style="left:${pctMedia}%" title="Media del equipo"></div>
    </div>
    <div class="equidad-metric-txt">${texto}</div>
  </div>`;
}

function renderDashEquidad(){
  const eq   = dashData.equidad;
  const cont = document.getElementById('dash-equidad-body');
  if(!eq){ cont.innerHTML = ''; return; }

  if(eq.excluido_de_balanceo){
    cont.innerHTML = `
      <div class="alert info mb-12">${ic('info','ic-16')}
        <span>Tu categoría tiene funciones de cobertura fija y por eso no entra en el reparto
        automático de equidad — aquí ves tus contadores, sin comparación con el equipo.</span>
      </div>
      ${_equidadContadoresSinComparar(eq)}`;
    return;
  }

  if(eq.datos_insuficientes){
    cont.innerHTML = `
      <div class="alert info mb-12">${ic('info','ic-16')}
        <span>Todavía no llevas suficientes semanas activas (menos de 4) para que la comparación con
        el equipo sea representativa. De momento se muestran solo tus contadores.</span>
      </div>
      ${_equidadContadoresSinComparar(eq)}`;
    return;
  }

  const semanas = eq.findes.semanas;
  const bloques = [
    _renderEquidadBarra(
      'Fines de semana libres', eq.findes.propios,
      eq.findes.ratio, eq.findes.media_equipo, eq.findes.posicion,
      `Llevas ${eq.findes.propios} fines de semana libres en ${semanas} semanas (${_fmtEquidad(eq.findes.ratio)}). `+
      `La media del equipo es ${_fmtEquidad(eq.findes.media_equipo)}. ${EQUIDAD_POSICION_TXT[eq.findes.posicion]}`
    ),
    _renderEquidadBarra(
      'Noches cubiertas', eq.noches.propias,
      eq.noches.propias, eq.noches.media_equipo, eq.noches.posicion,
      `Has cubierto ${eq.noches.propias} noches en tus últimas ${semanas} semanas activas. `+
      `La media del equipo es ${_fmtEquidad(eq.noches.media_equipo)}. ${EQUIDAD_POSICION_TXT[eq.noches.posicion]}`
    ),
    _renderEquidadBarra(
      'Festivos trabajados', eq.festivos.propios,
      eq.festivos.propios, eq.festivos.media_equipo, eq.festivos.posicion,
      `Has trabajado ${eq.festivos.propios} festivos en tus últimas ${semanas} semanas activas. `+
      `La media del equipo es ${_fmtEquidad(eq.festivos.media_equipo)}. ${EQUIDAD_POSICION_TXT[eq.festivos.posicion]}`
    ),
  ];

  cont.innerHTML = bloques.join('') + `
    <div class="alert info mt-2">${ic('info','ic-16')}
      <span>El reparto se hace de forma proporcional al tiempo trabajado por cada persona, no en
      cifras absolutas. Las diferencias pequeñas son normales — no todas las semanas permiten un
      reparto exacto — y el sistema tiende a compensarlas en las semanas siguientes.</span>
    </div>`;
}

// 6. EXPORTAR .ICS
document.getElementById('dash-btn-ics').onclick = ()=>{
  window.location.href = '/api/dashboard/ics';
};

async function cargarHorarioSemana(){
  const semana = isoDate(getMonday(weekOffset));
  updateGenerarState();
  mostrarEsqueletoParrilla('Cargando la semana…');
  const r = await api('GET',`/horario/semana?semana=${semana}&dept=${currentDept}`);
  if(!r.ok){
    renderSchedule({staff:[], schedule:{}, festivos:[], advertencias:[], dept: currentDept});
    showAlert('err', r.error); return;
  }
  if(!r.data.existe){
    lastSchedule = null; lastStaff = null; lastFestivos = []; lastAdvertencias = [];
    renderSchedule({staff:[], schedule:{}, festivos:[], advertencias:[], dept: currentDept});
    updateGenerarState();
    showAlert('info', semanaBloqueadaParaGenerar(weekOffset)
      ? 'Esta semana pasó sin generarse y quedó fija. Solo un administrador puede generarla.'
      : 'Aún no se ha generado el horario de esta semana. Pulsa "Generar".');
    return;
  }
  lastSchedule = r.data.schedule;
  lastStaff = r.data.staff;
  lastFestivos = r.data.festivos||[];
  lastAdvertencias = r.data.advertencias||[];
  updateGenerarState();
  renderSchedule(r.data);
  if(r.data.advertencias.length)
    showAlert('warn',`${r.data.advertencias.length} advertencia(s) en el horario — celdas resaltadas, detalle debajo de la parrilla.`);
  else
    showAlert('ok', isPastWeek(weekOffset) ? 'Horario fijo de esta semana.' : 'Horario vigente de esta semana.');
}

document.getElementById('btn-prev').onclick = ()=>{ weekOffset--; updateWeekLabel(); cargarHorarioSemana(); };
document.getElementById('btn-next').onclick = ()=>{ weekOffset++; updateWeekLabel(); cargarHorarioSemana(); };

document.getElementById('btn-generar').onclick = async ()=>{
  if(semanaBloqueadaParaGenerar(weekOffset)){
    showAlert('err','Esta semana ya pasó y quedó fija. Solo un administrador puede regenerarla.');
    return;
  }
  marcarBotonOcupado('btn-generar', 'Generando…');
  mostrarEsqueletoParrilla('Generando horario…');
  try{
    const semana = isoDate(getMonday(weekOffset));
    const r = await api('POST','/horario/generar',{semana, dept: currentDept});
    if(!r.ok){
      // Repinta lo que ya había: el esqueleto no puede quedarse girando.
      renderSchedule({staff:lastStaff||[], schedule:lastSchedule||{}, festivos:lastFestivos, advertencias:lastAdvertencias, dept: currentDept});
      showAlert('err',r.error); return;
    }
    lastSchedule = r.data.schedule;
    lastStaff = r.data.staff;
    lastFestivos = r.data.festivos||[];
    lastAdvertencias = r.data.advertencias||[];
    renderSchedule(r.data);
    if(r.data.advertencias.length)
      showAlert('warn',`${r.data.advertencias.length} advertencia(s) en el horario — celdas resaltadas, detalle debajo de la parrilla.`);
    else
      showAlert('ok','Horario generado correctamente — sin infracciones.');
  } finally {
    // SIEMPRE se restaura el botón, incluso si algo lanza una excepción
    // inesperada arriba -- un botón atascado en "Generando..." para
    // siempre es peor que un error visible que al menos permite reintentar.
    updateGenerarState();
  }
};

document.getElementById('btn-vaciar').onclick = async ()=>{
  if(semanaBloqueadaParaGenerar(weekOffset)){
    showAlert('err','Esta semana ya pasó y quedó fija. Solo un administrador puede modificarla.');
    return;
  }
  if(!confirm('¿Dejar la semana en blanco para asignar los turnos a mano? Si ya había un horario generado, se sustituye.')) return;
  marcarBotonOcupado('btn-vaciar', 'Vaciando…');
  mostrarEsqueletoParrilla('Dejando la semana en blanco…');
  try{
    const semana = isoDate(getMonday(weekOffset));
    const r = await api('POST','/horario/vaciar',{semana, dept: currentDept});
    if(!r.ok){
      renderSchedule({staff:lastStaff||[], schedule:lastSchedule||{}, festivos:lastFestivos, advertencias:lastAdvertencias, dept: currentDept});
      showAlert('err',r.error); return;
    }
    lastSchedule = r.data.schedule;
    lastStaff = r.data.staff;
    lastFestivos = r.data.festivos||[];
    lastAdvertencias = r.data.advertencias||[];
    renderSchedule(r.data);
    showAlert('info','Semana en blanco. Asigna los turnos a mano en la parrilla.');
  } finally {
    updateGenerarState();
  }
};

document.getElementById('btn-borrar-semana').onclick = async ()=>{
  if(semanaBloqueadaParaGenerar(weekOffset)){
    showAlert('err','Esta semana ya pasó y quedó fija. Solo un administrador puede modificarla.');
    return;
  }
  if(!confirm('¿Borrar el horario generado de esta semana? Esta acción no se puede deshacer.')) return;
  marcarBotonOcupado('btn-borrar-semana', 'Borrando…');
  mostrarEsqueletoParrilla('Borrando el horario…');
  try{
    const semana = isoDate(getMonday(weekOffset));
    const r = await api('DELETE',`/horario/semana?semana=${semana}&dept=${currentDept}`);
    if(!r.ok){
      renderSchedule({staff:lastStaff||[], schedule:lastSchedule||{}, festivos:lastFestivos, advertencias:lastAdvertencias, dept: currentDept});
      showAlert('err',r.error); return;
    }
    lastSchedule = null; lastStaff = null; lastFestivos = []; lastAdvertencias = [];
    renderSchedule({staff:[], schedule:{}, festivos:[], advertencias:[], dept: currentDept});
    showAlert('ok','Horario de la semana borrado.');
  } finally {
    updateGenerarState();
  }
};

document.getElementById('btn-exportar').onclick = async ()=>{
  // No se usa api() aquí: esa función asume una respuesta JSON, pero este
  // endpoint devuelve el .xlsx binario directamente para descargar.
  const btn = document.getElementById('btn-exportar');
  btn.innerHTML = '<span class="spinner"></span> Exportando…';
  btn.classList.add('is-loading'); btn.disabled = true;
  const semana = isoDate(getMonday(weekOffset));
  try{
    const resp = await fetch('/api/horario/exportar', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({semana, dept: currentDept}),
    });
    if(!resp.ok){
      const d = await resp.json().catch(()=>({}));
      if(d.login){ showLogin(); return; }
      // Incluir el código HTTP cuando el cuerpo NO es JSON: eso significa que
      // el servidor devolvió una página de error (un 500 de Flask por una
      // excepción sin capturar, o un error del túnel/proxy), y sin el código
      // el usuario solo veía "Error al exportar" sin ninguna pista. Fue
      // exactamente lo que ocultó el PermissionError al escribir el .xlsx en
      // el contenedor (ver app._enviar_excel).
      toast(d.error || `Error al exportar (HTTP ${resp.status})`, 'err');
      return;
    }
    const disposition = resp.headers.get('Content-Disposition') || '';
    const nombre = disposition.match(/filename="?([^"]+)"?/)?.[1] || `horario_${semana}.xlsx`;
    const blob = await resp.blob();
    const url  = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = nombre;
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
    toast('Excel descargado');
  } catch(e){
    toast('Error de red al exportar', 'err');
  } finally {
    btn.innerHTML = ic('download','ic-16') + ' Excel';
    btn.classList.remove('is-loading'); btn.disabled = false;
  }
};

// La semana vigente y futuras: encargado y admin pueden editar turnos a mano.
// Semanas pasadas: solo admin (misma regla que generar/regenerar).
function puedeEditarTurnos(){
  return tienePermiso('generar_horario') && !semanaBloqueadaParaGenerar(weekOffset);
}

const TURNOS_EDITABLES = ['Mañana','Tarde','Noche','Partido','Libre'];

function abrirEditorTurno(span){
  if(span.tagName === 'SELECT') return;
  const nombre = span.dataset.name;
  const dia    = Number(span.dataset.day);
  const actual = span.dataset.value;

  // "Vacaciones" es solo una etiqueta de presentación sobre "Libre": al
  // editar el turno, el valor interno real de esa celda sigue siendo "Libre".
  const actualInterno = actual === 'Vacaciones' ? 'Libre' : actual;
  const sel = document.createElement('select');
  sel.className = 'sc-edit-sel';
  TURNOS_EDITABLES.forEach(t=>{
    const opt = document.createElement('option');
    opt.value = t; opt.textContent = t;
    if(t === actualInterno) opt.selected = true;
    sel.appendChild(opt);
  });
  sel.onclick = e => e.stopPropagation();
  const cerrar = ()=> renderSchedule({staff:lastStaff||[], schedule:lastSchedule||{}, festivos:lastFestivos, advertencias:lastAdvertencias, dept: currentDept});
  sel.onblur = cerrar;
  sel.onchange = async ()=>{
    const turno  = sel.value;
    if(turno === actualInterno){ cerrar(); return; }  // sin cambio real, no pedir justificación
    const semana = isoDate(getMonday(weekOffset));
    const r = await api('PUT','/horario/turno',{dept: currentDept, semana, nombre, dia_idx: dia, turno});
    if(!r.ok){ toast(r.error,'err'); cerrar(); return; }
    lastSchedule    = r.data.schedule;
    lastAdvertencias = r.data.advertencias||[];
    renderSchedule({staff:lastStaff, schedule:lastSchedule, festivos:lastFestivos, advertencias:lastAdvertencias, dept: currentDept});
    toast(`Turno de ${nombre} actualizado`);
  };
  span.replaceWith(sel);
  sel.focus();
}

function renderSchedule(data){
  restaurarParrilla();
  // Dept del dato que se está pintando, no una variable global que puede
  // haber cambiado entre la petición y el render (ver etiquetaRol arriba).
  const dept = data.dept || currentDept;
  const dates = weekDates(weekOffset);
  const festDates = new Set((data.festivos||[]).map(f=>f.fecha));
  const advertencias = data.advertencias||[];
  // Set de "nombre|dia_idx" con todas las celdas en conflicto, de todas las
  // advertencias, para poder resaltarlas de un vistazo en la parrilla.
  const celdasConflicto = new Set(
    advertencias.flatMap(a => (a.celdas||[]).map(c => `${c.nombre}|${c.dia_idx}`))
  );

  const head = document.getElementById('schedule-head');
  head.innerHTML = '<th class="nl">Empleado</th>';
  dates.forEach((d,i)=>{
    const iso = isoDate(d);
    const th = document.createElement('th');
    th.innerHTML = `${DAYS[i].substring(0,3)} ${d.getDate()}` +
      (festDates.has(iso) ? ' ' + ic('party-popper','ic-16') : '');
    if(festDates.has(iso)) th.classList.add('th-festivo');
    head.appendChild(th);
  });

  const tbody = document.getElementById('schedule-body');
  tbody.innerHTML = '';
  (data.staff||[]).forEach(p=>{
    const shifts = data.schedule[p.name]||Array(7).fill('Libre');
    const tr = document.createElement('tr');
    let html = `<td class="nl"><div class="emp-cell">
      <div class="avatar av-28">${initials(p.name)}</div>
      <div><div class="emp-cell-name">${p.name}</div>
      <div class="emp-cell-role">${etiquetaRol(dept, p.role)}</div></div></div></td>`;
    const editable = puedeEditarTurnos();
    shifts.forEach((s,i)=>{
      const iso = isoDate(dates[i]);
      const fest = festDates.has(iso) && s!=='Libre' && s!=='Vacaciones';
      const conflicto = celdasConflicto.has(`${p.name}|${i}`);
      const cls = `sc ${s}${fest?' fest':''}${conflicto?' celda-conflicto':''}${editable?' editable':''}`;
      html += editable
        ? `<td><span class="${cls}" data-name="${p.name}" data-day="${i}" data-value="${s}" onclick="abrirEditorTurno(this)">${s}</span></td>`
        : `<td><span class="${cls}">${s}</span></td>`;
    });
    tr.innerHTML = html;
    tbody.appendChild(tr);
  });

  const stats = document.getElementById('horario-stats');
  const total = (data.staff||[]).length;
  stats.innerHTML = `
    <div class="stat"><div class="stat-val">${total}</div><div class="stat-lbl">Empleados</div></div>
    <div class="stat"><div class="stat-val">${(data.festivos||[]).length}</div><div class="stat-lbl">Festivos semana</div></div>
    <div class="stat"><div class="stat-val">${advertencias.length}</div><div class="stat-lbl">Advertencias</div><div class="stat-sub">${advertencias.length===0?'Todo correcto':''}</div></div>
    <div class="stat"><div class="stat-val">${weekOffset===0?'Actual':weekOffset>0?'+'+weekOffset:weekOffset}</div><div class="stat-lbl">Semana</div><div class="stat-sub">${isoDate(getMonday(weekOffset))}</div></div>`;

  // Semana sin generar: en vez de una parrilla en blanco, un estado vacío
  // que dice qué falta y ofrece la acción (si el usuario puede hacerla).
  const vacioWrap = document.getElementById('schedule-vacio');
  const hayFilas  = (data.staff||[]).length > 0;
  document.getElementById('schedule-wrap').classList.toggle('hidden', !hayFilas);
  vacioWrap.classList.toggle('hidden', hayFilas);
  if(!hayFilas){
    const bloqueada = semanaBloqueadaParaGenerar(weekOffset);
    vacioWrap.innerHTML = vacio({
      icono: 'calendar-off',
      titulo: 'Todavía no hay horario para esta semana',
      texto: bloqueada
        ? 'La semana ya pasó y quedó fija. Solo un administrador puede generarla.'
        : 'Pulsa «Generar» y el motor repartirá los turnos respetando vacaciones, peticiones y la ronda de noches.',
      accion: (!bloqueada && tienePermiso('generar_horario'))
        ? `<button class="btn pri" onclick="document.getElementById('btn-generar').click()">${ic('refresh','ic-16')} Generar horario</button>`
        : '',
    });
  }

  // Lista de advertencias bajo la parrilla: sin bloque si no hay ninguna,
  // para no meter ruido visual en el caso normal (sin conflictos).
  document.getElementById('horario-advertencias-lista').innerHTML = advertencias.length
    ? `<ul class="advertencias-lista">${advertencias.map(a=>`<li><span>${a.mensaje}</span></li>`).join('')}</ul>`
    : '';
}

const ALERT_ICONO = {ok:'check', err:'error', warn:'warn', info:'info'};

function showAlert(type, msg){
  const el = document.getElementById('horario-alert');
  const cls = {ok:'ok',err:'err',warn:'warn',info:'info'}[type]||'info';
  el.innerHTML = `<div class="alert ${cls}">${ic(ALERT_ICONO[cls],'ic-16')}<span>${msg}</span></div>`;
  setTimeout(()=>el.innerHTML='',6000);
}

// ── ESQUELETO DE CARGA DE LA PARRILLA ─────────────────────────────────────
// El motor CP-SAT puede tardar hasta ~20 s. Durante ese tiempo la zona de la
// parrilla muestra un esqueleto con el número de filas que va a tener, en vez
// de quedarse con el horario anterior congelado o en blanco.
function mostrarEsqueletoParrilla(mensaje='Generando horario…'){
  const filas = Math.max(4, (lastStaff || []).length || 8);
  document.getElementById('schedule-vacio').classList.add('hidden');
  const wrap = document.getElementById('schedule-wrap');
  wrap.classList.remove('hidden');
  wrap.innerHTML = `
    <div class="loading-banner">
      <span class="spinner"></span>
      <span>${mensaje}</span>
      <span class="loading-hint">puede tardar unos segundos</span>
    </div>
    ${Array.from({length: filas}, ()=>`
      <div class="skel-row">
        <div class="skel skel-avatar"></div>
        <div class="skel skel-name"></div>
        ${Array.from({length:7}, ()=>'<div class="skel skel-cell"></div>').join('')}
      </div>`).join('')}`;
}

// Devuelve la tabla real a su sitio tras el esqueleto (renderSchedule la
// vuelve a rellenar justo después).
function restaurarParrilla(){
  const wrap = document.getElementById('schedule-wrap');
  if(document.getElementById('schedule-tbl')) return;
  wrap.innerHTML = `<table class="tbl" id="schedule-tbl">
      <thead><tr id="schedule-head"></tr></thead>
      <tbody id="schedule-body"></tbody>
    </table>`;
}

// ── EMPLEADOS ─────────────────────────────────────────────────────────────
async function loadEmpleados(){
  const r = await api('GET',`/plantilla?dept=${currentDept}`);
  if(!r.ok) return;
  staffCache = r.data;
  document.getElementById('emp-count').textContent = `${r.data.length} empleados en plantilla`;
  if(!r.data.length){
    document.getElementById('emp-list').innerHTML = vacio({
      icono:'users', titulo:'La plantilla está vacía',
      texto:'Añade a los empleados de este departamento para poder generar horarios.',
      accion:`<button class="btn pri" onclick="document.getElementById('btn-add-emp').click()">${ic('plus','ic-16')} Añadir empleado</button>`});
    return;
  }
  document.getElementById('emp-list').innerHTML = r.data.map((e,i)=>`
    <div class="emp-row" onclick="openEditEmp(${i})">
      <div class="avatar av-34">${initials(e.name)}</div>
      <div class="flex-1">
        <div class="emp-row-name">${e.name}</div>
        <div class="emp-row-sub">${etiquetaRol(currentDept, e.role)}</div>
        <div class="row wrap gap-4 mt-4">
          ${(e.preferences||[]).map(p=>`<span class="badge ${p}">${p}</span>`).join('')}
          <span class="tag">${COMP[e.comp]||e.comp}</span>
          ${e.la_saldo>0?`<span class="tag info">LA: ${e.la_saldo}</span>`:''}
          ${e.festivos_trabajados>0?`<span class="tag">${ic('party-popper','ic-16')} ${e.festivos_trabajados} festivos</span>`:''}
        </div>
      </div>
      <span class="chev">${ic('chevron-right','ic-18')}</span>
    </div>`).join('');
}

document.getElementById('btn-add-emp').onclick = ()=>{
  editEmpIdx = null;
  document.getElementById('m-emp-title').textContent = 'Añadir empleado';
  document.getElementById('m-name').value='';
  const roles = ROLES_BY_DEPT[currentDept] || [];  // nunca caer a otro dept: mismo id, distinto label
  document.getElementById('m-role').innerHTML = roles.map(r=>`<option value="${r.v}">${r.l}</option>`).join('');
  document.getElementById('m-comp').value='cobrar';
  document.querySelectorAll('#m-prefs .pill').forEach(p=>p.classList.remove('on'));
  document.getElementById('btn-delete-emp').classList.add('hidden');
  document.getElementById('modal-emp').classList.remove('hidden');
};

function openEditEmp(i){
  editEmpIdx = i;
  const e = staffCache[i];
  document.getElementById('m-emp-title').textContent = 'Editar empleado';
  document.getElementById('m-name').value = e.name;
  const roles = ROLES_BY_DEPT[currentDept] || [];  // nunca caer a otro dept: mismo id, distinto label
  document.getElementById('m-role').innerHTML = roles.map(r=>`<option value="${r.v}" ${e.role===r.v?'selected':''}>${r.l}</option>`).join('');
  document.getElementById('m-comp').value = e.comp||'cobrar';
  document.querySelectorAll('#m-prefs .pill').forEach(p=>{
    p.classList.toggle('on',(e.preferences||[]).includes(p.dataset.v));
  });
  document.getElementById('btn-delete-emp').classList.remove('hidden');
  document.getElementById('modal-emp').classList.remove('hidden');
}

async function deleteEmp(){
  if(editEmpIdx===null) return;
  const nombre = staffCache[editEmpIdx].name;
  if(!confirm(`¿Eliminar a "${nombre}" de la plantilla? Esta acción no se puede deshacer.`)) return;
  // Disable button to prevent double-click
  const btn = document.getElementById('btn-delete-emp');
  btn.disabled = true;
  const r = await api('DELETE',`/plantilla/${editEmpIdx}?dept=${currentDept}`);
  btn.disabled = false;
  if(!r.ok){ toast(r.error,'err'); return; }
  closeModal('modal-emp');
  toast(`"${nombre}" eliminado de la plantilla`);
  loadEmpleados();
}

document.querySelectorAll('#m-prefs .pill').forEach(p=>p.onclick=()=>p.classList.toggle('on'));

document.getElementById('btn-save-emp').onclick = async ()=>{
  const name = document.getElementById('m-name').value.trim();
  if(!name){toast('El nombre es obligatorio','err');return;}
  const body = {
    name, role:document.getElementById('m-role').value,
    comp:document.getElementById('m-comp').value,
    preferences:[...document.querySelectorAll('#m-prefs .pill.on')].map(p=>p.dataset.v),
    dept: currentDept
  };
  const r = editEmpIdx===null
    ? await api('POST','/plantilla',body)
    : await api('PUT',`/plantilla/${editEmpIdx}`,body);
  if(!r.ok){toast(r.error,'err');return;}
  closeModal('modal-emp');
  toast(editEmpIdx===null?'Empleado añadido':'Empleado actualizado');
  loadEmpleados();
};

// ── CATEGORÍAS (admin) ──────────────────────────────────────────────────────
let editCatId = null;

async function loadCategorias(){
  const r = await api('GET', `/roles?dept=${currentDept}`);
  if(!r.ok) return;
  rolesCache[currentDept] = r.data;
  const entries = Object.entries(r.data);
  document.getElementById('cat-count').textContent = `${entries.length} categorías en ${currentDept}`;
  if(!entries.length){
    document.getElementById('cat-list').innerHTML = vacio({
      icono:'tags', titulo:'Sin categorías definidas',
      texto:'Las categorías definen turno fijo, días libres y reglas de cobertura de cada empleado.'});
    return;
  }
  document.getElementById('cat-list').innerHTML = entries.map(([id,c])=>`
    <div class="emp-row" onclick="openEditCat('${id}')">
      <div class="flex-1">
        <div class="emp-row-name">${c.label}${c.icon?' '+c.icon:''} ${c.protegido?'<span class="tag">protegida</span>':''}</div>
        <div class="emp-row-sub">
          ${c.turno_fijo ? 'Turno fijo: '+c.turno_fijo : 'Rota normalmente'}
          ${c.dias_libres_fijos ? ' · Libra: '+c.dias_libres_fijos.map(d=>DAYS[d].substring(0,3)).join('+') : ''}
          ${(c.dias_presencia&&c.dias_presencia.length) ? ' · Presencia: '+c.dias_presencia.map(d=>DAYS[d].substring(0,3)).join('+') : ''}
          ${c.sin_noches ? ' · Sin noches' : ''}
          ${c.unico ? ' · Única persona' : ''}
          ${c.cubre_ausencia_de ? ' · Cubre a: '+(r.data[c.cubre_ausencia_de]?.label||c.cubre_ausencia_de)+(c.cobertura_semana_completa?' (semana completa)':'') : ''}
          ${c.excluir_de_equidad ? ' · Excluida de equidad' : ''}
          · En uso: ${c.en_uso}
        </div>
      </div>
      <span class="chev">${ic('chevron-right','ic-18')}</span>
    </div>`).join('');
}

// Rellena el desplegable "Cubre la ausencia del rol" con las categorías del
// departamento actual, excluyendo la propia (un rol no puede cubrirse a sí
// mismo — ver validar_rol en herramientas.py).
function _poblarCubreRolSelect(excluirId){
  const sel = document.getElementById('m-cat-cubre-rol');
  const roles = rolesCache[currentDept] || {};
  sel.innerHTML = '<option value="">— ninguno —</option>' +
    Object.entries(roles)
      .filter(([id])=> id !== excluirId)
      .map(([id,c])=>`<option value="${id}">${c.label}</option>`).join('');
}

function _actualizarCoberturaTotalWrap(){
  const tieneRol = !!document.getElementById('m-cat-cubre-rol').value;
  document.getElementById('m-cat-cobertura-total-wrap').classList.toggle('hidden', !tieneRol);
  document.getElementById('m-cat-libres-fijos-wrap').classList.toggle('hidden', !tieneRol);
  if(!tieneRol){
    document.getElementById('m-cat-cobertura-total').checked = false;
    document.getElementById('m-cat-libres-fijos').checked = false;
  }
}

document.getElementById('m-cat-cubre-rol').onchange = _actualizarCoberturaTotalWrap;

function _resetCatModal(){
  const idInput = document.getElementById('m-cat-id');
  idInput.disabled = false; idInput.value = '';
  document.getElementById('m-cat-label').value = '';
  document.getElementById('m-cat-icon').value = '';
  const turnoSel = document.getElementById('m-cat-turno');
  turnoSel.value = ''; turnoSel.disabled = false;
  document.querySelectorAll('#m-cat-dias .pill').forEach(p=>p.classList.remove('on'));
  document.querySelectorAll('#m-cat-presencia .pill').forEach(p=>p.classList.remove('on'));
  document.getElementById('m-cat-presencia').classList.remove('disabled');
  document.getElementById('m-cat-sustituto-wrap').classList.add('hidden');
  document.getElementById('m-cat-sin-noches').checked = false;
  document.getElementById('m-cat-unico').checked = false;
  _poblarCubreRolSelect(editCatId);
  document.getElementById('m-cat-cubre-rol').value = '';
  document.getElementById('m-cat-cobertura-total').checked = false;
  document.getElementById('m-cat-cobertura-total-wrap').classList.add('hidden');
  document.getElementById('m-cat-libres-fijos').checked = false;
  document.getElementById('m-cat-libres-fijos-wrap').classList.add('hidden');
  document.getElementById('m-cat-excluir-equidad').checked = false;
  document.getElementById('m-cat-protegido-alert').classList.add('hidden');
  document.getElementById('btn-delete-cat').classList.add('hidden');
}

document.getElementById('btn-add-cat').onclick = ()=>{
  editCatId = null;
  _resetCatModal();
  document.getElementById('m-cat-title').textContent = 'Nueva categoría';
  document.getElementById('modal-cat').classList.remove('hidden');
};

function openEditCat(id){
  editCatId = id;
  const c = rolesCache[currentDept][id];
  _resetCatModal();
  document.getElementById('m-cat-title').textContent = 'Editar categoría';
  document.getElementById('m-cat-id').value = id;
  document.getElementById('m-cat-id').disabled = true;
  document.getElementById('m-cat-label').value = c.label;
  document.getElementById('m-cat-icon').value = c.icon||'';
  document.getElementById('m-cat-turno').value = c.turno_fijo||'';
  document.querySelectorAll('#m-cat-dias .pill').forEach(p=>{
    p.classList.toggle('on', (c.dias_libres_fijos||[]).includes(Number(p.dataset.v)));
  });
  document.querySelectorAll('#m-cat-presencia .pill').forEach(p=>{
    p.classList.toggle('on', (c.dias_presencia||[]).includes(Number(p.dataset.v)));
  });
  document.getElementById('m-cat-sin-noches').checked = !!c.sin_noches;
  document.getElementById('m-cat-unico').checked = !!c.unico;
  document.getElementById('m-cat-cubre-rol').value = c.cubre_ausencia_de || '';
  document.getElementById('m-cat-cobertura-total').checked = !!c.cobertura_semana_completa;
  document.getElementById('m-cat-libres-fijos').checked = !!c.cubrir_dias_libres_fijos;
  document.getElementById('m-cat-excluir-equidad').checked = !!c.excluir_de_equidad;
  _actualizarCoberturaTotalWrap();
  _actualizarPresenciaUI();

  if(id === 'night_fixed'){
    document.getElementById('m-cat-turno').disabled = true;
    document.getElementById('m-cat-sustituto-wrap').classList.remove('hidden');
    _actualizarSustitutoInfo();
  }
  if(c.protegido){
    // Los roles protegidos no admiten presencia parcial (ver validar_rol).
    document.getElementById('m-cat-presencia').classList.add('disabled');
    document.getElementById('m-cat-protegido-alert').classList.remove('hidden');
  }
  if(c.borrable && c.en_uso === 0){
    document.getElementById('btn-delete-cat').classList.remove('hidden');
  }
  document.getElementById('modal-cat').classList.remove('hidden');
}

// Los días de descanso del sustituto se calculan, no se eligen: son siempre
// los dos días siguientes a los días libres fijos del turno noche (con vuelta
// de semana, p.ej. fijos=Sáb+Dom -> descansa Lun+Mar de la semana que viene).
// Así nunca se puede romper el límite de máximo 2 libres juntos por semana.
const DIAS_NOMBRE = ['Lunes','Martes','Miércoles','Jueves','Viernes','Sábado','Domingo'];

function _diasSustitutoNoche(diasFijos){
  if(!diasFijos || diasFijos.length !== 2) return null;
  const b = Math.max(...diasFijos);
  return [...new Set([(b+1)%7, (b+2)%7])].sort((a,b)=>a-b);
}

function _actualizarSustitutoInfo(){
  const diasFijos = _diasPills('m-cat-dias');
  const info = document.getElementById('m-cat-sustituto-info');
  if(!diasFijos || diasFijos.length !== 2){
    info.textContent = 'Elige los 2 días libres fijos para calcular el descanso del sustituto.';
    return;
  }
  const [a,b] = diasFijos;
  if(b - a !== 1){
    info.textContent = 'Los días libres fijos deben ser consecutivos para poder calcular el descanso del sustituto.';
    return;
  }
  const dias = _diasSustitutoNoche(diasFijos).map(d=>DIAS_NOMBRE[d]).join(' y ');
  info.textContent = `El sustituto descansará ${dias} (calculado automáticamente a partir de los días libres fijos).`;
}

document.querySelectorAll('#m-cat-dias .pill').forEach(p=>{
  p.onclick = ()=>{
    p.classList.toggle('on');
    if(editCatId === 'night_fixed') _actualizarSustitutoInfo();
  };
});

document.querySelectorAll('#m-cat-presencia .pill').forEach(p=>{
  p.onclick = ()=>{
    const antes = !!_diasPills('m-cat-presencia');
    p.classList.toggle('on');
    const despues = !!_diasPills('m-cat-presencia');
    // Al activar presencia por primera vez se premarca "sin noches" — es
    // solo el default de creación (desmarcable si el contrato requiere
    // cubrir noches; ver crear_rol en herramientas.py).
    if(!antes && despues) document.getElementById('m-cat-sin-noches').checked = true;
    _actualizarPresenciaUI();
  };
});

// Con días de presencia la categoría queda EXCLUIDA del balanceo de equidad
// (herramientas.crear_rol/editar_rol lo fuerzan por backend); sin presencia,
// el admin vuelve a decidirlo con el checkbox.
function _actualizarPresenciaUI(){
  const tiene = !!_diasPills('m-cat-presencia');
  const chkEq = document.getElementById('m-cat-excluir-equidad');
  if(tiene){ chkEq.checked = true; chkEq.disabled = true; }
  else { chkEq.disabled = false; }
}

function _diasPills(containerId){
  const pills = [...document.querySelectorAll(`#${containerId} .pill.on`)];
  if(pills.length === 0) return null;
  return pills.map(p=>Number(p.dataset.v)).sort((a,b)=>a-b);
}

document.getElementById('btn-save-cat').onclick = async ()=>{
  const nDias = document.querySelectorAll('#m-cat-dias .pill.on').length;
  if(nDias !== 0 && nDias !== 2){ toast('Elige 0 o exactamente 2 días libres fijos','err'); return; }

  const diasPresencia = _diasPills('m-cat-presencia');
  if(diasPresencia && nDias){ toast('Una categoría con días de presencia no puede tener días libres fijos','err'); return; }

  const diasFijos = _diasPills('m-cat-dias');
  if(editCatId === 'night_fixed'){
    if(!diasFijos){ toast('El turno noche fijo necesita días libres fijos','err'); return; }
    const [a,b] = diasFijos;
    if(b - a !== 1){ toast('Los días libres fijos del turno noche deben ser dos días consecutivos','err'); return; }
  }

  const label = document.getElementById('m-cat-label').value.trim();
  if(!label){ toast('El nombre es obligatorio','err'); return; }

  const body = {
    dept: currentDept,
    id: document.getElementById('m-cat-id').value.trim(),
    label,
    icon: document.getElementById('m-cat-icon').value.trim(),
    turno_fijo: document.getElementById('m-cat-turno').value || null,
    dias_libres_fijos: diasFijos,
    dias_presencia: diasPresencia || [],
    sin_noches: document.getElementById('m-cat-sin-noches').checked,
    unico: document.getElementById('m-cat-unico').checked,
    cubre_ausencia_de: document.getElementById('m-cat-cubre-rol').value || null,
    cobertura_semana_completa: document.getElementById('m-cat-cobertura-total').checked,
    cubrir_dias_libres_fijos: document.getElementById('m-cat-libres-fijos').checked,
    excluir_de_equidad: document.getElementById('m-cat-excluir-equidad').checked,
  };

  const r = editCatId === null
    ? await api('POST', '/roles', body)
    : await api('PUT', `/roles/${editCatId}`, body);
  if(!r.ok){ toast(r.error, 'err'); return; }
  closeModal('modal-cat');
  toast(editCatId === null ? 'Categoría creada' : 'Categoría actualizada');
  await cargarRolesDept(currentDept);
  loadCategorias();
};

document.getElementById('btn-delete-cat').onclick = async ()=>{
  if(!editCatId || !confirm(`¿Eliminar la categoría "${editCatId}"?`)) return;
  const r = await api('DELETE', `/roles/${editCatId}?dept=${currentDept}`);
  if(!r.ok){ toast(r.error, 'err'); return; }
  closeModal('modal-cat');
  toast('Categoría eliminada');
  await cargarRolesDept(currentDept);
  loadCategorias();
};

// ── FESTIVOS ──────────────────────────────────────────────────────────────
async function loadFestivos(){
  // FIX: was mixing backtick open + single-quote close — caused SyntaxError crashing the whole app
  const r = await api('GET',`/festivos?año=2026`);
  if(!r.ok) return;
  document.getElementById('festivos-cal').innerHTML = r.data.map(f=>{
    const d = new Date(f.fecha+'T00:00:00');
    const dia = d.toLocaleDateString('es-ES',{weekday:'long'});
    return `<div class="festivo-row">
      <span class="festivo-fecha">${d.getDate().toString().padStart(2,'0')}/${(d.getMonth()+1).toString().padStart(2,'0')}</span>
      <div class="flex-1"><div class="t-base w-500">${f.nombre}</div>
      <div class="t-xs muted cap">${dia}</div></div>
      <span class="ftipo ${f.tipo}">${f.tipo}</span>
      ${f.registrado
        ? `<span class="t-sm accent-txt row gap-4">${ic('check-simple','ic-16')} registrado</span>`
        : '<span class="t-sm muted-3">—</span>'}
    </div>`;
  }).join('');

  const rt = await api('GET','/festivos/trabajados');
  if(rt.ok){
    document.getElementById('festivos-trab').innerHTML =
      `<div class="t-base w-600 mb-10">Festivos trabajados por empleado</div>` +
      (rt.data.length
        ? rt.data.map(e=>`
          <div class="list-row py-10">
            <div class="avatar av-28">${initials(e.nombre)}</div>
            <div class="flex-1 t-base w-500">${e.nombre}</div>
            <span class="mono t-base">${e.total} festivos</span>
          </div>`).join('')
        : vacio({icono:'party-popper', pequeno:true, titulo:'Ningún festivo trabajado todavía',
                 texto:'Se registran solos al generar la semana de cada festivo.'}));
  }
}

document.querySelectorAll('.tab-btn').forEach(btn=>{
  btn.onclick = ()=>{
    // El aspecto de la pestaña activa lo define .tab-btn.active en el CSS:
    // aquí solo se cambia la clase (antes se pisaban los colores a mano).
    document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
    btn.classList.add('active');
    const tab = btn.dataset.tab;
    document.getElementById('tab-calendario').classList.toggle('hidden',tab!=='calendario');
    document.getElementById('tab-trabajados').classList.toggle('hidden',tab!=='trabajados');
  };
});

// ── COMPENSACIONES ────────────────────────────────────────────────────────
async function loadCompensaciones(){
  const r = await api('GET',`/compensaciones?dept=${currentDept}`);
  if(!r.ok) return;
  const data = r.data;
  document.getElementById('total-la').textContent = data.reduce((s,e)=>s+e.la_saldo,0);
  document.getElementById('total-cobran').textContent = data.filter(e=>e.modalidad==='cobrar').length;

  if(!data.length){
    document.getElementById('comp-list').innerHTML = vacio({
      icono:'calendar-plus', titulo:'Sin empleados en este departamento',
      texto:'Los días LA y los festivos trabajados se acumulan por empleado.'});
    return;
  }
  document.getElementById('comp-list').innerHTML = data.map((e,i)=>`
    <div class="comp-card">
      <div class="comp-hd" onclick="toggleComp(${i})">
        <div class="avatar av-30">${initials(e.nombre)}</div>
        <div class="flex-1">
          <div class="emp-row-name">${e.nombre}</div>
          <div class="emp-row-sub">${etiquetaRol(currentDept, e.role)} · ${COMP[e.modalidad]||e.modalidad}</div>
        </div>
        <div class="row">
          ${e.la_saldo>0?`<span class="badge info">LA: ${e.la_saldo}</span>`:'<span class="no-la">Sin LA</span>'}
          <span class="chev" id="carr-${i}">${ic('chevron-down','ic-18')}</span>
        </div>
      </div>
      <div class="comp-body hidden" id="cbody-${i}">
        <div class="row-top between wrap gap-12">
          <div>
            <div class="comp-metric-lbl">Días LA acumulados</div>
            <div class="la-ctr">
              <button class="la-btn" onclick="adjLA('${e.nombre}',${i},-1)" aria-label="Restar un LA">${ic('minus','ic-16')}</button>
              <span class="la-num" id="lac-${i}">${e.la_saldo}</span>
              <button class="la-btn" onclick="adjLA('${e.nombre}',${i},+1)" aria-label="Sumar un LA">${ic('plus','ic-16')}</button>
            </div>
          </div>
          <div>
            <div class="comp-metric-lbl">Festivos trabajados</div>
            <div class="comp-metric-val">${e.festivos_trabajados}</div>
          </div>
          <div>
            <div class="comp-metric-lbl">Modalidad ${!e.modificable?'<span class="comp-solo-enero">(solo enero)</span>':''}</div>
            <select class="inp-sm" ${!e.modificable?'disabled':''} onchange="changeComp('${e.nombre}',this.value)">
              <option value="cobrar" ${e.modalidad==='cobrar'?'selected':''}>Cobra festivo</option>
              <option value="librar_mismo_dia" ${e.modalidad==='librar_mismo_dia'?'selected':''}>Libra ese día</option>
              <option value="dia_libre_eleccion" ${e.modalidad==='dia_libre_eleccion'?'selected':''}>Acumula LA</option>
            </select>
          </div>
        </div>
        ${e.la_saldo>0?`<button class="btn pri sm mt-12" onclick="openLA('${e.nombre}',${e.la_saldo})">Asignar LA en semana ${ic('arrow-right','ic-16')}</button>`:''}
        ${e.modalidad==='cobrar'?`<div class="alert warn mt-10 mb-0">${ic('info','ic-16')}<span>Si le toca libre un festivo, ese día se acumula automáticamente como LA.</span></div>`:''}
      </div>
    </div>`).join('');
}

function toggleComp(i){
  const b=document.getElementById('cbody-'+i),a=document.getElementById('carr-'+i);
  b.classList.toggle('hidden');
  a.innerHTML = ic(b.classList.contains('hidden') ? 'chevron-down' : 'chevron-up','ic-18');
}

async function adjLA(nombre, i, delta){
  const r = await api('PUT',`/compensaciones/${encodeURIComponent(nombre)}?dept=${currentDept}`,{ajuste_la:delta});
  if(r.ok){ document.getElementById('lac-'+i).textContent = r.data.la_saldo; }
  else toast(r.error,'err');
}

async function changeComp(nombre, val){
  const r = await api('PUT',`/compensaciones/${encodeURIComponent(nombre)}?dept=${currentDept}`,{modalidad:val});
  if(!r.ok){ toast(r.error,'err'); loadCompensaciones(); }
  else toast('Modalidad actualizada');
}

function openLA(nombre, saldo){
  laNombre = nombre;
  document.getElementById('m-la-title').textContent = `Asignar LA — ${nombre}`;
  document.getElementById('m-la-info').innerHTML =
    `${ic('info','ic-16')}<span><strong>${nombre}</strong> tiene <strong>${saldo}</strong> día(s) LA acumulados.<br>
     Este libre es <strong>adicional</strong> a los 2 días libres normales de esa semana.</span>`;
  // FIX: reliable next-Monday calculation (original could return today on Sundays)
  const _n = new Date();
  const _dow = _n.getDay(); // 0=Sun, 1=Mon, ...
  const _diff = _dow === 0 ? 1 : 8 - _dow; // days until next Monday
  const nextMon = new Date(_n);
  nextMon.setDate(_n.getDate() + _diff);
  document.getElementById('m-la-week').value = isoDate(nextMon);
  document.getElementById('modal-la').classList.remove('hidden');
}

document.getElementById('btn-save-la').onclick = async ()=>{
  if(!laNombre) return;
  const week = document.getElementById('m-la-week').value;
  const day  = document.getElementById('m-la-day').value;
  if(!week){toast('Selecciona una semana','err');return;}
  const r = await api('POST',`/compensaciones/${encodeURIComponent(laNombre)}/usar-la`,{semana:week,dia:day,dept:currentDept});
  if(!r.ok){toast(r.error,'err');return;}
  closeModal('modal-la');
  toast(r.data.message || 'LA asignado correctamente');
  loadCompensaciones();
};

// ── VACACIONES ────────────────────────────────────────────────────────────
async function loadVacaciones(){
  const r = await api('GET',`/vacaciones?dept=${currentDept}`);
  if(!r.ok) return;
  document.getElementById('vac-count').textContent = `${r.data.length} periodo(s) registrado(s)`;
  if(!r.data.length){
    document.getElementById('vac-list').innerHTML = `<div class="card0">${vacio({
      icono:'palmtree', titulo:'No hay vacaciones registradas',
      texto:'Los periodos que añadas aquí se respetan al generar el horario de esas semanas.',
      accion:`<button class="btn pri" onclick="document.getElementById('btn-add-vac').click()">${ic('plus','ic-16')} Añadir periodo</button>`})}</div>`;
    return;
  }
  document.getElementById('vac-list').innerHTML = r.data.map(v=>`
    <div class="card vac-card">
      <div class="avatar av-36">${initials(v.nombre)}</div>
      <div class="flex-1">
        <div class="emp-row-name">${v.nombre}</div>
        <div class="t-sm muted mt-2">
          ${new Date(v.inicio+'T00:00:00').toLocaleDateString('es-ES',{day:'2-digit',month:'short'})}
          → ${new Date(v.fin+'T00:00:00').toLocaleDateString('es-ES',{day:'2-digit',month:'short',year:'numeric'})}
          · ${v.dias} días${v.nota?' · '+escapeHtml(v.nota):''}
        </div>
      </div>
      <span class="tag ${v.estado==='activo'?'tag-activo':(v.estado==='próximo'?'':'tag-pasado')}">${v.estado}</span>
      <button class="btn danger sm" onclick="deleteVac('${v.id}')">${ic('trash','ic-16')} Eliminar</button>
    </div>`).join('');
}

document.getElementById('btn-add-vac').onclick = async ()=>{
  const r = await api('GET',`/plantilla?dept=${currentDept}`);
  const sel = document.getElementById('m-vac-emp');
  sel.innerHTML = (r.data||[]).map(e=>`<option>${e.name}</option>`).join('');
  document.getElementById('m-vac-ini').value='';
  document.getElementById('m-vac-fin').value='';
  document.getElementById('m-vac-nota').value='';
  document.getElementById('modal-vac').classList.remove('hidden');
};

document.getElementById('btn-save-vac').onclick = async ()=>{
  const r = await api('POST','/vacaciones',{dept: currentDept,
    nombre:document.getElementById('m-vac-emp').value,
    inicio:document.getElementById('m-vac-ini').value,
    fin:document.getElementById('m-vac-fin').value,
    nota:document.getElementById('m-vac-nota').value,
  });
  if(!r.ok){toast(r.error,'err');return;}
  closeModal('modal-vac'); toast('Vacaciones registradas'); loadVacaciones();
};

async function deleteVac(id){
  if(!confirm('¿Eliminar este periodo de vacaciones?')) return;
  const r = await api('DELETE',`/vacaciones/${id}?dept=${currentDept}`);
  if(r.ok){toast('Periodo eliminado');loadVacaciones();}
  else toast(r.error,'err');
}

// ── HISTORIAL ─────────────────────────────────────────────────────────────
async function loadHistorial(){
  const [rh, rm] = await Promise.all([api('GET',`/horario/semanas?dept=${currentDept}`), api('GET',`/meta?dept=${currentDept}`)]);
  if(rm.ok){
    const d=rm.data;
    document.getElementById('hist-stats').innerHTML=`
      <div class="stat"><div class="stat-val">${d.semanas_guardadas}</div><div class="stat-lbl">Semanas guardadas</div></div>
      <div class="stat"><div class="stat-val">${d.festivos_registrados}</div><div class="stat-lbl">Festivos registrados</div></div>
      <div class="stat"><div class="stat-val">${d.total_la_pendientes}</div><div class="stat-lbl">LA pendientes</div></div>`;
  }
  if(rh.ok){
    document.getElementById('hist-list').innerHTML = rh.data.slice(0,15).map(s=>`
      <div class="hist-row">
        <span class="hist-lbl">${s.label}</span>
        <span class="flex-1 muted">${(s.staff||[]).length} empleados</span>
        <a href="/api/horario/exportar-semana/${s.semana}?dept=${currentDept}" download class="btn sm ghost">${ic('download','ic-16')} Excel</a>
        <button class="btn sm" onclick="previewSemana('${s.semana}')">Ver ${ic('chevron-right','ic-16')}</button>
      </div>`).join('') || vacio({icono:'history', titulo:'No hay semanas guardadas aún',
        texto:'Cada horario que generes queda registrado aquí y alimenta las estadísticas.'});
  }
}

async function previewSemana(semana){
  const r = await api('GET',`/horario/semanas?dept=${currentDept}`);
  if(!r.ok) return;
  const entrada = r.data.find(s=>s.semana===semana);
  if(!entrada){ toast('Semana no encontrada','err'); return; }

  const d = new Date(semana+'T00:00:00');
  const fin = new Date(d); fin.setDate(fin.getDate()+6);
  const titulo = `${d.toLocaleDateString('es-ES',{day:'2-digit',month:'short'})} – ${fin.toLocaleDateString('es-ES',{day:'2-digit',month:'short',year:'numeric'})}`;

  const cols = ['Lun','Mar','Mié','Jue','Vie','Sáb','Dom'];
  const headerCells = cols.map(c=>`<th>${c}</th>`).join('');
  const rows = (entrada.staff||[]).map(p=>{
    const shifts = entrada.schedule[p.name]||Array(7).fill('Libre');
    const cells = shifts.map(s=>`<td><span class="sc ${s} sc-mini">${s}</span></td>`).join('');
    return `<tr><td class="nl nowrap w-500">${p.name}</td>${cells}</tr>`;
  }).join('');

  let existing = document.getElementById('modal-preview');
  if(existing) existing.remove();

  const modal = document.createElement('div');
  modal.id = 'modal-preview';
  modal.className = 'overlay';
  modal.innerHTML = `
    <div class="modal modal-lg">
      <div class="modal-hd">
        <span>Horario — ${titulo}</span>
        <button class="btn ghost btn-icon" onclick="document.getElementById('modal-preview').remove()" aria-label="Cerrar">${ic('x','ic-18')}</button>
      </div>
      <div class="scroll-x">
        <table class="tbl">
          <thead><tr><th class="nl">Empleado</th>${headerCells}</tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    </div>`;
  modal.addEventListener('click', e=>{ if(e.target===modal) modal.remove(); });
  document.body.appendChild(modal);
}

// ── ASISTENTE IA ──────────────────────────────────────────────────────────
const QUICK_QS = [
  '¿Quién trabajó más festivos este año?',
  '¿Qué empleados tienen LA pendientes?',
  'Resume el estado del equipo',
  '¿Hay algún empleado sin preferencia de turno asignada?',
  '¿Qué trabajadores acumulan días LA?',
];

function setupAsistente(){
  document.getElementById('quick-qs').innerHTML = QUICK_QS.map(q=>
    `<button class="btn sm" onclick="doAsk(${JSON.stringify(q)})">${q}</button>`
  ).join('');
}

async function doAsk(query){
  const out = document.getElementById('ai-out');
  out.innerHTML = '<span class="spinner"></span> Consultando…';
  const rp = await api('GET',`/plantilla?dept=${currentDept}`);
  const rc = await api('GET',`/compensaciones?dept=${currentDept}`);
  const ctx = {
    empleados: (rp.data||[]).map(e=>({nombre:e.name,rol:e.role,comp:e.comp,la:e.la_saldo,festivos:e.festivos_trabajados})),
    compensaciones: rc.data||[],
  };
  try {
    const resp = await fetch('https://api.anthropic.com/v1/messages',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({
        model:'claude-sonnet-4-20250514',
        max_tokens:600,
        system:`Eres el asistente del sistema de horarios de un hotel en Valencia.
Respondes en español, de forma concisa y práctica (máx 150 palabras).
Contexto del equipo: ${JSON.stringify(ctx)}`,
        messages:[{role:'user',content:query}]
      })
    });
    const data = await resp.json();
    out.textContent = data.content?.[0]?.text || 'Sin respuesta';
  } catch(e){ out.textContent = 'Error conectando con el asistente.'; }
}

document.getElementById('btn-ask').onclick = ()=>{
  const q = document.getElementById('ai-input').value.trim();
  if(q){ doAsk(q); document.getElementById('ai-input').value=''; }
};
document.getElementById('ai-input').addEventListener('keydown',e=>{
  if(e.key==='Enter') document.getElementById('btn-ask').click();
});

// ── PETICIONES ───────────────────────────────────────────────────────────────
let petWeekOffset  = 0;
let petEntriesData = {};   // datos completos del servidor: {nombre: {dias,la,motivo}}
let petManualOrder = null; // orden manual guardado en servidor (array o null)
let petOrderDraft  = null; // orden en edición aún no guardado (array o null)

function updatePetWeekLabel(){
  const dates = weekDates(petWeekOffset);
  const mon = dates[0], sun = dates[6];
  document.getElementById('pet-week-lbl').textContent =
    `${fmtDate(mon)} – ${fmtDate(sun)} ${sun.getFullYear()}`;
}

document.getElementById('pet-prev').onclick = ()=>{ petWeekOffset--; updatePetWeekLabel(); loadPeticiones(); };
document.getElementById('pet-next').onclick = ()=>{ petWeekOffset++; updatePetWeekLabel(); loadPeticiones(); };

async function loadPeticiones(){
  const semana = isoDate(getMonday(petWeekOffset));
  const r = await api('GET',`/peticiones?semana=${semana}&dept=${currentDept}`);
  const el = document.getElementById('pet-list');
  if(!r.ok){
    el.innerHTML = vacio({icono:'error', titulo:'No se han podido cargar las peticiones',
                          texto: r.error || 'Vuelve a intentarlo en unos segundos.'});
    return;
  }
  petEntriesData = r.data || {};
  petManualOrder = r.priority_order || null;
  petOrderDraft  = null;
  renderPetList();
  loadPetCupoInfo();
}

function _petDisplayOrder(){
  if(petOrderDraft)  return [...petOrderDraft];
  if(petManualOrder) return [...petManualOrder];
  return Object.keys(petEntriesData);
}

function _petItemHtml(nombre, entrada, semana, idx, total, showReorder){
  const dias   = Array.isArray(entrada) ? entrada : (entrada.dias||[]);
  const la     = Array.isArray(entrada) ? [] : (entrada.la||[]);
  const motivo = Array.isArray(entrada) ? '' : (entrada.motivo||'');
  const origen = Array.isArray(entrada) ? 'jefe' : (entrada.origen||'jefe');
  const diasStr   = dias.length ? `Libre: <strong>${dias.join(', ')}</strong>` : '';
  const laStr     = la.length   ? `&nbsp;· LA extra: <strong>${la.join(', ')}</strong>` : '';
  const motivoStr = motivo ? `<div class="pet-motivo">${escapeHtml(motivo)}</div>` : '';
  const origenBadge = origen === 'jefe'
    ? `<span class="tag" title="Creada/gestionada por encargado o admin">${ic('crown','ic-16')} jefe</span>` : '';
  const reorderHtml = showReorder ? `
    <div class="reorder">
      <button class="btn ghost xs" ${idx===0?'disabled':''} onclick="movePet('${nombre}',-1)" title="Subir prioridad">${ic('arrow-up','ic-16')}</button>
      <button class="btn ghost xs" ${idx===total-1?'disabled':''} onclick="movePet('${nombre}',1)" title="Bajar prioridad">${ic('arrow-down','ic-16')}</button>
    </div>` : '';
  const numBadge = showReorder ? `<div class="pet-num">#${idx+1}</div>` : '';
  const isEmpleado = currentUser?.rol === 'empleado';
  const modificarBtn = isEmpleado ? '' :
    `<button class="btn sm" onclick="modificarPeticion('${semana}','${nombre}')">Modificar</button>`;
  return `<div class="list-row">
    ${numBadge}
    <div class="avatar av-30">${initials(nombre)}</div>
    <div class="flex-1">
      <div class="emp-row-name row gap-6">${nombre}${origenBadge}</div>
      <div class="t-sm muted mt-2">${diasStr}${laStr}</div>
      ${motivoStr}
    </div>
    ${reorderHtml}
    ${modificarBtn}
    <button class="btn danger sm" onclick="deletePeticion('${semana}','${nombre}')">Cancelar</button>
  </div>`;
}

function renderPetList(){
  const el     = document.getElementById('pet-list');
  const semana = isoDate(getMonday(petWeekOffset));
  const isEmpleado = currentUser?.rol === 'empleado';

  if(isEmpleado){
    const vinculado = currentUser?.vinculado;
    const entrada   = vinculado ? petEntriesData[vinculado] : null;
    if(!entrada){
      el.innerHTML = vacio({icono:'inbox', titulo:'No tienes ninguna petición esta semana',
        texto:'Puedes solicitar un día libre mientras te quede cupo del mes.',
        accion:`<button class="btn pri" onclick="document.getElementById('btn-add-pet').click()">${ic('plus','ic-16')} Solicitar día libre</button>`});
    } else {
      el.innerHTML = _petItemHtml(vinculado, entrada, semana, 0, 1, false);
    }
    return;
  }

  // Vista admin / encargado
  const order = _petDisplayOrder();
  if(!order.length){
    el.innerHTML = vacio({icono:'inbox', titulo:'No hay peticiones para esta semana',
      texto:'Aquí aparecerán las peticiones de días libres del equipo, ordenadas por prioridad.',
      accion:`<button class="btn pri" onclick="document.getElementById('btn-add-pet').click()">${ic('plus','ic-16')} Nueva petición</button>`});
    return;
  }

  const isDirty  = petOrderDraft !== null;
  const isManual = petManualOrder !== null;
  const total    = order.length;

  let headerHtml = '';
  if(total > 1){
    let statusLabel = '';
    if(isDirty){
      statusLabel = `<span class="t-sm pet-estado-dirty">Orden modificado — sin guardar</span>`;
    } else if(isManual){
      statusLabel = `<span class="t-sm pet-estado-manual">Prioridad manual activa</span>`;
    } else {
      statusLabel = `<span class="t-sm muted">Prioridad automática (por historial)</span>`;
    }
    const actionBtns = isDirty
      ? `<button class="btn pri sm" onclick="savePetOrder()">Guardar orden</button>
         <button class="btn sm" onclick="cancelPetOrder()">Cancelar</button>`
      : (isManual
          ? `<button class="btn sm" onclick="resetPetOrder()">Restablecer automática</button>`
          : '');
    headerHtml = `<div class="pet-hd">
      ${statusLabel}
      <div class="row gap-6">${actionBtns}</div>
    </div>`;
  }

  const listHtml = order.map((nombre,idx)=>{
    const entrada = petEntriesData[nombre];
    if(!entrada) return '';
    return _petItemHtml(nombre, entrada, semana, idx, total, total > 1);
  }).join('');

  el.innerHTML = headerHtml + listHtml;
}

function movePet(nombre, dir){
  const order = _petDisplayOrder();
  const idx   = order.indexOf(nombre);
  if(idx < 0) return;
  const newIdx = idx + dir;
  if(newIdx < 0 || newIdx >= order.length) return;
  [order[idx], order[newIdx]] = [order[newIdx], order[idx]];
  petOrderDraft = order;
  renderPetList();
}

async function savePetOrder(){
  if(!petOrderDraft) return;
  const semana = isoDate(getMonday(petWeekOffset));
  const r = await api('PUT',`/peticiones/${semana}/prioridad`,{order: petOrderDraft, dept: currentDept});
  if(!r.ok){ toast(r.error,'err'); return; }
  petManualOrder = petOrderDraft;
  petOrderDraft  = null;
  toast('Orden de prioridad guardado');
  renderPetList();
}

function cancelPetOrder(){
  petOrderDraft = null;
  renderPetList();
}

async function resetPetOrder(){
  if(!confirm('¿Restablecer la prioridad automática basada en historial?')) return;
  const semana = isoDate(getMonday(petWeekOffset));
  const r = await api('PUT',`/peticiones/${semana}/prioridad`,{order: null, dept: currentDept});
  if(!r.ok){ toast(r.error,'err'); return; }
  petManualOrder = null;
  petOrderDraft  = null;
  toast('Prioridad restablecida a automática');
  renderPetList();
}

let petModalReady = false;
function initPetModal(){
  if(petModalReady) return;
  petModalReady = true;
  document.querySelectorAll('#m-pet-dias .pill').forEach(p=>p.onclick=()=>p.classList.toggle('on'));
  document.getElementById('btn-save-pet').onclick = async ()=>{
    const nombre = document.getElementById('m-pet-emp').value;
    const semana = document.getElementById('m-pet-week').value;
    const dias = [...document.querySelectorAll('#m-pet-dias .pill.on')].map(p=>p.dataset.v);
    const motivo = document.getElementById('m-pet-motivo').value.trim();
    const esOrigenJefe = currentUser?.rol !== 'empleado';
    const justificacion = document.getElementById('m-pet-justif').value.trim();
    if(!semana){ toast('Selecciona una semana','err'); return; }
    if(!dias.length){ toast('Selecciona al menos un día','err'); return; }
    if(esOrigenJefe && justificacion.length < 5){
      toast('La justificación es obligatoria (mínimo 5 caracteres) al crear una petición en nombre de otro empleado','err');
      return;
    }
    const r = await api('POST','/peticiones',{nombre, semana, dias, motivo: motivo||null, justificacion, dept: currentDept});
    if(!r.ok){ toast(r.error,'err'); return; }
    closeModal('modal-pet');
    toast('Petición guardada');
    loadPeticiones();
    if(document.getElementById('sec-dashboard').classList.contains('active')) cargarDashboard();
  };
}

document.getElementById('btn-add-pet').onclick = async ()=>{
  const isEmpleado = currentUser?.rol === 'empleado';
  if(isEmpleado && !currentUser?.vinculado){
    toast('Tu cuenta no está vinculada a ningún empleado de la plantilla','err'); return;
  }
  initPetModal();
  const empRow = document.getElementById('m-pet-emp-row');
  const sel    = document.getElementById('m-pet-emp');
  if(isEmpleado){
    empRow.classList.add('hidden');
    sel.innerHTML = `<option>${currentUser.vinculado}</option>`;
  } else {
    empRow.classList.remove('hidden');
    const r = await api('GET',`/plantilla?dept=${currentDept}`);
    sel.innerHTML = (r.data||[]).map(e=>`<option>${e.name}</option>`).join('');
  }
  // Justificación obligatoria solo cuando la petición no es propia (origen "jefe").
  document.getElementById('m-pet-justif-row').classList.toggle('hidden', isEmpleado);
  document.getElementById('m-pet-justif').value = '';
  document.getElementById('m-pet-week').value = isoDate(getMonday(petWeekOffset));
  document.querySelectorAll('#m-pet-dias .pill').forEach(p=>p.classList.remove('on'));
  document.getElementById('m-pet-motivo').value = '';
  document.getElementById('modal-pet').classList.remove('hidden');
};

async function deletePeticion(semana, nombre){
  if(!confirm(`¿Cancelar la petición de ${nombre}?`)) return;
  const r = await api('DELETE',`/peticiones/${semana}/${encodeURIComponent(nombre)}?dept=${currentDept}`);
  if(r.ok){ toast('Petición cancelada'); loadPeticiones(); }
  else toast(r.error,'err');
}

async function modificarPeticion(semana, nombre){
  const entrada = petEntriesData[nombre];
  if(!entrada) return;
  const diasActuales = (entrada.dias||[]).join(', ');
  const respuesta = prompt(
    `Días de libre para ${nombre} (separados por coma: Lunes, Martes, ...)`,
    diasActuales);
  if(respuesta === null) return;
  const dias = respuesta.split(',').map(d=>d.trim()).filter(Boolean);
  if(!dias.length){ toast('Debes indicar al menos un día','err'); return; }
  const nuevoNombre = prompt('Empleado de la petición (déjalo igual si no cambia):', nombre);
  if(nuevoNombre === null) return;
  const body = {dias, dept: currentDept};
  if(nuevoNombre.trim() && nuevoNombre.trim() !== nombre) body.nombre = nuevoNombre.trim();
  const r = await api('PUT',`/peticiones/${semana}/${encodeURIComponent(nombre)}`, body);
  if(!r.ok){ toast(r.error,'err'); return; }
  toast('Petición modificada');
  loadPeticiones();
}

// ── CUPOS DE PETICIONES ───────────────────────────────────────────────────────

async function loadPetCupoInfo(){
  const info = document.getElementById('pet-cupo-info');
  if(currentUser?.rol !== 'empleado' || !currentUser?.vinculado){ info.textContent=''; return; }
  const semana = isoDate(getMonday(petWeekOffset));
  const r = await api('GET',`/cupos/mio?semana=${semana}&dept=${currentDept}`);
  if(!r.ok){ info.textContent=''; return; }
  const {usadas, total, mes} = r.data;
  info.textContent = `Peticiones ${mes}: ${usadas} de ${total} usadas`;
}

let cupoExtraEmpleados = [];

async function loadCuposPanelManager(){
  const isEmpleado = currentUser?.rol === 'empleado';
  document.getElementById('pet-cupo-panel').classList.toggle('hidden', isEmpleado);
  document.getElementById('pet-transparencia-panel').classList.toggle('hidden', isEmpleado);
  if(isEmpleado) return;

  const [rPlantilla, rResumen] = await Promise.all([
    api('GET', `/plantilla?dept=${currentDept}`),
    api('GET', `/cupos/resumen?dept=${currentDept}`),
  ]);

  if(rPlantilla.ok){
    cupoExtraEmpleados = rPlantilla.data || [];
    const sel = document.getElementById('cupo-extra-emp');
    sel.innerHTML = cupoExtraEmpleados.map(e=>`<option>${e.name}</option>`).join('');
  }
  if(!document.getElementById('cupo-extra-mes').value){
    document.getElementById('cupo-extra-mes').value = new Date().toISOString().slice(0,7);
  }

  const tbody = document.getElementById('cupo-transparencia-body');
  if(!rResumen.ok){
    tbody.innerHTML = vacioFila(3, {icono:'error', titulo:'Error cargando los cupos'});
    return;
  }
  const filas = rResumen.data || [];
  if(!filas.length){
    tbody.innerHTML = vacioFila(3, {icono:'users', titulo:'Sin empleados en este departamento'});
    return;
  }
  document.getElementById('cupo-th-actual').textContent    = `Mes actual (${filas[0].mes_actual.mes})`;
  document.getElementById('cupo-th-siguiente').textContent = `Mes siguiente (${filas[0].mes_siguiente.mes})`;
  const celda = c => `<span class="${c.usadas > c.total ? 'cupo-excedido' : ''}">${c.usadas} / ${c.total}</span>`;
  tbody.innerHTML = filas.map(f=>`<tr>
    <td class="nl">
      <div class="row gap-6">
        <div class="avatar av-26">${initials(f.nombre)}</div>
        <span class="t-sm w-500">${f.nombre}</span>
      </div>
    </td>
    <td>${celda(f.mes_actual)}</td>
    <td>${celda(f.mes_siguiente)}</td>
  </tr>`).join('');
}

document.getElementById('btn-guardar-cupo-extra').onclick = async ()=>{
  const nombre = document.getElementById('cupo-extra-emp').value;
  const mes    = document.getElementById('cupo-extra-mes').value;
  const extra  = document.getElementById('cupo-extra-valor').value;
  if(!nombre || !mes){ toast('Selecciona empleado y mes','err'); return; }
  const r = await api('PUT','/cupos/extra',{nombre, mes, extra: Number(extra), dept: currentDept});
  if(!r.ok){ toast(r.error,'err'); return; }
  toast('Cupo extra guardado');
  loadCuposPanelManager();
};

// ── ESTADÍSTICAS ─────────────────────────────────────────────────────────────
const COMP_LABEL = {cobrar:'Cobra',librar_mismo_dia:'Libra ese día',dia_libre_eleccion:'Acumula LA'};

async function loadEstadisticas(){
  const r = await api('GET',`/estadisticas?dept=${currentDept}`);
  const tbody = document.getElementById('stats-body');
  if(!r.ok){ tbody.innerHTML = vacioFila(11,{icono:'error', titulo:'Error al cargar las estadísticas'}); return; }
  if(!r.data.length){
    tbody.innerHTML = vacioFila(11,{icono:'chart', titulo:'Sin semanas en el historial aún',
      texto:'Las estadísticas se calculan a partir de los horarios ya generados.'});
    return;
  }

  tbody.innerHTML = r.data.map(e=>`<tr>
      <td class="nl">
        <div class="row gap-6">
          <div class="avatar av-26">${initials(e.nombre)}</div>
          <span class="t-sm w-500">${e.nombre}</span>
        </div>
      </td>
      <td class="mono t-sm">${e.semanas}</td>
      <td class="col-man"><span class="mini-badge man">${e['Mañana']}</span></td>
      <td class="col-tar"><span class="mini-badge tar">${e['Tarde']}</span></td>
      <td class="col-noc"><span class="mini-badge noc">${e['Noche']}</span></td>
      <td class="col-par"><span class="mini-badge par">${e['Partido']}</span></td>
      <td class="col-lib"><span class="mini-badge lib">${e['Libre']}</span></td>
      <td class="col-fds"><span class="mini-badge fds">${e.fds_libres||0}</span></td>
      <td class="t-sm">${e.peticiones}</td>
      <td><span class="la-chip ${e.la_saldo>0?'on':''}">${e.la_saldo}</span></td>
      <td><span class="tag">${COMP_LABEL[e.modalidad]||e.modalidad}</span></td>
    </tr>`).join('');
}


document.querySelectorAll('.overlay').forEach(o=>{
  o.addEventListener('click',e=>{ if(e.target===o) o.classList.add('hidden'); });
});

// ══════════════════════════════════════════════════════════════════
// AUTH — LOGIN / SESSION / PERMISOS
// ══════════════════════════════════════════════════════════════════

let currentUser = null;
let currentDept = 'recepcion';

// Dept selector
document.getElementById('dept-selector').onchange = function() {
  currentDept = this.value;
  const active = document.querySelector('.nav-item.active');
  if (active) active.click();
};

// API helper — handles 401 globally by redirecting to login
async function api(method, path, body=null){
  const opts = {method, headers:{'Content-Type':'application/json'}};
  if(body) opts.body = JSON.stringify(body);
  const r = await fetch('/api'+path, opts);
  // Defensivo: un error no manejado en el backend (excepción sin capturar)
  // hace que Flask devuelva su página de error 500 por defecto (HTML, no
  // JSON) -- sin este try/catch, r.json() lanzaría una excepción aquí que
  // dejaría cualquier botón "Generando.../Borrando..." bloqueado para
  // siempre en la interfaz, porque el código que lo restaura nunca llegaría
  // a ejecutarse (ver diagnóstico de).
  let d;
  try{
    d = await r.json();
  }catch(e){
    return {ok:false, error:`Error inesperado del servidor (${r.status}). Vuelve a intentarlo o contacta al administrador si persiste.`};
  }
  if(d.login){ showLogin(); return d; }
  return d;
}

function tienePermiso(p){ return currentUser?.permisos?.includes(p); }

function showLogin(){
  currentUser = null;
  document.getElementById('login-page').classList.add('active');
  document.getElementById('app').classList.add('hidden');
}

async function showApp(user){
  currentUser = user;
  document.getElementById('login-page').classList.remove('active');
  document.getElementById('app').classList.remove('hidden');
  await cargarRolesTodosDepts();

  // El hover destructivo del botón de salir lo hace ahora .btn-icon.danger-hover
  // en el CSS, en vez de cuatro manipuladores inline de estilo.
  document.getElementById('user-badge-area').innerHTML = `
    <div class="user-badge" onclick="document.getElementById('modal-pass').classList.remove('hidden')" title="Cambiar contraseña">
      <span class="user-badge-name">${user.nombre}</span>
      <span class="role-pill ${user.rol}">${user.rol}</span>
    </div>
    <button class="btn-icon danger-hover" onclick="doLogout()" title="Cerrar sesión" aria-label="Cerrar sesión">${ic('power','ic-18')}</button>`;

  // Visibilidad por rol mediante clase, no style inline: así una regla CSS
  // no puede quedar pisada por un display heredado del HTML.
  document.querySelectorAll('.admin-only').forEach(el=>{
    el.classList.toggle('hidden', user.rol !== 'admin');
  });
  document.querySelectorAll('.empleado-only').forEach(el=>{
    el.classList.toggle('hidden', user.rol !== 'empleado');
  });

  applyNavPermissions();
  updateWeekLabel();
  // Página de aterrizaje según rol: el empleado entra directo a su panel
  // personal; jefe/encargado/admin mantienen el horario semanal de siempre.
  irASeccion(user.rol === 'empleado' ? 'dashboard' : 'horario');
}

function applyNavPermissions(){
  if(currentUser?.rol === 'empleado'){
    const allowed = ['dashboard','horario','peticiones'];
    document.querySelectorAll('.nav-item[data-sec]').forEach(el=>{
      el.classList.toggle('hidden', !allowed.includes(el.dataset.sec));
    });
  }
}

// Login form
document.getElementById('btn-login').onclick = doLogin;
document.getElementById('l-pass').addEventListener('keydown', e=>{
  if(e.key==='Enter') doLogin();
});
document.getElementById('l-user').addEventListener('keydown', e=>{
  if(e.key==='Enter') document.getElementById('l-pass').focus();
});

async function doLogin(){
  const username = document.getElementById('l-user').value.trim();
  const password = document.getElementById('l-pass').value;
  const errEl    = document.getElementById('login-err');
  const btn      = document.getElementById('btn-login');
  if(!username || !password){ errEl.textContent='Introduce usuario y contraseña.'; errEl.classList.add('visible'); return; }
  btn.textContent='Entrando...'; btn.disabled=true;
  const r = await fetch('/api/auth/login',{
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({username, password})
  });
  const d = await r.json();
  btn.textContent='Entrar'; btn.disabled=false;
  if(!d.ok){
    errEl.textContent = d.error || 'Error de autenticación';
    errEl.classList.add('visible');
    document.getElementById('l-pass').value='';
    return;
  }
  errEl.classList.remove('visible');
  document.getElementById('l-pass').value='';
  await showApp(d.data);
}

async function doLogout(){
  await fetch('/api/auth/logout',{method:'POST'});
  showLogin();
}

document.getElementById('btn-logout').onclick = doLogout;

// Cambiar contraseña propia
document.getElementById('btn-save-pass').onclick = async ()=>{
  const actual = document.getElementById('p-actual').value;
  const nueva  = document.getElementById('p-nueva').value;
  const repite = document.getElementById('p-repite').value;
  if(nueva !== repite){ toast('Las contraseñas no coinciden','err'); return; }
  if(nueva.length < 6){ toast('Mínimo 6 caracteres','err'); return; }
  const r = await api('POST','/auth/cambiar-password',{actual, nueva});
  if(!r.ok){ toast(r.error,'err'); return; }
  closeModal('modal-pass');
  ['p-actual','p-nueva','p-repite'].forEach(id=>document.getElementById(id).value='');
  toast('Contraseña cambiada correctamente');
};

// ── USUARIOS (admin) ──────────────────────────────────────────────
let editUsrId = null;

async function loadUsuarios(){
  const r = await api('GET','/usuarios');
  if(!r.ok) return;
  const users = r.data;
  document.getElementById('usr-count').textContent = `${users.length} usuarios registrados`;
  document.getElementById('usr-list').innerHTML = users.map(u=>`
    <div class="emp-row" onclick="openEditUsr('${u.id}')">
      <div class="avatar av-34">${initials(u.nombre)}</div>
      <div class="flex-1">
        <div class="emp-row-name">${u.nombre}
          ${!u.activo?'<span class="usr-inactivo">[inactivo]</span>':''}
        </div>
        <div class="emp-row-sub">@${u.username}${u.empleado_vinculado?' · '+u.empleado_vinculado:''}</div>
      </div>
      <span class="rol-chip ${u.rol}">${u.rol}</span>
      <span class="chev">${ic('chevron-right','ic-18')}</span>
    </div>`).join('');
}

document.getElementById('btn-add-usr').onclick = async ()=>{
  editUsrId = null;
  document.getElementById('m-usr-title').textContent = 'Nuevo usuario';
  document.getElementById('m-usr-username').value='';
  document.getElementById('m-usr-username').disabled=false;
  document.getElementById('m-usr-nombre').value='';
  document.getElementById('m-usr-rol').value='empleado';
  document.getElementById('m-usr-pass').value='';
  document.getElementById('m-usr-pass-label').textContent='Contraseña';
  document.getElementById('m-usr-pass2-row').classList.add('hidden');
  document.getElementById('btn-del-usr').classList.add('hidden');
  await _fillVincSelect(null);
  document.getElementById('modal-usr').classList.remove('hidden');
};

async function openEditUsr(id){
  editUsrId = id;
  const r = await api('GET','/usuarios');
  const u = r.data?.find(x=>x.id===id);
  if(!u) return;
  document.getElementById('m-usr-title').textContent = 'Editar usuario';
  document.getElementById('m-usr-username').value = u.username;
  document.getElementById('m-usr-username').disabled = true;
  document.getElementById('m-usr-nombre').value = u.nombre;
  document.getElementById('m-usr-rol').value = u.rol;
  document.getElementById('m-usr-pass').value='';
  document.getElementById('m-usr-pass-label').textContent='Nueva contraseña (dejar vacío para no cambiar)';
  document.getElementById('m-usr-pass2-row').classList.remove('hidden');
  document.getElementById('m-usr-activo').value = u.activo ? 'true':'false';
  document.getElementById('btn-del-usr').classList.toggle('hidden', u.id===currentUser?.id);
  await _fillVincSelect(u.empleado_vinculado);
  document.getElementById('modal-usr').classList.remove('hidden');
}

async function _fillVincSelect(selected){
  const rp = await api('GET',`/plantilla?dept=${currentDept}`);
  const sel = document.getElementById('m-usr-vinc');
  sel.innerHTML = '<option value="">— Sin vincular —</option>' +
    (rp.data||[]).map(e=>`<option value="${e.name}" ${e.name===selected?'selected':''}>${e.name}</option>`).join('');
}

document.getElementById('btn-save-usr').onclick = async ()=>{
  const nombre   = document.getElementById('m-usr-nombre').value.trim();
  const username = document.getElementById('m-usr-username').value.trim();
  const rol      = document.getElementById('m-usr-rol').value;
  const pass     = document.getElementById('m-usr-pass').value;
  const vinc     = document.getElementById('m-usr-vinc').value || null;
  const activo   = document.getElementById('m-usr-activo').value === 'true';

  if(!nombre){ toast('El nombre es obligatorio','err'); return; }

  let r;
  if(editUsrId === null){
    if(!username){ toast('El usuario es obligatorio','err'); return; }
    if(!pass){ toast('La contraseña es obligatoria para nuevos usuarios','err'); return; }
    r = await api('POST','/usuarios',{username, nombre, rol, password:pass, empleado_vinculado:vinc});
  } else {
    const body = {nombre, rol, activo, empleado_vinculado:vinc};
    if(pass) body.password = pass;
    r = await api('PUT',`/usuarios/${editUsrId}`, body);
  }
  if(!r.ok){ toast(r.error,'err'); return; }
  closeModal('modal-usr');
  toast(editUsrId===null ? 'Usuario creado' : 'Usuario actualizado');
  loadUsuarios();
};

async function deleteUsr(){
  if(!editUsrId) return;
  if(!confirm('¿Eliminar este usuario? Esta acción no se puede deshacer.')) return;
  const r = await api('DELETE',`/usuarios/${editUsrId}`);
  if(!r.ok){ toast(r.error,'err'); return; }
  closeModal('modal-usr');
  toast('Usuario eliminado');
  loadUsuarios();
}

// ── LOG DE ACTIVIDAD ────────────────────────────────────────────────
// El color de cada acción vive en el CSS (.log-accion.accion-XXX): una acción
// desconocida simplemente no lleva clase y hereda el gris secundario.

async function loadLog(){
  const r = await api('GET','/log?limite=100');
  const tbody = document.getElementById('log-body');
  if(!r.ok){ tbody.innerHTML = vacioFila(6,{icono:'error', titulo:'Error al cargar el registro'}); return; }
  if(!r.data.length){
    tbody.innerHTML = vacioFila(6,{icono:'scroll-text', titulo:'Sin registros aún',
      texto:'Aquí se anota cada acción relevante: accesos, generaciones y cambios de datos.'});
    return;
  }
  tbody.innerHTML = r.data.map(entry=>`<tr>
      <td class="nl mono t-xs muted nowrap">${entry.ts}</td>
      <td class="nl t-sm w-500">${entry.usuario}</td>
      <td class="nl"><span class="tag">${entry.rol}</span></td>
      <td class="nl"><span class="log-accion accion-${entry.accion}">${entry.accion}</span></td>
      <td class="nl t-sm muted">${entry.detalle||'—'}</td>
      <td class="nl mono t-xs muted-3">${entry.ip}</td>
    </tr>`).join('');
}

// ── AUDITORÍA DE INTERVENCIONES (solo admin) ─────────────────────────────
let auditoriaData = null;
let audSortCol = 'peticiones_origen_jefe';
let audSortAsc = false;

const AUD_COLUMNAS = [
  {key:'empleado', label:'Empleado'},
  {key:'ediciones_turno_favorables', label:'Favorables'},
  {key:'ediciones_turno_desfavorables', label:'Desfavorables'},
  {key:'ediciones_turno_neutras', label:'Neutras'},
  {key:'ediciones_en_finde', label:'En finde'},
  {key:'peticiones_origen_jefe', label:'Peticiones jefe'},
  {key:'cupo_extra_total_otorgado', label:'Cupo extra'},
  {key:'cambios_de_rol', label:'Cambios de rol'},
];

const AUD_TOTALES_LABEL = {
  ediciones_turno_favorables:'Ediciones favorables', ediciones_turno_desfavorables:'Ediciones desfavorables',
  ediciones_turno_neutras:'Ediciones neutras', ediciones_en_finde:'Ediciones en finde',
  peticiones_origen_jefe:'Peticiones origen jefe', cupo_extra_total_otorgado:'Cupo extra otorgado',
  cambios_de_rol:'Cambios de rol',
};

const AUD_TIPO_LABEL = {
  edicion_turno:'Edición de turno', peticion_jefe:'Petición (origen jefe)',
  cupo_extra:'Cupo extra', cambio_rol_plantilla:'Cambio de rol',
};

// Campos sobre los que se calcula la desviación respecto a la media del
// equipo (señal de atención, no una acusación — ver nota en la interfaz).
const AUD_CAMPOS_DESVIACION = ['peticiones_origen_jefe', 'ediciones_turno_favorables'];

function _mediaCampo(filas, campo){
  if(!filas.length) return 0;
  return filas.reduce((s,f)=>s+f[campo], 0) / filas.length;
}

async function loadAuditoria(){
  const desdeEl = document.getElementById('aud-desde');
  const hastaEl = document.getElementById('aud-hasta');
  if(!desdeEl.value){
    const hoy = new Date();
    const hace3m = new Date(hoy); hace3m.setMonth(hace3m.getMonth() - 3);
    desdeEl.value = isoDate(hace3m);
    hastaEl.value = isoDate(hoy);
  }
  if(!document.getElementById('aud-dept').value || document.getElementById('aud-dept').dataset.tocado !== '1'){
    document.getElementById('aud-dept').value = currentDept;
  }
  const params = new URLSearchParams({
    dept: document.getElementById('aud-dept').value,
    desde: desdeEl.value, hasta: hastaEl.value,
  });
  const r = await api('GET', `/auditoria/intervenciones?${params}`);
  if(!r.ok){ toast(r.error, 'err'); return; }
  auditoriaData = r.data;
  renderAuditoriaTotales();
  renderAuditoriaResumen();
  renderAuditoriaCharts();
  renderAuditoriaDetalle();
}

document.getElementById('aud-dept').addEventListener('change', e=>{ e.target.dataset.tocado = '1'; });

function renderAuditoriaTotales(){
  const t = auditoriaData.totales_periodo;
  document.getElementById('aud-totales').innerHTML = Object.entries(AUD_TOTALES_LABEL).map(([k,l])=>
    `<div class="stat"><div class="stat-val">${t[k]}</div><div class="stat-lbl">${l}</div></div>`
  ).join('');
}

function renderAuditoriaResumen(){
  const filas = [...auditoriaData.resumen_por_empleado];
  const medias = {};
  AUD_CAMPOS_DESVIACION.forEach(c => medias[c] = _mediaCampo(filas, c));

  filas.sort((a,b)=>{
    const va = a[audSortCol], vb = b[audSortCol];
    if(typeof va === 'string') return audSortAsc ? va.localeCompare(vb) : vb.localeCompare(va);
    return audSortAsc ? va - vb : vb - va;
  });

  document.getElementById('aud-resumen-head').innerHTML = AUD_COLUMNAS.map(c=>
    `<th class="th-sort${c.key==='empleado'?' nl':''}" onclick="ordenarAuditoria('${c.key}')">
      ${c.label}${audSortCol===c.key ? ic(audSortAsc?'chevron-up':'chevron-down','ic-16') : ''}
    </th>`
  ).join('');

  const tbody = document.getElementById('aud-resumen-body');
  if(!filas.length){
    tbody.innerHTML = vacioFila(AUD_COLUMNAS.length, {icono:'users',
      titulo:'Sin empleados en este departamento'});
    return;
  }
  tbody.innerHTML = filas.map(f=>{
    const desviado = AUD_CAMPOS_DESVIACION.some(c => medias[c] > 0 && f[c] > medias[c] * 2);
    return `<tr class="${desviado?'fila-desviada':''}">
      <td class="nl w-500">${f.empleado}${desviado?` <span title="Desviación respecto a la media del equipo">${ic('warn','ic-16')}</span>`:''}</td>
      <td>${f.ediciones_turno_favorables}</td>
      <td>${f.ediciones_turno_desfavorables}</td>
      <td>${f.ediciones_turno_neutras}</td>
      <td>${f.ediciones_en_finde}</td>
      <td>${f.peticiones_origen_jefe}</td>
      <td>${f.cupo_extra_total_otorgado}</td>
      <td>${f.cambios_de_rol}</td>
    </tr>`;
  }).join('');
}

function ordenarAuditoria(col){
  if(audSortCol === col) audSortAsc = !audSortAsc;
  else { audSortCol = col; audSortAsc = false; }
  renderAuditoriaResumen();
}

// ── GRÁFICOS COMPARATIVOS (barras SVG a mano, mismo enfoque que dash-chart) ──
// Colores fríos neutros (--aud-fav/--aud-desfav/--aud-jefe): nunca rojo de
// alarma ni verde de aprobación, para no inducir una lectura de
// "culpable/inocente" — ver nota fija bajo cada gráfico en el HTML.

function _nombreCortoAuditoria(nombre){
  if(window.innerWidth > 480 || nombre.length <= 10) return nombre;
  return nombre.slice(0, 9) + '…';
}

function renderAuditoriaCharts(){
  const filas = auditoriaData.resumen_por_empleado || [];
  const hayIntervenciones = (auditoriaData.detalle || []).length > 0;

  const vacioGrafico = () => vacio({icono:'chart', pequeno:true,
    titulo:'Sin intervenciones en este periodo',
    texto:'No se ha editado ningún turno ni concedido peticiones a mano entre estas fechas.'});
  ['divergente','peticiones'].forEach(k=>{
    document.getElementById(`aud-chart-${k}`).classList.toggle('hidden', !hayIntervenciones);
    const vac = document.getElementById(`aud-chart-${k}-empty`);
    vac.classList.toggle('hidden', hayIntervenciones);
    if(!hayIntervenciones) vac.innerHTML = vacioGrafico();
  });
  if(!hayIntervenciones) return;

  renderAuditoriaChartDivergente(filas);
  renderAuditoriaChartPeticiones(filas);
}

function renderAuditoriaChartDivergente(filas){
  const ref = (auditoriaData.totales_periodo.referencia_equipo || {});
  const mediaFav  = ref.tasa_favorables?.media    || 0;
  const mediaDesf = ref.tasa_desfavorables?.media || 0;

  const ordenadas = [...filas].sort((a,b)=> b.tasa_favorables - a.tasa_favorables);
  const rowH = 30, gap = 6, padTop = 32, padBottom = 8, nameW = 96, rightPad = 16;
  const W = 640;
  const chartL = nameW, chartR = W - rightPad;
  const centerX = chartL + (chartR - chartL) / 2;
  const halfW   = (chartR - chartL) / 2 - 6;
  const maxMag  = Math.max(0.05, mediaFav, mediaDesf,
    ...ordenadas.flatMap(f=>[f.tasa_favorables, f.tasa_desfavorables]));
  const H = padTop + ordenadas.length * (rowH + gap) + padBottom;

  const mediaFavX  = centerX + (mediaFav  / maxMag) * halfW;
  const mediaDesfX = centerX - (mediaDesf / maxMag) * halfW;

  const filasSvg = ordenadas.map((f,i)=>{
    const y = padTop + i * (rowH + gap);
    const insuf = f.semanas_presentes < 4;
    const favW  = (f.tasa_favorables    / maxMag) * halfW;
    const desfW = (f.tasa_desfavorables / maxMag) * halfW;
    return `<g opacity="${insuf ? 0.4 : 1}">
      <text x="${nameW-8}" y="${y+rowH/2+4}" text-anchor="end" font-size="11" fill="var(--text)">${_nombreCortoAuditoria(f.empleado)}</text>
      ${insuf ? `<text x="${nameW-8}" y="${y+rowH/2+16}" text-anchor="end" font-size="8.5" fill="var(--text-3)">muestra insuficiente</text>` : ''}
      <rect x="${centerX}" y="${y}" width="${Math.max(favW,0)}" height="${rowH}" rx="3" class="bar-aud-fav">
        <title>${f.empleado}: ${f.ediciones_turno_favorables} favorables (tasa ${f.tasa_favorables.toFixed(2)}/semana, ${f.semanas_presentes} semanas presente)</title>
      </rect>
      <rect x="${centerX-desfW}" y="${y}" width="${Math.max(desfW,0)}" height="${rowH}" rx="3" class="bar-aud-desfav">
        <title>${f.empleado}: ${f.ediciones_turno_desfavorables} desfavorables (tasa ${f.tasa_desfavorables.toFixed(2)}/semana, ${f.semanas_presentes} semanas presente)</title>
      </rect>
      ${favW>2 ? `<text x="${centerX+favW+4}" y="${y+rowH/2+4}" font-size="10" fill="var(--text-2)">${f.ediciones_turno_favorables}</text>` : ''}
      ${desfW>2 ? `<text x="${centerX-desfW-4}" y="${y+rowH/2+4}" text-anchor="end" font-size="10" fill="var(--text-2)">${f.ediciones_turno_desfavorables}</text>` : ''}
    </g>`;
  }).join('');

  const leyenda = `<g font-size="10.5">
    <rect x="${chartL}" y="4" width="10" height="10" rx="2" class="bar-aud-fav"></rect>
    <text x="${chartL+14}" y="13" fill="var(--text-2)">Favorables</text>
    <rect x="${chartL+92}" y="4" width="10" height="10" rx="2" class="bar-aud-desfav"></rect>
    <text x="${chartL+106}" y="13" fill="var(--text-2)">Desfavorables</text>
  </g>`;

  const lineas = `
    <line x1="${centerX}" y1="${padTop-4}" x2="${centerX}" y2="${H-padBottom}" stroke="var(--border-strong)" stroke-width="1"></line>
    <line x1="${mediaFavX}" y1="${padTop-4}" x2="${mediaFavX}" y2="${H-padBottom}" stroke="var(--text-3)" stroke-width="1" stroke-dasharray="3,3"></line>
    <text x="${mediaFavX}" y="${padTop-8}" text-anchor="middle" font-size="8.5" fill="var(--text-3)">media del equipo</text>
    <line x1="${mediaDesfX}" y1="${padTop-4}" x2="${mediaDesfX}" y2="${H-padBottom}" stroke="var(--text-3)" stroke-width="1" stroke-dasharray="3,3"></line>
    <text x="${mediaDesfX}" y="${padTop-8}" text-anchor="middle" font-size="8.5" fill="var(--text-3)">media del equipo</text>`;

  const svg = document.getElementById('aud-chart-divergente');
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.setAttribute('height', H);
  svg.innerHTML = leyenda + lineas + filasSvg;
}

function renderAuditoriaChartPeticiones(filas){
  const ref    = (auditoriaData.totales_periodo.referencia_equipo || {});
  const media  = ref.tasa_peticiones_jefe?.media || 0;

  const ordenadas = [...filas].sort((a,b)=> b.tasa_peticiones_jefe - a.tasa_peticiones_jefe);
  const rowH = 26, gap = 6, padTop = 16, padBottom = 8, nameW = 96, rightPad = 44;
  const W = 640;
  const barL = nameW, barR = W - rightPad;
  const maxTasa = Math.max(0.05, media, ...ordenadas.map(f=>f.tasa_peticiones_jefe));
  const H = padTop + ordenadas.length * (rowH + gap) + padBottom;
  const mediaX = barL + (media / maxTasa) * (barR - barL);

  const filasSvg = ordenadas.map((f,i)=>{
    const y = padTop + i * (rowH + gap);
    const insuf = f.semanas_presentes < 4;
    const w = (f.tasa_peticiones_jefe / maxTasa) * (barR - barL);
    return `<g opacity="${insuf ? 0.4 : 1}">
      <text x="${nameW-8}" y="${y+rowH/2+4}" text-anchor="end" font-size="11" fill="var(--text)">${_nombreCortoAuditoria(f.empleado)}</text>
      ${insuf ? `<text x="${nameW-8}" y="${y+rowH/2+15}" text-anchor="end" font-size="8.5" fill="var(--text-3)">muestra insuficiente</text>` : ''}
      <rect x="${barL}" y="${y}" width="${Math.max(w,0)}" height="${rowH}" rx="3" class="bar-aud-jefe">
        <title>${f.empleado}: ${f.peticiones_origen_jefe} peticiones de origen jefe (tasa ${f.tasa_peticiones_jefe.toFixed(2)}/semana, ${f.semanas_presentes} semanas presente)</title>
      </rect>
      ${w>2 ? `<text x="${barL+w+4}" y="${y+rowH/2+4}" font-size="10" fill="var(--text-2)">${f.peticiones_origen_jefe}</text>` : ''}
    </g>`;
  }).join('');

  const linea = `
    <line x1="${mediaX}" y1="${padTop-4}" x2="${mediaX}" y2="${H-padBottom}" stroke="var(--text-3)" stroke-width="1" stroke-dasharray="3,3"></line>
    <text x="${mediaX}" y="${padTop-8}" text-anchor="middle" font-size="8.5" fill="var(--text-3)">media del equipo</text>`;

  const svg = document.getElementById('aud-chart-peticiones');
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.setAttribute('height', H);
  svg.innerHTML = linea + filasSvg;
}

function renderAuditoriaDetalle(){
  const cont = document.getElementById('aud-detalle-list');
  const det  = auditoriaData.detalle;
  if(!det.length){
    cont.innerHTML = vacio({icono:'clipboard', titulo:'Sin intervenciones en este periodo',
      texto:'Prueba a ampliar el rango de fechas del filtro de arriba.'});
    return;
  }
  cont.innerHTML = det.map(i=>{
    const fecha  = (i.ts||'').replace('T',' ').slice(0,16);
    const cambio = (i.valor_anterior!=null || i.valor_nuevo!=null)
      ? `${i.valor_anterior ?? '—'} → ${i.valor_nuevo ?? '—'}` : '—';
    return `<div class="aud-row">
      <div class="row between wrap gap-10">
        <span><strong>${i.empleado_afectado||'—'}</strong> · ${AUD_TIPO_LABEL[i.tipo]||i.tipo} · ${cambio}</span>
        <span class="aud-fecha">${fecha}</span>
      </div>
      <div class="muted mt-4">Por: ${escapeHtml(i.usuario)} (${escapeHtml(i.rol_usuario)})${i.justificacion?' — '+escapeHtml(i.justificacion):''}</div>
    </div>`;
  }).join('');
}

// ── NAVIGATION ──────────────────────────────────────────────────────
function irASeccion(sec){
  document.querySelectorAll('.nav-item').forEach(n=>n.classList.remove('active'));
  document.querySelector(`.nav-item[data-sec="${sec}"]`)?.classList.add('active');
  document.querySelectorAll('.section').forEach(s=>s.classList.remove('active'));
  document.getElementById('sec-'+sec).classList.add('active');
  const [t,s]=SECS[sec]||['',''];
  document.getElementById('topbar-title').textContent = t;
  document.getElementById('topbar-sub').textContent = s;
  document.getElementById('topbar-right').classList.toggle('hidden', sec!=='horario');
  document.getElementById('topbar-contexto').classList.toggle('hidden', sec!=='horario');
  if(sec==='dashboard')cargarDashboard();
  if(sec==='horario'){ updateWeekLabel(); cargarHorarioSemana(); }
  if(sec==='empleados')loadEmpleados();
  if(sec==='categorias')loadCategorias();
  if(sec==='festivos')loadFestivos();
  if(sec==='compensaciones')loadCompensaciones();
  if(sec==='peticiones'){
    petWeekOffset=0;updatePetWeekLabel();
    document.getElementById('btn-add-pet').textContent =
      currentUser?.rol==='empleado' ? '+ Solicitar día libre' : '+ Nueva petición';
    loadPeticiones();
    loadCuposPanelManager();
  }
  if(sec==='vacaciones')loadVacaciones();
  if(sec==='estadisticas')loadEstadisticas();
  if(sec==='historial')loadHistorial();
  if(sec==='asistente')setupAsistente();
  if(sec==='usuarios')loadUsuarios();
  if(sec==='log')loadLog();
  if(sec==='temporadas')loadTemporadas();
  if(sec==='rotacion')loadRotacion();
  if(sec==='auditoria')loadAuditoria();
  cerrarMenuMovil();
}

document.querySelectorAll('.nav-item[data-sec]').forEach(el=>{
  el.addEventListener('click',()=> irASeccion(el.dataset.sec));
});

// ── MENÚ MÓVIL (hamburguesa) ──────────────────────────────────────
const sidebarEl = document.querySelector('.sidebar');
const sidebarOverlayEl = document.getElementById('sidebar-overlay');
function abrirMenuMovil(){ sidebarEl.classList.add('open'); sidebarOverlayEl.classList.add('active'); }
function cerrarMenuMovil(){ sidebarEl.classList.remove('open'); sidebarOverlayEl.classList.remove('active'); }
document.getElementById('btn-hamburger').onclick = ()=>{
  sidebarEl.classList.contains('open') ? cerrarMenuMovil() : abrirMenuMovil();
};
sidebarOverlayEl.onclick = cerrarMenuMovil;

SECS['usuarios']    = ['Usuarios','Gestión de acceso'];
SECS['log']         = ['Log de actividad','Registro de acciones'];
SECS['temporadas']  = ['Temporadas','Cobertura mínima y máxima por perfil'];
SECS['rotacion']    = ['Ronda de noches','Orden fijo de cobertura nocturna'];

// ── LIMPIAR HISTORIAL (admin) ─────────────────────────────────────
function confirmarLimpiarHistorial(){
  document.getElementById('modal-limpiar-hist').classList.remove('hidden');
}

async function ejecutarLimpiarHistorial(){
  const btn = document.getElementById('btn-confirmar-limpiar');
  btn.disabled = true;
  btn.textContent = 'Eliminando...';
  const r = await api('POST','/historial/limpiar');
  btn.disabled = false;
  btn.textContent = 'Sí, eliminar todo el historial';
  closeModal('modal-limpiar-hist');
  if(r.ok){
    toast('Historial eliminado correctamente');
    loadHistorial();
  } else {
    toast(r.error || 'Error al limpiar el historial','err');
  }
}

// ── RONDA DE NOCHES — orden fijo de cobertura nocturna (solo admin) ──
//
// El orden es una lista fija que define en qué secuencia se turnan los
// empleados elegibles para cubrir las noches. La generación semanal NO lo
// reordena: solo avanza el puntero por la lista (ver REGLAS.md §1.9).
let rotDatos  = null;   // respuesta del servidor tal cual llegó
let rotBorrador = null; // orden en edición; null = sin cambios sin guardar

async function loadRotacion(){
  const dept = document.getElementById('rot-dept').value;
  rotBorrador = null;
  const r = await api('GET',`/rotacion-noches?dept=${dept}`);
  if(!r.ok){ toast(r.error||'Error al cargar la ronda de noches','err'); return; }
  rotDatos = r.data;
  renderRotacion();
}

function _rotOrdenVisible(){
  return rotBorrador || (rotDatos?.orden || []).map(e=>e.nombre);
}

function renderRotacion(){
  const tbody = document.getElementById('rot-body');
  const orden = _rotOrdenVisible();
  const porNombre = Object.fromEntries((rotDatos?.orden||[]).map(e=>[e.nombre,e]));
  tbody.innerHTML = '';

  if(!orden.length){
    tbody.innerHTML = vacioFila(4, {icono:'moon',
      titulo:'Nadie puede cubrir noches en este departamento',
      texto:'Revisa la casilla «sin noches» de cada categoría en la sección Categorías.'});
  }

  orden.forEach((nombre, idx)=>{
    const e = porNombre[nombre] || {};
    const tr = document.createElement('tr');
    const esSiguiente = nombre === rotDatos?.siguiente_turno;
    tr.innerHTML = `
      <td class="rot-idx">${idx+1}</td>
      <td>
        ${escapeHtml(nombre)}
        ${esSiguiente ? '<span class="badge ok">próximo</span>' : ''}
      </td>
      <td class="muted">
        ${e.noches_cubiertas ?? 0}${e.deuda ? ` · ${e.deuda} turno(s) pendiente(s)` : ''}
      </td>
      <td class="nowrap">
        <button class="btn ghost xs" ${idx===0?'disabled':''} onclick="moveRot(${idx},-1)" title="Subir">${ic('arrow-up','ic-16')}</button>
        <button class="btn ghost xs" ${idx===orden.length-1?'disabled':''} onclick="moveRot(${idx},1)" title="Bajar">${ic('arrow-down','ic-16')}</button>
      </td>`;
    tbody.appendChild(tr);
  });

  const sig = rotDatos?.siguiente_turno;
  document.getElementById('rot-siguiente').textContent =
    sig ? `Próximo en cubrir noches: ${sig}` : '';
  document.getElementById('rot-guardar').disabled = !rotBorrador;
  document.getElementById('rot-estado').textContent =
    rotBorrador ? 'Cambios sin guardar' : '';
}

function moveRot(idx, dir){
  const orden = _rotOrdenVisible().slice();
  const nuevo = idx + dir;
  if(nuevo < 0 || nuevo >= orden.length) return;
  [orden[idx], orden[nuevo]] = [orden[nuevo], orden[idx]];
  rotBorrador = orden;
  renderRotacion();
}

async function saveRotacion(){
  if(!rotBorrador) return;
  const dept = document.getElementById('rot-dept').value;
  const btn = document.getElementById('rot-guardar');
  btn.disabled = true;
  try{
    const r = await api('PUT','/rotacion-noches',{dept, orden: rotBorrador});
    if(!r.ok){ toast(r.error,'err'); return; }
    toast('Orden de la ronda de noches guardado');
    await loadRotacion();   // relee para reflejar siguiente_turno del servidor
  } finally {
    // Si loadRotacion() falló, el botón debe volver a estar disponible para
    // reintentar en vez de quedarse bloqueado (mismo criterio que los botones
    // de la parrilla).
    if(rotBorrador) btn.disabled = false;
  }
}

// ── TEMPORADAS — cobertura mínima/máxima por perfil ─────────────────
const TURNOS_ORDER = ['Mañana','Tarde','Noche','Partido'];
const TURNO_KEYS   = {Mañana:'man', Tarde:'tar', Noche:'noc', Partido:'par'};

let tmpPerfilesCache = {};  // {perfil_id: {min_coverage, max_coverage}}
let tmpActivoCache   = '';

async function loadTemporadas(){
  const dept = document.getElementById('tmp-dept').value;
  const r    = await api('GET', `/coverage?dept=${dept}`);
  if(!r.ok){ toast(r.error||'Error al cargar temporadas','err'); return; }
  tmpPerfilesCache = r.data.perfiles || {};
  tmpActivoCache   = r.data.activo   || 'normal';

  // Actualizar selector de perfil activo
  const sel = document.getElementById('tmp-activo');
  sel.innerHTML = '';
  Object.keys(tmpPerfilesCache).forEach(pid=>{
    const opt = document.createElement('option');
    opt.value = pid;
    opt.textContent = pid.charAt(0).toUpperCase()+pid.slice(1);
    opt.selected = (pid===tmpActivoCache);
    sel.appendChild(opt);
  });

  // Renderizar tabla de perfiles
  const tbody = document.getElementById('tmp-table-body');
  tbody.innerHTML = '';
  Object.entries(tmpPerfilesCache).forEach(([pid, p])=>{
    const esActivo = (pid===tmpActivoCache);
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td class="nl ${esActivo?'w-600':'w-400'}">
        ${pid}${esActivo?' <span class="perfil-activo">activo</span>':''}
      </td>
      ${TURNOS_ORDER.map(t=>`
        <td>${p.min_coverage[t]??'—'}</td>
        <td>${p.max_coverage[t]??'—'}</td>
      `).join('')}
      <td class="right">
        <button class="btn sm" onclick="abrirEditPerfil('${pid}')">${ic('pencil','ic-16')} Editar</button>
      </td>`;
    tbody.appendChild(tr);
  });

  cerrarEditPerfil();
}

async function cambiarPerfilActivo(){
  const dept   = document.getElementById('tmp-dept').value;
  const perfil = document.getElementById('tmp-activo').value;
  const r = await api('PUT','/coverage/activo',{dept, perfil});
  if(r.ok){
    tmpActivoCache = perfil;
    toast(`Perfil activo → ${perfil}`);
    loadTemporadas();
  } else {
    toast(r.error||'Error al cambiar perfil','err');
    document.getElementById('tmp-activo').value = tmpActivoCache;
  }
}

function abrirEditPerfil(pid){
  const p = tmpPerfilesCache[pid];
  if(!p) return;
  document.getElementById('tmp-edit-title').textContent = `Editar perfil "${pid}"`;
  document.getElementById('tmp-edit-id').value        = pid;
  document.getElementById('tmp-edit-id').readOnly     = true;  // no renombrar perfil existente
  TURNOS_ORDER.forEach(t=>{
    const k = TURNO_KEYS[t];
    document.getElementById(`tmp-min-${k}`).value = p.min_coverage[t]??0;
    document.getElementById(`tmp-max-${k}`).value = p.max_coverage[t]??0;
  });
  document.getElementById('tmp-edit-panel').classList.remove('hidden');
}

function abrirNuevoPerfil(){
  document.getElementById('tmp-edit-title').textContent = 'Nuevo perfil';
  document.getElementById('tmp-edit-id').value          = '';
  document.getElementById('tmp-edit-id').readOnly       = false;
  TURNOS_ORDER.forEach(t=>{
    const k = TURNO_KEYS[t];
    document.getElementById(`tmp-min-${k}`).value = t==='Noche'?1:0;
    document.getElementById(`tmp-max-${k}`).value = t==='Noche'?1:t==='Partido'?1:3;
  });
  document.getElementById('tmp-edit-panel').classList.remove('hidden');
}

function cerrarEditPerfil(){
  document.getElementById('tmp-edit-panel').classList.add('hidden');
}

async function guardarPerfil(){
  const dept      = document.getElementById('tmp-dept').value;
  const perfil_id = document.getElementById('tmp-edit-id').value.trim().toLowerCase();
  if(!perfil_id){ toast('El nombre del perfil no puede estar vacío','err'); return; }

  const min_coverage = {}, max_coverage = {};
  TURNOS_ORDER.forEach(t=>{
    const k = TURNO_KEYS[t];
    min_coverage[t] = parseInt(document.getElementById(`tmp-min-${k}`).value, 10);
    max_coverage[t] = parseInt(document.getElementById(`tmp-max-${k}`).value, 10);
  });

  const r = await api('PUT','/coverage/perfil',{dept, perfil_id, min_coverage, max_coverage});
  if(r.ok){
    toast(`Perfil "${perfil_id}" guardado`);
    loadTemporadas();
  } else {
    toast(r.error||'Error al guardar perfil','err');
  }
}

// ── INIT — check existing session ─────────────────────────────────
(async ()=>{
  const r = await fetch('/api/auth/me');
  const d = await r.json();
  if(d.ok && d.data){
    await showApp(d.data);
  } else {
    showLogin();
  }
})();
