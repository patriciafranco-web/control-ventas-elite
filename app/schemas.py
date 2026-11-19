from datetime import date
from typing import Optional
from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    usuario: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class VentaRapidaCreate(BaseModel):
    cliente_nombre: str = Field(..., min_length=2)
    rubro: str = Field(..., pattern="^(sorteos|clinicas|institutos)$")
    modelo: str = Field(..., pattern="^(saas|local)$")
    tipo_operacion: str = Field(..., pattern="^(venta_nueva|renovacion|upgrade|soporte)$")
    plan: str = Field(..., min_length=2)
    monto: float = Field(..., gt=0)
    moneda: str = Field(default="PYG")
    metodo_pago: str = Field(default="Transferencia")
    telefono: Optional[str] = None
    ciudad: Optional[str] = None
    dias_vigencia: int = Field(default=30, ge=1)
    dias_gracia: int = Field(default=3, ge=0, le=30)
    usuarios_limite: int = Field(default=5, ge=1)
    notas: Optional[str] = None


class VentaUpdateRequest(BaseModel):
    plan: str = Field(..., min_length=2)
    monto: float = Field(..., gt=0)
    metodo_pago: str = Field(default="Transferencia")
    fecha_venta: Optional[date] = None
    notas: Optional[str] = None


class LicenciaRenovarRequest(BaseModel):
    dias_vigencia: int = Field(default=30, ge=1)
    monto: float = Field(..., gt=0)
    plan: Optional[str] = None
    metodo_pago: str = Field(default="Transferencia")


class LicenciaEstadoRequest(BaseModel):
    estado: str = Field(..., pattern="^(activa|suspendida)$")


class LicenciaHeartbeatRequest(BaseModel):
    codigo: str
    equipo_id: Optional[str] = None
    version_app: Optional[str] = None