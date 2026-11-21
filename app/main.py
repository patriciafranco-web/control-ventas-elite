import base64
import csv
import hashlib
import hmac
import io
import json
import os
import random
import string
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from typing import Optional
from fastapi import FastAPI, Depends, Request, Response, HTTPException, Header
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from .database import engine, Base, get_db, SessionLocal
from .models import Cliente, Licencia, Venta, AuditoriaLog
from .schemas import (
    LoginRequest,
    VentaRapidaCreate,
    VentaUpdateRequest,
    LicenciaRenovarRequest,
    LicenciaEstadoRequest,
    LicenciaHeartbeatRequest,
)
from .seed import poblar_datos_demo

Base.metadata.create_all(bind=engine)


def asegurar_columnas_db():
    """Agrega columnas nuevas sin borrar datos existentes en Render/SQLite."""
    inspector = inspect(engine)
    if "licencias" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("licencias")}
    with engine.begin() as conn:
        if "canon_mensual" not in cols:
            conn.execute(text("ALTER TABLE licencias ADD COLUMN canon_mensual FLOAT DEFAULT 0"))
        if "saldo_pendiente" not in cols:
            conn.execute(text("ALTER TABLE licencias ADD COLUMN saldo_pendiente FLOAT DEFAULT 0"))
        if "dias_ciclo" not in cols:
            conn.execute(text("ALTER TABLE licencias ADD COLUMN dias_ciclo INTEGER DEFAULT 30"))

    # Ajusta registros previos: si vencen en >45 días eran anuales/largos (canon_mensual = 0 hasta que toque),
    # si vencen en <=45 días y canon_mensual es 0, toma monto_referencia como canon mensual.
    db = SessionLocal()
    try:
        for lic in db.query(Licencia).all():
            duracion = (lic.fecha_vencimiento - (lic.fecha_inicio or date.today())).days
            if (lic.canon_mensual or 0) == 0 and (lic.monto_referencia or 0) > 0:
                if duracion <= 45:
                    lic.canon_mensual = lic.monto_referencia
                    lic.dias_ciclo = 30
                else:
                    lic.canon_mensual = 0.0
                    lic.dias_ciclo = 365 if duracion <= 400 else 3650
        db.commit()
    finally:
        db.close()


asegurar_columnas_db()

ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "medysis2026")
ADMIN_PIN = os.getenv("ADMIN_PIN", "2026")
SESSION_SECRET = os.getenv("SESSION_SECRET", "medysis-secret-key-change-in-prod")
COOKIE_NAME = "medysis_session"

CARGAR_DEMO = os.getenv("CARGAR_DEMO", "true").lower() == "true"
EMPRESA_NOMBRE = os.getenv("EMPRESA_NOMBRE", "MEDYSIS")
EMPRESA_RUC = os.getenv("EMPRESA_RUC", "4775796-5")
EMPRESA_LOGO = os.getenv("EMPRESA_LOGO", "/static/logo.png.jpeg")
ALIAS_PAGO = os.getenv("ALIAS_PAGO", "")


