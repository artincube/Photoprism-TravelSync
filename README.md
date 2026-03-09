# TravelSync PhotoPrism

> A toolkit for creating a lightweight, portable clone of a PhotoPrism library using the official YAML sidecar format, with safe bidirectional metadata synchronization via the PhotoPrism REST API.

TravelSync is a robust, bidirectional Python CLI toolkit designed to synchronize metadata between two separate PhotoPrism instances: a high-resolution **Master** library (containing RAWs and large files) and a lightweight **Travel** clone (containing resized JPEGs for laptops or portable drives).

It safely mirrors your albums, favorites, titles, captions, timestamps and places across both servers without duplicating the heavy physical media. It only mirrors those, nothing else.

## Why This Project Exists

PhotoPrism is already an outstanding self-hosted photo management system.

TravelSync does **not attempt to replace or compete with PhotoPrism**.  
Instead, it explores a very specific workflow: maintaining a **portable clone library** that can travel offline while safely synchronizing metadata back to the master archive.

This project exists because:

- Photo libraries often grow to **multiple terabytes**
- Carrying RAW archives while traveling is impractical
- Remote exposure of a private photo archive is undesirable

TravelSync demonstrates that a **deterministic metadata synchronization layer** can be built entirely on top of the PhotoPrism API.

If any part of this approach is useful for the PhotoPrism ecosystem, the ideas are completely open for discussion or reuse.

## Table of Contents

