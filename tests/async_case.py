import asyncio
import unittest


class AsyncTestCase(unittest.IsolatedAsyncioTestCase):
    """Isolated async test case without asyncio debug-mode overhead."""

    def _setupAsyncioRunner(self) -> None:
        assert getattr(self, "_asyncioRunner") is None, "asyncio runner is already initialized"
        setattr(self, "_asyncioRunner", asyncio.Runner(debug=False, loop_factory=self.loop_factory))
