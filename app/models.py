from datetime import datetime, date
from sqlalchemy import Column, Integer, String, Float, Date, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from .database import Base


class Cliente(Base):
    __tablename__ = "clientes"

    id = Column(Integer, primary_key=True, index=True)
    nombre = Column(String(150), nullable=False)
    rubro = Column(String(50), nullable=False)  # sorteos | clinicas | institutos
    contacto = Column(String(120), nullable=True)
    telefono = Column(String(50), nullable=True)
    ciudad = Column(String(80), nullable=True)
    creado_en = Column(DateTime, default=datetime.utcnow)

    licencias = relationship("Licencia", back_populates="cliente", cascade="all, delete-orphan")
    ventas = relationship("Venta", back_populates="cliente", cascade="all, delete-orphan")


class Licencia(Base):
    __tablename__ = "licencias"

    id = Column(Integer, primary_key=True, index=True)
    cliente_id = Column(Integer, ForeignKey("clientes.id"), nullable=False)
    codigo = Column(String(60), unique=True, index=True, nullable=False)
    rubro = Column(String(50), nullable=False)  # sorteos | clinicas | institutos
    modelo = Column(String(30), nullable=False)  # saas | local
    plan = Column(String(80), nullable=False)
    monto_referencia = Column(Float, default=0.0)
    fecha_inicio = Column(Date, default=date.today)
    fecha_vencimiento = Column(Date, nullable=False)
    dias_gracia = Column(Integer, default=3)
    estado = Column(String(30), default="activa")  # activa | por_vencer | en_gracia | vencida | suspendida
    usuarios_limite = Column(Integer, default=5)
    equipo_id = Column(String(120), nullable=True)
    version_app = Column(String(40), nullable=True)
    ultimo_heartbeat = Column(DateTime, nullable=True)

    cliente = relationship("Cliente", back_populates="licencias")


class Venta(Base):
    __tablename__ = "ventas"

    id = Column(Integer, primary_key=True, index=True)
    cliente_id = Column(Integer, ForeignKey("clientes.id"), nullable=False)
    rubro = Column(String(50), nullable=False)   # sorteos | clinicas | institutos
    modelo = Column(String(30), nullable=False)  # saas | local
    tipo_operacion = Column(String(40), nullable=False)  # venta_nueva | renovacion | upgrade | soporte
    plan = Column(String(80), nullable=False)
    monto = Column(Float, nullable=False)
    moneda = Column(String(10), default="PYG")
    metodo_pago = Column(String(50), default="Transferencia")
    fecha_venta = Column(Date, default=date.today)
    fecha_vencimiento = Column(Date, nullable=False)
    notas = Column(Text, nullable=True)
    creado_en = Column(DateTime, default=datetime.utcnow)

    cliente = relationship("Cliente", back_populates="ventas")


class AuditoriaLog(Base):
    __tablename__ = "auditoria_logs"

    id = Column(Integer, primary_key=True, index=True)
    accion = Column(String(60), nullable=False)
    detalle = Column(String(255), nullable=False)
    creado_en = Column(DateTime, default=datetime.utcnow)