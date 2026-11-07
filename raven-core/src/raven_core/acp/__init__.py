"""ACP (Agent Client Protocol) stdio agent so t3code can drive Raven.

t3code: Settings → Providers → ACP → Local command
    command: uv
    args:    run --project /Users/sinan/Documents/hackathon_h/raven-core raven-core acp
"""

from raven_core.acp.agent import RavenAcpAgent, serve_stdio

__all__ = ["RavenAcpAgent", "serve_stdio"]
