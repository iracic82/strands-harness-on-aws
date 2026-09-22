# AgentCore Runtime is arm64-only (P16). The CDK builds this on a native ARM64 CodeBuild host
# (LinuxArmBuildImage.AMAZON_LINUX_2023_STANDARD_3_0), so the plain `docker build` it runs there is
# already linux/arm64. Locally, build with `docker buildx build --platform linux/arm64`.
# Digest-pinned so the local build and the CodeBuild build agree byte-for-byte.
FROM public.ecr.aws/docker/library/python:3.12-slim-trixie@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9

RUN pip install --no-cache-dir uv

WORKDIR /app

ENV UV_SYSTEM_PYTHON=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_NO_PROGRESS=1 \
    PYTHONUNBUFFERED=1 \
    DOCKER_CONTAINER=1 \
    PATH="/app/.venv/bin:$PATH"

RUN useradd -m -u 1000 bedrock_agentcore

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# The whole app dir, not just the package: the wheel only packages ops_investigator/, but AGENTS.md
# and skills/ have to be on disk at runtime for the harness to load them.
COPY --chown=bedrock_agentcore:bedrock_agentcore . .
RUN uv sync --frozen --no-dev

# WORKDIR created /app as root and --chown only touches the copied entries, not the directory
# itself. The local profile serves from /app and writes session snapshots to /app/.agent, so the
# runtime user needs to own the directory or every turn dies with PermissionError.
RUN chown bedrock_agentcore:bedrock_agentcore /app

USER bedrock_agentcore

# AgentCore Runtime service contract: 8080 is HTTP mode.
EXPOSE 8080

# ADOT owns tracing (P8): opentelemetry-instrument wraps the process; OTEL_TRACES_EXPORTER must stay unset.
CMD ["opentelemetry-instrument", "python", "-m", "main"]
