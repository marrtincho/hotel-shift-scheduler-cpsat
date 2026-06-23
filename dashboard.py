#!/usr/bin/env python3
"""
Dashboard Web - Visualización de horarios y estadísticas del sistema de horarios hoteleros.
Utiliza Flask para el backend y Plotly/Dash para las visualizaciones interactivas.
"""

from flask import Flask, render_template, request, jsonify
import plotly.graph_objs as go
import plotly.utils
import json
from datetime import date, timedelta
import sys
import os

# Añadir el directorio actual al path para importar módulos locales
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from herramientas import (
    cargar_historial, cargar_plantilla, cargar_peticiones, cargar_vacaciones,
    cargar_festivos_trabajados, cargar_compensaciones, festivos_en_semana,
    obtener_peticiones_semana, vacaciones_activas_en_semana
)
from modelo import generate_schedule, DAYS, SHIFT_COLORS

app = Flask(__name__)

# ─── RUTAS DEL DASHBOARD ─────────────────────────────────────────────────────

@app.route('/')
def index():
    """Página principal del dashboard."""
    return render_template('dashboard.html')

@app.route('/api/historial')
def api_historial():
    """API para obtener el historial de semanas generadas."""
    historial = cargar_historial()
    semanas = []
    for semana_str, datos in sorted(historial.items(), reverse=True):
        d = date.fromisoformat(semana_str)
        fin = d + timedelta(days=6)
        festivos = festivos_en_semana(d)
        semanas.append({
            'semana': semana_str,
            'inicio': d.strftime('%d/%m/%Y'),
            'fin': fin.strftime('%d/%m/%Y'),
            'festivos': len(festivos),
            'empleados': len(datos['staff'])
        })
    return jsonify(semanas)

@app.route('/api/estadisticas')
def api_estadisticas():
    """API para obtener estadísticas generales."""
    historial = cargar_historial()
    staff = cargar_plantilla()
    
    # Conteo de turnos por empleado
    conteos = {}
    semanas_trabajadas = {}
    
    for semana_str, entrada in historial.items():
        schedule = entrada["schedule"]
        for nombre, turnos in schedule.items():
            if nombre not in conteos:
                conteos[nombre] = {t: 0 for t in ["Mañana", "Tarde", "Noche", "Partido", "Libre"]}
                semanas_trabajadas[nombre] = 0
            semanas_trabajadas[nombre] += 1
            for t in turnos:
                if t in conteos[nombre]:
                    conteos[nombre][t] += 1
    
    # Estadísticas de festivos trabajados
    festivos_trabajados = cargar_festivos_trabajados()
    conteo_festivos = {}
    for fecha, info in festivos_trabajados.items():
        for nombre in info["trabajadores"]:
            conteo_festivos[nombre] = conteo_festivos.get(nombre, 0) + 1
    
    # Distribución de roles
    roles = {}
    for p in staff:
        roles[p["role"]] = roles.get(p["role"], 0) + 1
    
    return jsonify({
        'turnos_por_empleado': conteos,
        'semanas_trabajadas': semanas_trabajadas,
        'festivos_trabajados': conteo_festivos,
        'distribucion_roles': roles
    })

@app.route('/api/horario/<semana>')
def api_horario_semana(semana):
    """API para obtener el horario de una semana específica."""
    try:
        d = date.fromisoformat(semana)
        historial = cargar_historial()
        
        if semana not in historial:
            # Generar horario si no existe
            staff = cargar_plantilla()
            schedule, staff_final, _ = generate_schedule(staff, d)
            
            # Aplicar peticiones y vacaciones
            staff_con_peticiones = obtener_peticiones_semana(d)
            staff_con_vacaciones = vacaciones_activas_en_semana(d)
            
            return jsonify({
                'generado': True,
                'schedule': schedule,
                'staff': staff_final,
                'peticiones': staff_con_peticiones,
                'vacaciones': staff_con_vacaciones
            })
        else:
            datos = historial[semana]
            return jsonify({
                'generado': False,
                'schedule': datos['schedule'],
                'staff': datos['staff'],
                'peticiones': obtener_peticiones_semana(d),
                'vacaciones': vacaciones_activas_en_semana(d)
            })
    except ValueError:
        return jsonify({'error': 'Formato de fecha inválido'}), 400

