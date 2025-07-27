import logging
from typing import Optional
import time
import re
import aiohttp
from pydantic import BaseModel, HttpUrl
from sqlalchemy.orm import Session

# Corrected and consolidated imports
from open_webui.models.tools import (
    Tool,
    ToolForm,
    ToolModel,
    ToolResponse,
    ToolUserResponse,
    Tools,
)
from open_webui.utils.plugin import load_tool_module_by_id, replace_imports
from open_webui.config import CACHE_DIR
from open_webui.constants import ERROR_MESSAGES
from fastapi import APIRouter, Depends, HTTPException, Request, status
from open_webui.utils.tools import get_tool_specs
from open_webui.utils.auth import get_admin_user, get_verified_user
from open_webui.utils.access_control import has_access, has_permission
from open_webui.env import SRC_LOG_LEVELS
from open_webui.utils.tools import get_tool_servers_data
from open_webui.internal.db import get_db, SessionLocal


log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MAIN"])


router = APIRouter()

############################
# GetTools (No changes needed)
############################


@router.get("/", response_model=list[ToolUserResponse])
async def get_tools(request: Request, user=Depends(get_verified_user)):
    if not request.app.state.TOOL_SERVERS:
        request.app.state.TOOL_SERVERS = await get_tool_servers_data(
            request.app.state.config.TOOL_SERVER_CONNECTIONS
        )

    tools = Tools.get_tools()
    for server in request.app.state.TOOL_SERVERS:
        tools.append(
            ToolUserResponse(
                **{
                    "id": f"server:{server['idx']}",
                    "user_id": f"server:{server['idx']}",
                    "name": server.get("openapi", {})
                    .get("info", {})
                    .get("title", "Tool Server"),
                    "meta": {
                        "description": server.get("openapi", {})
                        .get("info", {})
                        .get("description", ""),
                    },
                    "access_control": request.app.state.config.TOOL_SERVER_CONNECTIONS[
                        server["idx"]
                    ]
                    .get("config", {})
                    .get("access_control", None),
                    "updated_at": int(time.time()),
                    "created_at": int(time.time()),
                }
            )
        )

    if user.role != "admin":
        tools = [
            tool
            for tool in tools
            if tool.user_id == user.id
            or tool.user_id is None
            or has_access(user.id, "read", tool.access_control)
        ]

    return tools


############################
# GetToolList (No changes needed)
############################


@router.get("/list", response_model=list[ToolUserResponse])
async def get_tool_list(user=Depends(get_verified_user)):
    if user.role == "admin":
        tools = Tools.get_tools()
    else:
        tools = Tools.get_tools_by_user_id(user.id, "write")
    return tools


############################
# LoadFunctionFromLink (No changes needed)
############################


class LoadUrlForm(BaseModel):
    url: HttpUrl


def github_url_to_raw_url(url: str) -> str:
    m1 = re.match(r"https://github\.com/([^/]+)/([^/]+)/tree/([^/]+)/(.*)", url)
    if m1:
        org, repo, branch, path = m1.groups()
        return f"https://raw.githubusercontent.com/{org}/{repo}/refs/heads/{branch}/{path.rstrip('/')}/main.py"

    m2 = re.match(r"https://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.*)", url)
    if m2:
        org, repo, branch, path = m2.groups()
        return (
            f"https://raw.githubusercontent.com/{org}/{repo}/refs/heads/{branch}/{path}"
        )
    return url