def _crear_token_sesion(usuario: str) -> str:
    payload = {"u": usuario, "exp": int(time.time()) + 60 * 60 * 24 * 7}
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("utf-8")
    firma = hmac.new(SESSION_SECRET.encode("utf-8"), raw.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{raw}.{firma}"


def _sesion_valida(request: Request) -> Optional[str]:
    token = request.cookies.get(COOKIE_NAME)
    if not token or "." not in token:
        return None
    raw, firma = token.split(".", 1)
    esperada = hmac.new(SESSION_SECRET.encode("utf-8"), raw.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(firma, esperada):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(raw.encode("utf-8")).decode("utf-8"))
        if int(data.get("exp", 0)) < int(time.time()):
            return None
        return data.get("u")
    except Exception:
        return None


def requerir_acceso(
    request: Request,
    x_admin_pin: Optional[str] = Header(default=None),
):
    usuario_sesion = _sesion_valida(request)
    if usuario_sesion:
        return usuario_sesion
    if ADMIN_PIN and x_admin_pin and hmac.compare_digest(x_admin_pin, ADMIN_PIN):
        return ADMIN_USER
    raise HTTPException(status_code=401, detail="Debes iniciar sesión con usuario y contraseña")


def registrar_auditoria(db: Session, accion: str, detalle: str):
    db.add(AuditoriaLog(accion=accion, detalle=detalle))


def recalcular_estados_licencias(db: Session):
    hoy = date.today()
    licencias = db.query(Licencia).all()
    for lic in licencias:
        if lic.estado == "suspendida":
            continue
        dias_rest = (lic.fecha_vencimiento - hoy).days
        gracia = lic.dias_gracia or 0
        if dias_rest < -gracia:
            lic.estado = "vencida"
        elif dias_rest < 0:
            lic.estado = "en_gracia"
        elif dias_rest <= 15:
            lic.estado = "por_vencer"
        else:
            lic.estado = "activa"
    db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = SessionLocal()
    try:
        if CARGAR_DEMO:
            poblar_datos_demo(db)
        recalcular_estados_licencias(db)
    finally:
        db.close()
    yield


app = FastAPI(
    title="MEDYSIS · Control de Ventas & Licencias Élite",
    version="3.3.0",
    lifespan=lifespan,
)

if os.path.isdir("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

templates = Jinja2Templates(directory="templates")


def generar_codigo_licencia(rubro: str, modelo: str) -> str:
    prefijos = {"clinicas": "CLI", "institutos": "INS", "sorteos": "SRT"}
    sufijos = {"saas": "CLD", "local": "LOC"}
    aleatorio = "".join(random.choices(string.digits, k=4))
    return f"{prefijos.get(rubro, 'SYS')}-{date.today().year}-{aleatorio}-{sufijos.get(modelo, 'PRO')}"


def calcular_canon_efectivo(canon_input: Optional[float], monto: float, dias_vigencia: int) -> float:
    """Si el usuario indicó un canon mensual explícito, lo respeta.
    Si lo dejó vacío: solo si el ciclo es mensual (<=45 días) toma el monto como mensual;
    si es anual (365) o perpetuo, devuelve 0 porque ya pagó el año por adelantado."""
    if canon_input is not None:
        return float(canon_input)
    if dias_vigencia <= 45:
        return float(monto)
    return 0.0


@app.get("/api/auth/estado")
def estado_sesion(request: Request):
    usuario = _sesion_valida(request)
    return {
        "autenticado": bool(usuario),
        "usuario": usuario or "",
        "empresa": {
            "nombre": EMPRESA_NOMBRE,
            "ruc": EMPRESA_RUC,
            "logo": EMPRESA_LOGO,
        },
    }


@app.post("/api/auth/login")
def iniciar_sesion(payload: LoginRequest, response: Response):
    usuario_ok = hmac.compare_digest(payload.usuario.strip(), ADMIN_USER)
    pass_ok = hmac.compare_digest(payload.password, ADMIN_PASSWORD)
    if not (usuario_ok and pass_ok):
        raise HTTPException(status_code=401, detail="Usuario o contraseña incorrectos")

    token = _crear_token_sesion(ADMIN_USER)
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 7,
        path="/",
    )
    return {"status": "ok", "usuario": ADMIN_USER}


@app.post("/api/auth/logout")
def cerrar_sesion(response: Response):
    response.delete_cookie(key=COOKIE_NAME, path="/")
    return {"status": "ok"}


@app.get("/manifest.json")
def pwa_manifest():
    return JSONResponse({
        "name": f"{EMPRESA_NOMBRE} · Control de Ventas",
        "short_name": EMPRESA_NOMBRE,
        "start_url": "/",
        "display": "standalone",
        "background_color": "#070b14",
        "theme_color": "#070b14",
        "orientation": "portrait-primary",
        "icons": [
            {
                "src": EMPRESA_LOGO,
                "sizes": "512x512",
                "type": "image/jpeg",
                "purpose": "any maskable"
            }
        ]
    })


@app.get("/sw.js")
def pwa_service_worker():
    js_code = """
    self.addEventListener('install', (e) => self.skipWaiting());
    self.addEventListener('activate', (e) => e.waitUntil(clients.claim()));
    self.addEventListener('fetch', (e) => {
      e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));
    });
    """
    return Response(content=js_code, media_type="application/javascript")


@app.get("/", response_class=HTMLResponse)
def dashboard_view(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
    )


@app.get("/api/dashboard")
def obtener_metricas_dashboard(
    _: str = Depends(requerir_acceso),
    rubro: Optional[str] = "todos",
    modelo: Optional[str] = "todos",
    estado_lic: Optional[str] = "todos",
    buscar: Optional[str] = "",
    db: Session = Depends(get_db)
):
    recalcular_estados_licencias(db)
    hoy = date.today()
    inicio_mes_actual = hoy.replace(day=1)
    mes_anterior_fin = inicio_mes_actual - timedelta(days=1)
    inicio_mes_anterior = mes_anterior_fin.replace(day=1)

    query_ventas = db.query(Venta)
    query_licencias = db.query(Licencia)

    if rubro and rubro != "todos":
        query_ventas = query_ventas.filter(Venta.rubro == rubro)
        query_licencias = query_licencias.filter(Licencia.rubro == rubro)

    if modelo and modelo != "todos":
        query_ventas = query_ventas.filter(Venta.modelo == modelo)
        query_licencias = query_licencias.filter(Licencia.modelo == modelo)

    ventas = query_ventas.order_by(Venta.fecha_venta.asc()).all()
    todas_licencias_filtro = query_licencias.all()

    termino = (buscar or "").strip().lower()
    if termino:
        ventas = [
            v for v in ventas
            if termino in v.cliente.nombre.lower()
            or termino in v.plan.lower()
            or termino in (v.cliente.telefono or "").lower()
        ]
        todas_licencias_filtro = [
            l for l in todas_licencias_filtro
            if termino in l.cliente.nombre.lower()
            or termino in l.codigo.lower()
            or termino in l.plan.lower()
            or termino in (l.cliente.telefono or "").lower()
        ]

    licencias = list(todas_licencias_filtro)
    if estado_lic and estado_lic != "todos":
        licencias = [l for l in licencias if l.estado == estado_lic]

    ingresos_totales = sum(v.monto for v in ventas)
    ingresos_mes_actual = sum(v.monto for v in ventas if v.fecha_venta >= inicio_mes_actual)
    ingresos_mes_anterior = sum(
        v.monto for v in ventas
        if inicio_mes_anterior <= v.fecha_venta <= mes_anterior_fin
    )

    variacion_mes = (
        round(((ingresos_mes_actual - ingresos_mes_anterior) / ingresos_mes_anterior) * 100, 1)
        if ingresos_mes_anterior > 0 else 100.0
    )

    cobro_mensual_fijo = sum(
        float(l.canon_mensual or 0)
        for l in todas_licencias_filtro
        if l.estado in ("activa", "por_vencer", "en_gracia")
    )

    saldos_pendientes_total = sum(
        float(l.saldo_pendiente or 0)
        for l in todas_licencias_filtro
        if l.estado != "suspendida"
    )

    lic_activas = sum(1 for l in todas_licencias_filtro if l.estado in ("activa", "por_vencer"))
    lic_en_gracia = sum(1 for l in todas_licencias_filtro if l.estado == "en_gracia")
    lic_por_vencer = sum(1 for l in todas_licencias_filtro if l.estado == "por_vencer")
    lic_vencidas = sum(1 for l in todas_licencias_filtro if l.estado == "vencida")
    lic_suspendidas = sum(1 for l in todas_licencias_filtro if l.estado == "suspendida")
    ticket_promedio = round(ingresos_totales / len(ventas), 0) if ventas else 0.0

    total_cartera = len([l for l in todas_licencias_filtro if l.estado != "suspendida"])
    vigentes_cartera = lic_activas + lic_en_gracia
    tasa_retencion = round((vigentes_cartera / total_cartera) * 100, 1) if total_cartera > 0 else 100.0

    cobro_vencido = 0.0
    cobro_7d = 0.0
    cobro_15d = 0.0
    cobro_30d = 0.0

    for l in todas_licencias_filtro:
        if l.estado == "suspendida":
            continue
        dias_rest = (l.fecha_vencimiento - hoy).days
        a_cobrar_ciclo = float(l.canon_mensual or 0) + float(l.saldo_pendiente or 0)
        if (l.dias_ciclo or 30) >= 365 and dias_rest <= 30 and (l.canon_mensual or 0) == 0:
            a_cobrar_ciclo += float(l.monto_referencia or 0)

        if dias_rest < 0:
            cobro_vencido += a_cobrar_ciclo
        elif dias_rest <= 7:
            cobro_7d += a_cobrar_ciclo
        elif dias_rest <= 15:
            cobro_15d += a_cobrar_ciclo
        elif dias_rest <= 30:
            cobro_30d += a_cobrar_ciclo

    proyeccion_30d_total = round(cobro_7d + cobro_15d + cobro_30d, 0)

    acumulado_clientes = {}
    for v in ventas:
        cid = v.cliente_id
        if cid not in acumulado_clientes:
            acumulado_clientes[cid] = {
                "cliente_id": cid,
                "nombre": v.cliente.nombre,
                "rubro": v.rubro,
                "total": 0.0,
                "operaciones": 0
            }
        acumulado_clientes[cid]["total"] += v.monto
        acumulado_clientes[cid]["operaciones"] += 1

    top_clientes = sorted(
        [
            {**item, "total": round(item["total"], 0)}
            for item in acumulado_clientes.values()
        ],
        key=lambda x: x["total"],
        reverse=True
    )[:5]

    meses_labels = []
    serie_clinicas = []
    serie_institutos = []
    serie_sorteos = []
    serie_total = []
    serie_saas = []
    serie_local = []

    for i in range(5, -1, -1):
        anio = hoy.year
        mes = hoy.month - i
        while mes <= 0:
            mes += 12
            anio -= 1
        etiqueta = f"{anio}-{mes:02d}"
        meses_labels.append(etiqueta)

        v_mes = [v for v in ventas if v.fecha_venta.year == anio and v.fecha_venta.month == mes]
        c_val = round(sum(v.monto for v in v_mes if v.rubro == "clinicas"), 0)
        i_val = round(sum(v.monto for v in v_mes if v.rubro == "institutos"), 0)
        s_val = round(sum(v.monto for v in v_mes if v.rubro == "sorteos"), 0)

        saas_val = round(sum(v.monto for v in v_mes if v.modelo == "saas"), 0)
        loc_val = round(sum(v.monto for v in v_mes if v.modelo == "local"), 0)

        serie_clinicas.append(c_val)
        serie_institutos.append(i_val)
        serie_sorteos.append(s_val)
        serie_total.append(round(c_val + i_val + s_val, 0))
        serie_saas.append(saas_val)
        serie_local.append(loc_val)

    todas_ventas_modelo = db.query(Venta)
    if modelo and modelo != "todos":
        todas_ventas_modelo = todas_ventas_modelo.filter(Venta.modelo == modelo)
    lista_modelo = todas_ventas_modelo.all()

    distribucion_rubro = [
        {"name": "Médicos / Clínicas", "value": round(sum(v.monto for v in lista_modelo if v.rubro == "clinicas"), 0)},
        {"name": "Institutos", "value": round(sum(v.monto for v in lista_modelo if v.rubro == "institutos"), 0)},
        {"name": "Sorteos", "value": round(sum(v.monto for v in lista_modelo if v.rubro == "sorteos"), 0)},
    ]

    licencias_detalle = []
    for l in sorted(licencias, key=lambda x: x.fecha_vencimiento):
        dias_restantes = (l.fecha_vencimiento - hoy).days
        licencias_detalle.append({
            "id": l.id,
            "cliente_id": l.cliente_id,
            "codigo": l.codigo,
            "cliente": l.cliente.nombre,
            "telefono": l.cliente.telefono or "",
            "rubro": l.rubro,
            "modelo": l.modelo,
            "plan": l.plan,
            "monto_referencia": round(l.monto_referencia or 0, 0),
            "canon_mensual": round(l.canon_mensual or 0, 0),
            "saldo_pendiente": round(l.saldo_pendiente or 0, 0),
            "dias_ciclo": l.dias_ciclo or 30,
            "vencimiento": l.fecha_vencimiento.isoformat(),
            "dias_restantes": dias_restantes,
            "dias_gracia": l.dias_gracia or 0,
            "estado": l.estado,
            "version_app": l.version_app or "",
            "ultimo_heartbeat": l.ultimo_heartbeat.strftime("%Y-%m-%d %H:%M") if l.ultimo_heartbeat else "Sin conexión"
        })

    ventas_recientes = []
    for v in sorted(ventas, key=lambda x: (x.fecha_venta, x.id), reverse=True)[:15]:
        ventas_recientes.append({
            "id": v.id,
            "cliente_id": v.cliente_id,
            "cliente": v.cliente.nombre,
            "rubro": v.rubro,
            "modelo": v.modelo,
            "tipo_operacion": v.tipo_operacion,
            "plan": v.plan,
            "monto": round(v.monto, 0),
            "moneda": "PYG",
            "metodo_pago": v.metodo_pago,
            "fecha_venta": v.fecha_venta.isoformat(),
            "fecha_vencimiento": v.fecha_vencimiento.isoformat(),
            "notas": v.notas or ""
        })

    return {
        "moneda": "PYG",
        "empresa": {
            "nombre": EMPRESA_NOMBRE,
            "ruc": EMPRESA_RUC,
            "logo": EMPRESA_LOGO,
            "alias_pago": ALIAS_PAGO,
        },
        "kpis": {
            "ingresos_totales": round(ingresos_totales, 0),
            "ingresos_mes_actual": round(ingresos_mes_actual, 0),
            "variacion_mes": variacion_mes,
            "mrr_estimado": round(cobro_mensual_fijo, 0),
            "saldos_pendientes": round(saldos_pendientes_total, 0),
            "proyeccion_30d": proyeccion_30d_total,
            "cobro_vencido": round(cobro_vencido, 0),
            "tasa_retencion": tasa_retencion,
            "lic_activas": lic_activas,
            "lic_en_gracia": lic_en_gracia,
            "lic_por_vencer": lic_por_vencer,
            "lic_vencidas": lic_vencidas,
            "lic_suspendidas": lic_suspendidas,
            "ticket_promedio": ticket_promedio
        },
        "graficos": {
            "meses": meses_labels,
            "clinicas": serie_clinicas,
            "institutos": serie_institutos,
            "sorteos": serie_sorteos,
            "total": serie_total,
            "saas": serie_saas,
            "local": serie_local,
            "distribucion_rubro": distribucion_rubro,
            "proyeccion_cobros": {
                "vencido": round(cobro_vencido, 0),
                "dias_7": round(cobro_7d, 0),
                "dias_15": round(cobro_15d, 0),
                "dias_30": round(cobro_30d, 0),
            }
        },
        "top_clientes": top_clientes,
        "licencias": licencias_detalle,
        "ventas_recientes": ventas_recientes
    }


@app.get("/api/auditoria")
def listar_auditoria(_: str = Depends(requerir_acceso), db: Session = Depends(get_db)):
    logs = db.query(AuditoriaLog).order_by(AuditoriaLog.id.desc()).limit(40).all()
    return [
        {
            "id": l.id,
            "accion": l.accion,
            "detalle": l.detalle,
            "fecha": l.creado_en.strftime("%Y-%m-%d %H:%M")
        }
        for l in logs
    ]


@app.get("/api/ventas/exportar-csv")
def exportar_ventas_csv(_: str = Depends(requerir_acceso), db: Session = Depends(get_db)):
    ventas = db.query(Venta).order_by(Venta.fecha_venta.desc(), Venta.id.desc()).all()
    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow(["ID", "Fecha", "Cliente", "Rubro", "Modelo", "Operacion", "Plan", "Monto_PYG", "Vencimiento"])
    for v in ventas:
        writer.writerow([
            v.id,
            v.fecha_venta.isoformat(),
            v.cliente.nombre,
            v.rubro,
            v.modelo,
            v.tipo_operacion,
            v.plan,
            int(round(v.monto)),
            v.fecha_vencimiento.isoformat(),
        ])
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=medysis_ventas_{date.today().isoformat()}.csv"},
    )


