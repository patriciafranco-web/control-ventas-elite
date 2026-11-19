from datetime import date, datetime, timedelta
from sqlalchemy.orm import Session
from .models import Cliente, Licencia, Venta


def poblar_datos_demo(db: Session):
    if db.query(Cliente).count() > 0:
        return

    hoy = date.today()
    ahora = datetime.utcnow()

    clientes_demo = [
        Cliente(nombre="Clínica San Lucas", rubro="clinicas", contacto="Dr. Roberto Méndez", telefono="0981112233", ciudad="Ciudad del Este"),
        Cliente(nombre="Centro Médico Integral Vida", rubro="clinicas", contacto="Dra. Carla Benítez", telefono="0971445566", ciudad="Asunción"),
        Cliente(nombre="Instituto Técnico Superior Alfa", rubro="institutos", contacto="Lic. Marcos Giménez", telefono="0982778899", ciudad="Encarnación"),
        Cliente(nombre="Academia Global Idiomas", rubro="institutos", contacto="Prof. Laura Ortiz", telefono="0991332211", ciudad="Ciudad del Este"),
        Cliente(nombre="MegaSorteos Paraguay", rubro="sorteos", contacto="Diego Villalba", telefono="0985990011", ciudad="Asunción"),
        Cliente(nombre="Rifas & Eventos del Este", rubro="sorteos", contacto="Sofía Cabrera", telefono="0973665544", ciudad="Hernandarias"),
    ]
    db.add_all(clientes_demo)
    db.commit()
    for c in clientes_demo:
        db.refresh(c)

    operaciones = [
        (clientes_demo[0], "clinicas", "saas", "venta_nueva", "Medysis Clínica Pro", 1350000.0, hoy - timedelta(days=140), hoy - timedelta(days=110), "CLI-2026-8841-CLD"),
        (clientes_demo[2], "institutos", "local", "venta_nueva", "Licencia Local Campus", 4800000.0, hoy - timedelta(days=125), hoy + timedelta(days=240), "INS-2026-3310-LOC"),
        (clientes_demo[4], "sorteos", "saas", "venta_nueva", "SaaS Sorteos Gold", 1800000.0, hoy - timedelta(days=105), hoy - timedelta(days=75), "SRT-2026-5501-CLD"),
        (clientes_demo[1], "clinicas", "local", "venta_nueva", "Medysis Policlínica Local", 6500000.0, hoy - timedelta(days=85), hoy + timedelta(days=280), "CLI-2026-9920-LOC"),
        (clientes_demo[3], "institutos", "saas", "venta_nueva", "Instituto Cloud 300 Alumnos", 1100000.0, hoy - timedelta(days=65), hoy - timedelta(days=35), "INS-2026-7742-CLD"),
        (clientes_demo[5], "sorteos", "saas", "venta_nueva", "Sorteos Express Mensual", 900000.0, hoy - timedelta(days=50), hoy - timedelta(days=2), "SRT-2026-1209-CLD"),
        (clientes_demo[0], "clinicas", "saas", "renovacion", "Medysis Clínica Pro", 1350000.0, hoy - timedelta(days=25), hoy + timedelta(days=5), "CLI-2026-8841-CLD"),
        (clientes_demo[4], "sorteos", "saas", "upgrade", "SaaS Sorteos Enterprise", 2850000.0, hoy - timedelta(days=18), hoy + timedelta(days=12), "SRT-2026-5501-CLD"),
        (clientes_demo[3], "institutos", "saas", "renovacion", "Instituto Cloud 300 Alumnos", 1100000.0, hoy - timedelta(days=10), hoy + timedelta(days=20), "INS-2026-7742-CLD"),
        (clientes_demo[5], "sorteos", "local", "venta_nueva", "Terminal Sorteos Off-Line", 3500000.0, hoy - timedelta(days=3), hoy + timedelta(days=362), "SRT-2026-4490-LOC"),
        (clientes_demo[1], "clinicas", "saas", "upgrade", "Medysis Telemedicina + Lab", 1600000.0, hoy - timedelta(days=1), hoy + timedelta(days=29), "CLI-2026-4102-CLD"),
    ]

    licencias_creadas = {}
    for cli, rubro, modelo, tipo_op, plan, monto, f_venta, f_venc, cod in operaciones:
        venta = Venta(
            cliente_id=cli.id,
            rubro=rubro,
            modelo=modelo,
            tipo_operacion=tipo_op,
            plan=plan,
            monto=monto,
            moneda="PYG",
            metodo_pago="Transferencia",
            fecha_venta=f_venta,
            fecha_vencimiento=f_venc,
            notas="Datos iniciales de demostración"
        )
        db.add(venta)

        if cod not in licencias_creadas:
            lic = Licencia(
                cliente_id=cli.id,
                codigo=cod,
                rubro=rubro,
                modelo=modelo,
                plan=plan,
                monto_referencia=monto,
                fecha_inicio=f_venta,
                fecha_vencimiento=f_venc,
                dias_gracia=3,
                estado="activa",
                usuarios_limite=10,
                version_app="v2.4.1",
                ultimo_heartbeat=ahora - timedelta(hours=2)
            )
            db.add(lic)
            licencias_creadas[cod] = lic
        else:
            lic = licencias_creadas[cod]
            lic.monto_referencia = monto
            if f_venc > lic.fecha_vencimiento:
                lic.fecha_vencimiento = f_venc
                lic.plan = plan

    db.commit()