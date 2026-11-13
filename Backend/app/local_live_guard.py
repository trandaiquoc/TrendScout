"""Loopback-only ASGI guard for a future personal live-demo composition.

This guard alone does not provide quota protection or authorize provider calls.
"""

import ipaddress
import re

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from app.api.fetches import validate_public_request_origin


class LocalLiveGuard:
    def __init__(self, app):
        self.app = app
        self.body_limiter = CollectionBodySizeLimit(app)

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
        collection_read = (
            method == "GET" and path in {"/api/v1/collections", "/api/v1/collections/inbox"}
        ) or (method == "GET" and re.fullmatch(
            r"/api/v1/collections/(?:jobs/[^/]{1,64}|[0-9a-f-]{36}/keywords)", path
        ))
        algorithm_read = method == "GET" and path in {
            "/api/v1/algorithms", "/api/v1/algorithms/results",
        }
        dataset_read = method == "GET" and path in {
            "/api/v1/datasets/latest", "/api/v1/datasets/latest/keywords",
        }
        collection_write = (
            method == "POST" and path in {
                "/api/v1/collections", "/api/v1/collections/organize", "/api/v1/collections/mode",
            }
        ) or (method == "POST" and re.fullmatch(
            r"/api/v1/collections/(?:[^/]{1,64}/merge|[0-9a-f-]{36}/keywords|suggestions/[0-9a-f-]{36}/(?:accept|reject))", path
        )) or (method == "PATCH" and re.fullmatch(
            r"/api/v1/collections/[0-9a-f-]{36}", path
        )) or (method == "DELETE" and re.fullmatch(
            r"/api/v1/collections/[0-9a-f-]{36}(?:/keywords/[a-f0-9]{64})?", path
        ))
        allowed = (
            (method == "GET" and path in {
                "/", "/dashboard", "/dashboard/", "/api/v1/capabilities", "/api/health",
                "/api/v1/algorithms", "/api/v1/algorithms/results",
            })
            or (method == "GET" and path.startswith("/dashboard/"))
            or (method == "POST" and path == "/api/v1/fetches")
            or (method == "GET" and path.startswith("/api/v1/fetches/"))
            or collection_read or collection_write or algorithm_read or dataset_read
        )
        if not allowed:
            status = 405 if path in {
                "/", "/dashboard", "/dashboard/", "/api/v1/capabilities",
                "/api/health", "/api/v1/fetches",
                "/api/v1/algorithms", "/api/v1/algorithms/results",
                "/api/v1/datasets/latest", "/api/v1/datasets/latest/keywords",
            } or path.startswith("/api/v1/fetches/") else 404
            await JSONResponse({"detail": {"code": "LOCAL_ROUTE_NOT_ALLOWED"}},
                               status_code=status)(scope, receive, send)
            return
        await self.body_limiter(scope, receive, send)


class CollectionBodySizeLimit:
    """Bound JSON request bytes before FastAPI parses user-owned Collection inputs."""

    maximum_bytes = 32 * 1024

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (scope.get("type") != "http" or scope.get("method", "").upper() not in {"POST", "PATCH"}
                or not scope.get("path", "").startswith("/api/v1/collections")):
            await self.app(scope, receive, send)
            return
        content_length = None
        for key, value in scope.get("headers", []):
            if key.lower() == b"content-length":
                try:
                    content_length = int(value)
                except ValueError:
                    content_length = self.maximum_bytes + 1
        if content_length is not None and content_length > self.maximum_bytes:
            await JSONResponse({"detail": {"code": "REQUEST_TOO_LARGE"}}, status_code=413)(scope, receive, send)
            return
        messages, size = [], 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            size += len(message.get("body", b""))
            if size > self.maximum_bytes:
                await JSONResponse({"detail": {"code": "REQUEST_TOO_LARGE"}}, status_code=413)(scope, receive, send)
                return
            messages.append(message)
            if not message.get("more_body", False):
                break
        iterator = iter(messages)

        async def replay():
            try:
                return next(iterator)
            except StopIteration:
                return await receive()

        await self.app(scope, replay, send)