@app.route('/api/generar_horario', methods=['POST'])
def api_generar_horario():
    """API para generar un nuevo horario."""
    data = request.json
    try:
        semana_str = data.get('semana')
        if not semana_str:
            return jsonify({'error': 'Se requiere la fecha de la semana'}), 400
        
        d = date.fromisoformat(semana_str)
        staff = cargar_plantilla()
        schedule, staff_final, _ = generate_schedule(staff, d)
        
        # Guardar en historial
        from herramientas import registrar_semana
        registrar_semana(d, schedule, staff_final, forzar=True)
        
        return jsonify({
            'success': True,
            'schedule': schedule,
            'staff': staff_final
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ─── GENERACIÓN DE GRÁFICOS ──────────────────────────────────────────────────

def crear_grafico_barras_turnos(conteos):
    """Crea un gráfico de barras para la distribución de turnos."""
    empleados = list(conteos.keys())
    turnos = ["Mañana", "Tarde", "Noche", "Partido", "Libre"]
    
    data = []
    for turno in turnos:
        valores = [conteos[emp].get(turno, 0) for emp in empleados]
        data.append(go.Bar(
            name=turno,
            x=empleados,
            y=valores,
            marker_color=SHIFT_COLORS.get(turno, "gray")
        ))
    
    layout = go.Layout(
        title="Distribución de Turnos por Empleado",
        barmode='stack',
        xaxis_title="Empleados",
        yaxis_title="Cantidad de Turnos",
        height=400
    )
    
    return json.dumps(go.Figure(data=data, layout=layout), cls=plotly.utils.PlotlyJSONEncoder)

def crear_grafico_circular_roles(roles):
    """Crea un gráfico circular para la distribución de roles."""
    labels = list(roles.keys())
    values = list(roles.values())
    
    fig = go.Figure(data=[go.Pie(
        labels=labels,
        values=values,
        hole=0.3,
        textinfo='label+percent'
    )])
    
    fig.update_layout(
        title="Distribución de Roles en el Equipo",
        height=400
    )
    
    return json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)

def crear_grafico_lineas_semanas(semanas_trabajadas):
    """Crea un gráfico de líneas para semanas trabajadas."""
    empleados = list(semanas_trabajadas.keys())
    valores = [semanas_trabajadas[emp] for emp in empleados]
    
    fig = go.Figure(data=go.Scatter(
        x=empleados,
        y=valores,
        mode='lines+markers',
        line=dict(color='royalblue', width=3),
        marker=dict(size=8)
    ))
    
    fig.update_layout(
        title="Semanas Trabajadas por Empleado",
        xaxis_title="Empleados",
        yaxis_title="Semanas",
        height=400
    )
    
    return json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)

def crear_grafico_horario_semana(schedule, staff):
    """Crea un gráfico de calor para el horario semanal."""
    empleados = [p['name'] for p in staff]
    turnos = ["Mañana", "Tarde", "Noche", "Partido", "Libre"]
    
    # Crear matriz de valores numéricos para el heatmap
    z_data = []
    for emp in empleados:
        fila = []
        for dia in DAYS:
            turno = schedule[emp][DAYS.index(dia)]
            # Asignar valor numérico al turno para el color
            valor = turnos.index(turno) if turno in turnos else len(turnos)
            fila.append(valor)
        z_data.append(fila)
    
    fig = go.Figure(data=go.Heatmap(
        z=z_data,
        x=DAYS,
        y=empleados,
        colorscale=[
            [0, SHIFT_COLORS["Mañana"]],
            [0.25, SHIFT_COLORS["Tarde"]],
            [0.5, SHIFT_COLORS["Noche"]],
            [0.75, SHIFT_COLORS["Partido"]],
            [1, SHIFT_COLORS["Libre"]]
        ],
        text=[[schedule[emp][i] for i in range(7)] for emp in empleados],
        texttemplate="%{text}",
        textfont={"size": 10},
        showscale=False
    ))
    
    fig.update_layout(
        title="Horario Semanal",
        xaxis_title="Días de la Semana",
        yaxis_title="Empleados",
        height=500
    )
    
    return json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)

# ─── RUTAS PARA GRÁFICOS ─────────────────────────────────────────────────────

@app.route('/grafico/turnos')
def grafico_turnos():
    """Endpoint para el gráfico de distribución de turnos."""
    estadisticas = api_estadisticas().get_json()
    return crear_grafico_barras_turnos(estadisticas['turnos_por_empleado'])

@app.route('/grafico/roles')
def grafico_roles():
    """Endpoint para el gráfico de distribución de roles."""
    estadisticas = api_estadisticas().get_json()
    return crear_grafico_circular_roles(estadisticas['distribucion_roles'])

@app.route('/grafico/semanas')
def grafico_semanas():
    """Endpoint para el gráfico de semanas trabajadas."""
    estadisticas = api_estadisticas().get_json()
    return crear_grafico_lineas_semanas(estadisticas['semanas_trabajadas'])

@app.route('/grafico/horario/<semana>')
def grafico_horario(semana):
    """Endpoint para el gráfico de horario semanal."""
    try:
        datos = api_horario_semana(semana)
        if isinstance(datos, tuple):
            datos = datos[0]  # Si es una tupla (datos, status_code), tomar solo los datos
        if 'schedule' in datos and 'staff' in datos:
            return crear_grafico_horario_semana(datos['schedule'], datos['staff'])
        return jsonify({'error': 'No se encontraron datos para esta semana'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)