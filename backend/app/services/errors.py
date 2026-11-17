"""服务层统一业务异常：路由层据此转成对应的 HTTP 状态码。

约定里业务判断只允许留在 services；服务层遇到越权、冲突这类需要明确 HTTP 语义的
场景时抛出 ServiceError，由 main 里的异常处理器统一转换，路由函数保持薄。
"""
from __future__ import annotations


class ServiceError(Exception):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class PermissionDenied(ServiceError):
    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=403)


class ConflictState(ServiceError):
    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=409)
