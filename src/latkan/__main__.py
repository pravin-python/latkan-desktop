"""``python -m latkan`` entry point (also the PyInstaller entry script).

The import is absolute on purpose: PyInstaller runs this file as a top-level script, where
relative imports have no parent package.
"""

from latkan.app import main

if __name__ == "__main__":
    raise SystemExit(main())
