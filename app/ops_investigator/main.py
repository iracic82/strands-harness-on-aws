"""AgentCore Runtime HTTP entrypoint. Keep this file tiny; everything lives in the package."""

from __future__ import annotations

from ops_investigator.http_app import app, startup

if __name__ == "__main__":
    startup()
    app.run()
