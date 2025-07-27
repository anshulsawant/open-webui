import os
import re
import subprocess
import sys
from importlib import util
import types
import tempfile
import logging

from open_webui.env import SRC_LOG_LEVELS, PIP_OPTIONS, PIP_PACKAGE_INDEX_OPTIONS
from open_webui.models.functions import Functions
from open_webui.models.tools import Tools

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MAIN"])


def extract_frontmatter(content):
    """
    Extract frontmatter as a dictionary from the provided content string.
    """
    frontmatter = {}
    if '"""' not in content.split('\n', 1)[0]:
        return {}
        
    try:
        # Use regex to find the first docstring
        match = re.search(r'"""(.*?)"""', content, re.DOTALL)
        if not match:
            return {}
        
        frontmatter_str = match.group(1)
        for line in frontmatter_str.strip().split('\n'):
            if ':' in line:
                key, value = line.split(':', 1)
                frontmatter[key.strip().lower()] = value.strip()
    except Exception as e:
        log.exception(f"Failed to extract frontmatter: {e}")
        return {}

    return frontmatter


def replace_imports(content):
    """
    Replace the import paths in the content.
    """
    replacements = {
        "from utils": "from open_webui.utils",
        "from apps": "from open_webui.apps",
        "from main": "from open_webui.main",
        "from config": "from open_webui.config",
    }
    for old, new in replacements.items():
        content = content.replace(old, new)
    return content


def load_tool_module_by_id(tool_id, content=None):
    """
    Dynamically loads a tool's Python module.
    """
    if tool_id.startswith("custom_"):
        try:
            tool_name = tool_id.replace("custom_", "", 1)
            tools_dir = os.getenv("TOOLS_DIR", "/app/weidsyntara/tools")
            file_path = os.path.join(tools_dir, f"{tool_name}.py")

            if not os.path.exists(file_path):
                raise FileNotFoundError(f"Source file for tool '{tool_id}' not found at '{file_path}'.")
            
            log.info(f"Loading WeidSyntara system tool '{tool_id}' from file: {file_path}")
            module_name = f"tool_{tool_id}"
            
            spec = util.spec_from_file_location(module_name, file_path)
            if spec is None: raise ImportError(f"Could not create module spec for {file_path}")
            
            module = util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)

            with open(file_path, 'r', encoding='utf-8') as f:
                file_content = f.read()
            frontmatter = extract_frontmatter(file_content)
            
            if hasattr(module, "Tools"):
                return module.Tools(), frontmatter
            else:
                raise AttributeError("No Tools class found in module from file.")
        except Exception as e:
            log.error(f"Error loading WeidSyntara tool module '{tool_id}': {e}")
            if f"tool_{tool_id}" in sys.modules: del sys.modules[f"tool_{tool_id}"]
            raise e
    else:
        if content is None:
            tool = Tools.get_tool_by_id(tool_id)
            if not tool: raise Exception(f"Toolkit not found: {tool_id}")
            content = tool.content
            content = replace_imports(content)
            Tools.update_tool_by_id(tool_id, {"content": content})
        else:
            frontmatter = extract_frontmatter(content)
            install_frontmatter_requirements(frontmatter.get("requirements", ""))

        module_name = f"tool_{tool_id}"
        module = types.ModuleType(module_name)
        sys.modules[module_name] = module

        with tempfile.NamedTemporaryFile(mode="w", delete=False, encoding="utf-8") as temp_file:
            temp_file.write(content)
            temp_file_path = temp_file.name
        
        try:
            module.__dict__["__file__"] = temp_file_path
            exec(content, module.__dict__)
            frontmatter = extract_frontmatter(content)
            if hasattr(module, "Tools"):
                return module.Tools(), frontmatter
            else:
                raise Exception("No Tools class found in the module")
        except Exception as e:
            log.error(f"Error loading module: {tool_id}: {e}")
            del sys.modules[module_name]
            raise e
        finally:
            os.unlink(temp_file_path)


def load_function_module_by_id(function_id: str, content: str | None = None):
    # This function remains unchanged from the original
    if content is None:
        function = Functions.get_function_by_id(function_id)
        if not function: raise Exception(f"Function not found: {function_id}")
        content = function.content
        content = replace_imports(content)
        Functions.update_function_by_id(function_id, {"content": content})
    else:
        frontmatter = extract_frontmatter(content)
        install_frontmatter_requirements(frontmatter.get("requirements", ""))

    module_name = f"function_{function_id}"
    module = types.ModuleType(module_name)
    sys.modules[module_name] = module

    with tempfile.NamedTemporaryFile(mode="w", delete=False, encoding="utf-8") as temp_file:
        temp_file.write(content)
        temp_file_path = temp_file.name
    try:
        module.__dict__["__file__"] = temp_file_path
        exec(content, module.__dict__)
        frontmatter = extract_frontmatter(content)
        if hasattr(module, "Pipe"):
            return module.Pipe(), "pipe", frontmatter
        elif hasattr(module, "Filter"):
            return module.Filter(), "filter", frontmatter
        elif hasattr(module, "Action"):
            return module.Action(), "action", frontmatter
        else:
            raise Exception("No Function class found in the module")
    except Exception as e:
        log.error(f"Error loading module: {function_id}: {e}")
        del sys.modules[module_name]
        Functions.update_function_by_id(function_id, {"is_active": False})
        raise e
    finally:
        os.unlink(temp_file_path)


