"""Repository root pytest bootstrap.

Deliberately contains no sys.path manipulation: pytest.ini provides
``pythonpath = .`` and ``norecursedirs`` so the host suite never touches
TAPE worktree code.
"""
