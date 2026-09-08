"""FastAPI router modules.

Routes live here so backend/main.py stays focused on app assembly.
Each router exposes a `configure(...)` hook that main.py calls once during
startup to inject shared dependencies (auth deps, DB target, etc.) without
introducing import cycles.
"""
