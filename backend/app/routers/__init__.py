"""Route modules. Each owns a domain's endpoints and imports what it needs from
app.* directly - never from app.main, which would invert the dependency
direction (main -> routers) and make every patch("app.main.X") ambiguous.

Shared names live in app/runtime_config.py.
"""
