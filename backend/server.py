import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv


from prometheus_fastapi_instrumentator import Instrumentator

ROOT_DIR = Path(__file__).parent
# Load env BEFORE any app/router import: auth_service validates its secret at
# import time, so ordering here is load-bearing.
load_dotenv(ROOT_DIR / ".env")

from fastapi import APIRouter, Depends, FastAPI
from pydantic import BaseModel, Field
from starlette.middleware.cors import CORSMiddleware
from typing import List

from routers.welfare import router as welfare_router
from routers.auth import router as auth_router
from lib.auth_deps import get_current_user


# MongoDB connection
from lib.db import client, db, ensure_indexes


# Startup runs before the yield, shutdown after it. Add your own setup/teardown here.
@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.index_task = asyncio.create_task(ensure_indexes())  # background: a big index build must not block boot
    yield
    client.close()


# Create the main app without a prefix
app = FastAPI(lifespan=lifespan)
Instrumentator().instrument(app).expose(app)

# Create a router with the /api prefix
api_router = APIRouter(prefix="/api")
api_router.include_router(welfare_router)
api_router.include_router(auth_router)


# Define Models
class StatusCheck(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    client_name: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)

class StatusCheckCreate(BaseModel):
    client_name: str

# Add your routes to the router instead of directly to app
@api_router.get("/")
async def root():
    """PUBLIC — API health root."""
    return {"message": "Manobal-AI API", "status": "ready"}

@api_router.post("/status", response_model=StatusCheck)
async def create_status_check(input: StatusCheckCreate, current_user: dict = Depends(get_current_user)):
    """AUTHENTICATED — Record a status check.

    Requiring authentication prevents anonymous writes to the shared collection
    and keeps every mutation on an auditable identity.
    """
    status_dict = input.model_dump()
    status_obj = StatusCheck(**status_dict)
    _ = await db.status_checks.insert_one(status_obj.model_dump())
    return status_obj

@api_router.get("/status", response_model=List[StatusCheck])
async def get_status_checks(current_user: dict = Depends(get_current_user)):
    """AUTHENTICATED — Read status checks."""
    status_checks = await db.status_checks.find().to_list(1000)
    return [StatusCheck(**status_check) for status_check in status_checks]

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Include the router last so every route registered above is served under /api.
app.include_router(api_router)
