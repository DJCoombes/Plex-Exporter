import os
import threading
import logging
import warnings

import time
import requests
import schedule
from prometheus_client import start_http_server, Gauge, Counter, Summary, REGISTRY, Info

# --- Configuration ---
PLEX_URL = os.environ.get('PLEX_URL')
PLEX_TOKEN = os.environ.get('PLEX_TOKEN')
EXPORTER_PORT = int(os.environ.get('EXPORTER_PORT', 9595))
SCRAPE_INTERVAL_SECONDS = int(os.environ.get('SCRAPE_INTERVAL_SECONDS', 60))
REQUEST_TIMEOUT_SECONDS = int(os.environ.get('REQUEST_TIMEOUT_SECONDS', 10))
LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO').upper()
# *** New environment variable to control SSL verification ***
PLEX_SKIP_VERIFY_ENV = os.environ.get('PLEX_SKIP_VERIFY', 'false').lower()
PLEX_SKIP_VERIFY = PLEX_SKIP_VERIFY_ENV in ['true', '1', 't', 'y', 'yes']

# --- Logging Setup ---
logging.basicConfig(level=LOG_LEVEL, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Input Validation ---
if not PLEX_URL:
    logging.error("PLEX_URL environment variable not set. Exiting.")
    exit(1)
if not PLEX_TOKEN:
    logging.error("PLEX_TOKEN environment variable not set. Exiting.")
    exit(1)

# *** Add warning if SSL verification is disabled ***
if PLEX_SKIP_VERIFY:
    logging.warning("=" * 80)
    logging.warning("SECURITY WARNING: SSL certificate verification is DISABLED!")
    logging.warning("This configuration is insecure for non-local connections.")
    logging.warning("=" * 80)
    # Suppress only the specific InsecureRequestWarning from requests
    warnings.filterwarnings('ignore', message='Unverified HTTPS request')


# --- Prometheus Metrics Definition ---
# Remove default collectors to avoid potential conflicts or unwanted metrics
for coll in list(REGISTRY._collector_to_names.keys()):
    REGISTRY.unregister(coll)

# Exporter Metrics
plex_exporter_scrapes_total = Counter('plex_exporter_scrapes_total', 'Total number of scrapes performed.')
plex_exporter_scrape_errors_total = Counter('plex_exporter_scrape_errors_total', 'Total number of scrape errors.')
plex_exporter_scrape_duration_seconds = Summary('plex_exporter_scrape_duration_seconds', 'Duration of Plex API scrapes.')

# Plex Server Info & Status
plex_server_info = Info('plex_server', 'Plex Media Server information')
plex_server_up = Gauge('plex_server_up', 'Indicates if the Plex server is reachable (1 = yes, 0 = no).')
plex_updater_available = Gauge('plex_updater_available', 'Indicates if a Plex server update is available (1 = yes, 0 = no).', ['current_version', 'available_version'])
plex_devices_connected_count = Gauge('plex_devices_connected_count', 'Number of connected client devices.')
plex_activities_active_count = Gauge('plex_activities_active_count', 'Number of active background activities (scanning, processing).')

# Session Metrics
plex_sessions_active = Gauge('plex_sessions_active', 'Number of active Plex playback sessions.')
plex_session_details = Gauge('plex_session_details', 'Details about active playback sessions', [
    'session_key', 'user', 'player', 'product', 'state', 'type', 'address',
    'location', 'secure', 'media_title', 'progress_percent', 'duration_ms', 'view_offset_ms'
])
plex_transcode_sessions_active = Gauge('plex_transcode_sessions_active', 'Number of active transcode sessions.')
plex_transcode_session_details = Gauge('plex_transcode_session_details', 'Details about active transcode sessions', [
    'session_key', 'user', 'player', 'product', 'transcode_decision',
    'video_decision', 'audio_decision', 'subtitle_decision', 'speed', 'progress', 'throttled'
])

# Library Metrics
plex_library_sections_count = Gauge('plex_library_sections_count', 'Total number of library sections.')
plex_library_items_count = Gauge('plex_library_items_count', 'Total number of items in a library section', ['section_title', 'section_type'])

# --- Plex API Interaction ---
def fetch_plex_api(endpoint, params=None):
    """Fetches data from a Plex API endpoint."""
    headers = {'X-Plex-Token': PLEX_TOKEN, 'Accept': 'application/json'}
    url = f"{PLEX_URL.rstrip('/')}/{endpoint.lstrip('/')}"
    # *** Determine SSL verification setting ***
    ssl_verify = not PLEX_SKIP_VERIFY

    try:
        # *** Pass verify=ssl_verify to requests.get ***
        response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS, params=params, verify=ssl_verify)
        response.raise_for_status()
        if response.status_code == 200 and not response.content:
            return {}
        # Check if response content type is JSON before trying to decode
        content_type = response.headers.get('Content-Type', '')
        if 'application/json' in content_type:
            return response.json()
        else:
            logging.warning("Received non-JSON response (Content-Type: %s) from %s. Status: %s. Body: %s...", content_type, url, response.status_code, response.text[:200]) # Log start of body
            return {} # Treat as success but no usable data

    except requests.exceptions.JSONDecodeError as e:
        # This might happen if raise_for_status didn't catch an error but the body isn't JSON
        logging.error("Failed to decode JSON from %s. Status: %s. Error: %s. Response text: %s...", url, response.status_code, e, response.text[:200])
        plex_exporter_scrape_errors_total.inc()
        return None
    except requests.exceptions.SSLError as e:
        logging.error("SSL Error fetching %s: %s. If using self-signed certs, set PLEX_SKIP_VERIFY=true.", url, e)
        plex_exporter_scrape_errors_total.inc()
        return None
    except requests.exceptions.RequestException as e:
        logging.error("Error fetching %s: %s", url, e)
        plex_exporter_scrape_errors_total.inc()
        return None
    except Exception as e:
        logging.error("An unexpected error occurred fetching %s: %s", url, e)
        plex_exporter_scrape_errors_total.inc()
        return None

