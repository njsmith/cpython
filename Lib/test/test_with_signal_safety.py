"""Additional signal safety tests for "with" and "async with"
"""

from test.support import cpython_only, verbose
from _testcapi import install_error_injection_hook
import asyncio
import dis
import sys
import threading
import unittest

class InjectedException(Exception):
    """Exception injected into a running frame via a trace function"""
    pass

def raise_after_offset(target_function, target_offset):
    """Sets a trace function to inject an exception into given function

    Relies on the ability to request that a trace function be called for
    every executed opcode, not just every line
    """
    target_code = target_function.__code__
    def inject_exception():
        exc = InjectedException(f"Failing after {target_offset}")
        print(f"Raising injected exception: {exc}")
        raise exc
    # This installs a trace hook that's implemented in C, and hence won't
    # trigger any of the per-bytecode processing in the eval loop
    # This means it can register the pending call that raises the exception and
    # the pending call won't be processed until after the trace hook returns
    install_error_injection_hook(target_code, target_offset, inject_exception)

# TODO: Add a test case that ensures raise_after_offset is working
# properly (otherwise there's a risk the tests will pass due to the
# exception not being injected properly)

@cpython_only
class CheckSignalSafety(unittest.TestCase):
    """Ensure with statements are signal-safe.

    Signal safety means that, regardless of when external signals (e.g.
    KeyboardInterrupt) are received:

    1. If __enter__ succeeds, __exit__ will be called
    2. If __aenter__ succeeeds, __aexit__ will be called *and*
       the resulting awaitable will be awaited

    See https://bugs.python.org/issue29988 for more details
    """

    def setUp(self):
        old_trace = sys.gettrace()
        self.addCleanup(sys.settrace, old_trace)
        sys.settrace(None)

    def assert_lock_released(self, test_lock, target_offset, traced_operation):
        just_acquired = test_lock.acquire(blocking=False)
        # Either we just acquired the lock, or the test didn't release it
        test_lock.release()
        if not just_acquired:
            msg = ("Context manager entered without exit due to "
                  f"exception injected at offset {target_offset} in:\n"
                  f"{dis.Bytecode(traced_operation).dis()}")
            self.fail(msg)

    def test_synchronous_cm(self):
        # Must use a signal-safe CM, otherwise __exit__ will start
        # but then fail to actually run as the pending call gets processed
        test_lock = threading.Lock()
        def traced_function():
            with test_lock:
                1 + 1
            return
        target_offset = -1
        max_offset = len(traced_function.__code__.co_code) - 2
        while target_offset < max_offset:
            target_offset += 1
            raise_after_offset(traced_function, target_offset)
            try:
                traced_function()
            except InjectedException:
                # key invariant: if we entered the CM, we exited it
                self.assert_lock_released(test_lock, target_offset, traced_function)
            else:
                self.fail(f"Exception wasn't raised @{target_offset}")


    def test_asynchronous_cm(self):
        class AsyncTrackingCM():
            def __init__(self):
                self.enter_without_exit = None
            async def __aenter__(self):
                self.enter_without_exit = True
            async def __aexit__(self, *args):
                self.enter_without_exit = False
        tracking_cm = AsyncTrackingCM()
        async def traced_coroutine():
            async with tracking_cm:
                1 + 1
            return
        async def cushion():
            await traced_coroutine()
            # In case the last injected call spills over, we want the
            # exception to be raised here instead of deep in the bowels of
            # asyncio (which will probably lock up or something).
            while True:
                pass
        target_offset = -1
        max_offset = len(traced_coroutine.__code__.co_code) - 2
        loop = asyncio.get_event_loop()
        while target_offset < max_offset:
            target_offset += 1
            raise_after_offset(traced_coroutine, target_offset)
            try:
                loop.run_until_complete(cushion())
            except InjectedException as exc:
                # key invariant: if we entered the CM, we exited it. Meaning:
                # either __aexit__ ran fully, or else the exception was raised
                # *inside* __aexit__.
                tb = exc.__traceback__
                while tb is not None:
                    if tb.tb_frame.f_code is AsyncTrackingCM.__aexit__.__code__:
                        # This was raised inside __aexit__
                        break
                    tb = tb.tb_next
                else:
                    # It wasn't raised inside __aexit__
                    self.assertFalse(tracking_cm.enter_without_exit)
            else:
                self.fail(f"Exception wasn't raised @{target_offset}")


if __name__ == '__main__':
    unittest.main()
