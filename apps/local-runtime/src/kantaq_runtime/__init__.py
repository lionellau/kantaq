"""kantaq local runtime (MOD-14).

The FastAPI process each member runs on their own machine. It serves the REST
API, the built web UI, and (from Epic E09) the loopback MCP gateway. In Epic E01
it is a bootstrap shell: a health check and the static-UI mount.
"""

__version__: str = "0.0.5"