def get_function_module_from_cache(request, function_id, load_from_db=True):
    # This function remains unchanged from the original
    if load_from_db:
        function = Functions.get_function_by_id(function_id)
        if not function: raise Exception(f"Function not found: {function_id}")
        content = function.content
        new_content = replace_imports(content)
        if new_content != content:
            content = new_content
            Functions.update_function_by_id(function_id, {"content": content})
        if hasattr(request.app.state, "FUNCTION_CONTENTS") and function_id in request.app.state.FUNCTION_CONTENTS and hasattr(request.app.state, "FUNCTIONS") and function_id in request.app.state.FUNCTIONS:
            if request.app.state.FUNCTION_CONTENTS[function_id] == content:
                return request.app.state.FUNCTIONS[function_id], None, None
        function_module, function_type, frontmatter = load_function_module_by_id(function_id, content)
    else:
        if hasattr(request.app.state, "FUNCTIONS") and function_id in request.app.state.FUNCTIONS:
            return request.app.state.FUNCTIONS[function_id], None, None
        function_module, function_type, frontmatter = load_function_module_by_id(function_id)

    if not hasattr(request.app.state, "FUNCTIONS"): request.app.state.FUNCTIONS = {}
    if not hasattr(request.app.state, "FUNCTION_CONTENTS"): request.app.state.FUNCTION_CONTENTS = {}
    request.app.state.FUNCTIONS[function_id] = function_module
    request.app.state.FUNCTION_CONTENTS[function_id] = content
    return function_module, function_type, frontmatter


def install_frontmatter_requirements(requirements: str):
    # This function remains unchanged from the original
    if requirements:
        try:
            req_list = [req.strip() for req in requirements.split(",") if req.strip()]
            if not req_list: return
            log.info(f"Installing requirements: {' '.join(req_list)}")
            subprocess.check_call([sys.executable, "-m", "pip", "install"] + PIP_OPTIONS + req_list + PIP_PACKAGE_INDEX_OPTIONS)
        except Exception as e:
            log.error(f"Error installing packages: {' '.join(req_list)}")
            raise e
    else:
        log.info("No requirements found in frontmatter.")


def install_tool_and_function_dependencies():
    """
    WEIDSYNTARA FIX: This function now correctly finds and installs dependencies
    for both regular admin-created tools AND file-based system tools.
    """
    log.info("Installing dependencies for all system tools and active functions.")
    
    function_list = Functions.get_functions(active_only=True)
    tool_list = Tools.get_tools()
    all_dependencies = set()

    try:
        # Process functions (no change in logic)
        for function in function_list:
            frontmatter = extract_frontmatter(replace_imports(function.content))
            if dependencies := frontmatter.get("requirements"):
                for dep in dependencies.split(','): all_dependencies.add(dep.strip())

        # Process tools with corrected logic
        for tool in tool_list:
            frontmatter = {}
            # Check for WeidSyntara system tools (user_id is None)
            if tool.user_id is None and tool.id.startswith("custom_"):
                log.info(f'Extracting frontmatter for System Tool {tool.name}.')
                try:
                    tool_name = tool.id.replace("custom_", "", 1)
                    tools_dir = os.getenv("TOOLS_DIR", "/app/weidsyntara/tools")
                    file_path = os.path.join(tools_dir, f"{tool_name}.py")
                    with open(file_path, 'r', encoding='utf-8') as f:
                        file_content = f.read()
                    frontmatter = extract_frontmatter(file_content)
                except Exception as e:
                    log.error(f"Could not read content for system tool {tool.id} to install dependencies: {e}")
            
            # Check for regular admin tools
            elif tool.user is not None and tool.user.role == "admin":
                log.info(f'Extracting frontmatter for Admin Tool {tool.name}.')
                frontmatter = extract_frontmatter(replace_imports(tool.content))
            
            # Extract dependencies from the gathered frontmatter
            if dependencies := frontmatter.get("requirements"):
                 for dep in dependencies.split(','): all_dependencies.add(dep.strip())

        if all_dependencies:
            log.info(f'Found unique dependencies: {", ".join(all_dependencies)}')
            install_frontmatter_requirements(", ".join(all_dependencies))
        else:
            log.info("No dependencies found to install.")

    except Exception as e:
        log.error(f"Error during dependency installation: {e}")
