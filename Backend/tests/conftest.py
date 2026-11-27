"""Test-output policy for fixtures that may hold connection credentials."""


def pytest_configure(config):
    """Never render test-frame locals, even when a runner requests them."""
    config.option.showlocals = False
