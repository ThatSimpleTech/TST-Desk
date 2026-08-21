"""Re-export screen persist so TD-1710 imports keep working.

The write lives in ``tstd.screen.frames`` so desktop (TD-3401) does not
duplicate it or import the browser package.
"""

from ..screen.frames import persist_dir_of, persist_screen_frame

__all__ = ["persist_dir_of", "persist_screen_frame"]
