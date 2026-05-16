"""Process entry point for the provider simulator.

server.py is a library module — importing it must not have side effects.
Running it directly as `python server.py` would load it as the `__main__`
module; any later `from server import …` (e.g. handlers_ws importing the
WS subscription registry) would then load server.py a SECOND time as a
distinct `server` module, duplicating every module-level symbol including
`_WS_SUBSCRIPTIONS`. The handler would register subscriptions into one
copy while the control API looked them up in the other.

Booting via `run.py` sidesteps the problem: server.py only ever loads
once, as `server`, regardless of which module is `__main__`.
"""

from server import main

if __name__ == "__main__":
    main()