@app.get("/api/clientes/{cliente_id}")
def detalle_cliente(cliente_id: int, _: str = Depends(requerir_acceso), db: Session = Depends(get_db)):
    cliente = db.query(Cliente).filter(Cliente.id == cliente_id).first()
    if not cliente:
        raise HTTPException(status_code=404, detail="Cliente no encontrado")

    total_pagado = round(sum(v.monto for v in cliente.ventas), 0)
    return {
        "id": cliente.id,
        "nombre": cliente.nombre,
        "rubro": cliente.rubro,
        "contacto": cliente.contacto or "",
        "telefono": cliente.telefono or "",
        "ciudad": cliente.ciudad or "",
        "total_pagado": total_pagado,
        "cantidad_pagos": len(cliente.ventas),
        "licencias": [
            {
                "codigo": l.codigo,
                "modelo": l.modelo,
                "plan": l.plan,
                "canon_mensual": round(l.canon_mensual or 0, 0),
                "saldo_pendiente": round(l.saldo_pendiente or 0, 0),
                "estado": l.estado,
                "vencimiento": l.fecha_vencimiento.isoformat(),
                "version_app": l.version_app or "-",
                "ultimo_heartbeat": l.ultimo_heartbeat.strftime("%Y-%m-%d %H:%M") if l.ultimo_heartbeat else "Sin conexión"
            }
            for l in cliente.licencias
        ],
        "ventas": [
            {
                "id": v.id,
                "fecha_venta": v.fecha_venta.isoformat(),
                "tipo_operacion": v.tipo_operacion,
                "plan": v.plan,
                "monto": round(v.monto, 0),
                "metodo_pago": v.metodo_pago,
                "notas": v.notas or ""
            }
            for v in sorted(cliente.ventas, key=lambda x: x.fecha_venta, reverse=True)
        ]
    }


