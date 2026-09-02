from __future__ import annotations

from fastapi import Request

from .security import csrf_token, current_user, pop_flashes


def render(request: Request, name: str, **ctx):
    templates = request.app.state.templates
    context = {
        "request": request,
        "user": current_user(request),
        "csrf": csrf_token(request),
        "flashes": pop_flashes(request),
        "site": request.app.state.settings.server.name,
        **ctx,
    }
    return templates.TemplateResponse(request, name, context)
