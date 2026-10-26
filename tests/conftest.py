"""Shared test setup."""

import asyncio
from collections.abc import Callable, Mapping

import pytest


def pytest_asyncio_loop_factories(
    config: pytest.Config, item: pytest.Item
) -> Mapping[str, Callable[[], asyncio.AbstractEventLoop]]:
    """Run async tests on a selector event loop.

    psycopg's async mode can't use Windows' default ProactorEventLoop. The selector loop
    is already the default on Linux, so this only changes Windows.
    """
    return {"selector": asyncio.SelectorEventLoop}
