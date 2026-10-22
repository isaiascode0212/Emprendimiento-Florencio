import base64
import sys
import io
import time

# Forzar codificación UTF-8 en la terminal para evitar errores con tildes y caracteres especiales
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

from datetime import datetime
import json
import os
import urllib.request
from collections import defaultdict
from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text
from google import genai
from google.genai import types
from openai import OpenAI

app = Flask(__name__)
app.config['SECRET_KEY'] = 'florencio_secret_key'

basedir = os.path.abspath(os.path.dirname(__file__))
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(
    basedir, 'inventario.db'
)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['TEMPLATES_AUTO_RELOAD'] = True

db = SQLAlchemy(app)

# Inicializar clientes leyendo de forma segura desde las variables de entorno de Render
client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

openrouter_client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.environ.get("OPENROUTER_API_KEY")
)

groq_client = OpenAI(
    base_url="https://api.groq.com/openai/v1",
    api_key=os.environ.get("GROQ_API_KEY")
)


# --- MODELOS ---
class Producto(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    codigo_barras = db.Column(db.String(50), nullable=True, unique=True)
    nombre = db.Column(db.String(100), nullable=False, unique=True)
    cantidad = db.Column(db.Integer, nullable=False, default=0)
    costo_unitario = db.Column(db.Float, nullable=False, default=0.0)
    precio_venta = db.Column(db.Float, nullable=False, default=0.0)
    entradas = db.Column(db.Integer, nullable=False, default=0)
    salidas = db.Column(db.Integer, nullable=False, default=0)


class FacturaCompra(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    fecha = db.Column(db.DateTime, default=datetime.utcnow)
    proveedor = db.Column(db.String(100), nullable=True, default='Proveedor General')
    costo_envio = db.Column(db.Float, nullable=False, default=0.0)
    total = db.Column(db.Float, nullable=False, default=0.0)
    detalles = db.relationship(
        'DetalleCompra',
        backref='factura',
        lazy=True,
        cascade='all, delete-orphan',
    )


class DetalleCompra(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    factura_id = db.Column(
        db.Integer, db.ForeignKey('factura_compra.id'), nullable=False
    )
    codigo_barras = db.Column(db.String(50), nullable=True)
    producto_nombre = db.Column(db.String(100), nullable=False)
    cantidad = db.Column(db.Integer, nullable=False)
    costo_unitario = db.Column(db.Float, nullable=False)


class FacturaVenta(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    fecha = db.Column(db.DateTime, default=datetime.utcnow)
    metodo_pago = db.Column(db.String(50), nullable=False)
    total = db.Column(db.Float, nullable=False, default=0.0)
    tasa_bcv = db.Column(db.Float, nullable=False, default=0.0)
    total_bs = db.Column(db.Float, nullable=False, default=0.0)
    detalles = db.relationship(
        'DetalleVenta', backref='factura', lazy=True, cascade='all, delete-orphan'
    )


class DetalleVenta(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    factura_id = db.Column(
        db.Integer, db.ForeignKey('factura_venta.id'), nullable=False
    )
    producto_nombre = db.Column(db.String(100), nullable=False)
    cantidad = db.Column(db.Integer, nullable=False)
    precio_unitario = db.Column(db.Float, nullable=False)
    costo_historico = db.Column(db.Float, nullable=False, default=0.0)


class EscaneoRemoto(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    codigo = db.Column(db.String(100), nullable=False)
    procesado = db.Column(db.Boolean, default=False)
    fecha = db.Column(db.DateTime, default=datetime.utcnow)


# MIGRACIÓN AUTOMÁTICA Y CREACIÓN DE TABLAS
with app.app_context():
    db.create_all()
    try:
        db.session.execute(
            text(
                "ALTER TABLE factura_compra ADD COLUMN proveedor VARCHAR(100)"
                " DEFAULT 'Proveedor General'"
            )
        )
        db.session.commit()
    except Exception:
        db.session.rollback()

    try:
        db.session.execute(
            text("ALTER TABLE factura_compra ADD COLUMN costo_envio FLOAT DEFAULT 0.0")
        )
        db.session.commit()
    except Exception:
        db.session.rollback()

    try:
        db.session.execute(
            text("ALTER TABLE producto ADD COLUMN codigo_barras VARCHAR(50)")
        )
        db.session.commit()
    except Exception:
        db.session.rollback()

    try:
        db.session.execute(
            text("ALTER TABLE detalle_compra ADD COLUMN codigo_barras VARCHAR(50)")
        )
        db.session.commit()
    except Exception:
        db.session.rollback()

    try:
        db.session.execute(
            text("ALTER TABLE factura_venta ADD COLUMN tasa_bcv FLOAT DEFAULT 0.0")
        )
        db.session.commit()
    except Exception:
        db.session.rollback()

    try:
        db.session.execute(
            text("ALTER TABLE factura_venta ADD COLUMN total_bs FLOAT DEFAULT 0.0")
        )
        db.session.commit()
    except Exception:
        db.session.rollback()

    try:
        db.session.execute(
            text("ALTER TABLE detalle_venta ADD COLUMN costo_historico FLOAT DEFAULT 0.0")
        )
        db.session.commit()
    except Exception:
        db.session.rollback()


# --- FUNCIÓN TASA BCV CON CACHÉ Y REDONDEO UNIFORME ---
_tasa_cache = {'valor': 0.0, 'tiempo': None}


def obtener_tasa_bcv():
    global _tasa_cache
    tiempo_actual = datetime.now()

    if (
        _tasa_cache['valor'] > 0
        and _tasa_cache['tiempo']
        and (tiempo_actual - _tasa_cache['tiempo']).seconds < 300
    ):
        return _tasa_cache['valor']

    try:
        req = urllib.request.Request(
            'https://ve.dolarapi.com/v1/dolares/oficial',
            headers={
                'User-Agent': (
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                )
            },
        )
        with urllib.request.urlopen(req, timeout=4) as response:
            data = json.loads(response.read().decode('utf-8'))
            tasa = round(float(data['promedio']), 2)
            _tasa_cache['valor'] = tasa
            _tasa_cache['tiempo'] = tiempo_actual
            return tasa
    except Exception as e:
        print(f'Error obteniendo tasa BCV en API 1: {e}')
        try:
            req2 = urllib.request.Request(
                'https://rates.dolarvzla.com/bcv/current.json',
                headers={'User-Agent': 'Mozilla/5.0'},
            )
            with urllib.request.urlopen(req2, timeout=4) as response:
                data2 = json.loads(response.read().decode('utf-8'))
                tasa = round(float(data2['current']['usd']), 2)
                _tasa_cache['valor'] = tasa
                _tasa_cache['tiempo'] = tiempo_actual
                return tasa
        except Exception as ex:
            print(f'Error obteniendo tasa BCV en API 2: {ex}')
            return _tasa_cache['valor'] if _tasa_cache['valor'] > 0 else 0.0


# --- API ENDPOINTS ---
@app.route('/api/productos')
def api_productos():
    productos = Producto.query.all()
    return jsonify([{
        'id': p.id,
        'codigo_barras': p.codigo_barras or '',
        'nombre': p.nombre,
        'costo_unitario': p.costo_unitario,
        'precio_venta': p.precio_venta,
        'cantidad': p.cantidad,
    } for p in productos])


@app.route('/api/escanear_remoto', methods=['POST'])
def recibir_escanear_remoto():
    datos = request.json
    codigo = datos.get('codigo', '').strip()
    if not codigo:
        return jsonify({'success': False, 'message': 'Código vacío'})

    nuevo_escaneo = EscaneoRemoto(codigo=codigo, procesado=False)
    db.session.add(nuevo_escaneo)
    db.session.commit()
    return jsonify({'success': True, 'message': 'Código recibido'})


@app.route('/api/consultar_escaneos')
def consultar_escaneos():
    pendientes = EscaneoRemoto.query.filter_by(procesado=False).all()
    codigos = []
    for p in pendientes:
        codigos.append({'id': p.id, 'codigo': p.codigo})
        p.procesado = True
    db.session.commit()
    return jsonify({'codigos': codigos})


@app.route('/escanear_remoto')
def escanear_remoto():
    return render_template('escanear_remoto.html')


@app.route('/api/producto/codigo/<string:codigo>')
def buscar_producto_por_codigo(codigo):
    producto = Producto.query.filter_by(codigo_barras=codigo.strip()).first()
    if producto:
        return jsonify({
            'encontrado': True,
            'nombre': producto.nombre,
            'costo_unitario': producto.costo_unitario,
            'precio_venta': producto.precio_venta,
            'cantidad_actual': producto.cantidad,
        })
    else:
        return jsonify({
            'encontrado': False,
            'codigo': codigo.strip(),
            'message': 'Producto no registrado.',
        })


@app.route('/api/factura_compra/<int:id>')
def obtener_factura_compra(id):
    factura = FacturaCompra.query.get_or_404(id)
    detalles = [{
        'id': d.id,
        'codigo_barras': d.codigo_barras or '',
        'producto': d.producto_nombre,
        'cantidad': d.cantidad,
        'costo_unitario': d.costo_unitario,
        'subtotal': d.cantidad * d.costo_unitario,
    } for d in factura.detalles]
    return jsonify({
        'id': factura.id,
        'fecha': factura.fecha.strftime('%d/%m/%Y %H:%M'),
        'proveedor': factura.proveedor or 'Sin Proveedor',
        'costo_envio': factura.costo_envio,
        'total': factura.total,
        'detalles': detalles,
    })


@app.route('/api/factura_compra/editar/<int:id>', methods=['POST'])
def editar_factura_compra(id):
    factura = FacturaCompra.query.get_or_404(id)
    datos = request.json
    nuevos_detalles = datos.get('detalles', [])
    costo_envio = float(datos.get('costo_envio', 0.0))

    for d in factura.detalles:
        prod = Producto.query.filter_by(nombre=d.producto_nombre).first()
        if prod:
            prod.cantidad -= d.cantidad
            prod.entradas -= d.cantidad

    factura.detalles.clear()
    nuevo_total_mercancia = 0.0

    for item in nuevos_detalles:
        codigo = item.get('codigo_barras', '').strip()
        nombre = item['producto'].strip()
        cant = int(item['cantidad'])
        costo = float(item['costo_unitario'])

        nuevo_detalle = DetalleCompra(
            factura_id=factura.id,
            codigo_barras=codigo if codigo else None,
            producto_nombre=nombre,
            cantidad=cant,
            costo_unitario=costo,
        )
        db.session.add(nuevo_detalle)
        nuevo_total_mercancia += cant * costo

        prod = Producto.query.filter_by(nombre=nombre).first()
        if prod:
            prod.cantidad += cant
            prod.entradas += cant
            prod.costo_unitario = costo
            if codigo:
                prod.codigo_barras = codigo
        else:
            nuevo_prod = Producto(
                codigo_barras=codigo if codigo else None,
                nombre=nombre,
                cantidad=cant,
                costo_unitario=costo,
                entradas=cant
            )
            db.session.add(nuevo_prod)

    factura.costo_envio = costo_envio
    factura.total = nuevo_total_mercancia + costo_envio
    db.session.commit()
    return jsonify({'success': True, 'message': 'Factura actualizada con éxito.'})


# --- RUTA: PROCESAR FACTURA CON IA (TRIPLE RESPALDO Y CONTROL DE TIEMPO) ---
@app.route('/api/procesar_factura_ia', methods=['POST'])
def procesar_factura_ia():
    if 'imagen' not in request.files:
        return jsonify({'success': False, 'message': 'No se ha adjuntado ninguna imagen.'})

    imagen_file = request.files['imagen']
    proveedor_sugerido = request.form.get('proveedor', 'Proveedor General')
    costo_envio = float(request.form.get('costo_envio', 0.0))

    try:
        imagen_bytes = imagen_file.read()
        mime_tipo = imagen_file.content_type or 'image/jpeg'

        prompt = (
            "Analiza esta factura o nota de entrega de repuestos/mercancia. "
            "Extrae la siguiente informacion en formato JSON estricto, sin texto adicional:\n"
            "{\n"
            '  "proveedor": "Nombre del proveedor o empresa emisora si aparece, si no, usa null",\n'
            '  "costo_envio": 0.0,\n'
            '  "detalles": [\n'
            "    {\n"
            '      "codigo": "Codigo de barra o referencia del producto si existe, si no, \\"\\"\",\n'
            '      "nombre": "Nombre o descripcion exacta del producto",\n'
            '      "cantidad": 1,\n'
            '      "costo": 0.0\n'
            "    }\n"
            "  ]\n"
            "}\n"
            "Asegurate de que los valores numericos sean correctos (float para costos, int para cantidades)."
        )

        texto_respuesta = None
        ultimo_error = None
        tiempo_inicio = time.time()
        limite_tiempo = 60  # Límite máximo de 1 minuto en total

        # --- NIVEL 1: GOOGLE GEMINI (Modelos principales con reintentos) ---
        modelos_gemini = ['gemini-3.8-flash', 'gemini-2.5-flash']
        
        for nombre_modelo in modelos_gemini:
            if time.time() - tiempo_inicio > limite_tiempo:
                break
            for intento in range(3):
                if time.time() - tiempo_inicio > limite_tiempo:
                    break
                try:
                    response = client.models.generate_content(
                        model=nombre_modelo,
                        contents=[
                            types.Part.from_bytes(
                                data=imagen_bytes,
                                mime_type=mime_tipo,
                            ),
                            prompt
                        ],
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json"
                        ),
                    )
                    texto_respuesta = response.text.encode('utf-8', errors='ignore').decode('utf-8')
                    break
                except Exception as err:
                    ultimo_error = err
                    print(f"Aviso: Gemini ({nombre_modelo}) intento {intento+1} falló: {err}")
                    time.sleep(3)
            if texto_respuesta:
                break

        # --- NIVEL 2: OPENROUTER (Respaldo si Gemini no respondió) ---
        if not texto_respuesta and (time.time() - tiempo_inicio <= limite_tiempo):
            print("Gemini agotado. Intentando con OpenRouter...")
            try:
                base64_image = base64.b64encode(imagen_bytes).decode('utf-8')
                completion = openrouter_client.chat.completions.create(
                    model="google/gemini-2.5-flash",
                    messages=[{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": f"data:{mime_tipo};base64,{base64_image}"}}
                        ]
                    }],
                    response_format={"type": "json_object"}
                )
                texto_respuesta = completion.choices[0].message.content
            except Exception as err_or:
                ultimo_error = err_or
                print(f"OpenRouter también falló: {err_or}")

        # --- NIVEL 3: GROQ CLOUD (Respaldo final ultrarrápido) ---
        if not texto_respuesta and (time.time() - tiempo_inicio <= limite_tiempo):
            print("OpenRouter agotado. Intentando con Groq Cloud...")
            try:
                base64_image = base64.b64encode(imagen_bytes).decode('utf-8')
                completion = groq_client.chat.completions.create(
                    model="llama-3.3-70b-vision-versatile",
                    messages=[{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": f"data:{mime_tipo};base64,{base64_image}"}}
                        ]
                    }],
                    response_format={"type": "json_object"}
                )
                texto_respuesta = completion.choices[0].message.content
            except Exception as err_groq:
                ultimo_error = err_groq
                print(f"Groq Cloud también falló: {err_groq}")

        if not texto_respuesta:
            raise Exception(f"Todos los proveedores de IA fallaron o se agotó el tiempo límite de 1 minuto. Último error: {ultimo_error}")

        datos_factura = json.loads(texto_respuesta)

        proveedor = datos_factura.get('proveedor') or proveedor_sugerido
        envio_ia = float(datos_factura.get('costo_envio', 0.0))
        if costo_envio == 0.0:
            costo_envio = envio_ia

        detalles = datos_factura.get('detalles', [])

        if not detalles:
            return jsonify({'success': False, 'message': 'La IA no pudo detectar productos en la imagen.'})

        total_mercancia = sum(float(item['cantidad']) * float(item['costo']) for item in detalles)
        total_factura = total_mercancia + costo_envio

        nueva_factura = FacturaCompra(
            proveedor=proveedor,
            costo_envio=costo_envio,
            total=total_factura
        )
        db.session.add(nueva_factura)
        db.session.flush()

        for item in detalles:
            codigo = str(item.get('codigo', '')).strip()
            nombre = str(item['nombre']).strip()
            cantidad = int(item['cantidad'])
            costo = float(item['costo'])

            detalle = DetalleCompra(
                factura_id=nueva_factura.id,
                codigo_barras=codigo if codigo else None,
                producto_nombre=nombre,
                cantidad=cantidad,
                costo_unitario=costo,
            )
            db.session.add(detalle)

            producto = None
            if codigo:
                producto = Producto.query.filter_by(codigo_barras=codigo).first()
            if not producto:
                producto = Producto.query.filter_by(nombre=nombre).first()

            if producto:
                producto.cantidad += cantidad
                producto.costo_unitario = costo
                producto.entradas += cantidad
                if codigo:
                    producto.codigo_barras = codigo
            else:
                nuevo_prod = Producto(
                    codigo_barras=codigo if codigo else None,
                    nombre=nombre,
                    cantidad=cantidad,
                    costo_unitario=costo,
                    entradas=cantidad
                )
                db.session.add(nuevo_prod)

        db.session.commit()
        return jsonify({
            'success': True,
            'message': '¡Factura procesada con IA y registrada con éxito!',
            'factura_id': nueva_factura.id,
            'datos_extraidos': datos_factura
        })

    except Exception as e:
        db.session.rollback()
        error_limpio = str(e).encode('utf-8', errors='ignore').decode('utf-8')
        return jsonify({'success': False, 'message': f'Error al procesar con IA: {error_limpio}'})

# --- RUTAS ---
@app.route('/')
def menu():
    if 'tasa_bcv' not in session:
        session['tasa_bcv'] = obtener_tasa_bcv()

    tasa_actual = session['tasa_bcv']
    facturas = FacturaVenta.query.all()
    total_usd = sum(f.total for f in facturas)
    total_bs = total_usd * tasa_actual

    total_usd_str = f'{total_usd:,.2f}'
    total_bs_str = f'{total_bs:,.2f}'
    repuestos_criticos = Producto.query.filter(Producto.cantidad <= 1).all()

    return render_template(
        'menu.html',
        tasa_bcv=tasa_actual,
        total_usd=total_usd_str,
        total_bs=total_bs_str,
        repuestos_criticos=repuestos_criticos,
    )


@app.route('/inventario')
def inventario():
    productos = Producto.query.all()
    valor_total = sum(p.cantidad * p.costo_unitario for p in productos)
    return render_template(
        'inventario.html', productos=productos, valor_total=valor_total
    )


@app.route('/producto/editar/<int:id>', methods=['POST'])
def editar_producto(id):
    producto = Producto.query.get_or_404(id)
    producto.codigo_barras = request.form.get('codigo_barras') or None
    producto.nombre = request.form.get('nombre')
    producto.costo_unitario = float(request.form.get('costo_unitario'))
    producto.precio_venta = float(request.form.get('precio_venta'))
    db.session.commit()
    flash('Producto actualizado.', 'success')
    return redirect(url_for('inventario'))


@app.route('/compra', methods=['GET', 'POST'])
def compra():
    if request.method == 'POST':
        datos = request.json
        detalles = datos.get('detalles', [])
        proveedor = datos.get('proveedor', 'Proveedor General')
        costo_envio = float(datos.get('costo_envio', 0.0))
        total_mercancia = float(datos.get('total', 0.0))

        if not detalles:
            return jsonify({'success': False, 'message': 'No hay productos en la factura.'})

        total_factura = total_mercancia + costo_envio

        nueva_factura = FacturaCompra(
            proveedor=proveedor,
            costo_envio=costo_envio,
            total=total_factura
        )
        db.session.add(nueva_factura)
        db.session.flush()

        for item in detalles:
            codigo = item.get('codigo', '').strip()
            nombre = item['nombre'].strip()
            cantidad = int(item['cantidad'])
            costo = float(item['costo'])

            detalle = DetalleCompra(
                factura_id=nueva_factura.id,
                codigo_barras=codigo if codigo else None,
                producto_nombre=nombre,
                cantidad=cantidad,
                costo_unitario=costo,
            )
            db.session.add(detalle)

            producto = None
            if codigo:
                producto = Producto.query.filter_by(codigo_barras=codigo).first()
            if not producto:
                producto = Producto.query.filter_by(nombre=nombre).first()

            if producto:
                producto.cantidad += cantidad
                producto.costo_unitario = costo
                producto.entradas += cantidad
                if codigo:
                    producto.codigo_barras = codigo
            else:
                nuevo_prod = Producto(
                    codigo_barras=codigo if codigo else None,
                    nombre=nombre,
                    cantidad=cantidad,
                    costo_unitario=costo,
                    entradas=cantidad
                )
                db.session.add(nuevo_prod)

        db.session.commit()
        return jsonify({'success': True, 'message': 'Factura registrada con éxito.'})

    facturas = FacturaCompra.query.order_by(FacturaCompra.fecha.desc()).all()
    return render_template('agregar_compra.html', facturas=facturas)


@app.route('/venta', methods=['GET', 'POST'])
def venta():
    if request.method == 'POST':
        datos = request.json
        detalles = datos.get('detalles', [])
        metodo_pago = datos.get('metodo_pago', 'Efectivo')
        tasa_bcv = float(datos.get('tasa_bcv', session.get('tasa_bcv', 0.0)))
        total_bs_req = float(datos.get('total_bs', 0.0))

        if not detalles:
            return jsonify({'success': False, 'message': 'El carrito está vacío.'})

        total_venta = 0.0

        for item in detalles:
            nombre_prod = item.get('nombre', '').strip()
            producto = Producto.query.filter_by(nombre=nombre_prod).first()

            if not producto:
                return jsonify({'success': False, 'message': f'El producto "{nombre_prod}" no existe.'})
            if producto.cantidad < int(item['cantidad']):
                return jsonify({'success': False, 'message': f'Stock insuficiente para {nombre_prod}.'})

        nueva_factura = FacturaVenta(
            metodo_pago=metodo_pago,
            tasa_bcv=tasa_bcv,
            total_bs=total_bs_req
        )
        db.session.add(nueva_factura)
        db.session.flush()

        for item in detalles:
            nombre_prod = item.get('nombre', '').strip()
            producto = Producto.query.filter_by(nombre=nombre_prod).first()

            cant = int(item['cantidad'])
            precio = float(item['precio'])

            producto.cantidad -= cant
            producto.salidas += cant

            detalle = DetalleVenta(
                factura_id=nueva_factura.id,
                producto_nombre=producto.nombre,
                cantidad=cant,
                precio_unitario=precio,
                costo_historico=producto.costo_unitario
            )
            db.session.add(detalle)
            total_venta += cant * precio

        nueva_factura.total = total_venta
        if nueva_factura.total_bs == 0.0 and tasa_bcv > 0:
            nueva_factura.total_bs = total_venta * tasa_bcv

        db.session.commit()
        return jsonify({'success': True, 'message': 'Venta registrada con éxito.'})

    if 'tasa_bcv' not in session:
        session['tasa_bcv'] = obtener_tasa_bcv()

    tasa_actual = session['tasa_bcv']
    return render_template('venta.html', tasa_bcv=tasa_actual)


@app.route('/api/factura_venta/<int:id>')
def obtener_factura_venta(id):
    factura = FacturaVenta.query.get_or_404(id)
    detalles = [{
        'producto': d.producto_nombre,
        'cantidad': d.cantidad,
        'precio_unitario': d.precio_unitario,
        'subtotal': d.cantidad * d.precio_unitario,
    } for d in factura.detalles]
    return jsonify({
        'id': factura.id,
        'fecha': factura.fecha.strftime('%d/%m/%Y %H:%M'),
        'metodo_pago': factura.metodo_pago,
        'total': factura.total,
        'detalles': detalles,
    })


@app.route('/resumen_ventas')
def resumen_ventas():
    facturas = FacturaVenta.query.order_by(FacturaVenta.fecha.desc()).all()

    if 'tasa_bcv' not in session:
        session['tasa_bcv'] = obtener_tasa_bcv()
    tasa_actual = session['tasa_bcv']

    semanas_dict = defaultdict(lambda: defaultdict(list))

    for v in facturas:
        num_semana = v.fecha.strftime("Semana %W (%Y)")
        dia_str = v.fecha.strftime("%d/%m/%Y")
        hora_str = v.fecha.strftime("%I:%M %p")

        t_bcv = v.tasa_bcv if v.tasa_bcv > 0 else tasa_actual
        t_bs = v.total_bs if v.total_bs > 0 else (v.total * t_bcv)

        detalles_lista = []
        for d in v.detalles:
            detalles_lista.append({
                'nombre': d.producto_nombre,
                'cantidad': d.cantidad,
                'precio': d.precio_unitario
            })

        semanas_dict[num_semana][dia_str].append({
            'id': f"VNT-{v.id:04d}",
            'hora': hora_str,
            'metodo_pago': v.metodo_pago,
            'tasa_bcv': t_bcv,
            'total_usd': v.total,
            'total_bs': t_bs,
            'detalles': detalles_lista
        })

    semanas_lista = []
    for sem_titulo, dias_dict in semanas_dict.items():
        dias_lista = []
        tot_sem_usd = 0.0
        tot_sem_bs = 0.0

        for dia_str, lista_ventas in dias_dict.items():
            tot_dia_usd = sum(item['total_usd'] for item in lista_ventas)
            tot_dia_bs = sum(item['total_bs'] for item in lista_ventas)

            tot_sem_usd += tot_dia_usd
            tot_sem_bs += tot_dia_bs

            dias_lista.append({
                'fecha_str': dia_str,
                'total_usd': tot_dia_usd,
                'total_bs': tot_dia_bs,
                'ventas': lista_ventas
            })

        semanas_lista.append({
            'titulo': sem_titulo,
            'total_usd': tot_sem_usd,
            'total_bs': tot_sem_bs,
            'dias': dias_lista
        })

    return render_template('resumen_ventas.html', semanas=semanas_lista)


# --- RUTA: RESUMEN FINANCIERO Y FLUJO DE CAJA ---
@app.route('/resumen_financiero')
def resumen_financiero():
    total_inversion = db.session.query(db.func.sum(FacturaCompra.total)).scalar() or 0.0
    total_ventas = db.session.query(db.func.sum(FacturaVenta.total)).scalar() or 0.0

    productos = Producto.query.all()
    valor_inventario = sum(p.cantidad * p.costo_unitario for p in productos)

    ganancia_realizada = 0.0
    facturas_venta = FacturaVenta.query.all()
    for f in facturas_venta:
        for d in f.detalles:
            costo_ref = getattr(d, 'costo_historico', 0.0)
            if not costo_ref:
                prod = Producto.query.filter_by(nombre=d.producto_nombre).first()
                costo_ref = prod.costo_unitario if prod else 0.0

            ganancia_realizada += d.cantidad * (d.precio_unitario - costo_ref)

    margen_bruto_pct = (ganancia_realizada / total_ventas * 100) if total_ventas > 0 else 0.0
    flujo_caja_neto = total_ventas - total_inversion

    if 'tasa_bcv' not in session:
        session['tasa_bcv'] = obtener_tasa_bcv()
    tasa_actual = session['tasa_bcv']

    return render_template(
        'resumen_financiero.html',
        total_inversion=total_inversion,
        total_ventas=total_ventas,
        valor_inventario=valor_inventario,
        ganancia_realizada=ganancia_realizada,
        margen_bruto_pct=round(margen_bruto_pct, 1),
        flujo_caja_neto=flujo_caja_neto,
        tasa_bcv=tasa_actual
    )


@app.route('/eliminar_venta/<int:id>', methods=['POST'])
def eliminar_venta(id):
    factura = FacturaVenta.query.get_or_404(id)

    for detalle in factura.detalles:
        producto = Producto.query.filter_by(nombre=detalle.producto_nombre).first()
        if producto:
            producto.cantidad += detalle.cantidad
            producto.salidas -= detalle.cantidad

    db.session.delete(factura)
    db.session.commit()
    flash('Venta anulada y stock devuelto.', 'success')
    return redirect(url_for('resumen_ventas'))


if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=True, port=5051)