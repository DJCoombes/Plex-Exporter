# Custom Plex Prometheus Exporter

## Overview

This project provides a custom Prometheus exporter for Plex Media Server, written in Python and designed to be run as a Docker container. It periodically scrapes various endpoints of the Plex API to gather statistics and status information, exposing them in a format that Prometheus can consume.

This allows you to monitor your Plex server's activity, performance, and library status alongside other services in your Grafana dashboards.

## Features & Metrics

The exporter gathers the following key metrics:

**Exporter Health:**

* `plex_exporter_scrapes_total`: Total number of scrapes performed.
* `plex_exporter_scrape_errors_total`: Total number of errors during scrapes.
* `plex_exporter_scrape_duration_seconds`: Time taken for each scrape.

**Server Status & Info:**

* `plex_server_info`: Static server details (version, platform, name, identifier).
* `plex_server_up`: Indicates if the server is reachable (1=up, 0=down).
* `plex_updater_available`: Indicates if a server update is ready (1=yes, 0=no) with version labels.
* `plex_devices_connected_count`: Number of connected client devices.
* `plex_activities_active_count`: Number of active background server tasks.

**Playback Sessions:**

* `plex_sessions_active`: Current number of active playback sessions.
* `plex_session_details`: Detailed info per session (value is 1 if active), with labels:
    * `session_key`: Unique key for the session.
    * `user`: Username of the viewer.
    * `player`: Name of the player device.
    * `product`: Product name of the client (e.g., Plex Web, Plex for Android).
    * `state`: Current playback state (`playing`, `paused`, `buffering`).
    * `type`: Media type (`movie`, `episode`, `track`).
    * `address`: IP address of the client.
    * `location`: `local` or `remote`.
    * `secure`: `yes` or `no` for secure connection.
    * `media_title`: Formatted title of the media being played.
    * `progress_percent`: Playback progress percentage.
    * `duration_ms`: Total duration of the media in milliseconds.
    * `view_offset_ms`: Current playback position in milliseconds.
* `plex_transcode_sessions_active`: Current number of active transcode sessions.
* `plex_transcode_session_details`: Detailed info per transcode session (value is 1 if active), with labels:
    * `session_key`: Unique key for the session.
    * `user`: Username of the viewer.
    * `player`: Name of the player device.
    * `product`: Product name of the client.
    * `transcode_decision`: Overall status (`Direct Play`, `Direct Stream`, `Transcode`).
    * `video_decision`: Transcode decision for video (`copy`, `transcode`).
    * `audio_decision`: Transcode decision for audio (`copy`, `transcode`).
    * `subtitle_decision`: Transcode decision for subtitles (`copy`, `transcode`, `burn`, `N/A`).
    * `speed`: Transcode speed multiplier (-1 if unavailable).
    * `progress`: Transcode progress percentage (-1 if unavailable).
    * `throttled`: `yes` or `no`.

**Library:**

* `plex_library_sections_count`: Total number of configured libraries.
* `plex_library_items_count`: Total number of items per library (with library name and type labels).

*(Note: Experimental metrics for bandwidth and resource usage are present in the code but commented out by default.)*

## Requirements

* Docker
* Access to a Plex Media Server
* Plex API Token (See: [Find Your Plex Token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/))

## Setup Instructions

1.  **Save Files:** Place the `plex_exporter.py` script and the `Dockerfile` in the same directory.
2.  **Build Docker Image:** Navigate to the directory and run:
    ```bash
    docker build -t plex-exporter:latest .
    ```
    *(Replace `plex-exporter:latest` with your desired image name/tag).*
3.  **Run Docker Container:** Execute the following command, replacing placeholders:
    ```bash
    docker run -d \
      --name plex-exporter \
      -p 9595:9595 \
      -e PLEX_URL="https://YOUR_PLEX_IP_OR_HOST:32400" \
      -e PLEX_TOKEN="YOUR_PLEX_TOKEN" \
      -e PLEX_SKIP_VERIFY="true" \ # Add this line if using self-signed certs
      -e PUID=$MY_UID \
      -e PGID=$MY_GID \
      -e LOG_LEVEL="INFO" \
      --restart unless-stopped \
      djcoombes/plex-exporter:latest
    ```

## Environment Variables

* `PLEX_URL` (**Required**): Full URL of your Plex server (e.g., `http://192.168.1.10:32400` or `https://plex.yourdomain.com:32400`).
* `PLEX_TOKEN` (**Required**): Your Plex X-Plex-Token.
* `PLEX_SKIP_VERIFY` (Optional): Set to `true` (or `1`, `yes`, `y`, `t`) to disable SSL certificate verification. **Use only if your Plex server uses HTTPS with a self-signed or otherwise untrusted certificate.** Defaults to `false`.
* `EXPORTER_PORT` (Optional): Port the exporter listens on inside the container. Defaults to `9595`.
* `SCRAPE_INTERVAL_SECONDS` (Optional): How often the exporter scrapes the Plex API internally. Defaults to `60`.
* `REQUEST_TIMEOUT_SECONDS` (Optional): Timeout for requests made to the Plex API. Defaults to `10`.
* `PUID` (Optional): UID of the user to run the application as.
* `PGID` (Optional): GID of the user to run the application as.
* `LOG_LEVEL` (Optional): Logging level for the exporter script (e.g., `DEBUG`, `INFO`, `WARNING`, `ERROR`). Defaults to `INFO`.

## Prometheus Configuration

Add the following job to your `prometheus.yml` scrape configurations:

```yaml
scrape_configs:
  - job_name: 'plex-custom'
    static_configs:
      - targets: ['YOUR_DOCKER_HOST_IP:9595'] # IP of the machine running the container
    # Optional: Increase scrape timeout if needed
    # scrape_timeout: 30s