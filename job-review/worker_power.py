"""Keep macOS awake only for the lifetime of an active browser worker."""
from contextlib import contextmanager
import logging
import subprocess
import sys


LOGGER = logging.getLogger(__name__)


@contextmanager
def prevent_idle_sleep(pid):
    """Do not wake the display or change persistent system power settings."""
    guard = None
    if sys.platform == 'darwin':
        try:
            guard = subprocess.Popen(['/usr/bin/caffeinate', '-i', '-w', str(pid)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as error:
            LOGGER.warning('Browser worker sleep prevention unavailable (error=%s).', type(error).__name__)
    try:
        yield
    finally:
        if guard is not None:
            if guard.poll() is None:
                guard.terminate()
            try:
                guard.wait(timeout=2)
            except subprocess.TimeoutExpired:
                guard.kill()
                guard.wait(timeout=2)
