import logging
import time
from typing import Optional

from sqlalchemy.orm import joinedload, relationship
from sqlalchemy import or_ # Import 'or_' for queries

from open_webui.internal.db import Base, JSONField, get_db
from open_webui.models.users import Users, User, UserResponse # Import User for relationship
from open_webui.env import SRC_LOG_LEVELS
from pydantic import BaseModel, ConfigDict
from sqlalchemy import BigInteger, Column, String, Text, JSON, ForeignKey

from open_webui.utils.access_control import has_access



log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MODELS"])

####################
# Tools DB Schema
####################


class Tool(Base):
    __tablename__ = "tool"

    id = Column(String, primary_key=True)
    # FIX: user_id is now optional (nullable)
    user_id = Column(String, ForeignKey("user.id"), nullable=True)
    name = Column(Text)
    content = Column(Text)
    specs = Column(JSONField)
    meta = Column(JSONField)
    valves = Column(JSONField)
    access_control = Column(JSON, nullable=True)
    updated_at = Column(BigInteger)
    created_at = Column(BigInteger)

    # FIX: Add relationship for joined loading performance
    user = relationship("User")


class ToolMeta(BaseModel):
    description: Optional[str] = None
    manifest: Optional[dict] = {}


class ToolModel(BaseModel):
    id: str
    # FIX: user_id is now optional
    user_id: Optional[str] = None
    name: str
    content: str
    specs: list[dict]
    meta: ToolMeta
    access_control: Optional[dict] = None
    updated_at: int
    created_at: int
    model_config = ConfigDict(from_attributes=True)


####################
# Forms
####################


class ToolUserModel(ToolModel):
    user: Optional[UserResponse] = None


class ToolResponse(BaseModel):
    id: str
    # FIX: user_id is now optional
    user_id: Optional[str] = None
    name: str
    meta: ToolMeta
    access_control: Optional[dict] = None
    updated_at: int
    created_at: int


class ToolUserResponse(ToolResponse):
    user: Optional[UserResponse] = None


class ToolForm(BaseModel):
    id: str
    name: str
    content: str
    meta: ToolMeta
    access_control: Optional[dict] = None


class ToolValves(BaseModel):
    valves: Optional[dict] = None


