__version__ = "1.0.0"

"""
TravelSync – PhotoPrism Metadata Synchronization Toolkit
========================================================

TravelSync is a bidirectional metadata synchronization tool designed
to keep two PhotoPrism libraries consistent:

    MASTER  → canonical archive library
    TRAVEL  → lightweight mobile library (JPEG proxies)

The tool synchronizes metadata and albums through the PhotoPrism API
while maintaining strict safeguards to prevent destructive operations.

Main capabilities
-----------------
• Extract primary image list from MASTER
• Generate resized JPEG proxies for TRAVEL
• Seed TRAVEL with YAML sidecars from MASTER
• Bidirectional metadata synchronization via API
• Album definition and membership synchronization
• YAML sidecar auditing and statistical analysis

Key design goals
----------------
• Safety first: multiple safeguards prevent accidental data loss
• Deterministic conflict resolution
• Resilient API communication with retries
• Thread-safe state tracking
• Efficient scanning of very large libraries (100k+ items)

Concurrency Model
-----------------

Resize
    Uses RJOBS from config.ini.
    Optimal value depends on storage type and CPU.

Photo Sync
    • 2 threads for initial MASTER/TRAVEL index retrieval
    • JOBS workers for metadata comparisons and updates

    Recommended:
        SQLite backend  → JOBS = 1
        MariaDB backend → JOBS = 3–7

YAML Audit
    Uses two workers for sidecar scanning.

Performance Notes
-----------------

Typical timings on localhost:

    Index fetch (100k photos):
        ~12–15 seconds per library

    YAML parsing:
        Using CSafeLoader improves speed significantly.

Safety Systems
--------------

• Configuration sanity checks
• Travel directory sentinel lock file
• Proxy detection safeguards before deletion
• Soft-delete quarantine instead of destructive deletes
• Thread-safe state tracking
• API retry with exponential backoff

This project is intended to demonstrate advanced PhotoPrism API usage
and may serve as a reference implementation for third-party tooling.
"""

import sys
import os
import pathlib
from pathlib import Path
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import subprocess
import time
from datetime import datetime, timezone
import json
from typing import Dict, List
import requests
import configparser
from dotenv import load_dotenv
import argparse
import logging
import yaml
from collections import Counter

# Add PIL for the Python resizer
from PIL import Image, ImageOps

# Try to use the hyper-fast C-Loader, fallback to standard if not installed
YamlCLoader=False
try:
    from yaml import CSafeLoader as SafeLoader
    YamlCLoader=True
except ImportError:
    from yaml import SafeLoader
    
# ===============================
# CONFIGURATION LOADING
# ===============================
#
# Configuration sources:
#
#   .env
#       Contains secret credentials such as PhotoPrism App Passwords.
#
#   TravelSync_config.ini
#       Contains runtime settings including:
#           - server URLs
#           - filesystem paths
#           - performance parameters
#
# All paths are normalized to pathlib.Path objects to ensure
# consistent filesystem behavior across platforms.
#
# Load the secrets from the .env file
load_dotenv()
MASTER_PASSWORD = os.getenv("MASTER_APP_PASSWORD")
TRAVEL_PASSWORD = os.getenv("TRAVEL_APP_PASSWORD")

# Load the settings from the config.ini
script_dir = pathlib.Path(__file__).parent.absolute()
config_path = script_dir / "TravelSync_config.ini"
config = configparser.ConfigParser()
config.read(config_path)

MASTER_URL = config['SERVERS']['MASTER_URL']
TRAVEL_URL = config['SERVERS']['TRAVEL_URL']
USERNAME = config['SERVERS']['USERNAME']
REQUEST_TIMEOUT = config.getint('SERVERS', 'REQUEST_TIMEOUT')
MAX_ATTEMPTS = config.getint('SERVERS', 'MAX_ATTEMPTS')

# FIX: Wrap paths in Path() so they behave correctly
MASTER_ORIGINALS = Path(config['PATHS']['MASTER_ORIGINALS'])
MASTER_SIDECAR = Path(config['PATHS']['MASTER_SIDECAR'])
TRAVEL_ORIGINALS = Path(config['PATHS']['TRAVEL_ORIGINALS'])
TRAVEL_SIDECAR = Path(config['PATHS']['TRAVEL_SIDECAR'])

MAX_SIZE = config.getint('RESIZE', 'MAX_SIZE')
JPEG_QUALITY = config.getint('RESIZE', 'JPEG_QUALITY')
RESIZE_ENGINE = config.get('RESIZE', 'ENGINE', fallback='pillow').lower()
RESIZE_JOBS = config.getint('RESIZE', 'RJOBS')

# Number of Workers for sync and audit
JOBS = config.getint('GLOBAL', 'JOBS')
FORCEDEBUGJOB1 = config.getint('GLOBAL', 'FORCEDEBUGJOB1', fallback=1)
# Ensure User data directory exists
data_dir = script_dir / "TravelSync_UserData"
data_dir.mkdir(exist_ok=True)

# Ensure Travel Trash directory exists
# When seeding YAMLs, will put in that directory all orphan YAML and JPG from TRAVEL
# If they are marked absent from MASTER, instead of deleting them with os.remove
trash_dir = data_dir / "TravelSync_Trash"
trash_dir.mkdir(exist_ok=True)

# Ensure Logs directory exists
log_dir = data_dir / "logs/"
log_dir.mkdir(exist_ok=True)

STATE_FILE = data_dir / "travelsync_state.json"
PRIMS_FILE = data_dir / "TravelSync_primary_files.txt"
SEEDING_LOG = data_dir / "TravelSync_Seeding_manifest.txt"

# ===============================
# SETUP LOGGING - ALL GOOD
# ===============================
def setup_logging(debug=False):
    """
    Configure application logging.
    
    Logs are written to both:
        • console (stdout)
        • rotating timestamped log files
    
    Log location:
        TravelSync_UserData/logs/
    
    Debug mode increases verbosity and enables detailed
    diagnostic output useful for troubleshooting sync logic.
    """
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_file = f"travelsync_{timestamp}.log"
    log_file_path = log_dir / log_file
    level = logging.DEBUG if debug else logging.INFO
    
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_file_path),
            logging.StreamHandler()
        ],
    )
    
    logging.info("===== TravelSync started =====")
    logging.info(f"Log file (in TravelSync_UserData/log): {log_file}")

# ===============================
# SAFETY NETS
# ===============================
def validate_config_sanity():
    """
    Validate configuration to prevent catastrophic user errors.
    
    These checks run before any filesystem or API operations.
    
    Protections include:
        1. Prevent MASTER and TRAVEL servers from being identical
        2. Prevent MASTER and TRAVEL filesystem paths from overlapping
        3. Verify TRAVEL directory contains a sentinel marker file
    
    The sentinel file acts as a deliberate confirmation that the
    target directory is intended to be a disposable Travel library.
    
    Without this safeguard, a misconfigured path could cause
    accidental modification or deletion of the MASTER archive.
    """    
    # 1. Equality Checks
    if MASTER_URL.strip().lower() == TRAVEL_URL.strip().lower():
        logging.critical("CONFIG ERROR: MASTER_URL and TRAVEL_URL are identical. Aborting.")
        sys.exit(1)
    
    if MASTER_ORIGINALS.resolve() == TRAVEL_ORIGINALS.resolve():
        logging.critical("CONFIG ERROR: MASTER_ORIGINALS and TRAVEL_ORIGINALS point to the same directory. Aborting.")
        sys.exit(1)
    
    # 2. The Sentinel File Check
    sentinel_file = TRAVEL_ORIGINALS / ".travelsync_target"
    if not sentinel_file.exists():
        logging.critical(f" SAFETY LOCKOUT: The Travel directory is missing the sentinel file.")
        logging.critical(f"To prove this is actually your disposable Travel drive, you must create an empty file named '.travelsync_target' at:")
        logging.critical(f" -> {TRAVEL_ORIGINALS}")
        logging.critical("This prevents catastrophic deletion if you accidentally swap Master and Travel paths in your config. Aborting.")
        sys.exit(1)
    
    logging.info("Config sanity checks passed.")
    if YamlCLoader:
        logging.info("Fast YAML C loader available.")
    else:
        logging.info("Slow YAML loader available (non C version).")
    
# ===============================
# HELPERS - ALL GOOD
# ===============================
def retry(operation, name):
    """
    Execute a network operation with automatic retry logic.
    
    Retries are triggered for:
        • network timeouts
        • connection failures
        • HTTP 5xx server errors
    
    Client errors (HTTP 4xx) are considered permanent failures
    and will not be retried.
    
    Backoff strategy:
        exponential backoff with gentle growth
        wait ≈ 1.66^attempt seconds
    
    Parameters
    ----------
    operation : callable
        Function performing the API request.
    name : str
        Human-readable name used for logging.
    
    Returns
    -------
    result of operation()
    
    Raises
    ------
    RequestException after MAX_ATTEMPTS failures
    """
    
    for attempt in range(MAX_ATTEMPTS):
        try:
            if attempt != 0:
                logging.info(f"{name} attempt {attempt + 1}/{MAX_ATTEMPTS}")
            return operation()
        
        except requests.exceptions.RequestException as e:
            # If it's an HTTP Error, check the status code
            if isinstance(e, requests.exceptions.HTTPError):
                status = e.response.status_code
                # DO NOT retry on 4xx client errors (like Bad Request, Unauthorized)
                if 400 <= status < 500:
                    logging.error(f"{name} failed permanently (Client Error {status}): {e}")
                    raise
            
            # Otherwise (Timeouts, Connection Errors, 5xx Server Errors), wait and retry
            wait = round(10 * (1.66 ** attempt)) / 10
            if attempt == MAX_ATTEMPTS - 1:
                logging.error(f"{name} failed permanently after {MAX_ATTEMPTS} attempts: {e}")
                raise
            
            logging.warning(f"{name} failed. Retrying in {wait}s...")
            time.sleep(wait)

