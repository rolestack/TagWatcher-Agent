from typing import Optional
from pydantic import BaseModel


class ContainerInfo(BaseModel):
    container_id: str
    name: str
    image: str
    tag: str
    digest: Optional[str] = None
    status: str
    labels: dict = {}


class RegisterRequest(BaseModel):
    token: str
    hostname: str


class RegisterResponse(BaseModel):
    agent_secret: str
