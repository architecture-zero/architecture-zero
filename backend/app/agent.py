import os
import subprocess
import glob as glob_module
from pathlib import Path

ENABLE_AGENT_TOOLS = os.getenv("ENABLE_AGENT_TOOLS", "false").lower() == "true"
ENABLE_READ_TOOLS  = os.getenv("ENABLE_READ_TOOLS",  "true").lower()  == "true"
ENABLE_WRITE_TOOLS = os.getenv("ENABLE_WRITE_TOOLS", "false").lower() == "true"
ENABLE_SHELL_TOOLS = os.getenv("ENABLE_SHELL_TOOLS", "false").lower() == "true"
AGENT_WORKSPACE    = Path(os.getenv("AGENT_WORKSPACE", "/app/repo")).resolve()


def _inside_workspace(resolved: Path) -> bool:
    """The one containment rule for every file tool: a RESOLVED path is inside
    AGENT_WORKSPACE or it is the workspace itself. is_relative_to compares
    path components, so a sibling that merely shares the prefix
    (/app/repo-evil beside /app/repo) is outside."""
    return resolved.is_relative_to(AGENT_WORKSPACE)


def _safe_path(path: str) -> Path:
    """Resolve path and ensure it stays within AGENT_WORKSPACE."""
    resolved = (AGENT_WORKSPACE / path).resolve()
    if not _inside_workspace(resolved):
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
from app.permissions import OWNER_LEVEL


def _may_read(resolved: Path, user_level: int | None) -> bool:
    """True if a caller at `user_level` may read `resolved`. None == Owner."""
    if user_level is None:
        return True
    rel = resolved.relative_to(AGENT_WORKSPACE).as_posix()
    return department_min_level(dept_for_source(rel)) <= user_level


# -- What read_file returns (2026-10-29) --------------------------------------
# read_file returned p.read_text(errors="replace") for every path: a PDF or
# docx came back as its bytes decoded ("%PDF-1.4 ... endobj", a docx's zip),
# an image the same, and any file came back whole - nothing between the tool
# and the model's context capped it (the chat loop and the eval runner append
# the result as it is). Now a document type the extractor handles is returned
# as its extracted text, any other file with a NUL byte in its first 8000
# bytes (git's own binary test) is refused unread, and what is returned is
# cut at READ_FILE_MAX_CHARS with a marker saying so. Every other text file
# reads exactly as before, through the same decode.
from app.logger import log_error
from app.runtime_config import MAX_CONTEXT_TOKENS
from app.text_extract import SUPPORTED_EXTS, TEXT_EXTS, extract_text

_DOCUMENT_EXTS = tuple(e for e in SUPPORTED_EXTS if e not in TEXT_EXTS)
_BINARY_SNIFF_BYTES = 8000
# The bound is the chat's history budget, MAX_CONTEXT_TOKENS, at the chat's
# own estimate of four characters a token (routers/chat.py _estimate_tokens):
# one file read never carries more text than the whole conversation the chat
# budgets for a turn - 24000 characters at the default 6000.
READ_FILE_MAX_CHARS = MAX_CONTEXT_TOKENS * 4


def _read_file_text(p: Path, shown: str) -> str:
    """read_file's answer for an existing path the caller may read. `shown`
    is the path as the caller gave it, the only name a reply carries."""
    ext = p.name.rsplit(".", 1)[-1].lower() if "." in p.name else ""
    if ext in _DOCUMENT_EXTS:
        try:
            text = extract_text(p.name, p.read_bytes())
        except Exception as e:
            # A broken or textless document: a fixed sentence to the model
            # and the stream, the library's words to the log only, bounded.
            log_error("agent_read_file_extract_failed", ext=ext,
                      error=f"{type(e).__name__}: {str(e)[:200]}")
            return f"Error: no text could be extracted from '{shown}'."
    else:
        with p.open("rb") as f:
            if b"\x00" in f.read(_BINARY_SNIFF_BYTES):
                return (f"Error: '{shown}' is a binary file; read_file returns "
                        f"text files and documents "
                        f"({', '.join('.' + e for e in sorted(_DOCUMENT_EXTS))}) only.")
        # read_text's own open and decode, bounded: no more than the cap and
        # one character is ever read in.
        with p.open("r", encoding="utf-8", errors="replace") as f:
            text = f.read(READ_FILE_MAX_CHARS + 1)
    if len(text) > READ_FILE_MAX_CHARS:
        return (text[:READ_FILE_MAX_CHARS]
                + f"\n\n[read_file: cut at {READ_FILE_MAX_CHARS} characters - "
                  f"the file is longer and the rest was not returned]")
    return text


