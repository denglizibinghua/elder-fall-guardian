"""Raspberry Pi entrypoint.

The systemd service executes this small wrapper. Hardware workers and edge
orchestration live in ``edge_runtime.py``; the bus and detector are separate
modules so a failed optional dependency cannot corrupt the web app.
"""

from edge_runtime import run


if __name__ == "__main__":
    run()
