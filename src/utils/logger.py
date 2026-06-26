import os
import time
import logging
from datetime import datetime
from queue import Queue

# Ensure logs directory exists
LOGS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "logs"))
os.makedirs(LOGS_DIR, exist_ok=True)

# Define file paths
MIGRATION_LOG_PATH = os.path.join(LOGS_DIR, "migration.log")
ERROR_LOG_PATH = os.path.join(LOGS_DIR, "error.log")
VERIFICATION_LOG_PATH = os.path.join(LOGS_DIR, "verification.log")
PERFORMANCE_LOG_PATH = os.path.join(LOGS_DIR, "performance.log")

# In-memory queue for SSE live log streaming to UI
live_log_queue = Queue(maxsize=1000)

def format_log_entry(db, table, op, duration, result, exception=None):
    """
    Formats the log entry as:
    [Timestamp] [Database] [Table] [Operation] [Duration] [Result] [Exception]
    """
    timestamp = datetime.now().isoformat()
    db_str = db if db else "-"
    table_str = table if table else "-"
    duration_str = f"{duration:.3f}s" if isinstance(duration, (int, float)) else str(duration)
    result_str = str(result)
    exc_str = str(exception).replace("\n", " ") if exception else "-"
    
    return f"[{timestamp}] [{db_str}] [{table_str}] [{op}] [{duration_str}] [{result_str}] [{exc_str}]"

def write_to_log_file(file_path, log_message):
    """Appends the formatted message to a log file."""
    try:
        with open(file_path, "a", encoding="utf-8") as f:
            f.write(log_message + "\n")
    except Exception as e:
        print(f"Failed to write to log file {file_path}: {e}")

def emit_log(log_type, db, table, op, duration, result, exception=None):
    """
    Logs an entry to the specific log file and duplicates it to the console and live queue.
    """
    msg = format_log_entry(db, table, op, duration, result, exception)
    
    # 1. Print to server console
    print(f"[{log_type.upper()}] {msg}")
    
    # 2. Write to specific file
    if log_type == "migration":
        write_to_log_file(MIGRATION_LOG_PATH, msg)
    elif log_type == "error":
        write_to_log_file(ERROR_LOG_PATH, msg)
        # All errors are also logged in migration.log
        write_to_log_file(MIGRATION_LOG_PATH, msg)
    elif log_type == "verification":
        write_to_log_file(VERIFICATION_LOG_PATH, msg)
    elif log_type == "performance":
        write_to_log_file(PERFORMANCE_LOG_PATH, msg)
        
    # 3. Add to live log queue for SSE
    if live_log_queue.full():
        try:
            live_log_queue.get_nowait()
        except Exception:
            pass
    live_log_queue.put_nowait(f"{log_type.upper()}: {msg}")

def log_migration(db, table, op, duration, result, exception=None):
    emit_log("migration", db, table, op, duration, result, exception)

def log_error(db, table, op, duration, result, exception=None):
    emit_log("error", db, table, op, duration, result, exception)

def log_verification(db, table, op, duration, result, exception=None):
    emit_log("verification", db, table, op, duration, result, exception)

def log_performance(db, table, op, duration, result, exception=None):
    emit_log("performance", db, table, op, duration, result, exception)
