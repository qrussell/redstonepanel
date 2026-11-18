from pydantic import BaseModel
from typing import Dict, Any, Optional

class ServerDeployRequest(BaseModel):
    edition: str
    template_id: Optional[str] = None
    loader: str = "vanilla"          # Java: vanilla, paper or fabric
    version: Optional[str] = None    # Java: a Minecraft release; None = the newest

class ServerUpdateRequest(BaseModel):
    version: Optional[str] = None    # Java: a Minecraft release; None = the newest

class ServerActionRequest(BaseModel):
    action: str 

class ServerPropertiesUpdate(BaseModel):
    properties: Dict[str, Any]