# ===============================
# EXTRACT PRIMARIES
# ===============================
def extract_primaries():
    logging.info("TASK: Extracting MASTER Library Primary Images Paths...")
    
    # Access Master Library through API using PhotoprismClient
    master = PhotoprismClient(MASTER_URL, MASTER_PASSWORD, "MASTER")
    try:
        master.login()
    except Exception:
        return # Error already logged by retry helper
    
    logging.info("Fetching primary photo paths... (This might take a few seconds)")
    
    # Use the unified _request wrapper
    r = master._request(
        "GET", "/api/v1/photos", 
        params={"count": 10000000000, "q": "*", "primary": "true", "merged": "true"}
    )
    
    photos = r.json()
    paths_to_include = []
    
    for p in photos:
        file_name = p.get("FileName")
        if not file_name and "Files" in p:
            for f in p["Files"]:
                if f.get("Primary"):
                    file_name = f.get("Name")
                    break
        
        if file_name:
            paths_to_include.append(file_name)
    
    with open(PRIMS_FILE, "w") as f:
        for path in paths_to_include:
            f.write(f"{path}\n")
    
    logging.info(f"Extracted {len(paths_to_include)} primary paths to {PRIMS_FILE}")

# ===============================
# SEED YAMLS
# ===============================
def get_master_candidate(travel_file_path, master_yaml_map):
    rel_path = travel_file_path.relative_to(TRAVEL_ORIGINALS)
    base_name = rel_path.name.split('.')[0]
    expected_yml_name = base_name + ".yml"
    
    # 1st attempt: Strict path check (Fastest)
    master_yaml_path = MASTER_SIDECAR / rel_path.parent / expected_yml_name
    if master_yaml_path.exists():
        return master_yaml_path, False  # False = Did not use fallback
    
    # 2nd attempt: Global filename fallback (Catches renamed folders)
    fallback_path = master_yaml_map.get(expected_yml_name.lower())
    if fallback_path:
        return fallback_path, True  # True = Used fallback
    
    return None, False

def seed_yamls():
    logging.info("TASK: Seeding YAMLs from MASTER Sidecar to TRAVEL Originals...")
    
    if not MASTER_SIDECAR.exists():
        logging.error(f"ERROR: Master Sidecar not found at {MASTER_SIDECAR}")
        return
    
    if not TRAVEL_ORIGINALS.exists():
        logging.error(f"ERROR: TRAVEL Originals not found. Did you run 'resize' first?")
        return
    
    # Build a global map of Master YAMLs to protect against folder renaming
    logging.info("Indexing Master Sidecars for robust matching...")
    master_yaml_map = {}
    for yml_path in MASTER_SIDECAR.rglob("*.yml"):
        master_yaml_map[yml_path.name.lower()] = yml_path
    
    count = 0
    exists_count = 0
    missing_count = 0
    missing_list = []
    removed_count = 0
    warned_folders = set() # Tracks folders we have already warned the user about
    
    # Using list() to avoid modifying the directory tree while iterating over it
    jpeg_files = list(TRAVEL_ORIGINALS.rglob("*.jpg"))
    total_jpegs = len(jpeg_files)
    
    logging.info(f"Scanning {total_jpegs} JPEGs...")
    
    for i, jpg_path in enumerate(jpeg_files):
        
        # Unpack the tuple to get the path and the fallback flag
        master_yaml, used_fallback = get_master_candidate(jpg_path, master_yaml_map)
        
        # Print a clean warning once per affected directory
        if used_fallback:
            folder_name = jpg_path.parent.name
            if folder_name not in warned_folders:
                # The \n breaks the progress bar carriage return cleanly
                logging.warning(f"\n[!] Path mismatch in '{folder_name}'. Using YAML fallback search (Did you rename a Master directory?)")
                warned_folders.add(folder_name)
        
        # We want the sidecar to sit right next to the jpg with the exact same name + .yml
        travel_yaml = jpg_path.parent / (jpg_path.name + ".yml")
        
        if master_yaml:
            if not travel_yaml.exists():
                # INJECT THE MARKER: Read Master, Write to Travel with a header
                try:
                    with open(master_yaml, 'r', encoding='utf-8') as f_in:
                        content = f_in.read()
                    
                    with open(travel_yaml, 'w', encoding='utf-8') as f_out:
                        f_out.write("# __travelsync_proxy__\n")
                        f_out.write(content)
                    
                    # Preserve the original file timestamps
                    shutil.copystat(master_yaml, travel_yaml)
                    count += 1
                except Exception as e:
                    logging.error(f"Failed to copy and mark YAML for {jpg_path.name}: {e}")
            else:
                exists_count += 1
        else:
            # NO MASTER YAML FOUND -> Proceed to quarantine
            missing_list.append(str(jpg_path.relative_to(TRAVEL_ORIGINALS)))
            missing_count += 1
            
            # --- THE LAST STOP CHECK (Provenance Marker & Extension) ---
            is_safe_to_trash = False
            
            # 1. Primary Check: The YAML Watermark
            if travel_yaml.exists():
                try:
                    with open(travel_yaml, 'r', encoding='utf-8') as f:
                        first_line = f.readline()
                        if "# __travelsync_proxy__" in first_line:
                            is_safe_to_trash = True
                except Exception as e:
                    logging.error(f"Could not read YAML marker for {travel_yaml.name}: {e}")
              
            # 2. Secondary Check: The Extension Chain (If YAML is missing)
            if not is_safe_to_trash:
                valid_proxy_suffixes = (".jpg.jpg", ".jpeg.jpg", ".png.jpg")
                if jpg_path.name.lower().endswith(valid_proxy_suffixes):
                    is_safe_to_trash = True
            
            # The Final Verdict
            if not is_safe_to_trash:
                logging.critical(f"🚨 SAFETY ABORT: Attempted to quarantine {jpg_path.name}.")
                logging.critical("File lacks the proxy YAML marker AND the proxy extension chain.")
                logging.critical("This does not appear to be a Travel proxy. Halting to prevent data loss. Aborting.")
                sys.exit(1)
            
            # --- SOFT DELETE (QUARANTINE) ---
            # If it passes the checks, proceed with moving it to the trash
            
            try:
                trashed_file = trash_dir / jpg_path.name
                shutil.move(str(jpg_path), str(trashed_file))
                removed_count += 1
                
                if travel_yaml.exists():
                    trashed_yaml = trash_dir / travel_yaml.name
                    shutil.move(str(travel_yaml), str(trashed_yaml))
                
            except Exception as e:
                logging.error(f"Could not move {jpg_path.name} to trash: {e}")
        
        # Progress counter
        #if i % 100 == 0 or i == total_jpegs - 1:
        print(f"\r  Processed: {i+1}/{total_jpegs} | Seeded: {count} | Exists: {exists_count} | Missing/Removed: {removed_count}...", end="", flush=True)
    
    # Log missing items
    if missing_list:
        with open(SEEDING_LOG, 'w') as f:
            f.write("OUTbound Missing YAML\n" + "\n".join(missing_list))
        logging.info(f"--- MISSING YAMLs LOGGED: {SEEDING_LOG} ---")
    
    logging.info(f"--- DONE: Hydrated {count} seed files. Total Exist: {exists_count}. Total Removed: {removed_count} ---")

