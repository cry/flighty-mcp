FROM python:3.13-slim

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

# Install dependencies (cached layer, only reruns when pyproject.toml or uv.lock change)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Install the project
COPY src/ ./src/
RUN uv sync --frozen --no-dev

# Both tokens come from the environment here. There is no macOS install to read
# them from, and no keyring: FLIGHTY_BUILD_TOKEN replaces the plutil lookup
# against Flighty.app's Info.plist, and FLIGHTY_AUTH_TOKEN replaces the
# Account.authToken row. See config.build_token and db.get_auth_token.
#
# The database is still needed for the airline/airport reference tables and the
# account's flight list, so a snapshot is mounted read-only at /data. It has no
# usable sync cursor, which is why the write tools are off.
ENV FLIGHTY_DB_PATH=/data/MainFlightyDatabase.db
ENV FLIGHTY_READONLY=1

ENV FLIGHTY_TRANSPORT=streamable-http
ENV FLIGHTY_HOST=0.0.0.0
ENV FLIGHTY_PORT=8000
EXPOSE 8000

ENV PATH="/app/.venv/bin:$PATH"
CMD ["flighty-mcp"]
