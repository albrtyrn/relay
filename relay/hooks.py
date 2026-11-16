"""Named pause and crash points for tests and the demo (DESIGN.md section 7.5).

The worker calls `hooks.fire("<name>")` at a few fixed lines. Normally that does nothing.
A test can make a specific line pause or kill the process, which is how races and crashes
are forced at an exact moment instead of hoped for with sleeps.

Two ways to arm a hook:
- In-process (thread tests): `hooks.register(name, fn)`; `fn` runs when the line is reached.
- By environment variable (a separate worker process): `RELAY_HOOK=<name>:crash` makes the
  process die on the spot; `RELAY_HOOK=<name>:pause` makes it block forever so something
  else can kill it.
"""

import os
import threading
from collections.abc import Callable

# Every point the code fires. M4 adds the execute hooks, M5 the idempotency hook.
HOOK_NAMES = frozenset({
    "claim.after_commit",   # the claim has committed; no work has started
    "draft.before_save",    # the LLM returned and the policy passed; nothing is saved yet
})

CRASH_EXIT_CODE = 137  # the code a process killed with `kill -9` reports (128 + signal 9)

_registered: dict[str, Callable[[], None]] = {}


def register(name: str, fn: Callable[[], None]) -> None:
    """Run `fn` whenever the hook `name` fires in this process. One function per name."""
    if name not in HOOK_NAMES:
        raise ValueError(f"unknown hook {name!r}")
    _registered[name] = fn


def clear() -> None:
    """Remove every registered function. A test fixture calls this after each test."""
    _registered.clear()


def fire(name: str) -> None:
    """Called by the worker at a named point. Does nothing unless a hook is armed."""
    if name not in HOOK_NAMES:
        raise ValueError(f"unknown hook {name!r}")

    fn = _registered.get(name)
    if fn is not None:
        fn()

    armed = os.environ.get("RELAY_HOOK", "")
    if armed == f"{name}:crash":
        # os._exit ends the process immediately: no `finally` blocks, no connection close,
        # no flushing. That is what a real crash looks like to Postgres and to other workers.
        os._exit(CRASH_EXIT_CODE)
    if armed == f"{name}:pause":
        print(f"PAUSED at {name}", flush=True)
        threading.Event().wait()  # never set: blocks until the process is killed
