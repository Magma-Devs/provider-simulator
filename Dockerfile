# Pinned by digest (python:3.12-slim as of MAG-2318) so a base-image rebuild
# upstream can't silently change what ships. Re-pin deliberately: `docker
# pull python:3.12-slim && docker inspect python:3.12-slim --format='{{.RepoDigests}}'`.
FROM python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Copy stdlib Python sources at /app root AND the vendored proto stubs at
# /app/cosmos_pb2/ (MAG-1780). cosmos_pb2/__init__.py splices its own path
# onto sys.path so generated absolute imports resolve.
COPY *.py ./
COPY cosmos_pb2/ ./cosmos_pb2/
# The redesigned core (provider identity, config, telemetry, topology,
# registry). server.py migrates onto it story by story; shipping it in the
# image from day one means a wiring story can't silently deploy a pod whose
# imports only work in a full checkout.
COPY provider_simulator/ ./provider_simulator/
# Non-root (MAG-2318): the simulator has no filesystem writes (in-memory
# state only, see CLAUDE.md) and no reason to bind ports <1024, so it never
# needs root. --system avoids UID/GID collisions with the host or other
# images. /app is owned by the new user so pip-installed console scripts
# and __pycache__ writes (if any) don't hit a permission error.
RUN groupadd --system simulator && useradd --system --gid simulator --home-dir /app simulator \
    && chown -R simulator:simulator /app
USER simulator
EXPOSE 18545 18546 18547 19000 18548 18549 18550
CMD ["python", "-u", "run.py"]