@router.post("/load/url", response_model=Optional[dict])
async def load_tool_from_url(form_data: LoadUrlForm, user=Depends(get_admin_user)):
    url = str(form_data.url)
    if not url:
        raise HTTPException(status_code=400, detail="Please enter a valid URL")

    url = github_url_to_raw_url(url)
    url_parts = url.rstrip("/").split("/")
    file_name = url_parts[-1]
    tool_name = (
        file_name[:-3]
        if file_name.endswith(".py")
        and not file_name.startswith(("main.py", "index.py", "__init__.py"))
        else url_parts[-2] if len(url_parts) > 1 else "function"
    )

    try:
        async with aiohttp.ClientSession(trust_env=True) as session:
            async with session.get(
                url, headers={"Content-Type": "application/json"}
            ) as resp:
                resp.raise_for_status()
                data = await resp.text()
                if not data:
                    raise HTTPException(
                        status_code=400, detail="No data received from the URL"
                    )
        return {"name": tool_name, "content": data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error importing tool: {e}")


############################
# ExportTools (No changes needed)
############################


@router.get("/export", response_model=list[ToolModel])
async def export_tools(user=Depends(get_admin_user)):
    return Tools.get_tools()


############################
# CreateNewTools (No changes needed)
############################


@router.post("/create", response_model=Optional[ToolResponse])
async def create_new_tools(
    request: Request, form_data: ToolForm, user=Depends(get_verified_user)
):
    if user.role != "admin" and not has_permission(
        user.id, "workspace.tools", request.app.state.config.USER_PERMISSIONS
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.UNAUTHORIZED,
        )

    if not form_data.id.isidentifier():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only alphanumeric characters and underscores are allowed in the id",
        )
    form_data.id = form_data.id.lower()

    if Tools.get_tool_by_id(form_data.id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=ERROR_MESSAGES.ID_TAKEN
        )

    try:
        form_data.content = replace_imports(form_data.content)
        tool_module, frontmatter = load_tool_module_by_id(
            form_data.id, content=form_data.content
        )
        form_data.meta.manifest = frontmatter

        request.app.state.TOOLS[form_data.id] = tool_module
        specs = get_tool_specs(request.app.state.TOOLS[form_data.id])
        tool = Tools.insert_new_tool(user.id, form_data, specs)

        (CACHE_DIR / "tools" / form_data.id).mkdir(parents=True, exist_ok=True)

        if tool:
            return tool
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT("Error creating tool"),
        )
    except Exception as e:
        log.exception(f"Failed to load tool by id {form_data.id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT(str(e)),
        )


############################
# GetToolsById (No changes needed)
############################


@router.get("/id/{id}", response_model=Optional[ToolModel])
async def get_tools_by_id(id: str, user=Depends(get_verified_user)):
    tool = Tools.get_tool_by_id(id)
    if not tool:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.NOT_FOUND,
        )

    if (
        user.role == "admin"
        or tool.user_id == user.id
        or tool.user_id is None
        or has_access(user.id, "read", tool.access_control)
    ):
        return tool

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=ERROR_MESSAGES.UNAUTHORIZED,
    )


############################
# UpdateToolsById (FIXED)
############################


@router.post("/id/{id}/update", response_model=Optional[ToolModel])
async def update_tools_by_id(
    request: Request, id: str, form_data: ToolForm, user=Depends(get_verified_user)
):
    tool = Tools.get_tool_by_id(id)
    if not tool:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=ERROR_MESSAGES.NOT_FOUND
        )

    if (
        tool.user_id is not None
        and tool.user_id != user.id
        and not has_access(user.id, "write", tool.access_control)
        and user.role != "admin"
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=ERROR_MESSAGES.UNAUTHORIZED
        )

    try:
        updated_data = form_data.model_dump(exclude={"id"}, exclude_unset=True)

        # If it's a system tool, protect its core definition.
        if tool.user_id is None:
            allowed_system_updates = {}
            if "name" in updated_data:
                allowed_system_updates["name"] = updated_data["name"]
            if "meta" in updated_data:
                allowed_system_updates["meta"] = {**tool.meta, **updated_data["meta"]}

            if not allowed_system_updates:
                return tool  # Nothing to update
            
            updated_tool = Tools.update_tool_by_id(id, allowed_system_updates)
        else:
            # It's a user-created tool, so re-parse and update everything.
            form_data.content = replace_imports(form_data.content)
            tool_module, frontmatter = load_tool_module_by_id(id, content=form_data.content)
            updated_data["meta"]["manifest"] = frontmatter

            TOOLS = request.app.state.TOOLS
            TOOLS[id] = tool_module
            updated_data["specs"] = get_tool_specs(TOOLS[id])
            
            updated_tool = Tools.update_tool_by_id(id, updated_data)

        if updated_tool:
            return updated_tool
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT("Error updating tool"),
        )
    except Exception as e:
        log.exception(f"Failed to update tool by id {id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT(str(e)),
        )