# -- One budget for a turn's tool results (2026-10-29) ------------------------
# READ_FILE_MAX_CHARS bounds ONE result, but the chat runs up to five tool
# rounds (the eval runner three), each with any number of calls, and appended
# every result whole: five capped reads are about 30000 tokens. The smallest
# window any lane runs with is the local Ollama lane's num_ctx
# (providers.OLLAMA_NUM_CTX, 32768 tokens by default; the cloud lanes' windows
# are several times that), and Ollama cuts an over-long prompt from the FRONT
# - the system prompt and its safety rules go first. The arithmetic, in tokens
# at the chat's own four characters a token:
#     32768  the smallest window (OLLAMA_NUM_CTX)
#   -  4096  the reply (a signed-in turn's max_tokens; it shares the window)
#   -  6000  the history budget (MAX_CONTEXT_TOKENS)
#   -  3000  the system prompt (the code-held rules and the default persona
#            are about 6000 characters, 1500 tokens; the rest is room for an
#            edited persona or the identity card)
#   -  1700  the retrieved CONTEXT block (RERANK_TOP_K 5 chunks of about
#            1200 characters, with their labels)
#   -  5000  the peer block (5 pieces at the 4000-character piece bound)
#   -   500  the tool definitions
#   = 12472 left. The budget is 12000 tokens, 48000 characters: two full
#   reads. One budget for every lane - the cloud lanes need a bound too
#   (each round resends every result), and this one fits the smallest.
TOOL_RESULTS_TURN_MAX_CHARS = 48000
TOOL_BUDGET_SPENT = ("[tool results: this turn's tool budget is used - this "
                     "result was not returned. Answer from the results above.]")


class TurnToolBudget:
    """What one turn's tool results may add to the prompt, together. Every
    loop that appends a tool result to the messages it sends takes it through
    fit(): the chat route's rounds and the eval runner's. A result that fits
    is returned as it is; one that would take the turn past the budget is cut
    to what is left, with a marker; once the budget is spent a further result
    is the fixed TOOL_BUDGET_SPENT sentence. The cut result, marker included,
    stays inside the budget."""

    def __init__(self, total: int | None = None):
        self.total = TOOL_RESULTS_TURN_MAX_CHARS if total is None else total
        self.left = self.total

    def fit(self, result: str) -> str:
        if len(result) <= self.left:
            self.left -= len(result)
            return result
        marker = (f"\n\n[tool results: cut at this turn's tool budget of "
                  f"{self.total} characters - the rest of this result was not "
                  f"returned]")
        keep = self.left - len(marker)
        self.left = 0
        if keep <= 0:
            return TOOL_BUDGET_SPENT
        return result[:keep] + marker


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
    # THE FLAG GATE: the ENABLE_* flags gate EXECUTION, not just what the
    # model is offered. Dispatch used to key on the tool name alone, so a
    # known name arriving by any path other than the offered list ran
    # whatever the flags said - run_command included, with shell tools off.
    # The active set comes from the SAME source the offer does
    # (get_active_tools), so the two cannot disagree; test_agent_tool_gating.py
    # pins its contents per flag combination. Both dispatch call sites (chat
    # and the eval runner) flow through here.
    active = {t.get("function", {}).get("name") for t in get_active_tools()}
    if name not in active:
        return (f"Tool '{name}' is not enabled on this instance - the agent "
                f"tool flags gate execution, not just advertisement.")
    try:
        if name == "read_file":
            p = _safe_path(args["path"])
            if not _may_read(p, user_level):
                return f"Permission denied: '{args['path']}' is above your access level."
            if not p.exists():
                return f"Error: file not found: {args['path']}"
            return _read_file_text(p, args["path"])

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
            # A match counts only when its RESOLVED path is inside the
            # workspace - _safe_path's rule. relative_to alone is lexical, so
            # a "../" pattern passed it and listed outside names as ordinary
            # hits, and an absolute pattern raised an error that named the
            # outside path. Then drop matches above the caller's level - a
            # search must not surface a higher-tier path (same isolation as
            # read_file, applied to the hit list).
            rel = []
            for m in matches:
                resolved = Path(m).resolve()
                if _inside_workspace(resolved) and _may_read(resolved, user_level):
                    rel.append(str(resolved.relative_to(AGENT_WORKSPACE)))
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
            # Same clearance rule as write_file: creating a directory in a
            # department above the caller's level is a write into space the
            # caller cannot even read.
            if not _may_read(p, user_level):
                return f"Permission denied: '{args['path']}' is above your access level."
            p.mkdir(parents=True, exist_ok=True)
            return f"Created: {args['path']}"

        elif name == "run_command":
            # A shell command can touch ANY workspace path, so per-path
            # clearance cannot bound it - the only honest tier rule is
            # Owner-only. None is the trusted internal caller (offline
            # scripts, an eval question with no as_level), the same
            # convention as _may_read; chat always passes the caller's rung.
            if user_level is not None and user_level < OWNER_LEVEL:
                return "Permission denied: shell commands are Owner-only."
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