@app.post("/api/ventas/rapida")
def registrar_venta_rapida(
    payload: VentaRapidaCreate,
    _: str = Depends(requerir_acceso),
    db: Session = Depends(get_db)
):
    hoy = date.today()
    fecha_op = payload.fecha_pago or hoy
    fecha_venc = fecha_op + timedelta(days=payload.dias_vigencia)
    canon = calcular_canon_efectivo(payload.canon_mensual, payload.monto, payload.dias_vigencia)

    cliente = db.query(Cliente).filter(
        Cliente.nombre.ilike(payload.cliente_nombre.strip()),
        Cliente.rubro == payload.rubro
    ).first()

    if not cliente:
        cliente = Cliente(
            nombre=payload.cliente_nombre.strip(),
            rubro=payload.rubro,
            telefono=payload.telefono,
            ciudad=payload.ciudad
        )
        db.add(cliente)
        db.commit()
        db.refresh(cliente)
    else:
        if payload.telefono:
            cliente.telefono = payload.telefono
        if payload.ciudad:
            cliente.ciudad = payload.ciudad

    venta = Venta(
        cliente_id=cliente.id,
        rubro=payload.rubro,
        modelo=payload.modelo,
        tipo_operacion=payload.tipo_operacion,
        plan=payload.plan,
        monto=payload.monto,
        moneda="PYG",
        metodo_pago=payload.metodo_pago,
        fecha_venta=fecha_op,
        fecha_vencimiento=fecha_venc,
        notas=payload.notas
    )
    db.add(venta)

    licencia = db.query(Licencia).filter(
        Licencia.cliente_id == cliente.id,
        Licencia.rubro == payload.rubro,
        Licencia.modelo == payload.modelo
    ).first()

    if payload.actualizar_licencia:
        if licencia and payload.tipo_operacion in ("renovacion", "upgrade", "soporte"):
            base_fecha = licencia.fecha_vencimiento if licencia.fecha_vencimiento > hoy else fecha_op
            licencia.fecha_vencimiento = base_fecha + timedelta(days=payload.dias_vigencia)
            licencia.plan = payload.plan
            licencia.monto_referencia = payload.monto
            licencia.canon_mensual = canon
            licencia.saldo_pendiente = payload.saldo_pendiente
            licencia.dias_ciclo = payload.dias_vigencia
            licencia.dias_gracia = payload.dias_gracia
            licencia.estado = "activa"
        elif not licencia:
            codigo = generar_codigo_licencia(payload.rubro, payload.modelo)
            licencia = Licencia(
                cliente_id=cliente.id,
                codigo=codigo,
                rubro=payload.rubro,
                modelo=payload.modelo,
                plan=payload.plan,
                monto_referencia=payload.monto,
                canon_mensual=canon,
                saldo_pendiente=payload.saldo_pendiente,
                dias_ciclo=payload.dias_vigencia,
                fecha_inicio=fecha_op,
                fecha_vencimiento=fecha_venc,
                dias_gracia=payload.dias_gracia,
                estado="activa",
                usuarios_limite=payload.usuarios_limite
            )
            db.add(licencia)
            db.flush()

    registrar_auditoria(
        db,
        "VENTA / ALTA",
        f"{cliente.nombre} · {payload.plan} · Gs. {int(payload.monto):,} ({fecha_op.isoformat()})"
    )
    db.commit()

    return {
        "status": "ok",
        "mensaje": "Venta registrada correctamente",
        "codigo_licencia": licencia.codigo if licencia else "SIN-LICENCIA",
        "vencimiento": licencia.fecha_vencimiento.isoformat() if licencia else fecha_venc.isoformat()
    }


