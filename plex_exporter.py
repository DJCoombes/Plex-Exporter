# -*- coding: utf-8 -*-
"""
Prometheus exporter for Plex Media Server.

This script periodically fetches status and statistics from a Plex Media Server API
and exposes them as Prometheus metrics on an HTTP endpoint.

Features:
- Gathers server status, version, update availability.
- Collects detailed active session information (playback, transcode).
- Reports library section counts and item counts per library.
- Monitors connected devices and background activities.
- Uses a requests.Session for connection pooling.
- Supports optional rate limiting for Plex API calls.
- Supports optional disabling of SSL certificate verification for self-signed certs.
- Runs metrics collection in a scheduled background thread.

Configuration is handled via environment variables (see README.md).
"""

import os
import time
import threading
import logging
import warnings

import requests
import schedule
from prometheus_client import start_http_server, Gauge, Counter, Summary, REGISTRY, Info

# --- Configuration ---
# Load configuration from environment variables
PLEX_URL = os.environ.get('PLEX_URL')
PLEX_TOKEN = os.environ.get('PLEX_TOKEN')
EXPORTER_PORT = int(os.environ.get('EXPORTER_PORT', 9595))
SCRAPE_INTERVAL_SECONDS = int(os.environ.get('SCRAPE_INTERVAL_SECONDS', 60))
REQUEST_TIMEOUT_SECONDS = int(os.environ.get('REQUEST_TIMEOUT_SECONDS', 10))
LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO').upper()
PLEX_SKIP_VERIFY_ENV = os.environ.get('PLEX_SKIP_VERIFY', 'false').lower()
PLEX_SKIP_VERIFY = PLEX_SKIP_VERIFY_ENV in ['true', '1', 't', 'y', 'yes']
PLEX_API_RATE_LIMIT = float(os.environ.get('PLEX_API_RATE_LIMIT', 0)) # Rate limit in requests/sec (0 = disabled)

