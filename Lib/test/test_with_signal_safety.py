"""Additional signal safety tests for "with" and "async with"
"""

from test.support import cpython_only, verbose
import asyncio
import dis
import sys
import unittest

class InjectedException(Exception):
    """Exception injected into a running frame via a trace function"""
    pass

def raise_after_instruction(target_function, target_instruction):
    """Sets a trace function to inject an exception into given function

    Relies on the ability to request that a trace function be called for
    every executed opcode, not just every line
    """
    target_code = target_function.__code__
    def inject_exception(frame, event, arg):
        if frame.f_code is not target_code:
            return
        frame.f_trace_opcodes = True
        if frame.f_lasti >= target_instruction:
            if frame.f_lasti > frame.f_pendingi:
                raise InjectedException(f"Failing after {frame.f_lasti}")
        return inject_exception
    sys.settrace(inject_exception)

# TODO: Add a test case that ensures raise_after_instruction is working
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

    def assert_cm_exited(self, tracking_cm, target_instruction, traced_operation):
        if tracking_cm.enter_without_exit:
            msg = ("Context manager entered without exit due to "
            f"exception injected at offset {target_instruction} in:\n"
            f"{dis.Bytecode(traced_operation).dis()}")
            self.fail(msg)

    def test_synchronous_cm(self):
        class TrackingCM():
            def __init__(self):
                self.enter_without_exit = None
            def __enter__(self):
                self.enter_without_exit = True
            def __exit__(self, *args):
                self.enter_without_exit = False
        tracking_cm = TrackingCM()
        def traced_function():
            with tracking_cm:
                1 + 1
            return
        target_instruction = -1
        num_instructions = len(traced_function.__code__.co_code) - 2
        while target_instruction < num_instructions:
            target_instruction += 1
            raise_after_instruction(traced_function, target_instruction)
            try:
                traced_function()
            except InjectedException:
                # key invariant: if we entered the CM, we exited it
                self.assert_cm_exited(tracking_cm, target_instruction, traced_function)
            else:
                self.fail(f"Exception wasn't raised @{target_instruction}")


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
        target_instruction = -1
        num_instructions = len(traced_coroutine.__code__.co_code) - 2
        loop = asyncio.get_event_loop()
        while target_instruction < num_instructions:
            target_instruction += 1
            raise_after_instruction(traced_coroutine, target_instruction)
            try:
                loop.run_until_complete(traced_coroutine())
            except InjectedException:
                # key invariant: if we entered the CM, we exited it
                self.assert_cm_exited(tracking_cm, target_instruction, traced_coroutine)
            else:
                self.fail(f"Exception wasn't raised @{target_instruction}")


if __name__ == '__main__':
    unittest.main()
