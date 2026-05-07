"""Entry point for ``python -m krewcli``.

The supervisor (``krewcli.daemon.supervisor``) spawns the detached
daemon as ``[sys.executable, "-m", "krewcli", "daemon", "start",
"--foreground", ...]``. ``-m krewcli`` requires this module to
exist; without it the child crashes immediately with
``No module named krewcli.__main__``.
"""
from __future__ import annotations

from krewcli.cli import main

if __name__ == "__main__":
    main()
