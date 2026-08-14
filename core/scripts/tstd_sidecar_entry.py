"""PyInstaller entry for the bundled tstd sidecar (TD-1301).

Kept as a file because PyInstaller analyzes a script path, not a
``module:function`` spec.  Adds nothing to the installed package.
"""

from tstd.daemon import main

if __name__ == "__main__":
    main()