@app.put("/api/ventas/{venta_id}")
def editar_venta(
    venta_id: int,
    payload: VentaUpdateRequest,
    _: str = Depends(requerir_acceso),
    db: Session = Depends(get_db)
):
    venta = db.query(Venta).filter(Venta.id == venta_id).first()
    if not venta:
        raise HTTPException(status_code=404, detail="Venta no encontrada")

    monto_anterior = int(venta.monto)
    venta.plan = payload.plan.strip()
    venta.monto = payload.monto
    venta.metodo_pago = payload.metodo_pago
    if payload.fecha_venta:
        venta.fecha_venta = payload.fecha_venta
    venta.notas = payload.notas

    registrar_auditoria(
        db,
        "EDICIÓN VENTA",
        f"ID #{venta.id} ({venta.cliente.nombre}): Gs. {monto_anterior:,} -> Gs. {int(payload.monto):,}"
    )
    db.commit()
    return {"status": "ok", "mensaje": "Movimiento actualizado"}


@app.delete("/api/ventas/{venta_id}")
def eliminar_venta(
    venta_id: int,
    _: str = Depends(requerir_acceso),
    db: Session = Depends(get_db)
):
    venta = db.query(Venta).filter(Venta.id == venta_id).first()
    if not venta:
        raise HTTPException(status_code=404, detail="Venta no encontrada")

    registrar_auditoria(
        db,
        "ANULACIÓN",
        f"Anulada venta #{venta.id} de {venta.cliente.nombre} por Gs. {int(venta.monto):,}"
    )
    db.delete(venta)
    db.commit()
    return {"status": "ok", "mensaje": "Movimiento eliminado"}


