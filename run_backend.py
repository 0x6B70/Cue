"""Frozen-app entry point.

PyInstaller runs its entry script as ``__main__``, which strips the package
context and breaks the relative imports inside ``backend``. Pointing the spec
at this module instead keeps ``backend`` a real package.
"""

from backend.main import run

if __name__ == "__main__":
    run()
