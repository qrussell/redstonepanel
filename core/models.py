from pydantic import BaseModel
from typing import Dict, Any, Optional

class ServerDeployRequest(BaseModel):
    edition: str
    template_id: Optional[str] = None
    loader: str = "vanilla"          # Java: vanilla, paper or fabric
    version: Optional[str] = None    # Java: a Minecraft release; None = the newest
    backup: bool = True              # Java: a "Before update" backup first, for Roll back (1.19)
    advanced: Optional[Dict[str, Any]] = None   # New server's Advanced options (1.17.2), core/deployopts.py

class ServerUpdateRequest(BaseModel):
    version: Optional[str] = None    # Java: a Minecraft release; None = the newest
    backup: bool = True              # Java: a "Before update" backup first, for Roll back (1.19)

class ServerActionRequest(BaseModel):
    action: str 

class ServerPropertiesUpdate(BaseModel):
    properties: Dict[str, Any]