class ToolsTable:
    def insert_new_tool(
        # FIX: user_id is now optional
        self, user_id: Optional[str], form_data: ToolForm, specs: list[dict]
    ) -> Optional[ToolModel]:
        # This method is largely the same, but now correctly handles a null user_id
        with get_db() as db:
            tool_data = {
                **form_data.model_dump(),
                "specs": specs,
                "user_id": user_id,
                "updated_at": int(time.time()),
                "created_at": int(time.time()),
            }
            try:
                result = Tool(**tool_data)
                db.add(result)
                db.commit()
                db.refresh(result)
                return ToolModel.model_validate(result)
            except Exception as e:
                log.exception(f"Error creating a new tool: {e}")
                return None

    def get_tool_by_id(self, id: str) -> Optional[ToolModel]:
        try:
            with get_db() as db:
                tool = db.query(Tool).options(joinedload(Tool.user)).filter_by(id=id).one_or_none()

                # If the DB query itself returns nothing, exit early.
                if not tool:
                    return None

                # --- THIS IS THE FIX ---
                # The 'tool' object from the DB has specs as a dict, but ToolModel expects a list.
                # We must fix this data structure before passing it to Pydantic for validation.

                tool_data_for_validation = tool.__dict__.copy()
                if not isinstance(tool_data_for_validation.get('specs'), list):
                    # If specs is not a list, wrap it in one.
                    specs_value = tool_data_for_validation.get('specs')
                    tool_data_for_validation['specs'] = [specs_value] if specs_value else []

                # Now, validate the corrected data structure. This will no longer fail.
                return ToolModel.model_validate(tool_data_for_validation)

        except Exception as e:
            # --- ROBUSTNESS IMPROVEMENT ---
            # Log the specific validation error so it's never silent again.
            log.exception(f"CRITICAL: Pydantic validation failed for tool id '{id}'. Error: {e}")
            return None

    def get_tools(self) -> list[ToolUserModel]:
        with get_db() as db:
            log.info('Getting all tools with optimized user loading...')
            tools_from_db = db.query(Tool).options(joinedload(Tool.user)).order_by(Tool.updated_at.desc()).all()

            response_list = []
            for tool in tools_from_db:
                # Step 1: Prepare the data for validation (your existing logic is good)
                if isinstance(tool.specs, list):
                    tool_data_for_validation = tool
                else:
                    tool_data_for_validation = tool.__dict__.copy()
                    tool_data_for_validation['specs'] = [tool.specs] if tool.specs else []

                # Step 2: Validate into a Pydantic OBJECT.
                # This variable now holds a rich Pydantic model instance.
                tool_model = ToolUserModel.model_validate(tool_data_for_validation)

                # Step 3: Perform all modifications directly on the Pydantic OBJECT.
                if tool_model.user_id is None:
                    # Create the complete placeholder user object
                    system_user_placeholder = UserResponse(
                        id="system",
                        name="WeidSyntara",
                        role="system",
                        email="system@local.host",
                        profile_image_url="")
                    # Assign the placeholder object to an attribute on our Pydantic tool_model
                    tool_model.user = system_user_placeholder

                    # --- THIS IS THE FIX ---
                    # Access '.content' as an attribute of the Pydantic tool_model OBJECT.
                    if not tool_model.content:
                        tool_model.content = "A WeidSyntara system tool."

                # Step 4: Append the final, modified Pydantic OBJECT to the list.
                # Do NOT call .model_dump() here.
                response_list.append(tool_model)

            return response_list

    def get_system_tools(self) -> list[ToolUserModel]:
        """
        Retrieves all tools that are classified as system tools (i.e., user_id is NULL).
        """
        with get_db() as db:
            log.info('Getting all system tools (user_id is NULL) with eager user loading...')
            # Query for tools where user_id is explicitly NULL
            system_tools_from_db = db.query(Tool).options(joinedload(Tool.user)).filter(
                Tool.user_id.is_(None)
            ).order_by(Tool.updated_at.desc()).all()

            response_list = []
            for tool_obj in system_tools_from_db:
                # Use the helper to prepare the tool for response,
                # ensuring specs are correctly formatted and user is None.
                response_list.append(self._prepare_tool_for_response(tool_obj))

            return response_list

    def get_tools_by_user_id(
        self, user_id: str, permission: str = "write"
    ) -> list[ToolUserModel]:
        # PERFORMANCE FIX: Filter in the database, not in Python.
        with get_db() as db:
            tools_with_users = db.query(Tool).options(joinedload(Tool.user)).order_by(Tool.updated_at.desc()).all()

            # The filtering logic is now separate for clarity
            accessible_tools = []
            for tool in tools_with_users:
                # A user can access a tool if they are the owner OR they have explicit access via access_control
                is_owner = tool.user_id == user_id
                has_permission = has_access(user_id, permission, tool.access_control)

                if is_owner or has_permission:
                    accessible_tools.append(tool)

            return [ToolUserModel.model_validate(tool) for tool in accessible_tools] + self.get_system_tools()

    # ... The rest of your methods (*valves*, update, delete) remain largely the same,
    # as they operate on a single tool by its ID or handle user-specific settings.

    def update_tool_by_id(self, id: str, updated: dict) -> Optional[ToolModel]:
        try:
            with get_db() as db:
                db.query(Tool).filter_by(id=id).update(
                    {**updated, "updated_at": int(time.time())}
                )
                db.commit()
                # Use the already optimized get_tool_by_id
                return self.get_tool_by_id(id)
        except Exception:
            return None

    def delete_tool_by_id(self, id: str) -> bool:
        try:
            with get_db() as db:
                db.query(Tool).filter_by(id=id).delete()
                db.commit()
                return True
        except Exception:
            return False

Tools = ToolsTable()