@app.post("/api/licencias/{licencia_id}/renovar")
def renovar_licencia_rapida(
    licencia_id: int,
    payload: LicenciaRenovarRequest,
    _: str = Depends(requerir_acceso),
    db: Session = Depends(get_db)
):
    lic = db.query(Licencia).filter(Licencia.id == licencia_id).first()
    if not lic:
        raise HTTPException(status_code=404, detail="Licencia no encontrada")

    hoy = date.today()
    fecha_op = payload.fecha_pago or hoy
    base_fecha = lic.fecha_vencimiento if lic.fecha_vencimiento > hoy else fecha_op
    nueva_fecha = base_fecha + timedelta(days=payload.dias_vigencia)
    plan_final = payload.plan.strip() if payload.plan and payload.plan.strip() else lic.plan
    canon = calcular_canon_efectivo(payload.canon_mensual, payload.monto, payload.dias_vigencia)

    lic.fecha_vencimiento = nueva_fecha
    lic.plan = plan_final
    lic.monto_referencia = payload.monto
    lic.canon_mensual = canon
    lic.saldo_pendiente = payload.saldo_pendiente
    lic.dias_ciclo = payload.dias_vigencia
    lic.estado = "por_vencer" if (nueva_fecha - hoy).days <= 15 else "activa"

    venta = Venta(
        cliente_id=lic.cliente_id,
        rubro=lic.rubro,
        modelo=lic.modelo,
        tipo_operacion="renovacion",
        plan=plan_final,
        monto=payload.monto,
        moneda="PYG",
        metodo_pago=payload.metodo_pago,
        fecha_venta=fecha_op,
        fecha_vencimiento=nueva_fecha,
        notas=f"Cobro/renovación licencia {lic.codigo} (+{payload.dias_vigencia} días)"
    )
    db.add(venta)
    registrar_auditoria(
        db,
        "RENOVACIÓN",
        f"{lic.cliente.nombre} ({lic.codigo}) +{payload.dias_vigencia}d · Gs. {int(payload.monto):,} ({fecha_op.isoformat()})"
    )
    db.commit()

    return {
        "status": "ok",
        "mensaje": f"Licencia {lic.codigo} renovada hasta {nueva_fecha.isoformat()}",
        "vencimiento": nueva_fecha.isoformat()
    }


