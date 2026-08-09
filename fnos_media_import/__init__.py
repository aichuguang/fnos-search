"""飞牛影视统一入库维护系统。

Keep the Flask composition root lazy.  Importing a domain/transfer module for
unit tests or a worker utility must not eagerly construct the whole Web module
graph (and, in particular, must not make an unrelated optional runtime service
a prerequisite for importing the package).
"""

from __future__ import annotations

from typing import Any


def create_app(*args: Any, **kwargs: Any) -> Any:
    from .app import create_app as factory

    return factory(*args, **kwargs)


__all__ = ["create_app"]

