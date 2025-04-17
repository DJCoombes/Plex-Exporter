# Stage 1: Builder - Install dependencies
FROM python:3.13-slim-bookworm AS builder

# Set working directory
WORKDIR /app

# Prevent Python from writing pyc files and buffering stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Upgrade pip
RUN pip install --no-cache-dir --upgrade pip

# Copy requirements file
COPY requirements.txt .

# Install dependencies into a specific location (/install)
# Using --no-warn-script-location because we install to a prefix
RUN pip install --no-cache-dir --no-warn-script-location --prefix=/install -r requirements.txt

# Stage 2: Runtime - Create the final minimal image
FROM python:3.13-slim-bookworm AS runtime

# Define arguments for user/group IDs and names (can be overridden at build time)
# These set the *default* UID/GID for the internal user if PUID/PGID are not provided at runtime
ARG UID=10001
ARG GID=10001
ARG USERNAME=exporter
ARG GROUPNAME=exporter

# Create a non-root group and user using the arguments
# Also install wget (for HEALTHCHECK) and gosu (for entrypoint privilege drop)
RUN apt-get update && apt-get install -y wget gosu --no-install-recommends && \
    addgroup --system --gid $GID $GROUPNAME && \
    adduser --system --uid $UID --gid $GID --no-create-home $USERNAME && \
    apt-get purge -y --auto-remove && \
    rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Prevent Python from writing pyc files and buffering stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Copy installed dependencies from the builder stage to the final image
COPY --from=builder /install /usr/local

# Copy the exporter script into the container and set ownership using arguments
COPY --chown=$USERNAME:$GROUPNAME plex_exporter.py .

# Copy the entrypoint script and make it executable
COPY --chown=root:root entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Make the exporter port available
EXPOSE 9595

# Define environment variables (defaults can be overridden at runtime)
ENV PLEX_URL=""
ENV PLEX_TOKEN=""
ENV EXPORTER_PORT="9595"
ENV SCRAPE_INTERVAL_SECONDS="60"
ENV REQUEST_TIMEOUT_SECONDS="10"
ENV LOG_LEVEL="INFO"
ENV PLEX_SKIP_VERIFY="false"

# *** USER instruction removed - entrypoint runs as root initially, then drops privileges ***
# USER $USERNAME

# Add HEALTHCHECK instruction
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD wget -q --spider http://localhost:9595/metrics || exit 1

# Set the entrypoint script to run on container start
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

# Define the default command that the entrypoint script will execute
CMD ["python", "plex_exporter.py"]