@app.patch("/api/licencias/{licencia_id}/estado")
def cambiar_estado_licencia(
    licencia_id: int,
    payload: LicenciaEstadoRequest,
    _: str = Depends(requerir_acceso),
    db: Session = Depends(get_db)
):
    lic = db.query(Licencia).filter(Licencia.id == licencia_id).first()
    if not lic:
        raise HTTPException(status_code=404, detail="Licencia no encontrada")

    if payload.estado == "suspendida":
        lic.estado = "suspendida"
    else:
        lic.estado = "activa"
        recalcular_estados_licencias(db)

    registrar_auditoria(
        db,
        "ESTADO LICENCIA",
        f"{lic.cliente.nombre} ({lic.codigo}) -> {lic.estado.upper()}"
    )
    db.commit()
    return {"status": "ok", "estado": lic.estado}


@app.post("/api/licencias/heartbeat")
@app.post("/api/licencias/validar")
def validar_y_heartbeat_licencia(payload: LicenciaHeartbeatRequest, db: Session = Depends(get_db)):
    recalcular_estados_licencias(db)
    lic = db.query(Licencia).filter(Licencia.codigo == payload.codigo.strip()).first()
    if not lic:
        raise HTTPException(status_code=404, detail="Licencia no encontrada")

    lic.ultimo_heartbeat = datetime.utcnow()
    if payload.equipo_id:
        lic.equipo_id = payload.equipo_id.strip()
    if payload.version_app:
        lic.version_app = payload.version_app.strip()
    db.commit()

    hoy = date.today()
    dias_restantes = (lic.fecha_vencimiento - hoy).days
    valida = lic.estado in ("activa", "por_vencer", "en_gracia")

    aviso = ""
    if lic.estado == "en_gracia":
        aviso = f"Licencia vencida en período de gracia. Regularizar pago de Gs. {int(lic.canon_mensual or lic.monto_referencia or 0):,}."
    elif lic.estado == "por_vencer":
        aviso = f"Tu licencia vence en {dias_restantes} días ({lic.fecha_vencimiento.isoformat()})."

    return {
        "valida": valida,
        "estado": lic.estado,
        "cliente": lic.cliente.nombre,
        "rubro": lic.rubro,
        "modelo": lic.modelo,
        "plan": lic.plan,
        "fecha_vencimiento": lic.fecha_vencimiento.isoformat(),
        "dias_restantes": dias_restantes,
        "usuarios_limite": lic.usuarios_limite,
        "aviso": aviso
    }


@app.delete("/api/admin/limpiar-todo")
def limpiar_base_para_produccion(
    _: str = Depends(requerir_acceso),
    db: Session = Depends(get_db)
):
    db.query(Venta).delete()
    db.query(Licencia).delete()
    db.query(Cliente).delete()
    db.query(AuditoriaLog).delete()
    db.commit()
    return {"status": "ok", "mensaje": "Base limpiada. Listo para cargar clientes reales."}