# ===============================
# PYTHON RESIZE to JPG - THIS HAS LIMITATIONS: WIL NOT CONVERT BIG IMAGES, MAY HAVE ISSUES WITH RAW
# ===============================
def _resize_worker(rel_path_str):
    rel_path = Path(rel_path_str.strip())
    target_file = TRAVEL_ORIGINALS / f"{rel_path}.jpg"
    
    if target_file.exists():
        return "SKIP"
    
    # Priority Check: Sidecar first, then Original
    src_sidecar = MASTER_SIDECAR / rel_path
    src_orig = MASTER_ORIGINALS / rel_path
    
    actual_src = None
    if src_sidecar.exists(): actual_src = src_sidecar
    elif src_orig.exists(): actual_src = src_orig
    else: return f"MISSING: {rel_path}"
    
    try:
        target_file.parent.mkdir(parents=True, exist_ok=True)
        
        # --- ENGINE: PIL / PILLOW ---
        if RESIZE_ENGINE == "pillow":
            # Disable DecompressionBomb protection for >89MPixel files
            Image.MAX_IMAGE_PIXELS = None 
            with Image.open(actual_src) as img:
                img = ImageOps.exif_transpose(img)
                img.thumbnail((MAX_SIZE, MAX_SIZE))
                if img.mode != 'RGB':
                    img = img.convert('RGB')
                img.save(target_file, "JPEG", quality=JPEG_QUALITY)
        
        # --- ENGINE: SIPS (macOS Native) ---
        elif RESIZE_ENGINE == "sips":
            cmd = [
                "sips", "-Z", str(MAX_SIZE), 
                "-s", "format", "jpeg", 
                "-s", "formatOptions", str(JPEG_QUALITY), 
                str(actual_src), "--out", str(target_file)
            ]
            # capture_output suppresses the terminal spam
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                return f"ERROR (sips) processing {actual_src.name}: {result.stderr.strip()}"
        
        # --- ENGINE: IMAGEMAGICK ---
        elif RESIZE_ENGINE == "magick":
            # The > symbol ensures it only shrinks, never enlarges
            cmd = [
                "magick", str(actual_src), 
                "-resize", f"{MAX_SIZE}x{MAX_SIZE}>", 
                "-quality", str(JPEG_QUALITY), 
                str(target_file)
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                return f"ERROR (magick) processing {actual_src.name}: {result.stderr.strip()}"
        
        else:
            return f"ERROR: Unknown resize engine '{RESIZE_ENGINE}'"
        
        return "SUCCESS"
    
    except Exception as e:
        return f"ERROR processing {actual_src}: {e}"

def cross_platform_resize():
    logging.info("TASK: Resizing to JPG from MASTER Primary Image to TRAVEL Originals...")
    
    # NEW: Safety check to ensure source drives are actually mounted/accessible
    if not MASTER_ORIGINALS.exists() and not MASTER_SIDECAR.exists():
        logging.error(f"ERROR: Source directories not found. Are your MASTER drives mounted?")
        logging.info(f"Checked: {MASTER_ORIGINALS}")
        logging.info(f"Checked: {MASTER_SIDECAR}")
        return
    
    if not PRIMS_FILE.exists():
        logging.error(f"ERROR: No {PRIMS_FILE}, please run 'extract' first")
        return
    
    with open(PRIMS_FILE, 'r') as f:
        manifest = f.readlines()
    
    total = len(manifest)
    logging.info(f"Processing {total} files using {RESIZE_JOBS} workers with {RESIZE_ENGINE} converter...")
    
    processed = 0
    skipped = 0
    errors = 0
    
    with ThreadPoolExecutor(max_workers=RESIZE_JOBS) as executor:
        futures = {executor.submit(_resize_worker, path): path for path in manifest}
        for future in as_completed(futures):
            processed += 1
            result = future.result()
            if result == "SKIP":
                skipped += 1
            elif result.startswith("ERROR") or result.startswith("MISSING"):
                errors += 1
                # Optional: Only print the first few errors to avoid flooding the console
                if errors < 10: 
                    logging.info(f"{result}")
                elif errors == 10:
                    logging.info(f"... (Further ERRORs suppressed in console)")
            
            #if processed % 100 == 0:
            print(f"\r  Progress: {processed}/{total} (Skipped: {skipped}, Errors: {errors})", end="", flush=True)
    
    print("")      
    logging.info(f"Resize complete. Processed: {total}, Skipped: {skipped}, Errors: {errors}")

# ===============================
# ------ PHOTOPRISM CLIENT ------
# ===============================
AUTHORITATIVE_SRCS = {"manual", "batch"}
KEY_SRCS = ["TitleSrc", "CaptionSrc", "TakenSrc", "PlaceSrc"]
MtT = "M->T" # When syncing MASTER to TRAVEL
TtM = "T->M" # When syncing TRAVEL to MASTER

class PhotoprismClient:
    """
    Thin API client wrapper for PhotoPrism.
    
    Responsibilities
    ----------------
    • Authentication
    • Unified request wrapper with retry logic
    • Simplified helper methods for common API operations
    
    All HTTP requests pass through `_request()` which provides:
        
        • automatic retry
        • timeout enforcement
        • standardized error handling
        • consistent logging
        
    This prevents duplicated error-handling logic throughout the codebase.
    """
    
    def __init__(self, base_url: str, password: str, name: str):
        self.base_url = base_url
        self.password = password  
        self.name = name  # <--- Stores "MASTER" or "TRAVEL"
        self.session = requests.Session()
        self.headers = None
    
    # --------------------------------
    # Core resilient request wrapper
    # --------------------------------
    def _request(self, method, endpoint, **kwargs):
        url = f"{self.base_url}{endpoint}"
        
        def operation():
            response = self.session.request(
                method=method,
                url=url,
                headers=self.headers, # Use class headers automatically
                timeout=REQUEST_TIMEOUT,
                **kwargs
            )
            response.raise_for_status()
            return response
        
        return retry(operation, f"{self.name} {method}")
    
    def login(self):
        logging.info(f"Authenticating {self.name} Library at {self.base_url}...")
        r = self._request("POST", "/api/v1/session", json={"username": USERNAME, "password": self.password})
        token = r.json().get("access_token") or r.json().get("id")
        self.headers = {
            "X-Auth-Token": token,
            "Content-Type": "application/json",
        }
        logging.info(f"Authenticated  {self.name} at {self.base_url}")

    # ---------- photos ----------
    def list_photos_minimal(self) -> Dict[str, tuple]:
        params = {"primary": "true", "merged": "true", "count": 1000000000, "q": "*"}
        r = self._request("GET", "/api/v1/photos", params=params)
        
        resultat = {}
        for p in r.json():
            uid = p["UID"]
            edited_at = p.get("EditedAt", "")
            fav = p.get("Favorite", False)
            resultat[uid] = (edited_at, fav)
        
        logging.info(f"{self.name} Medias found: {len(resultat)}")
        return resultat
    
    def get_photo(self, uid: str) -> dict:
        r = self._request("GET", f"/api/v1/photos/{uid}")
        return r.json()
    
    def update_photo(self, uid: str, payload: dict):
        self._request("PUT", f"/api/v1/photos/{uid}", json=payload)
        logging.debug(f"Updated media {uid} with payload: {payload}")
    
    # ---------- albums ----------
    def list_albums(self) -> Dict[str, dict]:
        r = self._request("GET", "/api/v1/albums", params={"type": "album", "count": 100000})
        result = {}
        for a in r.json():
            cover_uid = a.get("Thumb")
            if not cover_uid and "Cover" in a and isinstance(a["Cover"], dict):
                cover_uid = a["Cover"].get("UID")
            
            result[a["Title"]] = {
                "UID": a["UID"],
                "UpdatedAt": a.get("UpdatedAt", ""),
                "CoverUID": cover_uid or ""
            }
        return result
    
    def create_album(self, title: str):
        self._request("POST", "/api/v1/albums", json={"Title": title, "Type": "album"})
    
    def list_album_photos(self, album_uid: str) -> List[str]:
        r = self._request("GET", "/api/v1/photos", params={"count": 10000000, "q": f"album:{album_uid}", "merged": "true"})
        return [p["UID"] for p in r.json()]
    
    def add_photos_to_album(self, album_uid: str, photo_uids: List[str]):
        # Batch add in chunks of 666
        chunk_size = 666
        for i in range(0, len(photo_uids), chunk_size):
            chunk = photo_uids[i:i + chunk_size]
            self._request("POST", f"/api/v1/albums/{album_uid}/photos", json={"photos": chunk})
            logging.debug(f"Added chunk of {len(chunk)} photos to album {album_uid}")

# ===============================
# ---------- SYNCENGINE ---------
# ===============================
class SyncEngine:
    """
    Core synchronization engine.
    
    Responsibilities
    ----------------
    • Detect metadata drift between libraries
    • Resolve conflicts deterministically
    • Apply updates via PhotoPrism API
    • Maintain local sync state to prevent echo updates
    
    State Tracking
    --------------
    A local JSON state file records the last known metadata
    timestamps and favorite flags for each photo.
    
    This enables a "fast shield" optimization that avoids
    expensive API calls when nothing changed.
    
    Thread Safety
    -------------
    Multiple worker threads may update shared statistics and
    state structures.
    
    A threading.Lock (`state_lock`) protects these operations.
    """
    
    def __init__(self, master, travel, reset=False, dry_run=False, debug=False):
        self.master = master
        self.travel = travel
        self.reset = reset
        self.dry_run = dry_run
        self.state = self._load_state()
        self.debug = debug
        #self.tie_priority = tie_priority.upper() # "MASTER" or "TRAVEL"
        self.conflicts = []
        self.state_lock = threading.Lock()
        self.stats = {
            "processed": 0,
            "synced_m2t": 0,
            "synced_t2m": 0,
            "skipped": 0,
        }
    
    # ---------- state ----------
    
    def _load_state(self):
        if not os.path.exists(STATE_FILE):
            # Default structure for a fresh start
            return {"last_sync": None, "master": {}, "travel": {}}
        
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
            # Migration for old files: Ensure 'last_sync' exists
            if "last_sync" not in data:
                return {"last_sync": None, "master": data.get("master", {}), "travel": data.get("travel", {})}
            return data
    
    def _save_state(self):
        # Update the timestamp to NOW (UTC)
        self.state["last_sync"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        
        with open(STATE_FILE, "w") as f:
            json.dump(self.state, f, indent=2)
    
    # ---------- field logic ----------
    
    def _normalize(self, val):
        if val is None: return ""
        
        # 1. Handle Booleans (Favorite)
        if isinstance(val, bool): return val
        if isinstance(val, int) and val in [0, 1]: return bool(val)
        
        # 2. Handle Floats (Location)
        if isinstance(val, float): 
            return round(val, 6)
        
        # 3. Handle Strings & Dates
        s = str(val).strip()
        
        # Date Normalization: Remove 'Z' and milliseconds
        if "T" in s and ":" in s:
            s = s.replace("Z", "")
            if "." in s: s = s.split(".")[0]
        
        # TimeZone Normalization: Treat "zz" (unknown) same as empty or Local if needed
        # But usually strict comparison is better for sync.
        
        return s if s.lower() not in ["none", "null", ""] else ""
    
    def resolve_conflict(self, m, t, m_state_fav, t_state_fav, uid=""):
        """
        Determine which library should win when metadata differs.
        
        Conflict Resolution Strategy
        -----------------------------
        
        The algorithm performs a field-level "Smart Merge" using five decision layers:
        
        1. SMART DIFF DETECTION
           Only meaningful differences are considered.
           Auto-generated differences (faces, locations, etc.) are ignored 
           unless the change was made manually.
           
        2. FAVORITE ISOLATION
           Because PhotoPrism does not update the `EditedAt` timestamp when 
           a Favorite is toggled, Favorite states are decoupled from text/location 
           fields. State tracking determines which side actually toggled it, 
           allowing for simultaneous, bidirectional merges on the same photo.
        
        3. AUTHORITY CHECK (Text, Dates, Locations)
           For the remaining fields, if a manual/batch edit exists on only 
           one side, that side is considered authoritative.
        
        4. TIMESTAMP COMPARISON
           If both sides contain manual edits, the newest `EditedAt` timestamp 
           dictates the direction for the entire block of text/location fields.
        
        5. FINAL TIE BREAKER
           A global priority defaults to MASTER to resolve any remaining ambiguity.
        
        Returns
        -------
        (m2t_fields, t2m_fields)
        
        m2t_fields : list of fields where MASTER overwrites TRAVEL
        t2m_fields : list of fields where TRAVEL overwrites MASTER
        """
        
        # 1. SMART DIFF IDENTIFICATION
        #         
        diff_fields = []
        
        # These are the fields/keys we are interested in.
        # We do no check any other excepted EditedAt for timestamps.
        # Data fields have Source Keys (*Src).
        # Source Keys tell if the Data field was
        #    edited manual, batch or anything else that means auto to us.
        # Map the data field to its corresponding Source key
        check_map = {
            "Title": "TitleSrc",
            "Caption": "CaptionSrc",
            "TakenAt": "TakenSrc",
            "Lat": "PlaceSrc",
            "Lng": "PlaceSrc",
            "Favorite": None  # Booleans don't have source keys
        }
        
        # LOOP through the data fields and Src keys
        for field, src_key in check_map.items():
             # NORMALIZE values to ensure accurate Diff
             m_val = self._normalize(m.get(field))
             t_val = self._normalize(t.get(field))
             
             if m_val != t_val:
                 # There is a Diff
                 logging.debug(f"DIFF [{uid}] {field}: '{m_val}' vs '{t_val}'")
                 if src_key:
                     m_src = m.get(src_key)
                     t_src = t.get(src_key)
                     # We consider only manual and batch defined in AUTHORITATIVE_SRCS
                     # IF none is user generated, then we ignore any Diff and continue
                     # when these are due to auto-generated Title or else from updating Faces, Location...
                     if m_src not in AUTHORITATIVE_SRCS and t_src not in AUTHORITATIVE_SRCS:
                         logging.debug(f"IGNORING AUTO-DIFF [{uid}] {field}: '{m_val}' vs '{t_val}'")
                         continue # Skip this field, do not trigger a sync
                 
                 # If it's a manual edit, or a field without a Src key (Favorite, Timezone),
                 #     we flag it as a real diff
                 diff_fields.append(field)
        
        # STOP LOOP: If the only differences were auto-generated fields, return nothing
        if not diff_fields:
            return [], []
        
        logging.debug(f"DIFF FIELDS [{uid}]: '{diff_fields}'")
        
        # ==============================================================
        m2t_fields = []
        t2m_fields = []

        # 1. ISOLATE FAVORITE (Because it does not bump EditedAt timestamps)
        if "Favorite" in diff_fields:
            m_fav = self._normalize(m.get("Favorite"))
            t_fav = self._normalize(t.get("Favorite"))
            
            # Smart Override: Check the state tracker
            m_changed = (m_fav != m_state_fav)
            t_changed = (t_fav != t_state_fav)
            
            if m_changed and not t_changed:
                logging.debug(f"[{uid}]: MtT (Master toggled Favorite)")
                m2t_fields.append("Favorite")
            elif t_changed and not m_changed:
                logging.debug(f"[{uid}]: TtM (Travel toggled Favorite)")
                t2m_fields.append("Favorite")
            else:
                # Fallback tie-breaker
                m_time, t_time = m.get("EditedAt", ""), t.get("EditedAt", "")
                if m_time > t_time: m2t_fields.append("Favorite")
                elif t_time > m_time: t2m_fields.append("Favorite")
                else: m2t_fields.append("Favorite") # Default to Master

            diff_fields.remove("Favorite") # Remove from the general pool
        
        
        # ==============================================================

        # 2. EVALUATE THE REST FIELD-BY-FIELD (Text, Dates, Locations)
        if diff_fields:
            src_lookup = {
                "Title": "TitleSrc", 
                "Caption": "CaptionSrc", 
                "TakenAt": "TakenSrc", 
                "Lat": "PlaceSrc", 
                "Lng": "PlaceSrc"
            }
            
            m_time = m.get("EditedAt", "")
            t_time = t.get("EditedAt", "")

            for field in diff_fields:
                src_key = src_lookup.get(field)
                
                # If the field has a known Source key
                if src_key:
                    m_src = m.get(src_key)
                    t_src = t.get(src_key)
                    
                    m_is_manual = m_src in AUTHORITATIVE_SRCS
                    t_is_manual = t_src in AUTHORITATIVE_SRCS
                    
                    # Rule A: Absolute Manual Authority
                    # If one side is manual and the other is auto, the human wins regardless of timestamps.
                    if m_is_manual and not t_is_manual:
                        logging.debug(f"[{uid}] {field}: MtT (Master absolute manual authority)")
                        m2t_fields.append(field)
                    elif t_is_manual and not m_is_manual:
                        logging.debug(f"[{uid}] {field}: TtM (Travel absolute manual authority)")
                        t2m_fields.append(field)
                    else:
                        # Rule B: Tie-Breaker (Both manual OR both auto)
                        # Fall back to the overall EditedAt timestamp for this specific field.
                        if m_time > t_time:
                            logging.debug(f"[{uid}] {field}: MtT (Timestamp tie-breaker)")
                            m2t_fields.append(field)
                        elif t_time > m_time:
                            logging.debug(f"[{uid}] {field}: TtM (Timestamp tie-breaker)")
                            t2m_fields.append(field)
                        else:
                            logging.debug(f"[{uid}] {field}: MtT (Default tie-breaker)")
                            m2t_fields.append(field)
                else:
                    # Timestamps Fallback for fields without a specific Src key (if any besides Favorite end up here)
                    if m_time > t_time:
                        m2t_fields.append(field)
                    elif t_time > m_time:
                        t2m_fields.append(field)
                    else:
                        m2t_fields.append(field) # Ultimate decision is in favor of Master if complete tie

        return m2t_fields, t2m_fields
        
    def compare_fields(self, m, t, uid=""):
        
        diff_fields = []
        
        check_map = {
            "Title": "TitleSrc",
            "Caption": "CaptionSrc",
            "TakenAt": "TakenSrc",
            "Lat": "PlaceSrc",
            "Lng": "PlaceSrc",
            "Favorite": None  # Booleans don't have source keys
            #"TimeZone": None   # TimeZone doesn't have a source key (or is it PlaceSrc?)
        }
        
        check_fields = [
            "Title", "TitleSrc",
            "Caption", "CaptionSrc",
            "TakenAt", "TakenSrc",
            "Lat", "PlaceSrc",
            "Lng",
            "Favorite"
        ]
        
        # LOOP through the data fields and Src keys
        for field, src_key in check_map.items():
             # NORMALIZE values to ensure accurate Diff
             m_val = self._normalize(m.get(field))
             t_val = self._normalize(t.get(field))
             
             if m_val != t_val:
                 # There is a Diff
                 logging.debug(f"DIFF [{uid}] {field}: '{m_val}' vs '{t_val}'")
                 if src_key:
                     m_src = m.get(src_key)
                     t_src = t.get(src_key)
                     # We consider only manual and batch defined in AUTHORITATIVE_SRCS
                     # IF none is user generated, then we ignore any Diff and continue
                     # when these are due to auto-generated Title or else from Faces, Location...
                     if m_src not in AUTHORITATIVE_SRCS and t_src not in AUTHORITATIVE_SRCS:
                         logging.debug(f"IGNORING AUTO-DIFF [{uid}] {field}: '{m_val}' vs '{t_val}'")
                         continue # Skip this field, do not trigger a sync
                 
                 # If it's a manual edit, or a field without a Src key (Favorite, Timezone),
                 #     we flag it as a real diff
                 logging.info(f"Diff [{uid}] {field} Master vs Travel: '{m_val}' vs '{t_val}'")
                 diff_fields.append(field)
        
        # STOP LOOP: If the only differences were auto-generated fields, return nothing
        if not diff_fields:
            return None, []
        
        return None, diff_fields
    
    # ===============================
    # ---------- photo sync ----------
    
    def sync_photos(self):
        logging.info("TASK: Syncing photos...")
        
        last_sync = self.state.get("last_sync")
        logging.info(f"Mode: FULL SCAN | Last Sync: {last_sync}")
        
        try:
            logging.info("Fetching MASTER and TRAVEL photo indexes...")
            
            # Spin up exactly 2 workers for our 2 endpoints
            with ThreadPoolExecutor(max_workers=2) as fetch_executor:
                future_master = fetch_executor.submit(self.master.list_photos_minimal)
                future_travel = fetch_executor.submit(self.travel.list_photos_minimal)
                
                # .result() blocks until the specific thread finishes, 
                # effectively waiting for both to complete before moving on.
                master_index = future_master.result()
                travel_index = future_travel.result()
        
        except KeyboardInterrupt:
            logging.warning("Ctrl-C detected! Aborting index fetch...")
            sys.exit(0)
        except Exception as e:
            logging.error(f"FAILED fetching photo indexes:")
            logging.error(f"{e}")
            return
        
        common = set(master_index) & set(travel_index)
        total = len(common)
        logging.info(f"UIDs to process: {total}")
        
        active_workers = 1 if (self.debug and (FORCEDEBUGJOB1 == 1)) else JOBS
        
        executor = ThreadPoolExecutor(max_workers=active_workers)
        
        logging.info(f"Syncing started with {active_workers} workers")
        futures = []
        
        # consecutive_errors is a test that
        # Let every call retry consecutive_errors_max times
        # Then try next call, 5 times again
        # After 5x5 times errors the task is entirely aborted
        # Any syncs missed by these errors will be processed normally on next run
        consecutive_errors = 0
        consecutive_errors_max = 5
        
        try:
            for uid in common:
                self.stats["processed"] += 1
                print(f"Processed: {self.stats['processed']}/{len(common)}\r", end="", flush=True)
                
                m_lib_time, m_lib_fav = master_index[uid]
                t_lib_time, t_lib_fav = travel_index[uid]
                
                # --- Load State ---
                m_state_val = self.state["master"].get(uid)
                if isinstance(m_state_val, str):
                    m_state_time, m_state_fav = m_state_val, False
                elif m_state_val:
                    m_state_time, m_state_fav = m_state_val
                else:
                    m_state_time, m_state_fav = "", False
                
                t_state_val = self.state["travel"].get(uid)
                if isinstance(t_state_val, str):
                    t_state_time, t_state_fav = t_state_val, False
                elif t_state_val:
                    t_state_time, t_state_fav = t_state_val
                else:
                    t_state_time, t_state_fav = "", False
                
                # --- Fast Shield ---
                #
                # If the metadata timestamps and favorite flags match the
                # last recorded state, we know neither library has changed
                # since the previous sync.
                #
                # This allows us to skip fetching full metadata via API,
                # reducing network traffic and processing time
                # when scanning large libraries.
                if (
                    m_lib_time == m_state_time
                    and t_lib_time == t_state_time
                    and m_lib_fav == m_state_fav
                    and t_lib_fav == t_state_fav
                ):
                    self.stats["skipped"] += 1
                    # We don't log debug the item. That would spam the logs.
                    continue
                
                logging.debug(
                    f"Change detected for {uid} | "
                    f"M_lib={m_lib_time}, T_lib={t_lib_time}"
                )
                
                # --- Fetch Full Metadata ---
                logging.debug(f"Fetching Details for {uid} in Master and Travel")
                try:
                    m = self.master.get_photo(uid)
                    t = self.travel.get_photo(uid)
                    consecutive_errors = 0 # Success Reset
                except Exception as e:
                    logging.error(f"FAILED fetching details for {uid}:")
                    logging.error(f"{e}")
                    consecutive_errors += 1 # Failure Increment
                    if consecutive_errors >= consecutive_errors_max: # Critical Check
                        logging.critical("CRITICAL: 5 consecutive API failures. Aborting Sync!")
                        break
                    
                    continue
                
                m2t_fields, t2m_fields = self.resolve_conflict(
                    m, t, m_state_fav, t_state_fav, uid
                )
        
                if m2t_fields or t2m_fields:
                    if not self.dry_run:
                        # Submit a single task to handle the bidirectional merge safely
                        futures.append(
                            executor.submit(self._apply_bidirectional, uid, m, t, m2t_fields, t2m_fields)
                        )
                    else:
                        if m2t_fields: logging.info(f"DRY-RUN! {uid} → MASTER → TRAVEL | Fields: {m2t_fields}")
                        if t2m_fields: logging.info(f"DRY-RUN! {uid} → TRAVEL → MASTER | Fields: {t2m_fields}")
        
                else:
                    logging.debug(f"[{uid}] Ignored: Timestamp changed, but no tracked fields were modified.")
                    # That above could spam the logs if the user edited a large number of items in fields not tracked.
                    # eg the user batch assign keywords to thousands of photos -> all will be logged
                    # comment the line if needed
                    
                    self.stats["skipped"] += 1
                    # Acknowledge we checked the item by updating state timestamps
                    self._update_state(
                        uid,
                        [m_lib_time, m_lib_fav],
                        [t_lib_time, t_lib_fav],
                    )
            
            # --- Await Updates ---
            for f in as_completed(futures):
                try:
                    f.result()
                except Exception as e:
                    logging.error(f"Sync task FAILED:")
                    logging.error(f"{e}")
        
        except KeyboardInterrupt:
            # BAILOUT ROUTINE
            logging.warning("Ctrl-C detected! Halting sync operations...")
            
            # Cancel all tasks that haven't started yet (Requires Python 3.9+)
            executor.shutdown(wait=False, cancel_futures=True)
            
            # CRITICAL: Save whatever progress we made before exiting
            try:
                self._save_state()
                logging.info("Progress successfully saved to state file.")
            except Exception as e:
                logging.error(f"FAILED saving state file during shutdown: {e}")
            
            logging.info("Graceful exit complete. Goodbye!")
            sys.exit(0)
        
        # Standard clean shutdown
        executor.shutdown(wait=True)
        
        logging.info(
            f"Progress: {self.stats['processed']}/{total} "
            f"| Skipped: {self.stats['skipped']} "
            f"| M→T: {self.stats['synced_m2t']} "
            f"| T→M: {self.stats['synced_t2m']}"
        )
        
        try:
            self._save_state()
        except Exception as e:
            logging.error(f"FAILED saving state file:")
            logging.error(f"{e}")
        
        logging.info("Photo sync complete")
    
    def _build_payload(self, source_data, fields):
        payload = {}
        if "Title" in fields:
            payload["Title"] = source_data.get("Title")
            payload["TitleSrc"] = source_data.get("TitleSrc")
        if "Caption" in fields:
            payload["Caption"] = source_data.get("Caption")
            payload["CaptionSrc"] = source_data.get("CaptionSrc")
        if "Favorite" in fields:
            payload["Favorite"] = source_data.get("Favorite")
        if "TakenAt" in fields:
            payload["TakenAt"] = source_data.get("TakenAt")
            payload["TakenSrc"] = source_data.get("TakenSrc")
        if "Lat" in fields or "Lng" in fields:
            payload["Lat"] = source_data.get("Lat")
            payload["Lng"] = source_data.get("Lng")
            payload["PlaceSrc"] = source_data.get("PlaceSrc")
        return payload

    def _apply_bidirectional(self, uid, m_data, t_data, m2t_fields, t2m_fields):
        """Executes a smart merge by safely pushing targeted payloads in both directions."""
        success = True

        if m2t_fields:
            payload = self._build_payload(m_data, m2t_fields)
            logging.info(f"Sync {uid} : M->T (Updating: {', '.join(payload.keys())})")
            try:
                self.travel.update_photo(uid, payload)
                with self.state_lock: self.stats["synced_m2t"] += 1
            except Exception as e:
                logging.error(f"FAILED updating Travel {uid}: {e}")
                success = False

        if t2m_fields:
            payload = self._build_payload(t_data, t2m_fields)
            logging.info(f"Sync {uid} : T->M (Updating: {', '.join(payload.keys())})")
            try:
                self.master.update_photo(uid, payload)
                with self.state_lock: self.stats["synced_t2m"] += 1
            except Exception as e:
                logging.error(f"FAILED updating Master {uid}: {e}")
                success = False

        if not success: return

        # ANTI-ECHO: Fetch the latest state from BOTH sides so our local state file is perfectly accurate.
        try:
            new_m = self.master.get_photo(uid)
            new_t = self.travel.get_photo(uid)
            with self.state_lock:
                self.state["master"][uid] = [new_m.get("EditedAt", ""), new_m.get("Favorite", False)]
                self.state["travel"][uid] = [new_t.get("EditedAt", ""), new_t.get("Favorite", False)]
        except Exception as e:
            logging.debug(f"Failed to fetch updated state for {uid}: {e}")

    def _update_state(self, uid, m_data, t_data):
        with self.state_lock:
            if m_data is not None:
                self.state["master"][uid] = m_data
            if t_data is not None:
                self.state["travel"][uid] = t_data
    
    def syncSummary(self, duration):
        logging.info("========== SYNC SUMMARY ==========")
        logging.info(f"Processed:        {self.stats['processed']}")
        logging.info(f"Skipped:          {self.stats['skipped']}")
        logging.info(f"Master → Travel:  {self.stats['synced_m2t']}")
        logging.info(f"Travel → Master:  {self.stats['synced_t2m']}")
        logging.info(f"Conflicts:        {len(self.conflicts)}")
        logging.info(f"Duration:         {round(duration,2)} sec")
        logging.info("===================================")
        
        if self.conflicts:
            logging.info("Conflict Details:")
            for c in self.conflicts[:100]:
                logging.info(f"  {c['uid']}  field={c['field']}")
            if len(self.conflicts) > 100:
                logging.info("  ... (100+ truncated)")
    
    def auditSummary(self, duration):
        logging.info("========== AUDIT SUMMARY ==========")
        logging.info(f"Processed:        {self.stats['processed']}")
        logging.info(f"Skipped:          {self.stats['skipped']}")
        logging.info(f"Conflicts:        {len(self.conflicts)}")
        logging.info(f"Duration:         {round(duration,2)} sec")
        logging.info("===================================")
        
        if self.conflicts:
            logging.info("Conflict Details:")
            for c in self.conflicts[:100]:
                logging.info(f"  {c['uid']}  field={c['field']}")
            if len(self.conflicts) > 100:
                logging.info("  ... (100+ truncated)")
    
    # ===============================
    # ---------- photo audit --------
    
    def audit_photos_API(self):
        # TO WORK ON LATER: Integrity Audit: TRAVEL vs MASTER yaml Sidecars...
        # This audit uses the same simplified logic as sync_photos()
        
        logging.info("TASK: Auditing photos...")
        
        # GET every UID from MASTER and master_index[uid] = edited_at
        try:
            logging.info("Fetching MASTER and TRAVEL photo indexes...")
            
            # Spin up exactly 2 workers for our 2 endpoints
            with ThreadPoolExecutor(max_workers=2) as fetch_executor:
                future_master = fetch_executor.submit(self.master.list_photos_minimal)
                future_travel = fetch_executor.submit(self.travel.list_photos_minimal)
                
                # .result() blocks until the specific thread finishes, 
                # effectively waiting for both to complete before moving on.
                master_index = future_master.result()
                travel_index = future_travel.result()
        
        except KeyboardInterrupt:
            logging.warning("Ctrl-C detected! Aborting index fetch...")
            sys.exit(0)
        except Exception as e:
            logging.error(f"FAILED fetching photo indexes:")
            logging.error(f"{e}")
            return
        
        # Set of Common UIDs to both MASTER and TRAVEL
        common = set(master_index) & set(travel_index)
        logging.info(f"UIDs to process: {len(common)}")
        
        executor = ThreadPoolExecutor(max_workers=1)
        # Using more than 1 not impactful
        logging.info(f"Auditing started with {JOBS} worker")
        futures = []
        
        consecutive_errors = 0
        consecutive_errors_max = 5
        try:
            for uid in common:
                self.stats["processed"] += 1
                print(f"Processed: {self.stats['processed']}/{len(common)}\r", end="", flush=True)
                # UNPACK NEW TUPLE: Time and Favorite
                m_lib_time, m_lib_fav = master_index[uid]
                t_lib_time, t_lib_fav = travel_index[uid]
                
                # FETCH FULL DETAILS for uid from MASTER and TRAVEL
                try:
                    m = self.master.get_photo(uid)
                    t = self.travel.get_photo(uid)
                    consecutive_errors = 0
                except Exception as e:
                    logging.error(f"ERROR fetching details for {uid}:")
                    logging.error(f"{e}")
                    #self.stats["errors"] = self.stats.get("errors", 0) + 1
                    consecutive_errors += 1
                    if consecutive_errors >= consecutive_errors_max:
                        logging.critical("CRITICAL: 5 consecutive API failures. Aborting Audit!")
                        break
                    continue
                
                # Now comparing both sets of data
                # UNPACK THE TUPLE HERE
                decision, diff_fields = self.compare_fields(m, t, uid)
            
            # --- Await Updates ---
            for f in as_completed(futures):
                try:
                    f.result()
                except Exception as e:
                    logging.error(f"Audit task FAILED:")
                    logging.error(f"{e}")
        
        except KeyboardInterrupt:
            # BAILOUT ROUTINE
            # Cancel all tasks that haven't started yet (Requires Python 3.9+)
            executor.shutdown(wait=False, cancel_futures=True)
            logging.warning("Ctrl-C detected! Halting Audit operations...")
            logging.info("Graceful exit complete. Goodbye!")
            sys.exit(0)
        
        # Standard clean shutdown if no Ctrl-C was pressed
        executor.shutdown(wait=True)
        
        logging.info(f"Audited: {self.stats['processed']}/{len(common)}")
        logging.info("Photo Audit complete")
    
    def get_from_yaml(self, path, key):
        """
        Extracts a specific key from a YAML sidecar file.
        Returns the value if found, or None if the file/key doesn't exist.
        """
        # Ensure the path actually exists before trying to open it
        if not path.exists():
            return None
        
        try:
            with open(path, 'r', encoding='utf-8') as f:
                # Use the C-Loader for maximum speed
                data = yaml.load(f, Loader=SafeLoader) or {}
            
            # .get() safely returns None if the key isn't in the file
            return data.get(key)
            
        except Exception as e:
            logging.debug(f"Failed to extract '{key}' from {path.name}: {e}")
            return None
    
    def _yaml_worker(self, m_path, t_path, keys_to_check, value_tracking_keys):
        """Worker thread that parses a single pair of YAMLs and returns the diffs."""
        local_diffs = Counter()
        local_values = {k: Counter() for k in value_tracking_keys}
        local_debug = [] # Thread-safe local debug list
        
        try:
            with open(m_path, 'r', encoding='utf-8') as mf:
                m_data = yaml.load(mf, Loader=SafeLoader) or {}
            with open(t_path, 'r', encoding='utf-8') as tf:
                t_data = yaml.load(tf, Loader=SafeLoader) or {}
        except Exception as e:
            return local_diffs, local_values, local_debug, False
        
        for key in keys_to_check:
            m_val = self._normalize(m_data.get(key))
            t_val = self._normalize(t_data.get(key))
            
            #if key == "TakenSrc" and (m_val == "" or t_val == ""):
            #    local_debug.append(f"{self.get_from_yaml(m_path, "UID")} [{key}] {m_path.name} | M: '{m_val}' vs T: '{t_val}'")
            
            if m_val != t_val:
                local_diffs[key] += 1
                
                # --- INVESTIGATION SANDBOX ---
                # Drop your temporary debugging logic in here. 
                # It will be collected and printed after the summary.
                #if key == "OriginalName":
                #    local_debug.append(f"[{key}] {m_path.name} | M: '{m_val}' vs T: '{t_val}'")
                '''
                if key == "TakenSrc":
                    local_debug.append(f"{self.get_from_yaml(m_path, "UID")} [{key}] {m_path.name} | M: '{m_val}' vs T: '{t_val}'")
                    local_debug.append(f"{self.get_from_yaml(m_path, "UID")} [TakenAt] {m_path.name} | M: '{self.get_from_yaml(m_path, "TakenAt")}' vs T: '{self.get_from_yaml(t_path, "TakenAt")}'")
                    local_debug.append(f"{self.get_from_yaml(m_path, "UID")} [TimeZone] {m_path.name} | M: '{self.get_from_yaml(m_path, "TimeZone")}' vs T: '{self.get_from_yaml(t_path, "TimeZone")}'")
                # -----------------------------
                '''
            
            if key in value_tracking_keys:
                local_values[key][str(m_val)] += 1
                if t_val != m_val: 
                    local_values[key][str(t_val)] += 1
        
        # Return local_debug as the third item
        return local_diffs, local_values, local_debug, True
    
    def audit_yaml_sidecars(self):
        
        # 1. Define the keys we care about
        keys_to_check = [
            "TakenAt", "TakenAtLocal", "TakenSrc", "Type", "Title", "TitleSrc", 
            "OriginalName", "TimeZone", "PlaceSrc", "Altitude", "Lat", "Lng", 
            "Duration", "CreatedBy"
        ]
        
        value_tracking_keys = ["TitleSrc", "TakenSrc", "PlaceSrc", "Type", "CreatedBy"]
        
        # 2. Setup our Statistical Counters
        diff_counters = Counter()
        value_counters = {k: Counter() for k in value_tracking_keys}
        master_debug_log = [] # Master debug collection list
        processed = 0
        
        try:
            logging.info("Scanning directories for matching YAMLs...")
            # Find all YAMLs in Travel
            travel_yamls = list(TRAVEL_SIDECAR.rglob("*.yml"))
            total_travel_yamls = len(travel_yamls)
            
            if total_travel_yamls == 0:
                logging.warning("No Travel sidecar YAMLS found, check your paths. Aborting.")
                sys.exit(1)
            
            logging.info(f"Found {total_travel_yamls} Travel YAML sidecars...")

            common_yamls = []
            
            # Match them against Master
            for t_path in travel_yamls:
                rel_path = t_path.relative_to(TRAVEL_SIDECAR)
                m_path = MASTER_SIDECAR / rel_path
                if m_path.exists():
                    common_yamls.append((m_path, t_path))
            
            total_common = len(common_yamls)

            logging.info(f"Found {total_common} common YAML sidecars...")
            
            if total_common == 0:
                logging.warning("No Common sidecar YAMLS found, check your paths. Aborting.")
                sys.exit(1)

            # Spin up the executor
            # 2 is 10% faster than 1
            # Using more than 2 is slower
            active_workers = 1 if (self.debug and (FORCEDEBUGJOB1 == 1)) else 2
            logging.info(f"Audit starting with {active_workers} workers")
            
            with ThreadPoolExecutor(max_workers=active_workers) as executor:
                # Submit all tasks to the pool
                futures = [
                    executor.submit(
                        self._yaml_worker, m_path, t_path, keys_to_check, value_tracking_keys
                    ) 
                    for m_path, t_path in common_yamls
                ]
                
                # Process them as they finish
                for future in as_completed(futures):
                    try:
                        # Unpack also the local_debug list
                        local_diffs, local_values, local_debug, success = future.result()
                        
                        if success:
                            processed += 1
                            # Merge the thread's local counters into our master counters
                            diff_counters.update(local_diffs)
                            for k in value_tracking_keys:
                                value_counters[k].update(local_values[k])
                            
                            # Safely merge the worker's debug findings
                            if local_debug:
                                master_debug_log.extend(local_debug)
                            
                            print(f"\r  Processed: {processed}/{total_common}...", end="", flush=True)
                            
                    except Exception as e:
                        logging.debug(f"Thread failed: {e}")
        
        except KeyboardInterrupt:
            logging.warning("\nCtrl-C detected! Halting Audit operations...")
        
        print("")
        # --- PRINT THE SUMMARY REPORT ---
        logging.info("========== YAML AUDIT SUMMARY ==========")
        logging.info(f"Total Sidecars Compared: {processed}")
        logging.info("----------------------------------------")
        logging.info("DIFFERENCES BY KEY:")
        
        if processed == 0:
            logging.warning("No files processed. Check your paths.")
            return
        
        # Force all checked keys into the counter so 0s print
        for key in keys_to_check:
            if key not in diff_counters:
                diff_counters[key] = 0
        
        # Sort keys by highest number of differences
        for key, count in diff_counters.most_common():
            percentage = (count / processed) * 100
            logging.info(f"  {key:<15}: {count:<6} ({percentage:.2f}%)")
        
        logging.info("----------------------------------------")
        logging.info("VALUE ECOSYSTEM (Occurrences):")
        
        for key in value_tracking_keys:
            logging.info(f"  [{key}]")
            if not value_counters[key]:
                logging.info("     (No values found)")
            else:
                for val, count in value_counters[key].most_common():
                    # NEW: Format empty strings as '' so they are visible
                    display_val = f"'{val}'" if val != "" else "''"
                    logging.info(f"     {display_val}: {count}")
        
        logging.info("========================================")
        # Print the Debug Log
        if master_debug_log:
            print("\n")
            logging.info("========== DEBUG INVESTIGATION LOG ==========")
            for entry in master_debug_log:
                logging.info(entry)
            logging.info("=============================================")
    
    def _yaml_worker_Single(self, m_path, keys_to_check, value_tracking_keys):
        """Worker thread that parses a YAML and returns the values."""
        local_populated = Counter()
        local_values = {k: Counter() for k in value_tracking_keys}
        local_debug = [] 
        
        try:
            with open(m_path, 'r', encoding='utf-8') as mf:
                m_data = yaml.load(mf, Loader=SafeLoader) or {}
        except Exception:
            return local_populated, local_values, local_debug, False
        
        for key in keys_to_check:
            m_val = self._normalize(m_data.get(key))
            
            # Count if the field actually contains data
            if m_val != "":
                local_populated[key] += 1
            
            # --- INVESTIGATION SANDBOX ---
            if key == "EditedAt" and m_val == "":
                # Grab UID directly from memory instead of hitting the disk again
                uid = m_data.get("UID", "NO_UID")
                #local_debug.append(f"{uid} [{key}] {m_path.name}: '{m_val}'")
            # -----------------------------
            if key == "CreatedBy" and m_val == "":
                # Grab UID directly from memory instead of hitting the disk again
                uid = m_data.get("UID", "NO_UID")
                #local_debug.append(f"{uid} [{key}] {m_path.name}: '{m_val}'")
            # -----------------------------
            
            if key in value_tracking_keys:
                if key == "EditedAt" and m_val != "":
                    # Group by Day: Slice 'YYYY-MM-DD' from the timestamp string
                    day_val = str(m_val)[:10]
                    local_values[key][day_val] += 1
                else:
                    local_values[key][str(m_val)] += 1
        
        return local_populated, local_values, local_debug, True
    
    def audit_yaml_sidecar_single(self, whatlib):
        
        keys_to_check = [
            "UID",
            "CreatedBy",
            "EditedAt",
            "TakenSrc", "TakenAt", "TakenAtLocal", "TimeZone", 
            "TitleSrc", "Title", 
            "Type", "OriginalName", 
            "PlaceSrc", "Lat", "Lng", "Altitude", 
            "Duration"
        ]
        
        value_tracking_keys = ["TitleSrc", "TakenSrc", "PlaceSrc", "Type", "CreatedBy", "EditedAt"]
        
        populated_counters = Counter()
        value_counters = {k: Counter() for k in value_tracking_keys}
        master_debug_log = [] 
        processed = 0
        
        try:
            logging.info("Scanning directory for YAMLs...")
            if whatlib == "Master":
                all_yamls = list(MASTER_SIDECAR.rglob("*.yml"))
            elif whatlib == "Travel":
                all_yamls = list(TRAVEL_SIDECAR.rglob("*.yml"))
            else:
                logging.warning(f"Unknown {whatlib} Library. Aborting.")
                return
            
            total_all = len(all_yamls)
            logging.info(f"Found {total_all} {whatlib} YAML sidecars...")
            if total_all == 0:
                logging.warning("No YAMLS found, check your paths. Aborting.")
                sys.exit(1)
            
            # Spin up the executor
            # 2 is 10% faster than 1
            # Using more than 2 is slower
            # Forces 1 when debug if enabled in ini
            active_workers = 1 if (self.debug and (FORCEDEBUGJOB1 == 1)) else 2
            logging.info(f"Audit starting with {active_workers} workers")
            
            with ThreadPoolExecutor(max_workers=active_workers) as executor:
                futures = [
                    executor.submit(
                        self._yaml_worker_Single, m_path, keys_to_check, value_tracking_keys
                    ) 
                    for m_path in all_yamls
                ]
                
                for future in as_completed(futures):
                    try:
                        local_populated, local_values, local_debug, success = future.result()
                        
                        if success:
                            processed += 1
                            populated_counters.update(local_populated)
                            
                            for k in value_tracking_keys:
                                value_counters[k].update(local_values[k])
                            
                            if local_debug:
                                master_debug_log.extend(local_debug)
                            
                            print(f"\r  Processed: {processed}/{total_all}...", end="", flush=True)
                    
                    except Exception as e:
                        logging.debug(f"Thread failed: {e}")
        
        except KeyboardInterrupt:
            logging.warning("\nCtrl-C detected! Halting Audit operations...")
        
        print("")
        logging.info(f"========== {whatlib} LIBRARY AUDIT SUMMARY ==========")
        logging.info(f"Total Sidecars Analyzed: {processed}")
        logging.info("----------------------------------------")
        
        if processed == 0:
            logging.warning("No files processed. Check your paths.")
            return
        
        logging.info("POPULATED FIELDS (Files containing data):")
        # Ensure all keys print, even if 0
        for key in keys_to_check:
            count = populated_counters.get(key, 0)
            percentage = (count / processed) * 100
            logging.info(f"  {key:<15}: {count:<6} ({percentage:.2f}%)")
        
        logging.info("----------------------------------------")
        logging.info("VALUE ECOSYSTEM (Occurrences):")
        
        for key in value_tracking_keys:
            logging.info(f"  [{key}]")
            if not value_counters[key]:
                logging.info("     (No values found)")
            else:
                # Apply different sorting logic based on the key
                if key == "EditedAt":
                    # Sort alphabetically descending (Newest dates first)
                    # Note: The empty string '' naturally sorts to the very bottom.
                    sorted_items = sorted(value_counters[key].items(), key=lambda item: item[0], reverse=True)
                else:
                    # Sort by frequency (Highest count first)
                    sorted_items = value_counters[key].most_common()
                
                for val, count in sorted_items:
                    display_val = f"'{val}'" if val != "" else "''"
                    logging.info(f"     {display_val}: {count}")
        
        logging.info("========================================")   
        
        if master_debug_log:
            print("\n")
            logging.info("========== DEBUG INVESTIGATION LOG ==========")
            for entry in master_debug_log:
                logging.info(entry)
            logging.info("=============================================")               
    
    # ===============================
    # ---------- album sync ----------
    
    def sync_albums(self):
        logging.info("TASK: Syncing Album Definitions...")
        
        master_albums = self.master.list_albums()
        travel_albums = self.travel.list_albums()
        
        # Create missing albums on Travel
        for title in master_albums:
            if title not in travel_albums:
                logging.info(f"Creating album in TRAVEL: '{title}'")
                if not self.dry_run:
                    self.travel.create_album(title)
        
        # Create missing albums on Master
        for title in travel_albums:
            if title not in master_albums:
                logging.info(f"Creating album in MASTER: '{title}'")
                if not self.dry_run:
                    self.master.create_album(title)
        
        logging.info("Album definitions sync complete")
    
    def sync_album_contents(self):
        """
        Synchronize album membership using an additive merge.
        
        This method does not remove photos from albums.
        It only adds missing items on each side.
        
        Rationale
        ---------
        PhotoPrism albums may be edited independently on both
        libraries, and destructive merges could cause data loss.
        
        Therefore the merge strategy is strictly additive.
        """
        
        logging.info("TASK: Syncing Album Contents (Additive Merge)...")
        
        valid_m_uids = set(self.state["master"].keys())
        valid_t_uids = set(self.state["travel"].keys())
        
        m_albums = self.master.list_albums()
        t_albums = self.travel.list_albums()
        
        common_titles = set(m_albums.keys()) & set(t_albums.keys())
        
        for title in common_titles:
            m_info = m_albums[title]
            t_info = t_albums[title]
            
            m_uid = m_info["UID"]
            t_uid = t_info["UID"]
            
            # CONTENTS SYNC 
            try:
                m_photos = set(self.master.list_album_photos(m_uid))
                t_photos = set(self.travel.list_album_photos(t_uid))
            except Exception as e:
                logging.error(f"ERROR scanning album '{title}':")
                logging.error(f"{e}")
                continue
            
            missing_in_travel = (m_photos & valid_t_uids) - t_photos
            missing_in_master = (t_photos & valid_m_uids) - m_photos
            
            if missing_in_travel:
                logging.info(f"Contents '{title}': M -> T ({len(missing_in_travel)} photos)")
                if not self.dry_run:
                    self.travel.add_photos_to_album(t_uid, list(missing_in_travel))
            
            if missing_in_master:
                logging.info(f"Contents '{title}': T -> M ({len(missing_in_master)} photos)")
                if not self.dry_run:
                    self.master.add_photos_to_album(m_uid, list(missing_in_master))
        
        logging.info("Album content sync complete")

# ===============================
# CREATE VIDEO ALBUM AND
# PUT ALL VIDEOS IN IT ON MASTER
# ===============================
def video_album():
    ALBUM_TITLE = "ALL_VIDEOS"
    logging.info(f"TASK: Creating/Updating '{ALBUM_TITLE}' Album in MASTER library...")
    
    # Use the new resilient client!
    master = PhotoprismClient(MASTER_URL, MASTER_PASSWORD, "MASTER")
    try:
        master.login()
    except Exception:
        return
    
    logging.info("Finding all videos in Master library...")
    r = master._request(
        "GET", "/api/v1/photos",
        params={"count": 100000000, "q": "type:video", "merged": "true"}
    )
    video_uids = [p["UID"] for p in r.json()]
    logging.info(f"Found {len(video_uids)} videos.")
    
    if not video_uids:
        logging.info("No videos found. Done.")
        return
    
    logging.info(f"Checking for Album '{ALBUM_TITLE}'...")
    albums = master.list_albums() 
    
    if ALBUM_TITLE in albums:
        target_album_uid = albums[ALBUM_TITLE]["UID"]
        logging.info(f"Found existing Album '{ALBUM_TITLE}' (UID: {target_album_uid})")
    else:
        logging.info(f"Creating Album '{ALBUM_TITLE}' (not found).")
        master.create_album(ALBUM_TITLE)
        # Refresh to get the new UID
        albums = master.list_albums()
        target_album_uid = albums[ALBUM_TITLE]["UID"]
        logging.info(f"Created album '{ALBUM_TITLE}' (UID: {target_album_uid})")
    
    logging.info(f"Adding/Updating videos in '{ALBUM_TITLE}'...")
    # Call the method properly through the 'master' object
    master.add_photos_to_album(target_album_uid, video_uids)
    
    logging.info("Done! Ready for Sync.")

# ===============================
# ---------- RUN TASKS ----------
# ===============================
def run_audit_API(args):
    logging.info("TASK: Audit Metadata Differences via API...")
    # TO WORK ON LATER: Integrity Audit: TRAVEL vs MASTER yaml Sidecars...
    # This audit below uses the same but simplified logic as sync_photos()
    
    # Pass the args into the sync initialization
    master = PhotoprismClient(MASTER_URL, MASTER_PASSWORD, "MASTER")
    travel = PhotoprismClient(TRAVEL_URL, TRAVEL_PASSWORD, "TRAVEL")
    
    try:
        travel.login()
        master.login()
    except requests.exceptions.RequestException:
        return
    
    # Initialize Engine with args
    engine = SyncEngine(
        master, travel
    )
    
    start = time.time()
    
    engine.audit_photos()
    
    duration = time.time() - start
    engine.auditSummary(duration)

def run_audit_yaml(args):    
    # We pass None for the API clients since this operates entirely on the disk
    engine = SyncEngine(None, None, dry_run=args.dry_run, debug=args.debug)
    
    start = time.time()    
    
    if args.diff:
        logging.info("===================================================================")   
        logging.info("TASK: Running Diff Audit...")
        engine.audit_yaml_sidecars()
    elif args.master:
        logging.info("===================================================================")   
        logging.info("TASK: Running Master Audit...")
        engine.audit_yaml_sidecar_single("Master")
    elif args.travel:
        logging.info("===================================================================")   
        logging.info("TASK: Running Travel Audit...")
        engine.audit_yaml_sidecar_single("Travel")
    else:
        logging.warning("One arg is needed before audit: --diff --travel --master")
        logging.warning("eg: python3 TravelSync.py --master audit")
        
    duration = time.time() - start
    logging.info(f"Audit completed in {round(duration, 2)} seconds.")

def run_Sync(args):
    logging.info("TASK: Sync with API...")
    # Pass the args into the sync initialization
    master = PhotoprismClient(MASTER_URL, MASTER_PASSWORD, "MASTER")
    travel = PhotoprismClient(TRAVEL_URL, TRAVEL_PASSWORD, "TRAVEL")
    
    try:
        travel.login()
        master.login()
    except requests.exceptions.RequestException:
        return
    
    # Initialize Engine with args
    engine = SyncEngine(
        master, travel,
        reset=False, # Add reset to args later if needed
        dry_run=args.dry_run,
        debug=args.debug
    )
    
    start = time.time()
    
    sync_everything = not args.photos and not args.albums
    # Execute Conditionally
    if args.photos or sync_everything:
        logging.info("TASK: Sync Photos with API...")
        engine.sync_photos()
    
    if args.albums or sync_everything:
        logging.info("TASK: Sync Albums with API...")
        engine.sync_albums()
        engine.sync_album_contents()
    
    duration = time.time() - start
    engine.syncSummary(duration)

# ===============================
# ---------- MAIN ---------------
# ===============================
def main():
    
    parser = argparse.ArgumentParser(description="PhotoPrism TravelSync Toolkit")
    
    # Global arguments applied to all commands
    parser.add_argument("--debug", action="store_true", help="Print verbose debug output; to keep it organized, forces JOBS=1")
    parser.add_argument("--dry-run", action="store_true", help="Simulate changes without modifying databases")
    parser.add_argument("--photos", action="store_true", help="Sync only Photos metadata (no Albums)")
    parser.add_argument("--albums", action="store_true", help="Sync only Albums definitions and content (no Photos metadata)")
    parser.add_argument("--diff", action="store_true", help="Audit Diff Master/Travel")
    parser.add_argument("--master", action="store_true", help="Audit only Master Library")
    parser.add_argument("--travel", action="store_true", help="Audit only Travel Library")
    
    subparsers = parser.add_subparsers(dest="command", required=True)
    
    subparsers.add_parser("audit", help="Analyze YAML sidecars to detect metadata drift")
    subparsers.add_parser("extract", help="Extract MASTER Library Primary Images Paths")
    subparsers.add_parser("resize", help="Resize images using Python (Windows/Linux fallback)")
    subparsers.add_parser("seed", help="Seeding YAMLs from MASTER Sidecar to TRAVEL Originals")
    subparsers.add_parser("sync", help="Run the bidirectional Photos metadata and Albums sync between MASTER and TRAVEL")
    subparsers.add_parser("video-album", help="Group all videos into an ALL_VIDEOS album on MASTER")
    
    args = parser.parse_args()
    
    setup_logging(debug=args.debug)
    logging.info("Hello!");
    
    # Secure the perimeter before doing anything else
    validate_config_sanity()
    
    # Pass args downstream
    if args.command == "audit": run_audit_yaml(args)
    elif args.command == "extract": extract_primaries()
    elif args.command == "resize": cross_platform_resize()
    elif args.command == "seed": seed_yamls() # You can pass args.debug here later if you add debug logs to seed_yamls
    elif args.command == "sync": run_Sync(args)
    elif args.command == "video-album": video_album()
    
    logging.info("GoodBye!");

if __name__ == "__main__":
    main()
