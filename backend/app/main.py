"""FastAPI application for Market Regime Classifier"""
import logging
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import regime, features, health, chart_v2, orderflow, websocket

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup/shutdown"""
    # Startup
    logger.info("Starting Market Regime Classifier API...")
    # TODO: Initialize data store, load historical data, etc.
    yield
    # Shutdown
    logger.info("Shutting down Market Regime Classifier API...")
    # TODO: Cleanup resources


app = FastAPI(
    title="Market Regime Classifier API",
    description="Multi-timeframe regime classification for MNQ futures using order flow analysis",
    version="0.1.0",
    lifespan=lifespan
)

# CORS middleware for React frontend
# In production, allow all origins or set CORS_ORIGINS env var
cors_origins_env = os.getenv("CORS_ORIGINS", "")
if cors_origins_env == "*" or os.getenv("ENVIRONMENT") == "production":
    cors_origins = ["*"]
elif cors_origins_env:
    cors_origins = [origin.strip() for origin in cors_origins_env.split(",")]
else:
    cors_origins = [
        "http://localhost:5173",  # Vite default (localhost)
        "http://127.0.0.1:5173",  # Vite default (127.0.0.1)
        "http://localhost:3000",  # Alternative
        "http://127.0.0.1:3000",  # Alternative
        "http://localhost",       # Docker frontend
        "http://127.0.0.1",       # Docker frontend
    ]

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(health.router, prefix="/api", tags=["health"])
app.include_router(chart_v2.router, prefix="/api/v2", tags=["chart-v2"])
app.include_router(regime.router, prefix="/api/regime", tags=["regime"])
app.include_router(features.router, prefix="/api/features", tags=["features"])
app.include_router(orderflow.router, prefix="/api/orderflow", tags=["orderflow"])
app.include_router(websocket.router, prefix="/ws", tags=["websocket"])


@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "name": "Market Regime Classifier API",
        "version": "0.1.0",
        "status": "running"
    }


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    """Global exception handler"""
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"}
    )
