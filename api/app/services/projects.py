"""Project context: instructions + knowledge files folded into agent prompts.

A project (ChatGPT/Claude-style) groups chats and carries two things the
agent can read: free-form instructions and small knowledge files (business
rules, metric definitions, schema docs). This module renders both into a
single bounded text block that is appended to the system prompt for the
turn's LLM calls.

The block is context, not a transcript: budgets are char-based and files
are truncated, so one large project can never crowd out the STATE JSON.
"""

from __future__ import annotations

from app.core.logging import log
from app.store import repository as repo

# text-ish files a knowledge upload may be; binaries (pdf/docx) are rejected
# with a clear message rather than a silent mojibake store.
KNOWLEDGE_EXTENSIONS = {
    ".md",
    ".markdown",
    ".txt",
    ".sql",
    ".ddl",
    ".csv",
    ".tsv",
    ".json",
    ".jsonl",
    ".ndjson",
    ".yml",
    ".yaml",
    ".toml",
    ".ini",
    ".cfg",
    ".proto",
    ".py",
    ".pyi",
}

MAX_PROJECT_FILE_BYTES = 256 * 1024
MAX_PROJECT_FILES = 40
# what reaches the prompt per file / for all files combined
FILE_PROMPT_BUDGET = 6_000
TOTAL_PROMPT_BUDGET = 30_000


def is_supported_knowledge_file(name: str) -> bool:
    lowered = name.lower()
    return any(lowered.endswith(ext) for ext in KNOWLEDGE_EXTENSIONS)


def decode_knowledge_file(data: bytes) -> str | None:
    """UTF-8 decode a knowledge file, or None when it is not plausible text."""
    if len(data) > MAX_PROJECT_FILE_BYTES:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = data.decode("latin-1")
        except Exception:  # noqa: BLE001
            return None
    # control characters beyond tab/newline/carriage return mean binary-ish
    if sum(1 for ch in text[:4096] if ord(ch) < 32 and ch not in "\t\n\r") > 32:
        return None
    return text


def project_context_for_session(session_id: str) -> str | None:
    """Render the thread's project as a prompt block, or None when unfiled."""
    try:
        project_id = repo.get_thread_project_id(session_id)
        if not project_id:
            return None
        project = repo.get_project(project_id)
        if not project:
            return None
        return render_project_context(
            project["name"],
            project.get("instructions") or "",
            repo.list_project_files(project_id, with_content=True),
        )
    except Exception as exc:  # noqa: BLE001 - context is an enhancement, never fatal
        log.warning("project context build failed", error=str(exc))
        return None


def render_project_context(
    name: str, instructions: str, files: list[dict], budget_left: int = TOTAL_PROMPT_BUDGET
) -> str | None:
    parts: list[str] = [f"# Project: {name}"]
    used = len(parts[0])

    instructions = (instructions or "").strip()
    if instructions:
        instructions = instructions[: budget_left - used]
        parts.append(f"## Instructions\n{instructions}")
        used += len(instructions)

    shown = 0
    for f in files:
        text = (f.get("text") or "").strip()
        if not text:
            continue
        if used >= budget_left:
            parts.append("## Knowledge files (truncated)")
            break
        room = min(FILE_PROMPT_BUDGET, budget_left - used)
        clipped = text[:room]
        if len(clipped) < len(text):
            clipped += "\n…[truncated]"
        parts.append(f"## Knowledge file: {f['name']}\n{clipped}")
        used += len(clipped)
        shown += 1
    if shown and shown < len(files):
        parts.append(f"({shown} of {len(files)} knowledge files fit in the context budget)")

    if len(parts) == 1:
        return None
    return "\n\n".join(parts)


__all__ = [
    "KNOWLEDGE_EXTENSIONS",
    "MAX_PROJECT_FILE_BYTES",
    "MAX_PROJECT_FILES",
    "decode_knowledge_file",
    "is_supported_knowledge_file",
    "project_context_for_session",
    "render_project_context",
]
