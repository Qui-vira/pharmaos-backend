"""
PharmaOS AI - API v1 Router
"""

from fastapi import APIRouter

from app.api.v1.endpoints import (
    auth,
    organizations,
    products,
    sales,
    orders,
    catalog,
    patients,
    consultations,
    webhooks,
    notifications,
    admin,
    voice,
)

api_router = APIRouter()

api_router.include_router(auth.router)
api_router.include_router(organizations.router)
api_router.include_router(products.router)
api_router.include_router(sales.router)
api_router.include_router(orders.router)
api_router.include_router(catalog.router)
api_router.include_router(patients.router)
api_router.include_router(consultations.router)
api_router.include_router(webhooks.router)
api_router.include_router(notifications.router)
api_router.include_router(admin.router)
api_router.include_router(voice.router)
