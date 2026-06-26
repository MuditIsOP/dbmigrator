import os
import json
import time
from queue import Empty
from flask import Flask, jsonify, request, Response, send_from_directory
import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.services.state import load_profiles, add_profile, delete_profile, get_db_checkpoint
from src.services.db_client import test_connection, discover_databases
from src.migration.migrator import start_async_migration, migration_progress
from src.utils.logger import live_log_queue, log_migration

app = Flask(__name__)

# Determine paths for frontend resources
UI_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "ui"))
REPORTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "reports"))

# Serve the UI files
@app.route("/")
def serve_index():
    return send_from_directory(UI_DIR, "index.html")

@app.route("/<path:path>")
def serve_ui_files(path):
    # If file exists in UI_DIR, serve it
    if os.path.exists(os.path.join(UI_DIR, path)):
        return send_from_directory(UI_DIR, path)
    # Otherwise, fallback
    return jsonify({"error": "File not found"}), 404

# ==========================================
# Connection Profiles Endpoints
# ==========================================

@app.route("/api/profiles", methods=["GET"])
def get_all_profiles():
    profiles = load_profiles()
    # Mask passwords for UI security
    ui_profiles = {}
    for name, config in profiles.items():
        ui_profiles[name] = {
            "aws": {k: (v if k != "password" else "********") for k, v in config["aws"].items()},
            "azure": {k: (v if k != "password" else "********") for k, v in config["azure"].items()}
        }
    return jsonify(ui_profiles)

@app.route("/api/profiles", methods=["POST"])
def create_profile():
    data = request.json
    name = data.get("name")
    aws_config = data.get("aws")
    azure_config = data.get("azure")
    
    if not name or not aws_config or not azure_config:
        return jsonify({"error": "Missing profile name or configs"}), 400
        
    success = add_profile(name, aws_config, azure_config)
    if success:
        return jsonify({"message": "Profile created successfully"})
    else:
        return jsonify({"error": "Failed to save profile"}), 500

@app.route("/api/profiles/<name>", methods=["DELETE"])
def remove_profile(name):
    success = delete_profile(name)
    if success:
        return jsonify({"message": "Profile deleted successfully"})
    else:
        return jsonify({"error": "Profile not found or failed to delete"}), 404

# ==========================================
# Connection Verification & Discovery
# ==========================================

@app.route("/api/connect/test", methods=["POST"])
def test_conn():
    data = request.json
    config = data.get("config")
    profile_name = data.get("profile_name")
    target = data.get("target") # "aws" or "azure"
    
    # Resolve config if profile_name is specified
    if profile_name:
        profiles = load_profiles()
        profile = profiles.get(profile_name)
        if not profile:
            return jsonify({"success": False, "message": "Profile not found"}), 404
        config = profile.get(target)
        
    if not config:
        return jsonify({"success": False, "message": "No connection details provided"}), 400
        
    # If password is masked, reload original password from profile
    if config.get("password") == "********" and profile_name:
        original_profile = load_profiles().get(profile_name)
        if original_profile and target in original_profile:
            config["password"] = original_profile[target]["password"]

    success, message = test_connection(config)
    return jsonify({"success": success, "message": message})

@app.route("/api/databases", methods=["POST"])
def get_databases():
    data = request.json
    config = data.get("config")
    profile_name = data.get("profile_name")
    
    if profile_name:
        profiles = load_profiles()
        profile = profiles.get(profile_name)
        if not profile:
            return jsonify({"error": "Profile not found"}), 404
        config = profile.get("aws")
        
    if not config:
        return jsonify({"error": "No connection details provided"}), 400
        
    if config.get("password") == "********" and profile_name:
        original_profile = load_profiles().get(profile_name)
        if original_profile and "aws" in original_profile:
            config["password"] = original_profile["aws"]["password"]

    try:
        discovery = discover_databases(config)
        return jsonify(discovery)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ==========================================
# Migration Control Endpoints
# ==========================================