# --- Helper Function ---
def get_media_title(session):
    """Constructs a readable media title from session data."""
    media_type = session.get('type', 'unknown')
    title = session.get('title', '')

    if media_type == 'episode':
        show_title = session.get('grandparentTitle', '')
        season_num = str(session.get('parentIndex', '?')).zfill(2)
        episode_num = str(session.get('index', '?')).zfill(2)
        episode_title = title
        return f"{show_title} - S{season_num}E{episode_num} - {episode_title}"
    elif media_type == 'movie':
        year = session.get('year', '')
        return f"{title} ({year})" if year else title
    elif media_type == 'track':
        artist_title = session.get('grandparentTitle', '') # Artist
        album_title = session.get('parentTitle', '') # Album
        return f"{artist_title} - {album_title} - {title}"
    else:
        return title # Fallback

# --- Metric Collection Logic ---
@plex_exporter_scrape_duration_seconds.time()
def collect_plex_metrics():
    """Collects metrics from Plex API and updates Prometheus gauges/counters."""
    logging.info("Starting Plex metrics scrape...")
    plex_exporter_scrapes_total.inc()
    server_is_up = 0
    server_info_dict = {} # Store server info for later use

    # --- Reset Metrics With Labels ---
    plex_session_details.clear()
    plex_transcode_session_details.clear()
    plex_library_items_count.clear()
    plex_updater_available.clear()

    # 1. Check Server Reachability & Get Identity/Version
    # Use a more reliable JSON endpoint if possible, like /identity
    identity_data = fetch_plex_api('/identity')
    if identity_data is not None and 'MediaContainer' in identity_data:
        server_is_up = 1
        plex_server_up.set(server_is_up)
        logging.info("Successfully connected to Plex server.")
        mc = identity_data['MediaContainer']
        server_info_dict = {
            'version': mc.get('version', 'unknown'),
            'platform': mc.get('platform', 'unknown'),
            'platform_version': mc.get('platformVersion', 'unknown'),
            'friendly_name': mc.get('friendlyName', 'unknown'),
            'machine_identifier': mc.get('machineIdentifier', 'unknown')
        }
        plex_server_info.info(server_info_dict)
        logging.info("Plex version: %s, Platform: %s", server_info_dict['version'], server_info_dict['platform'])
    else:
        # If fetch_plex_api returned None due to an error, it already logged it.
        # If it returned an empty dict (e.g. non-json response), log that here.
        if identity_data is not None: # i.e. fetch didn't return None
            logging.error("Failed to get valid identity data from Plex server, but connection seemed successful.")
        plex_server_up.set(0) # Set server down if identity check fails
        plex_sessions_active.set(0)
        plex_transcode_sessions_active.set(0)
        plex_library_sections_count.set(0)
        plex_devices_connected_count.set(0)
        plex_activities_active_count.set(0)
        plex_server_info.info({})
        return # Stop scrape if server is down or identity fails

    # --- Collect Metrics (only if server is up and identity confirmed) ---

    # 2. Get Active Sessions & Transcodes
    sessions_data = fetch_plex_api('/status/sessions')
    # Check if sessions_data is not None before proceeding
    if sessions_data is not None and 'MediaContainer' in sessions_data:
        container = sessions_data['MediaContainer']
        active_sessions = int(container.get('size', 0))
        plex_sessions_active.set(active_sessions)
        logging.info("Active sessions: %s", active_sessions)

        transcode_count = 0
        if 'Metadata' in container:
            for session in container.get('Metadata', []):
                # --- Basic Session Info ---
                user = session.get('User', {}).get('title', 'Unknown')
                player_info = session.get('Player', {})
                player = player_info.get('title', 'Unknown')
                product = player_info.get('product', 'Unknown')
                state = player_info.get('state', 'unknown') # playing, paused, buffering
                session_key = session.get('sessionKey', 'unknown')
                session_type = session.get('type', 'unknown') # movie, episode, track
                address = player_info.get('address', 'unknown')
                is_local = player_info.get('local', False)
                is_secure = player_info.get('secure', False)
                location = "local" if is_local else "remote"
                secure_conn = "yes" if is_secure else "no"

                # --- Media Info ---
                media_title = get_media_title(session)
                duration_ms = session.get('duration', 0)
                view_offset_ms = session.get('viewOffset', 0)
                progress_percent = 0
                # Ensure duration_ms is treated as a number and is positive
                try:
                    duration_num = int(duration_ms)
                    view_offset_num = int(view_offset_ms)
                    if duration_num > 0:
                        progress_percent = round((view_offset_num / duration_num) * 100, 2)
                except (ValueError, TypeError):
                    logging.warning("Could not calculate progress for session %s, invalid duration/offset: D=%s, O=%s", session_key, duration_ms, view_offset_ms)
                    duration_num = 0
                    view_offset_num = 0


                logging.debug("Session: User=%s, Player=%s, State=%s, Title=%s, Progress=%s%%", user, player, state, media_title, progress_percent)

                # --- Set Detailed Session Metric ---
                plex_session_details.labels(
                    session_key=session_key, user=user, player=player,
                    product=product, state=state, type=session_type, address=address,
                    location=location, secure=secure_conn, media_title=media_title,
                    progress_percent=progress_percent, duration_ms=duration_num, view_offset_ms=view_offset_num # Use numeric values
                ).set(1)

                # --- Transcode Info ---
                transcode_session = session.get('TranscodeSession')
                transcode_decision = "Direct Play"
                video_decision = "Direct Play"
                audio_decision = "Direct Play"
                subtitle_decision = "N/A" # Default if no transcode session or subtitle stream
                speed = -1.0
                progress = -1.0
                throttled = "no"

                if transcode_session:
                    transcode_count += 1
                    video_decision = transcode_session.get('videoDecision', 'unknown')
                    audio_decision = transcode_session.get('audioDecision', 'unknown')
                    # Subtitle decision might not exist if none are involved
                    subtitle_decision = transcode_session.get('subtitleDecision', 'N/A')
                    speed = transcode_session.get('speed', -1.0)
                    progress = transcode_session.get('progress', -1.0)
                    throttled = "yes" if transcode_session.get('throttled', False) else "no"

                    if video_decision == 'copy' and audio_decision == 'copy':
                        transcode_decision = "Direct Stream"
                    elif video_decision == 'transcode' or audio_decision == 'transcode':
                        transcode_decision = "Transcode"
                    # If subtitle decision involves burn, consider it transcode? Maybe keep separate?
                    # For simplicity, base overall decision on video/audio for now.
                    else:
                        transcode_decision = "Unknown"

                    logging.debug("Transcode: Decision=%s, Speed=%s, Progress=%s%%", transcode_decision, speed, progress)

                    plex_transcode_session_details.labels(
                        session_key=session_key, user=user, player=player, product=product,
                        transcode_decision=transcode_decision,
                        video_decision=video_decision,
                        audio_decision=audio_decision,
                        subtitle_decision=subtitle_decision,
                        speed=speed,
                        progress=progress,
                        throttled=throttled
                    ).set(1)

        plex_transcode_sessions_active.set(transcode_count)
        logging.info("Active transcode sessions: %s", transcode_count)
    # Handle case where sessions_data might be None (due to API error)
    elif sessions_data is None:
        logging.error("Skipping session processing due to previous API error.")
        plex_sessions_active.set(0)
        plex_transcode_sessions_active.set(0)
    else:
        # Handle case where sessions_data is not None but lacks MediaContainer (unexpected format)
        plex_sessions_active.set(0)
        plex_transcode_sessions_active.set(0)
        logging.warning("Could not retrieve session data or data format was unexpected.")


    # 3. Get Library Information (Includes Item Counts)
    libraries_data = fetch_plex_api('/library/sections')
    if libraries_data is not None and 'MediaContainer' in libraries_data:
        container = libraries_data['MediaContainer']
        sections_count = int(container.get('size', 0))
        plex_library_sections_count.set(sections_count)
        logging.info("Library sections found: %s", sections_count)

        if 'Directory' in container:
            for section in container.get('Directory', []):
                section_key = section.get('key')
                section_title = section.get('title', 'Unknown')
                section_type = section.get('type', 'Unknown')
                if section_key:
                    section_details = fetch_plex_api(f'/library/sections/{section_key}/all?X-Plex-Container-Start=0&X-Plex-Container-Size=0')
                    if section_details is not None and 'MediaContainer' in section_details:
                        item_count = int(section_details['MediaContainer'].get('totalSize', section_details['MediaContainer'].get('size', 0)))
                        plex_library_items_count.labels(section_title=section_title, section_type=section_type).set(item_count)
                        logging.debug("Library '%s' (%s): %s items", section_title, section_type, item_count)
                    else:
                        logging.warning("Could not retrieve item count for section '%s' (Key: %s)", section_title, section_key)
                else:
                    logging.warning("Found library section without a key.")
    elif libraries_data is None:
        logging.error("Skipping library processing due to previous API error.")
        plex_library_sections_count.set(0)
    else:
        plex_library_sections_count.set(0)
        logging.warning("Could not retrieve library sections data or data format was unexpected.")

    # 4. Get Connected Devices Count
    devices_data = fetch_plex_api('/devices')
    if devices_data is not None and 'MediaContainer' in devices_data:
        server_id = server_info_dict.get('machine_identifier') if server_info_dict else None
        connected_devices = [
            d for d in devices_data['MediaContainer'].get('Device', [])
            if not server_id or d.get('clientIdentifier') != server_id
        ]
        devices_count = len(connected_devices)
        plex_devices_connected_count.set(devices_count)
        logging.info("Connected client devices: %s", devices_count)
    elif devices_data is None:
        logging.error("Skipping device count processing due to previous API error.")
        plex_devices_connected_count.set(0)
    else:
        plex_devices_connected_count.set(0)
        logging.warning("Could not retrieve devices data or data format was unexpected.")

    # 5. Get Active Activities Count
    activities_data = fetch_plex_api('/activities')
    if activities_data is not None and 'MediaContainer' in activities_data:
        activities_count = int(activities_data['MediaContainer'].get('size', 0))
        plex_activities_active_count.set(activities_count)
        logging.info("Active background activities: %s", activities_count)
    elif activities_data is None:
        logging.error("Skipping activity count processing due to previous API error.")
        plex_activities_active_count.set(0)
    else:
        plex_activities_active_count.set(0)
        logging.warning("Could not retrieve activities data or data format was unexpected.")

    # 6. Get Updater Status
    updater_data = fetch_plex_api('/updater/status')
    if updater_data is not None and 'MediaContainer' in updater_data:
        container = updater_data['MediaContainer']
        update_available_flag = int(container.get('status', 0)) == 1
        current_version = container.get('version', 'unknown')
        available_version = container.get('Release', [{}])[0].get('version', 'unknown') if update_available_flag else current_version
        plex_updater_available.labels(current_version=current_version, available_version=available_version).set(1 if update_available_flag else 0)
        logging.info("Update available: %s (Current: %s, Available: %s)", 
                     "Yes" if update_available_flag else "No", current_version, available_version)
    elif updater_data is None:
        logging.error("Skipping updater status processing due to previous API error.")
    else:
        logging.warning("Could not retrieve updater status or data format was unexpected.")

    logging.info("Plex metrics scrape finished.")


# --- Scheduler Setup ---
def run_scheduler():
    """Runs the scheduled tasks in a loop."""
    while True:
        schedule.run_pending()
        time.sleep(1)

# --- Main Execution ---
if __name__ == '__main__':
    logging.info("Starting Plex Exporter on port %s", EXPORTER_PORT)
    logging.info("Scraping Plex metrics every %s seconds", SCRAPE_INTERVAL_SECONDS)

    # Perform an initial scrape immediately
    collect_plex_metrics()

    # Schedule periodic scrapes
    schedule.every(SCRAPE_INTERVAL_SECONDS).seconds.do(collect_plex_metrics)

    # Start the Prometheus HTTP server
    start_http_server(EXPORTER_PORT)
    logging.info("Prometheus HTTP server started.")

    # Start the scheduler in a separate thread
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()

    # Keep the main thread alive
    while True:
        time.sleep(1)
