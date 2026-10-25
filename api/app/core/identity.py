"""Request-local authenticated identity used by repository scoping."""

from contextvars import ContextVar

user_id_context: ContextVar[str | None] = ContextVar("assayer_user_id", default=None)
workspace_id_context: ContextVar[str | None] = ContextVar("assayer_workspace_id", default=None)
guest_id_context: ContextVar[str | None] = ContextVar("assayer_guest_id", default=None)


def current_user_id() -> str | None:
    return user_id_context.get()


def current_workspace_id() -> str | None:
    return workspace_id_context.get()


def current_guest_id() -> str | None:
    return guest_id_context.get()
