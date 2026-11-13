"""Loopback-only ASGI guard for a future personal live-demo composition.

This guard alone does not provide quota protection or authorize provider calls.
"""

import ipaddress

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from app.api.fetches import validate_public_request_origin


class LocalLiveGuard:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        try:
            peer = scope.get("client")
            if (not peer or not ipaddress.ip_address(peer[0]).is_loopback
                    or scope.get("scheme") != "http"
                    or len(request.headers.getlist("host")) != 1
                    or len(request.headers.getlist("origin")) > 1):
                raise ValueError("not a loopback request")
            validate_public_request_origin(request, "local")
        except (ValueError, HTTPException):
            await JSONResponse({"detail": {"code": "LOCAL_ONLY"}}, status_code=403)(scope, receive, send)
            return
        path = scope.get("path", "")
        method = scope.get("method", "").upper()
        allowed = (
            (method == "GET" and path in {
                "/", "/dashboard", "/dashboard/", "/api/v1/capabilities", "/api/health",
            })
            or (method == "GET" and path.startswith("/dashboard/"))
            or (method == "POST" and path == "/api/v1/fetches")
            or (method == "GET" and path.startswith("/api/v1/fetches/"))
        )
        if not allowed:
            status = 405 if path in {
                "/", "/dashboard", "/dashboard/", "/api/v1/capabilities",
                "/api/health", "/api/v1/fetches",
            } or path.startswith("/api/v1/fetches/") else 404
            await JSONResponse({"detail": {"code": "LOCAL_ROUTE_NOT_ALLOWED"}},
                               status_code=status)(scope, receive, send)
            return
        await self.app(scope, receive, send)