- [Features](#features)
- [The Clone Library Concept](#the-clone-library-concept)
- [Installation and Setup](#installation-and-setup)
- [Docker Travel Library Setup](#setting-up-the-travel-library-docker)
- [Image Resizing Engines](#image-resizing-engines)
- [Usage and Workflow](#usage-and-workflow)
- [Audit and Inspection Tools](#audit-and-inspection-tools)
- [Sync Engine Internals](#sync-engine-internals)
- [Sync Decision Logic](#sync-decision-logic)
- [Smart Merge in Action (Test Cases)](#smart-merge-in-action-test-cases)
- [Limitations](#limitations)
- [Author's Note](#authors-note)

## Quick Architecture Overview

TravelSync relies on PhotoPrism's YAML sidecar format, which is normally used for metadata backup and restoration. By seeding these sidecars into the clone library before indexing, the Travel instance reconstructs photos using the same internal identifiers as the Master server.

```text
Master PhotoPrism Library
(Primary Images and Sidecar YAMLs)
        │
        │ extract + resize + seed
        ▼
Travel PhotoPrism Clone Library
(JPEG proxies)
        │
        │ metadata edits on either Master or Travel
        ▼
Travel PhotoPrism
        ▲
        │ TravelSync between Master and Travel
        ▼
Master PhotoPrism
(metadata merged safely)
```

### Features

- **Smart Bidirectional Sync**  
    TravelSync merges metadata changes intelligently using a deterministic conflict-resolution engine.  
    Manual edits always override auto-generated AI data, and true conflicts fall back to timestamp comparison.
    
- **State-Aware Sync Engine**  
    A local `travelsync_state.json` file tracks metadata timestamps and favorite states to avoid redundant API calls and prevent echo updates.
    
- **Resilient API Architecture**  
    All API communication uses retry wrappers with exponential backoff and failure detection to gracefully handle network interruptions or Docker restarts. While some syncs may be missed during these events, they will be done on the next run.
    
- **Concurrent Metadata Processing**  
    Sync operations parallelize metadata comparisons while preserving thread-safe state updates.
    
- **Hardware-Optimized Resizing**  
    Image resizing can be routed through Python (`pillow`), macOS native tools (`sips`), or (`ImageMagick`) to efficiently process large RAW libraries.
    
- **Album Mirroring**  
    Album definitions and contents are synchronized across both libraries using a safe additive merge strategy.
    
- **Multiple Safety Layers**  
    Sentinel files, configuration validation, and proxy-detection safeguards prevent accidental modifications.
    
- **Library Inspection Tools**  
    Built-in audit utilities analyze metadata drift and YAML sidecar files to help understand PhotoPrism behavior and library health.
    

* * *

## The "Clone Library" Concept

PhotoPrism is fantastic, but taking a multi-TB library of RAW and master files on the road with a laptop isn't practical. TravelSync solves this by maintaining an ultra-lightweight **Clone Library**.

For all intents and purposes, the "Travel" library is a perfect, miniaturized twin of your "Master" library.

1.  We extract your primary images and resize them into highly compressed JPEGs.
2.  We seed your Master's YAML sidecar files into the Travel Originals directory alongside the new JPEGs.
3.  When the Travel PhotoPrism indexes these JPEGs, it reads the seeded YAMLs and assigns the *exact same Database UIDs* to the photos and files, with the metadata PhotoPrism put in the backup YAMLs.

This allows you to take your entire library on the road. You can favorite photos, edit titles, write captions, and organize albums from a cafe or a tent. When you get home, TravelSync compares the lightweight clone against your Master server and surgically syncs only the metadata you changed. Any data manually edited on Master will be synced to Travel the same way in the same task.

* * *

## Installation and Setup

### 1\. Prerequisites

- Python 3.9+
- `requirements.txt`
- Two running PhotoPrism instances (Master and Travel)
- *(Optional)* ImageMagick installed on your system for resizing.

Clone the repository and install the required Python packages:

```bash
git clone [https://github.com/yourusername/PhotoPrism-TravelSync.git](https://github.com/yourusername/PhotoPrism-TravelSync.git)
cd PhotoPrism-TravelSync
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

Configure the toolkit by editing `TravelSync_config.example.ini` and rename it  `TravelSync_config.ini`.

### 2\. The Sentinel File (Safety Lock)

To protect your Master Originals directory from accidental modification due to a misconfigured path, TravelSync requires a physical "anchor" in your Travel clone (JPEGed Primaries) directory. **The script treats your Master Originals as strictly read-only**, accessing them only to retrieve primary images during the resize process, when these are not present in sidecar.

Navigate to your Travel Originals folder (e.g., `/Volumes/myTravelPath/smallOriginals`) and create an empty file named exactly:

`.travelsync_target`

Note: The script will refuse to run if it does not find this file at the `TRAVEL_ORIGINALS` path defined in your config.

**The Quarantine Safety Net**  
To further prevent accidental data loss, TravelSync employs a strict quarantine protocol when it detects that an image has been removed from the Master library:

- **Soft Deletion**: Files from Travel are never permanently deleted; they are moved to a local `TravelSync_Trash` directory.
    
- **Proxy Validation**: Before moving any file to the trash, the script verifies it is a generated proxy. It must have the double-extension created during the resize process: primary extension dot jpg (e.g., `.jpeg.jpg`, `.jpg.jpg`, or `.png.jpg`).
    
- **Hard Abort**: The script expects only JPEGs in the Travel directory. If it attempts to quarantine a RAW file, a video, or any file missing these proxy characteristics, it assumes a path configuration error and immediately aborts to protect your data.
    

### 3\. Setup & Authentication

To keep your Master library secure, TravelSync uses App Passwords and a hidden `.env` file. This ensures your main PhotoPrism administrator password is never saved in plain text.

**Step A: Generate App Passwords**

1.  Open your **Master** PhotoPrism GUI in your browser.
2.  Navigate to **Settings** > **Account**.
3.  Scroll down to **App Passwords** (or Personal Access Tokens).
4.  Create a new token named `TravelSync` and copy the generated password.
5.  Repeat this process on your **Travel** PhotoPrism instance.

**Step B: Create your `.env` File**  
In the root folder of this project, create a new text file named exactly `.env`. Paste your App Passwords into it:

```text
# .env
MASTER_APP_PASSWORD=your_master_generated_password_here
TRAVEL_APP_PASSWORD=your_travel_generated_password_here
```

**Step C: Configure `config.ini`**  
Rename the provided `TravelSync_config.example.ini` to `TravelSync_config.ini`. Open it and fill in your specific server URLs and directory full paths. Do not put your passwords in this file!

* * *

## Setting up the Travel Library (Docker)

Your Travel instance needs to run on a separate port so it doesn't conflict with your Master server. Here is a minimal `docker-compose.yml` example for your Travel environment.

The basic principles are to disable anything related to auto-generation as these will never be synced. Backup to YAML has to be enabled on both libraries.

While PhotoPrism's default **SQLite** database is perfectly fine and simple enough for a portable travel instance, using **MariaDB** is highly recommended if you want maximum stability and possibly parallel syncing.

```yaml
# docker-compose.yml (Travel Instance Recommended Configuration)
# This configuration is optimized for a lightweight, offline clone.
# It explicitly disables heavy AI indexing and RAW processing to save 
# laptop battery and prevent metadata conflicts with your Master archive.

services:
  photoprism:
    image: photoprism/photoprism:latest
    container_name: photoprism_travel
    depends_on:
      - mariadb
    stop_grace_period: 15s
    security_opt:
      - seccomp:unconfined
      - apparmor:unconfined
    ports:
      - "2343:2342" # Accessible at localhost:2343 (prevents port clashing)
    
    environment:
      PHOTOPRISM_USAGE_INFO: "true"
      PHOTOPRISM_INDEX_SCHEDULE: ""                # Disabled for manual control
      PHOTOPRISM_APP_NAME: "TRAVEL Library"
      
      # --- LIGHTWEIGHT & STATIC METADATA MODE ---
      # Disables heavy AI to save battery and prevent auto-tagging drift
      PHOTOPRISM_DISABLE_TENSORFLOW: "true"      
      PHOTOPRISM_DISABLE_FACES: "true"           
      PHOTOPRISM_DISABLE_CLASSIFICATION: "true"  
      PHOTOPRISM_DETECT_NSFW: "false"
      PHOTOPRISM_DISABLE_WEBDAV: "true"          
      PHOTOPRISM_DISABLE_PLACES: "false"           # Keep maps working
      PHOTOPRISM_DISABLE_EXIFTOOL: "true"
      
      # --- DISABLE RAW & HEAVY PROCESSORS ---
      # (Since Travel only uses pre-resized JPEGs)
      PHOTOPRISM_DISABLE_DARKTABLE: "true"
      PHOTOPRISM_DISABLE_RAWTHERAPEE: "true"
      PHOTOPRISM_DISABLE_IMAGEMAGICK: "true"

      # --- METADATA CONFIG ---
      PHOTOPRISM_SIDECAR_YAML: "true"              # REQUIRED for YAML Audit
      PHOTOPRISM_READONLY: "false"               
      PHOTOPRISM_DISABLE_BACKUPS: "false"        
      PHOTOPRISM_BACKUP_DATABASE: "false"
      
      # --- IMAGE SIZING ---
      PHOTOPRISM_JPEG_QUALITY: 55
      PHOTOPRISM_JPEG_SIZE: 1920
      PHOTOPRISM_PNG_SIZE: 1920
      PHOTOPRISM_THUMB_SIZE: 1920
      PHOTOPRISM_ORIGINALS_LIMIT: 500000           # MB
      
      # --- BASIC SETTINGS ---
      PHOTOPRISM_ADMIN_USER: "admin"               # Use the same as your Master, and update it also in the ini file
      PHOTOPRISM_ADMIN_PASSWORD: "travel_password" # Change this!
      PHOTOPRISM_SITE_URL: "http://localhost:2343/"
      PHOTOPRISM_SITE_TITLE: "TRAVEL Library"
      PHOTOPRISM_HTTP_COMPRESSION: "gzip"
      
      # --- MARIADB DATABASE CONFIG ---
      PHOTOPRISM_DATABASE_DRIVER: "mysql"
      PHOTOPRISM_DATABASE_SERVER: "mariadb:3306"
      PHOTOPRISM_DATABASE_NAME: "photoprism"
      PHOTOPRISM_DATABASE_USER: "photoprism"
      PHOTOPRISM_DATABASE_PASSWORD: "travel_db_password" # Must match MYSQL_PASSWORD
      
    volumes:
      # 1. Your generated JPEGs
      - "/Your/Travel/Drive/Originals:/photoprism/originals"
      # 2. Local storage for the travel database and cache
      - "./storage:/photoprism/storage"

  mariadb:
    image: mariadb:10.11
    container_name: photoprism_travel_db
    security_opt:
      - seccomp:unconfined
      - apparmor:unconfined
    # CRITICAL: Tuned max-connections and timeouts for concurrent API syncing
    command: mysqld --transaction-isolation=READ-COMMITTED --character-set-server=utf8mb4 --collation-server=utf8mb4_unicode_ci --max-connections=512 --innodb-rollback-on-timeout=OFF --innodb-lock-wait-timeout=120
    volumes:
      - "./database:/var/lib/mysql" 
    environment:
      MYSQL_ROOT_PASSWORD: "travel_root_password"
      MYSQL_DATABASE: "photoprism"
      MYSQL_USER: "photoprism"
      MYSQL_PASSWORD: "travel_db_password"
```

* * *

## Image Resizing Engines

TravelSync supports routing the resize operations to dedicated, hardware-optimized system tools.

**Supported Engines:**

- **`pillow` (Default):** The standard Python library. Great for standard JPEGs and smaller libraries. Requires no extra system installations.
- **`sips` (macOS Native):** A fast, zero-dependency option for Mac users. Highly recommended if you are running macOS, as it utilizes Apple's optimized Core Image framework.
- **`magick` (Cross-Platform):** Uses ImageMagick. The standard for batch processing. It handles virtually any RAW format and multi-gigabyte composite.

**1\. Installing Dependencies:**

- **`pillow`:** Already included when you run `pip install -r requirements.txt`.
- **`sips`:** Pre-installed on all macOS machines. No action required.
- **`magick`:** Requires ImageMagick to be installed on your system path.
    - **macOS:** `brew install imagemagick`
    - **Linux (Debian/Ubuntu):** `sudo apt-get install imagemagick`
    - **Windows:** Download from the official [ImageMagick website](https://imagemagick.org/script/download.php).

**2\. Configuring the Engine:**  
Open your `TravelSync_config.ini` file and adjust the `[RESIZE]` section.

```ini
[RESIZE]
MAX_SIZE = 1920
JPEG_QUALITY = 55
ENGINE = sips  # Options: pillow, magick, sips
RJOBS = 6      # Number of parallel CPU threads for resizing
```

* * *

## Usage and Workflow

To build your Travel library for the first time, run these commands sequentially:

1.  **`python travelsync.py extract`**  
    Scans the Master API and builds a manifest of all primary media files.
2.  **`python travelsync.py resize`**  
    Reads the manifest, finds the primaries, resizes the images using your chosen engine, and saves them to the Travel Originals. Skips files that already exist. No Libraries need to be online.
3.  **`python travelsync.py seed`**  
    Copies the YAML metadata files from Master Sidecar to Travel Originals. This ensures PhotoPrism retains the exact same internal UIDs when it indexes the new files. Skips files that already exist. No Libraries need to be online.
4.  **Index your Travel Library:** Open your Travel PhotoPrism GUI (cli: photoprism index) and run a library index.
5.  **`python travelsync.py sync`**  
    The core engine. Performs a bidirectional sync of metadata, creates missing albums, and updates album contents. Run this command anytime after you make edits on either libraries. The first time the command is used, it will take significantly more time as it builds a cache of Edited timestamps of all photos. Expect 20-30mn for 100k UIDs first run, 20-60s following runs (with adequate hardware and config).
6.  When Master library has items deleted and new items added, the first installation steps are just repeated: **`extract`**, **`resize`** and **`seed`**. In Travel: Existing jpegs and yamls are skipped, new are resized and seeded, missing from Master (deleted) are moved to the TravelSync trash directory (for the user to check and clean up). Then **Index your Travel Library** will need to be done so Travel mirrors the changes.

### Additional Commands

- **`python travelsync.py video-album`**: A helper utility that groups all video files on your Master server into an "ALL_VIDEOS" album for easier syncing.
- Use `python travelsync.py --help` for a full list of commands and global flags (like `--dry-run` and `--debug`).

## Audit and Inspection Tools

TravelSync includes several diagnostic tools designed to help understand how PhotoPrism evolves metadata over time.

These utilities were originally built to validate synchronization logic, but they also provide useful insights into the internal behavior of PhotoPrism's background workers. 

- **`python travelsync.py --diff audit`**: compares metadata between the two PhotoPrism servers using the same normalization logic as the sync engine.
- **`python travelsync.py --travel audit`**: reports metadata from Travel Library items.
- **`python travelsync.py --master audit`**: reports metadata from Master Library items.

TravelSync audit scans the YAML sidecar files generated by PhotoPrism.
It doesn't use the API and can be done offline (access to sidecar directories needed). So it is very fast, specially if these are on NVMe.

### Why This Exists

PhotoPrism performs many background operations that modify metadata over time (location enrichment, face recognition, AI-generated captions, etc.). The audit tools help distinguish between manual user edits and automatic system mutations. This insight is useful both for debugging synchronization logic and for understanding how PhotoPrism evolves metadata internally.

* * *

## Sync Engine Internals

TravelSync does not blindly overwrite metadata.  
Every potential change goes through a deterministic conflict-resolution pipeline.

1.  **The Fast Shield:** Before requesting full metadata, the engine compares the API `EditedAt` timestamps and favorite flags against a local state file created on first sync run: `travelsync_state.json`
2.  **Smart Diffing:** If a change is detected, it pulls the full metadata from both servers and normalizes the data (stripping trailing zeros from GPS coordinates, standardizing date strings) to prevent false-positive drifts.
3.  **Field-Level Isolation**: The engine decouples independent toggles (Favorites) and other edits, allowing it to merge complex, mixed-authority edits bidirectionally in edge case scenarii.
4.  **Authoritative Wins:** Manual user edits (`TitleSrc: manual` or `batch`) will always overwrite auto-generated data (`TitleSrc: anything or absent`), regardless of the timestamp. In the event of a true tie, it defaults to the most recently edited version.
5.  **Targeted Payload:** If it decides a sync is necessary, it constructs a dynamic payload. It *only* sends the specific fields that changed (e.g., just the `Title` and `TitleSrc`) and nothing else.

* * *

## Sync Decision Logic

The synchronization engine evaluates metadata differences through a deterministic  
decision pipeline. Each photo passes through the following steps to determine  
whether metadata should be pushed from **Master → Travel** or **Travel → Master**.

* * *

## Visual Flow of Sync Decision Logic

```text
START
│
├─ Detect diffs (ignore auto-only)
│
├─ No meaningful diffs?
│   └─ YES → RETURN None
│
├─ Isolate Favorite Toggle (State Tracker)
│   ├─ TRAVEL toggled → TtM (Favorite)
│   └─ MASTER toggled → MtT (Favorite)
│
├─ Evaluate Text/Location Fields Field-by-Field
│   ├─ Absolute Manual Authority?
│   │   ├─ MASTER is manual, TRAVEL is auto → MtT
│   │   └─ TRAVEL is manual, MASTER is auto → TtM
│   │
│   └─ Authority Tied? (Fallback to Timestamp)
│       ├─ MASTER newer → MtT
│       └─ TRAVEL newer → TtM
│
└─ Merge Payloads and Dispatch Sync

```

**Legend**
- **MtT** — Master → Travel update
- **TtM** — Travel → Master update

* * *

### Smart Merge in Action (Test Cases)

To understand how the conflict resolution engine makes decisions, here are four real-world edge cases. TravelSync evaluates metadata at the *field level*, decoupling independent toggles (like Favorites) from bulk text edits, and prioritizing human authority over database timestamps.

---
**Scenario 1: The Bidirectional Split**
- **Master Action:** Edits Caption
- **Travel Action:** Toggles Favorite
- **Engine Logic:** The engine recognizes Master's authority over the text fields, but verifies Travel's authority over the Favorite toggle via the local state tracker.
- **Sync Result (Split):** `M->T` (Caption) and `T->M` (Favorite)
  
---
**Scenario 2: The Timestamp Tie**
- **Master Action:** Manually edits Title (Older)
- **Travel Action:** Manually edits Title (Newer)
- **Engine Logic:** Since both edits are human/manual, authority is tied. The engine falls back to the `EditedAt` clock.
- **Sync Result:** `T->M` (Title)
  
---
**Scenario 3: The Authority Trap**
- **Travel Action:** Manually edits Title *(Older timestamp)*
- **Master Action:** Edits Notes *(Gets newer overall timestamp, but Title remains Auto-generated, and Notes is not synced by TravelSync)*
- **Engine Logic:** Master has a newer timestamp, but its Title is auto-generated. The engine ignores the clock and respects the human input on Travel.
- **Sync Result:** `T->M` (Title)
  
---
**Scenario 4: The Mixed Authority Merge**
- **Master Action:** Manually edits Title *(Older timestamp)*
- **Travel Action:** Manually edits Caption *(Newer timestamp)*
- **Engine Logic:** The engine evaluates field-by-field. Master has absolute manual authority over the Title, while Travel has absolute manual authority over the Caption. It ignores the overall timestamp discrepancy and respects human input on both sides.
- **Sync Result (Bidirectional):** `M->T` (Title) and `T->M` (Caption)

---

> **Performance Note:** In the background, the engine's "Fast Shield" scans metadata timestamps and state histories to safely skip unaltered photos. In benchmark testing, scanning 100,000 items, resolving conflicts, dispatching targeted API payloads, and updating local states takes roughly 20-60 seconds on a local network. The first sync run will take significantly more time as it builds the state file and get details for all photos via API.

* * *

## Limitations

- **Limited Metadata Synchronization:** Currently only Title, Caption, Lat, Lng and TakenAt are synchronized (and their corresponding Src fields), as well as Favorite. Synchronizing only top-level keys such as these is more straightforward than nested ones, such as Notes, and we wanted to keep it to a minimum but usefull package, taking no risks with PhotoPrism processes and behaviors we do not understand.
- **CTRL-C Interrupts:** Graceful shutdown via `KeyboardInterrupt` is currently optimized for the core photo synchronization loop, ensuring progress is saved to the JSON state file. Other parts of the toolkit may halt more abruptly.

* * *

## Author's Note

I originally built this out of personal need to manage my own library while on the road, where I do not want or can open my library to the internet. There are a lot of off-grid locations around where I live anyway.

In its early days, the script was essentially a series of "dirty hacks"—parsing YAMLs and injecting data directly into the SQLite/MariaDB databases via hundreds of raw `UPDATE` commands. I could not use xmps in Originals and index as many of my originals already had an imported one I did not want to overwrite.

While it seemed to work for the most basic, it was fundamentally a bad idea. As I began using the tool on a daily basis, I realized that relying on direct database injection was a recipe for eventual corruption. It was fast though. I decided to rebuild it from the ground up, shifting entirely to PhotoPrism's official REST API.

What started as a quick script turned into a bit of a maniacal endeavor to implement strong, organized logic, resilient error handling, and readable code. I had a lot of fun building this, and I learned a tremendous amount about API architectures and Python concurrency in the process, as well as playing with logic.

I hope this toolkit is genuinely helpful to other photoprismers and travelers, and perhaps the code itself serves as a curiosity or a learning tool for others. Contributions, forks, and improvements are always welcome!
