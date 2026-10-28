"""Let CLI cleanup/export run when a terminal closes or sends SIGTERM."""
import signal


def install_shutdown_handlers():
    def interrupt(signum, frame):
        raise KeyboardInterrupt
    for name in ("SIGTERM", "SIGHUP"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), interrupt)
