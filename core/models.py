from pydantic import BaseModel
from typing import Dict, Any, Optional

class ServerDeployRequest(BaseModel):
    edition: str
    template_id: Optional[str] = None

class ServerActionRequest(BaseModel):
    action: str 

class ServerPropertiesUpdate(BaseModel):
    properties: Dict[str, Any]