############################
# DeleteToolsById (No changes needed)
############################


@router.delete("/id/{id}/delete", response_model=bool)
async def delete_tools_by_id(
    request: Request, id: str, user=Depends(get_verified_user)
):
    tool = Tools.get_tool_by_id(id)
    if not tool:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=ERROR_MESSAGES.NOT_FOUND
        )

    if (
        tool.user_id != user.id
        and not has_access(user.id, "write", tool.access_control)
        and user.role != "admin"
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=ERROR_MESSAGES.UNAUTHORIZED
        )

    if Tools.delete_tool_by_id(id):
        if id in request.app.state.TOOLS:
            del request.app.state.TOOLS[id]
        return True
    return False


############################
# GetToolValves (FIXED)
############################


@router.get("/id/{id}/valves", response_model=Optional[dict])
async def get_tools_valves_by_id(id: str, user=Depends(get_verified_user), db: Session = Depends(get_db)):
    # This function now correctly finds the right valves for the user.
    # It checks for a user's personal copy first, then falls back to the system default.

    # A personal copy of a tool has a predictable ID format.
    personal_copy_id = f"{id}_user_{user.id}"
    personal_copy = db.query(Tool).filter(Tool.id == personal_copy_id, Tool.user_id == user.id).first()

    if personal_copy:
        log.info(f"Returning valves from personal copy '{personal_copy.id}' for user '{user.id}'.")
        return personal_copy.valves

    # If no personal copy, find the system blueprint tool (or a tool created by the user).
    tool_blueprint = db.query(Tool).filter(Tool.id == id).first()
    
    if not tool_blueprint:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ERROR_MESSAGES.NOT_FOUND)

    # Final permission check
    if (tool_blueprint.user_id is not None and tool_blueprint.user_id != user.id and user.role != "admin"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=ERROR_MESSAGES.UNAUTHORIZED)

    log.info(f"Returning default valves from blueprint '{tool_blueprint.id}' for user '{user.id}'.")
    return tool_blueprint.valves


############################
# GetToolValvesSpec (No changes needed)
############################


@router.get("/id/{id}/valves/spec", response_model=Optional[dict])
async def get_tools_valves_spec_by_id(
    request: Request, id: str, user=Depends(get_verified_user)
):
    tool = Tools.get_tool_by_id(id)
    if not tool:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=ERROR_MESSAGES.NOT_FOUND
        )

    # Permission check for spec is implicitly handled by get_tool_by_id above
    if (user.role != "admin" and tool.user_id is not None and tool.user_id != user.id):
         raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=ERROR_MESSAGES.UNAUTHORIZED
        )

    if id in request.app.state.TOOLS:
        tools_module = request.app.state.TOOLS[id]
    else:
        tools_module, _ = load_tool_module_by_id(id)
        request.app.state.TOOLS[id] = tools_module

    if hasattr(tools_module, "Valves"):
        return tools_module.Valves.model_json_schema()
    return None


############################
# UpdateToolValves (FIXED)
############################


