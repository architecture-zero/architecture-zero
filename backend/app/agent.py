import os
import subprocess
import glob as glob_module
from pathlib import Path

ENABLE_AGENT_TOOLS = os.getenv("ENABLE_AGENT_TOOLS", "false").lower() == "true"
ENABLE_READ_TOOLS  = os.getenv("ENABLE_READ_TOOLS",  "true").lower()  == "true"
ENABLE_WRITE_TOOLS = os.getenv("ENABLE_WRITE_TOOLS", "false").lower() == "true"
ENABLE_SHELL_TOOLS = os.getenv("ENABLE_SHELL_TOOLS", "false").lower() == "true"
AGENT_WORKSPACE    = Path(os.getenv("AGENT_WORKSPACE", "/app/repo")).resolve()


def _safe_path(path: str) -> Path:
    """Resolve path and ensure it stays within AGENT_WORKSPACE."""
    resolved = (AGENT_WORKSPACE / path).resolve()
    # Must check with trailing separator - a bare startswith allows
    # sibling paths like /app/repo-evil to pass /app/repo.
    workspace_str = str(AGENT_WORKSPACE)
    if resolved != AGENT_WORKSPACE and not str(resolved).startswith(workspace_str + os.sep):
        raise PermissionError(f"Path '{path}' escapes the agent workspace")
    return resolved


# -- Access-tier gate for the file tools --------------------------------------
# The file tools are a SECOND retrieval surface: read_file could hand a lower
# tier the Owner-only session log straight past retrieve()'s department gate.
# So the tools apply the SAME department clearance retrieval does - a file's
# department (rag_config.dept_for_source) must be readable at the caller's
# level (rag_config.department_min_level). user_level is the caller's
# clearance rung; None == Owner/full access, so trusted internal callers
# (offline scripts) are unchanged, exactly like retrieve().
from app.rag_config import dept_for_source, department_min_level


def _may_read(resolved: Path, user_level: int | None) -> bool:
    """True if a caller at `user_level` may read `resolved`. None == Owner."""
    if user_level is None:
        return True
    rel = resolved.relative_to(AGENT_WORKSPACE).as_posix()
    return department_min_level(dept_for_source(rel)) <= user_level


# -- Tool definitions (OpenAI function format) --------------------------------

READ_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file within the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path to the file"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "List files and directories at the given path within the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path to list (default: '.')"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Search for files matching a glob pattern within the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern, e.g. '**/*.py'"}
                },
                "required": ["pattern"]
            }
        }
    },
]

WRITE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file within the workspace. Creates the file if it doesn't exist.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path to the file"},
                    "content": {"type": "string", "description": "Content to write"}
                },
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_directory",
            "description": "Create a directory within the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path for the new directory"}
                },
                "required": ["path"]
            }
        }
    },
]

SHELL_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a shell command within the workspace directory. Use with caution.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to execute"},
                    "timeout": {"type": "integer", "description": "Timeout in seconds (default 30)"}
                },
                "required": ["command"]
            }
        }
    },
]


def get_active_tools() -> list[dict]:
    """Tools offered to the model this turn. Everything here is gated behind
    ENABLE_AGENT_TOOLS (off by default, off in prod unless deliberately on):
    the read/write/shell tools are an indirect-prompt-injection surface, so
    they are opt-in per instance, least-privilege per class."""
    tools: list[dict] = []
    if not ENABLE_AGENT_TOOLS:
        return tools
    if ENABLE_READ_TOOLS:
        tools.extend(READ_TOOLS)
    if ENABLE_WRITE_TOOLS:
        tools.extend(WRITE_TOOLS)
    if ENABLE_SHELL_TOOLS:
        tools.extend(SHELL_TOOLS)
    return tools


def get_tool_config() -> dict:
    return {
        "agent_enabled": ENABLE_AGENT_TOOLS,
        "read_tools": ENABLE_READ_TOOLS and ENABLE_AGENT_TOOLS,
        "write_tools": ENABLE_WRITE_TOOLS and ENABLE_AGENT_TOOLS,
        "shell_tools": ENABLE_SHELL_TOOLS and ENABLE_AGENT_TOOLS,
        "workspace": str(AGENT_WORKSPACE),
    }


# -- Tool executors -----------------------------------------------------------

def execute_tool(name: str, args: dict, user_level: int | None = None) -> str:
    try:
        if name == "read_file":
            p = _safe_path(args["path"])
            if not _may_read(p, user_level):
                return f"Permission denied: '{args['path']}' is above your access level."
            if not p.exists():
                return f"Error: file not found: {args['path']}"
            return p.read_text(encoding="utf-8", errors="replace")

        elif name == "list_directory":
            p = _safe_path(args.get("path", "."))
            if not p.exists():
                return f"Error: path not found: {args.get('path', '.')}"
            # Hide entries the caller can't read, so a lower tier can't even
            # learn the Owner-only file's NAME by listing its directory.
            entries = sorted(
                (e for e in p.iterdir() if _may_read(e, user_level)),
                key=lambda x: (x.is_file(), x.name),
            )
            return "\n".join(
                ("[dir]  " if e.is_dir() else "[file] ") + e.name for e in entries
            )

        elif name == "search_files":
            pattern = args["pattern"]
            matches = glob_module.glob(str(AGENT_WORKSPACE / pattern), recursive=True)
            # Drop matches above the caller's level - a search must not
            # surface a higher-tier path (same isolation as read_file,
            # applied to the hit list).
            rel = [str(Path(m).relative_to(AGENT_WORKSPACE))
                   for m in matches if _may_read(Path(m).resolve(), user_level)]
            return "\n".join(rel) if rel else "No files found matching pattern"

        elif name == "write_file":
            p = _safe_path(args["path"])
            if not _may_read(p, user_level):
                return f"Permission denied: '{args['path']}' is above your access level."
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(args["content"], encoding="utf-8")
            return f"Written: {args['path']}"

        elif name == "create_directory":
            p = _safe_path(args["path"])
            p.mkdir(parents=True, exist_ok=True)
            return f"Created: {args['path']}"

        elif name == "run_command":
            timeout = int(args.get("timeout", 30))
            # shell=True is intentional: this IS the agent's shell tool (needs
            # pipes, globs, redirection). The risk (command injection via
            # indirect prompt injection) is managed OUT-OF-BAND, not by
            # dropping shell: the whole tool class is gated behind
            # ENABLE_AGENT_TOOLS (off by default) and belongs behind an
            # allowlist + per-item confirmation before any production use.
            result = subprocess.run(
                args["command"],
                shell=True,  # nosec B602 - see comment above; gated, off-by-default tool
                cwd=str(AGENT_WORKSPACE),
                capture_output=True,
                text=True,
                timeout=timeout
            )
            out = result.stdout.strip()
            err = result.stderr.strip()
            parts = []
            if out:
                parts.append(out)
            if err:
                parts.append(f"stderr: {err}")
            if result.returncode != 0:
                parts.append(f"exit code: {result.returncode}")
            return "\n".join(parts) if parts else "(no output)"

        else:
            return f"Error: unknown tool '{name}'"

    except PermissionError as e:
        return f"Permission denied: {e}"
    except Exception as e:
        return f"Error executing {name}: {e}"