@app.route("/api/migrate/start", methods=["POST"])
def start_migration():
    if migration_progress["status"] == "RUNNING":
        return jsonify({"error": "A migration is already in progress"}), 400
        
    data = request.json
    profile_name = data.get("profile_name")
    databases = data.get("databases", [])
    dry_run = data.get("dry_run", False)
    resume = data.get("resume", False)
    batch_size = int(data.get("batch_size", 5000))
    confirm_overwrite = data.get("confirm_overwrite", False)
    
    if not profile_name:
        return jsonify({"error": "Profile name is required"}), 400
    if not databases:
        return jsonify({"error": "Select at least one database"}), 400
        
    profiles = load_profiles()
    profile = profiles.get(profile_name)
    if not profile:
        return jsonify({"error": "Profile not found"}), 404
        
    aws_config = profile["aws"]
    azure_config = profile["azure"]
    
    # Check if database already exists on Azure and warn if not confirmed
    if not dry_run and not confirm_overwrite:
        # Check if any database exists on Azure
        existing_dbs = []
        for db in databases:
            success, msg = test_connection(azure_config)
            if success:
                from src.services.db_client import get_connection
                conn = None
                try:
                    conn = get_connection(azure_config)
                    with conn.cursor() as cursor:
                        cursor.execute("SHOW DATABASES")
                        dbs = [r["Database"] for r in cursor.fetchall()]
                        if db in dbs:
                            existing_dbs.append(db)
                except Exception:
                    pass
                finally:
                    if conn:
                        conn.close()
        
        if existing_dbs:
            return jsonify({
                "warning": "database_exists",
                "message": f"The following database(s) already exist on Azure: {', '.join(existing_dbs)}. Do you want to proceed and overwrite existing tables?",
                "existing_databases": existing_dbs
            }), 200

    # Start migration in background
    log_migration(None, None, f"Starting migration for {', '.join(databases)} (Dry run: {dry_run})", 0, "START")
    start_async_migration(aws_config, azure_config, databases, dry_run, resume, batch_size)
    return jsonify({"message": "Migration started successfully"})

@app.route("/api/migrate/status", methods=["GET"])
def get_migration_status():
    return jsonify(migration_progress)

@app.route("/api/migrate/cancel", methods=["POST"])
def cancel_migration():
    if migration_progress["status"] != "RUNNING":
        return jsonify({"error": "No active migration to cancel"}), 400
        
    migration_progress["cancel_requested"] = True
    return jsonify({"message": "Cancellation request sent"})

@app.route("/api/migrate/resume-check/<db_name>", methods=["GET"])
def check_resume_status(db_name):
    checkpoint = get_db_checkpoint(db_name)
    has_checkpoint = len(checkpoint["completed_tables"]) > 0 or any(len(v) > 0 for v in checkpoint["completed_objects"].values())
    return jsonify({
        "has_checkpoint": has_checkpoint,
        "checkpoint": checkpoint
    })

# ==========================================
# Logs Streaming & Reports Endpoints
# ==========================================

@app.route("/api/logs")
def stream_logs():
    def event_stream():
        # Clear out queue initially to make room for active run logs
        while not live_log_queue.empty():
            try:
                live_log_queue.get_nowait()
            except Exception:
                break
                
        yield "data: LOGS_CONNECTED: Live logger connected\n\n"
        
        while True:
            try:
                log_msg = live_log_queue.get(timeout=1.0)
                yield f"data: {log_msg}\n\n"
            except Empty:
                # Keepalive heartbeat
                yield "data: :heartbeat\n\n"
            except Exception:
                break
                
    return Response(event_stream(), mimetype="text/event-stream")

@app.route("/api/reports", methods=["GET"])
def get_report():
    report_json = os.path.join(REPORTS_DIR, "report.json")
    if not os.path.exists(report_json):
        return jsonify({"error": "No report generated yet"}), 404
    try:
        with open(report_json, "r", encoding="utf-8") as f:
            return jsonify(json.load(f))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/reports/html", methods=["GET"])
def get_report_html():
    return send_from_directory(REPORTS_DIR, "report.html")

if __name__ == "__main__":
    # Start server on localhost:5000
    print("Starting SQL Migration server on http://localhost:5000")
    app.run(host="127.0.0.1", port=5000, debug=False)