@router.post("/id/{id}/valves/update", response_model=Optional[dict])
async def update_tools_valves_by_id(
    request: Request, id: str, form_data: dict, user=Depends(get_verified_user), db: Session = Depends(get_db)
):
    try:
        # Find the system tool blueprint the user wants to configure
        tool_blueprint = db.query(Tool).filter(Tool.id == id, Tool.user_id == None).first()

        if not tool_blueprint:
            # If it's not a system tool, it must be a user's own editable tool
            user_tool = db.query(Tool).filter(Tool.id == id, Tool.user_id == user.id).first()
            if not user_tool:
                raise HTTPException(status_code=404, detail=ERROR_MESSAGES.NOT_FOUND)
            tool_to_update = user_tool
            blueprint_id_for_module_load = user_tool.id
        else:
            # It is a system tool. Check for the user's personal copy.
            personal_copy_id = f"{id}_user_{user.id}"
            personal_copy = db.query(Tool).filter(Tool.id == personal_copy_id).first()

            if not personal_copy:
                log.info(f"Creating personal tool copy for user '{user.id}' from system tool '{id}'")
                personal_copy = Tool(
                    id=personal_copy_id,
                    user_id=user.id,
                    name=tool_blueprint.name,
                    content=tool_blueprint.content,
                    specs=tool_blueprint.specs,
                    meta={"parent_tool_id": id, **tool_blueprint.meta},
                    valves=form_data,
                    updated_at=int(time.time()),
                    created_at=int(time.time()),
                )
                db.add(personal_copy)
            
            tool_to_update = personal_copy
            blueprint_id_for_module_load = tool_blueprint.id

        # Load the Python module of the original blueprint to get the correct Valve schema
        if blueprint_id_for_module_load in request.app.state.TOOLS:
            tools_module = request.app.state.TOOLS[blueprint_id_for_module_load]
        else:
            tools_module, _ = load_tool_module_by_id(blueprint_id_for_module_load) 
            request.app.state.TOOLS[blueprint_id_for_module_load] = tools_module

        if not hasattr(tools_module, "Valves"):
            raise HTTPException(status_code=400, detail="Tool does not support customizable valves.")

        Valves = tools_module.Valves
        validated_valves = Valves(**{k: v for k, v in form_data.items() if v is not None})
        
        tool_to_update.valves = validated_valves.model_dump()
        tool_to_update.updated_at = int(time.time())
        
        db.commit()
        db.refresh(tool_to_update)
        return tool_to_update.valves

    except Exception as e:
        db.rollback()
        log.exception(f"Failed to update tool valves for tool {id}: {e}")
        raise HTTPException(
            status_code=400, detail=ERROR_MESSAGES.DEFAULT(str(e))
        )
    finally:
        db.close()


############################
# UserValves Endpoints (No changes needed)
# Note: These use a separate mechanism from the main "Valves" logic above.
############################

@router.get("/id/{id}/valves/user", response_model=Optional[dict])
async def get_tools_user_valves_by_id(id: str, user=Depends(get_verified_user)):
    tool = Tools.get_tool_by_id(id)
    if not tool:
        raise HTTPException(status_code=404, detail=ERROR_MESSAGES.NOT_FOUND)
    try:
        return Tools.get_user_valves_by_id_and_user_id(id, user.id)
    except Exception as e:
        raise HTTPException(status_code=400,detail=ERROR_MESSAGES.DEFAULT(str(e)))


@router.get("/id/{id}/valves/user/spec", response_model=Optional[dict])
async def get_tools_user_valves_spec_by_id(
    request: Request, id: str, user=Depends(get_verified_user)
):
    tool = Tools.get_tool_by_id(id)
    if not tool:
        raise HTTPException(status_code=404, detail=ERROR_MESSAGES.NOT_FOUND)

    if id in request.app.state.TOOLS:
        tools_module = request.app.state.TOOLS[id]
    else:
        tools_module, _ = load_tool_module_by_id(id)
        request.app.state.TOOLS[id] = tools_module

    if hasattr(tools_module, "UserValves"):
        return tools_module.UserValves.model_json_schema()
    return None


@router.post("/id/{id}/valves/user/update", response_model=Optional[dict])
async def update_tools_user_valves_by_id(
    request: Request, id: str, form_data: dict, user=Depends(get_verified_user)
):
    tool = Tools.get_tool_by_id(id)
    if not tool:
        raise HTTPException(status_code=404, detail=ERROR_MESSAGES.NOT_FOUND)

    if id in request.app.state.TOOLS:
        tools_module = request.app.state.TOOLS[id]
    else:
        tools_module, _ = load_tool_module_by_id(id)
        request.app.state.TOOLS[id] = tools_module

    if hasattr(tools_module, "UserValves"):
        UserValves = tools_module.UserValves
        try:
            user_valves = UserValves(**{k: v for k, v in form_data.items() if v is not None})
            Tools.update_user_valves_by_id_and_user_id(
                id, user.id, user_valves.model_dump()
            )
            return user_valves.model_dump()
        except Exception as e:
            log.exception(f"Failed to update user valves by id {id}: {e}")
            raise HTTPException(status_code=400, detail=ERROR_MESSAGES.DEFAULT(str(e)))
    else:
        raise HTTPException(status_code=400, detail="Tool does not support user valves.")