# --- Logging Setup ---
logging.basicConfig(level=LOG_LEVEL, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Input Validation & Warnings ---
if not PLEX_URL:
    logging.error("PLEX_URL environment variable not set. Exiting.")
    exit(1)
if not PLEX_TOKEN:
    logging.error("PLEX_TOKEN environment variable not set. Exiting.")
    exit(1)

if PLEX_SKIP_VERIFY:
    logging.warning("=" * 80)
    logging.warning("SECURITY WARNING: SSL certificate verification is DISABLED!")
    logging.warning("This configuration is insecure for non-local connections.")
    logging.warning("=" * 80)
    # Suppress only the specific InsecureRequestWarning from requests library
    warnings.filterwarnings('ignore', message='Unverified HTTPS request')

# --- Rate Limiting Setup ---
if PLEX_API_RATE_LIMIT > 0:
    logging.info(f"Plex API rate limiting enabled: {PLEX_API_RATE_LIMIT} requests/second.")
    # Calculate minimum delay needed between requests to meet the rate limit
    MIN_REQUEST_INTERVAL = 1.0 / PLEX_API_RATE_LIMIT
else:
    MIN_REQUEST_INTERVAL = 0 # No rate limiting if value is 0 or less
# Global variable to track the timestamp of the last API request
last_request_time = 0

# --- Prometheus Metrics Definition ---
# Unregister default collectors (e.g., gc_collector, platform_collector)
# to avoid potential conflicts or unwanted metrics.
for coll in list(REGISTRY._collector_to_names.keys()):
    REGISTRY.unregister(coll)

# Exporter internal metrics
plex_exporter_scrapes_total = Counter('plex_exporter_scrapes_total', 'Total number of scrapes performed.')
plex_exporter_scrape_errors_total = Counter('plex_exporter_scrape_errors_total', 'Total number of scrape errors.')
plex_exporter_scrape_duration_seconds = Summary('plex_exporter_scrape_duration_seconds', 'Duration of Plex API scrapes.')

# Plex server general metrics
plex_server_info = Info('plex_server', 'Plex Media Server information (version, platform, etc.)')
plex_server_up = Gauge('plex_server_up', 'Indicates if the Plex server is reachable (1 = yes, 0 = no).')
plex_updater_available = Gauge('plex_updater_available', 'Indicates if a Plex server update is available (1 = yes, 0 = no).', ['current_version', 'available_version'])
plex_devices_connected_count = Gauge('plex_devices_connected_count', 'Number of connected client devices reported by Plex.')
plex_activities_active_count = Gauge('plex_activities_active_count', 'Number of active background activities (scanning, processing).')

# Plex session metrics
plex_sessions_active = Gauge('plex_sessions_active', 'Number of active Plex playback sessions.')
plex_session_details = Gauge('plex_session_details', 'Details about active playback sessions (value is 1)', [
    'session_key', 'user', 'player', 'product', 'state', 'type', 'address',
    'location', 'secure', 'media_title', 'progress_percent', 'duration_ms', 'view_offset_ms'
])
plex_transcode_sessions_active = Gauge('plex_transcode_sessions_active', 'Number of active transcode sessions.')
plex_transcode_session_details = Gauge('plex_transcode_session_details', 'Details about active transcode sessions (value is 1)', [
    'session_key', 'user', 'player', 'product', 'transcode_decision',
    'video_decision', 'audio_decision', 'subtitle_decision', 'speed', 'progress', 'throttled'
])

# Plex library metrics
plex_library_sections_count = Gauge('plex_library_sections_count', 'Total number of library sections.')
plex_library_items_count = Gauge('plex_library_items_count', 'Total number of items in a library section', ['section_title', 'section_type'])

# --- Global Requests Session ---
# Create a single requests.Session object to reuse connections
# and set default headers and SSL verification settings.
plex_session = requests.Session()
plex_session.headers.update({'X-Plex-Token': PLEX_TOKEN, 'Accept': 'application/json'})
plex_session.verify = not PLEX_SKIP_VERIFY # Configure SSL verification for the session

# --- Plex API Interaction (using Session) ---
def fetch_plex_api(endpoint, params=None):
    """
    Fetches data from a specific Plex API endpoint using the global session.

    Handles request timeouts, rate limiting, SSL verification override,
    and basic error handling (logging, incrementing error counter).

    Args:
        endpoint (str): The API endpoint path (e.g., '/status/sessions').
        params (dict, optional): URL parameters for the request. Defaults to None.

    Returns:
        dict: The JSON response data as a dictionary if successful.
        dict: An empty dictionary if the response was successful but empty or non-JSON.
        None: If a request exception (connection, timeout, SSL, HTTP error) occurred.
    """
    global last_request_time
    url = f"{PLEX_URL.rstrip('/')}/{endpoint.lstrip('/')}"

    # --- Rate Limiting ---
    if MIN_REQUEST_INTERVAL > 0:
        current_time = time.monotonic() # Use monotonic clock for interval measurement
        elapsed = current_time - last_request_time
        if elapsed < MIN_REQUEST_INTERVAL:
            sleep_time = MIN_REQUEST_INTERVAL - elapsed
            logging.debug(f"Rate limiting: sleeping for {sleep_time:.3f} seconds before request to {endpoint}.")
            time.sleep(sleep_time)
        # Update last request time *after* potential sleep, before making the request
        last_request_time = time.monotonic()
    # --- End Rate Limiting ---

    try:
        logging.debug(f"Fetching Plex API endpoint: {endpoint}")
        # Use the global session object for the request
        response = plex_session.get(url, timeout=REQUEST_TIMEOUT_SECONDS, params=params)
        # Raise an exception for bad status codes (4xx or 5xx)
        response.raise_for_status()

        # Handle successful responses that might be empty
        if response.status_code == 200 and not response.content:
            logging.debug(f"Received empty successful response from {url}")
            return {}

        # Check content type before attempting JSON decode
        content_type = response.headers.get('Content-Type', '')
        if 'application/json' in content_type:
            return response.json()
        else:
            # Log unexpected content types but treat as success (no data)
            logging.warning(f"Received non-JSON response (Content-Type: {content_type}) from {url}. Status: {response.status_code}. Body: {response.text[:200]}...")
            return {}

    # --- Exception Handling ---
    except requests.exceptions.JSONDecodeError as e:
        # Handle cases where the response is not valid JSON
        logging.error(f"Failed to decode JSON from {url}. Status: {response.status_code}. Error: {e}. Response text: {response.text[:200]}...")
        plex_exporter_scrape_errors_total.inc()
        return None
    except requests.exceptions.SSLError as e:
        # Handle SSL specific errors (e.g., verification failure)
        logging.error(f"SSL Error fetching {url}: {e}. If using self-signed certs, set PLEX_SKIP_VERIFY=true.")
        plex_exporter_scrape_errors_total.inc()
        return None
    except requests.exceptions.RequestException as e:
        # Handle other request errors (connection, timeout, HTTP errors)
        logging.error(f"RequestException fetching {url}: {e}")
        plex_exporter_scrape_errors_total.inc()
        return None
    except Exception as e:
        # Catch any other unexpected errors during the request
        logging.error(f"An unexpected error occurred fetching {url}: {e}")
        plex_exporter_scrape_errors_total.inc()
        return None

# --- Helper Function ---
def get_media_title(session_metadata):
    """
    Constructs a readable media title string from Plex session metadata.

    Handles different formats for movies, TV show episodes, and music tracks.

    Args:
        session_metadata (dict): The metadata dictionary for a single session
                                 from the /status/sessions endpoint.

    Returns:
        str: A formatted string representing the media title.
    """
    media_type = session_metadata.get('type', 'unknown')
    title = session_metadata.get('title', '') # Episode title, movie title, or track title

    try:
        if media_type == 'episode':
            # Format: "Show Title - SXXEXX - Episode Title"
            show_title = session_metadata.get('grandparentTitle', '') # Show title
            season_num = str(session_metadata.get('parentIndex', '?')).zfill(2) # Season number
            episode_num = str(session_metadata.get('index', '?')).zfill(2) # Episode number
            episode_title = title
            return f"{show_title} - S{season_num}E{episode_num} - {episode_title}"
        elif media_type == 'movie':
            # Format: "Movie Title (Year)"
            year = session_metadata.get('year', '')
            return f"{title} ({year})" if year else title
        elif media_type == 'track':
            # Format: "Artist - Album - Track Title"
            artist_title = session_metadata.get('grandparentTitle', '') # Artist name
            album_title = session_metadata.get('parentTitle', '') # Album title
            return f"{artist_title} - {album_title} - {title}"
        else:
            # Fallback for other types or unknown
            return title
    except Exception as e:
        logging.warning(f"Error formatting media title (type: {media_type}): {e} - Metadata: {session_metadata}")
        return title # Return basic title on error

# --- Metric Collection Sub-Functions ---
def _update_server_status():
    """
    Fetches server identity information (/identity).

    Updates plex_server_up and plex_server_info metrics.

    Returns:
        tuple: (bool, dict) indicating if the server is reachable and the server info dictionary.
    """
    identity_data = fetch_plex_api('/identity')
    server_info_dict = {}
    if identity_data is not None and 'MediaContainer' in identity_data:
        # Server is up and responded with expected container
        plex_server_up.set(1)
        logging.info("Successfully connected to Plex server and got identity.")
        mc = identity_data['MediaContainer']
        # Extract server details for the Info metric
        server_info_dict = {
            'version': mc.get('version', 'unknown'),
            'platform': mc.get('platform', 'unknown'),
            'platform_version': mc.get('platformVersion', 'unknown'),
            'friendly_name': mc.get('friendlyName', 'unknown'),
            'machine_identifier': mc.get('machineIdentifier', 'unknown')
        }
        plex_server_info.info(server_info_dict)
        logging.info(f"Plex version: {server_info_dict['version']}, Platform: {server_info_dict['platform']}")
        return True, server_info_dict # Success
    else:
        # Handle cases where the fetch failed or returned unexpected data
        if identity_data is not None: # Fetch succeeded but data was wrong format
             logging.error("Failed to get valid identity data from Plex server (unexpected format).")
        # Set server status to down
        plex_server_up.set(0)
        plex_server_info.info({}) # Clear stale info
        return False, {} # Failure

def _update_session_metrics():
    """
    Fetches active sessions (/status/sessions).

    Updates aggregate session counts (active, transcoding) and detailed metrics
    (plex_session_details, plex_transcode_session_details) for each session.
    """
    sessions_data = fetch_plex_api('/status/sessions')
    active_sessions_count = 0
    transcode_sessions_count = 0

    # Reset metrics with labels before populating to remove stale entries
    plex_session_details.clear()
    plex_transcode_session_details.clear()

    if sessions_data is not None and 'MediaContainer' in sessions_data:
        container = sessions_data['MediaContainer']
        # Get the overall count reported by Plex
        active_sessions_count = int(container.get('size', 0))

        # Iterate through individual session metadata if present
        if 'Metadata' in container:
            for session in container.get('Metadata', []):
                # --- Extract Session Details ---
                user = session.get('User', {}).get('title', 'Unknown')
                player_info = session.get('Player', {})
                player = player_info.get('title', 'Unknown')
                product = player_info.get('product', 'Unknown')
                state = player_info.get('state', 'unknown') # playing, paused, buffering
                session_key = session.get('sessionKey', 'unknown')
                session_type = session.get('type', 'unknown') # movie, episode, track
                address = player_info.get('address', 'unknown')
                location = "local" if player_info.get('local', False) else "remote"
                secure_conn = "yes" if player_info.get('secure', False) else "no"

                # --- Extract Media Details ---
                media_title = get_media_title(session)
                duration_ms = session.get('duration', 0)
                view_offset_ms = session.get('viewOffset', 0)
                progress_percent = 0
                duration_num = 0
                view_offset_num = 0

                # Calculate progress percentage safely
                try:
                    duration_num = int(duration_ms)
                    view_offset_num = int(view_offset_ms)
                    if duration_num > 0:
                        progress_percent = round((view_offset_num / duration_num) * 100, 2)
                except (ValueError, TypeError):
                    # Log warning if duration or offset are not valid numbers
                    logging.warning(f"Could not calculate progress for session {session_key}, invalid duration/offset: D={duration_ms}, O={view_offset_ms}")

                # --- Set Session Detail Metric ---
                # Use value '1' just to indicate the presence of this session with these labels
                plex_session_details.labels(
                    session_key=session_key, user=user, player=player, product=product, state=state,
                    type=session_type, address=address, location=location, secure=secure_conn,
                    media_title=media_title, progress_percent=progress_percent,
                    duration_ms=duration_num, view_offset_ms=view_offset_num
                ).set(1)

                # --- Extract Transcode Details (if present) ---
                transcode_session = session.get('TranscodeSession')
                if transcode_session:
                    transcode_sessions_count += 1
                    video_decision = transcode_session.get('videoDecision', 'unknown')
                    audio_decision = transcode_session.get('audioDecision', 'unknown')
                    subtitle_decision = transcode_session.get('subtitleDecision', 'N/A') # Default to N/A
                    speed = transcode_session.get('speed', -1.0) # Use -1 if missing
                    progress = transcode_session.get('progress', -1.0) # Use -1 if missing
                    throttled = "yes" if transcode_session.get('throttled', False) else "no"

                    # Determine summarized transcode status
                    transcode_decision = "Direct Play" # Assume this if transcode session exists but decisions are copy? (Shouldn't happen often)
                    if video_decision == 'copy' and audio_decision == 'copy':
                        transcode_decision = "Direct Stream"
                    elif video_decision == 'transcode' or audio_decision == 'transcode':
                        transcode_decision = "Transcode"
                    # Consider subtitle burn? For now, base on video/audio.
                    else: # Should ideally not happen if decisions are known
                        transcode_decision = "Unknown"

                    # --- Set Transcode Detail Metric ---
                    plex_transcode_session_details.labels(
                        session_key=session_key, user=user, player=player, product=product,
                        transcode_decision=transcode_decision, video_decision=video_decision,
                        audio_decision=audio_decision, subtitle_decision=subtitle_decision,
                        speed=speed, progress=progress, throttled=throttled
                    ).set(1) # Use value '1' to indicate presence

    elif sessions_data is None:
        # Log error if the API fetch failed
        logging.error("Skipping session processing due to API error fetching /status/sessions.")
    else:
        # Log warning if the API fetch succeeded but data format was unexpected
        logging.warning("Could not retrieve session data or data format was unexpected for /status/sessions.")

    # Set the aggregate session count metrics
    plex_sessions_active.set(active_sessions_count)
    plex_transcode_sessions_active.set(transcode_sessions_count)
    logging.info(f"Updated session metrics: Active={active_sessions_count}, Transcoding={transcode_sessions_count}")

def _update_library_metrics():
    """
    Fetches library sections (/library/sections) and item counts for each section.

    Updates plex_library_sections_count and plex_library_items_count metrics.
    """
    libraries_data = fetch_plex_api('/library/sections')
    sections_count = 0

    # Reset item count metric (labels) before populating
    plex_library_items_count.clear()

    if libraries_data is not None and 'MediaContainer' in libraries_data:
        container = libraries_data['MediaContainer']
        sections_count = int(container.get('size', 0))

        if 'Directory' in container:
            # Iterate through each library section found
            for section in container.get('Directory', []):
                section_key = section.get('key')
                section_title = section.get('title', 'Unknown')
                section_type = section.get('type', 'Unknown')
                if section_key:
                    # Fetch details for this specific section to get item count
                    # Using Size=0 returns metadata including totalSize without the items themselves
                    section_details = fetch_plex_api(f'/library/sections/{section_key}/all?X-Plex-Container-Start=0&X-Plex-Container-Size=0')
                    if section_details is not None and 'MediaContainer' in section_details:
                        # Get item count, preferring totalSize if available
                        item_count = int(section_details['MediaContainer'].get('totalSize', section_details['MediaContainer'].get('size', 0)))
                        plex_library_items_count.labels(section_title=section_title, section_type=section_type).set(item_count)
                        logging.debug(f"Library '{section_title}' ({section_type}): {item_count} items")
                    else:
                        # Log warning if item count fetch failed for a section
                        logging.warning(f"Could not retrieve item count for section '{section_title}' (Key: {section_key})")
                else:
                    # Log warning if a section lacks a key (shouldn't normally happen)
                    logging.warning("Found library section without a key.")
    elif libraries_data is None:
        logging.error("Skipping library processing due to API error fetching /library/sections.")
    else:
        logging.warning("Could not retrieve library sections data or data format was unexpected for /library/sections.")

    # Set the total library section count
    plex_library_sections_count.set(sections_count)
    logging.info(f"Updated library metrics: Sections={sections_count}")

def _update_device_metrics(server_info_dict):
    """
    Fetches connected devices (/devices).

    Updates plex_devices_connected_count metric, excluding the server itself.

    Args:
        server_info_dict (dict): Dictionary containing server identity info,
                                 used to filter out the server from device list.
    """
    devices_data = fetch_plex_api('/devices')
    devices_count = 0
    if devices_data is not None and 'MediaContainer' in devices_data:
        # Get server's identifier to filter it out
        server_id = server_info_dict.get('machine_identifier') if server_info_dict else None
        # Count devices that are not the server itself
        connected_devices = [
            d for d in devices_data['MediaContainer'].get('Device', [])
            if not server_id or d.get('clientIdentifier') != server_id
        ]
        devices_count = len(connected_devices)
    elif devices_data is None:
        logging.error("Skipping device count processing due to API error fetching /devices.")
    else:
        logging.warning("Could not retrieve devices data or data format was unexpected for /devices.")

    plex_devices_connected_count.set(devices_count)
    logging.info(f"Updated connected client devices count: {devices_count}")

def _update_activity_metrics():
    """
    Fetches current background activities (/activities).

    Updates plex_activities_active_count metric.
    """
    activities_data = fetch_plex_api('/activities')
    activities_count = 0
    if activities_data is not None and 'MediaContainer' in activities_data:
        activities_count = int(activities_data['MediaContainer'].get('size', 0))
    elif activities_data is None:
        logging.error("Skipping activity count processing due to API error fetching /activities.")
    else:
        logging.warning("Could not retrieve activities data or data format was unexpected for /activities.")

    plex_activities_active_count.set(activities_count)
    logging.info(f"Updated active background activities count: {activities_count}")

def _update_updater_status():
    """
    Fetches server update status (/updater/status).

    Updates plex_updater_available metric with version labels.
    """
    updater_data = fetch_plex_api('/updater/status')

    # Reset metric (labels) before populating
    plex_updater_available.clear()

    if updater_data is not None and 'MediaContainer' in updater_data:
        container = updater_data['MediaContainer']
        # Status 1 indicates an update is available
        update_available_flag = int(container.get('status', 0)) == 1
        current_version = container.get('version', 'unknown')
        # Get the available version string if an update is available
        available_version = container.get('Release', [{}])[0].get('version', 'unknown') if update_available_flag else current_version

        plex_updater_available.labels(current_version=current_version, available_version=available_version).set(1 if update_available_flag else 0)
        logging.info(f"Update available: {'Yes' if update_available_flag else 'No'} (Current: {current_version}, Available: {available_version})")
    elif updater_data is None:
        logging.error("Skipping updater status processing due to API error fetching /updater/status.")
    else:
        logging.warning("Could not retrieve updater status or data format was unexpected for /updater/status.")


# --- Main Metric Collection Logic (Refactored Orchestrator) ---
@plex_exporter_scrape_duration_seconds.time()
def collect_plex_metrics():
    """
    Orchestrates the collection of all Plex metrics.

    Calls sub-functions to fetch data for each metric group.
    Handles the overall scrape counter and ensures server status is checked first.
    Resets metrics if the server is down.
    """
    logging.info("Starting Plex metrics scrape cycle...")
    plex_exporter_scrapes_total.inc() # Increment scrape counter

    # 1. Check server status first - this is critical
    server_ok, server_info = _update_server_status()

    # Only proceed if server is up and identity was confirmed
    if not server_ok:
        logging.warning("Skipping further metric collection as server is down or identity check failed.")
        # Reset potentially stale metrics if server is down
        plex_sessions_active.set(0)
        plex_transcode_sessions_active.set(0)
        plex_library_sections_count.set(0)
        plex_library_items_count.clear()
        plex_devices_connected_count.set(0)
        plex_activities_active_count.set(0)
        plex_updater_available.clear()
        plex_session_details.clear()
        plex_transcode_session_details.clear()
        return # Exit the collection cycle early

    # --- Collect other metrics now that server is confirmed up ---
    try:
        _update_session_metrics()
        _update_library_metrics()
        _update_device_metrics(server_info) # Pass server info if needed by the function
        _update_activity_metrics()
        _update_updater_status()

    except Exception as e:
        # Catch unexpected errors during the collection of secondary metrics
        logging.error(f"Unexpected error during secondary metric collection: {e}", exc_info=True)
        plex_exporter_scrape_errors_total.inc() # Count this as a scrape error

    logging.info("Plex metrics scrape cycle finished.")


# --- Scheduler Setup ---
def run_scheduler():
    """
    Runs the scheduled metrics collection task in a loop.

    This function is intended to be run in a separate thread.
    """
    logging.info("Scheduler thread started.")
    while True:
        schedule.run_pending()
        time.sleep(1) # Check schedule every second

# --- Main Execution ---
if __name__ == '__main__':
    """
    Main entry point of the script.

    Sets up the scheduler, starts the Prometheus HTTP server,
    and runs the main loop.
    """
    logging.info(f"Starting Plex Exporter on port {EXPORTER_PORT}")
    logging.info(f"Scraping Plex metrics every {SCRAPE_INTERVAL_SECONDS} seconds")

    # Perform an initial scrape immediately on startup
    logging.info("Performing initial metric collection...")
    collect_plex_metrics()
    logging.info("Initial metric collection complete.")

    # Schedule the collect_plex_metrics function to run periodically
    schedule.every(SCRAPE_INTERVAL_SECONDS).seconds.do(collect_plex_metrics)

    # Start the Prometheus client's HTTP server to expose metrics
    try:
        start_http_server(EXPORTER_PORT)
        logging.info(f"Prometheus HTTP server started on port {EXPORTER_PORT}.")
    except Exception as e:
        logging.error(f"Failed to start Prometheus HTTP server: {e}", exc_info=True)
        exit(1)

    # Start the scheduler loop in a background thread
    # Using daemon=True so the thread exits when the main thread exits
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()

    # Keep the main thread alive indefinitely (or until interrupted)
    try:
        while True:
            time.sleep(60) # Keep main thread alive, sleep for a minute
    except KeyboardInterrupt:
        logging.info("Exporter stopped by user.")
        exit(0)
    except Exception as e:
        logging.error(f"Exporter encountered critical error in main loop: {e}", exc_info=True)
        exit(1)
