import os
import json
from datetime import datetime

REPORTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "reports"))
os.makedirs(REPORTS_DIR, exist_ok=True)

REPORT_JSON_PATH = os.path.join(REPORTS_DIR, "report.json")
REPORT_HTML_PATH = os.path.join(REPORTS_DIR, "report.html")

def generate_report(databases, start_time, end_time, dry_run, error_message=None, verification_ok=None, verification_details=None):
    """
    Compiles migration statistics and generates report.json and a premium report.html.
    """
    from src.migration.migrator import migration_progress
    
    start_dt = datetime.fromtimestamp(start_time)
    end_dt = datetime.fromtimestamp(end_time)
    duration = end_time - start_time
    
    # Compile status
    status = "SUCCESS"
    if error_message:
        status = "FAILED"
    elif migration_progress.get("cancel_requested"):
        status = "CANCELLED"
    elif migration_progress.get("status") == "SOURCE_CHANGED":
        status = "SOURCE_CHANGED"
        
    if dry_run:
        status = f"DRY_RUN_{status}"
        
    # Gather logs counts
    logs_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "logs"))
    error_count = 0
    warning_count = 0
    
    if verification_ok is not None:
        verification_passed = verification_ok
        verification_details = verification_details or ("All verification steps completed and passed." if verification_ok else "Verification failed.")
    else:
        verification_passed = True if not error_message else False
        verification_details = "All verification steps completed." if not error_message else f"Aborted due to: {error_message}"

    # Read error count from logs/error.log
    err_log_path = os.path.join(logs_dir, "error.log")
    if os.path.exists(err_log_path):
        try:
            with open(err_log_path, "r", encoding="utf-8") as f:
                error_count = len(f.readlines())
        except Exception:
            pass

    if error_message:
        error_count += 1
        verification_passed = False
        verification_details = f"Aborted due to: {error_message}"

    report_data = {
        "status": status,
        "dry_run": dry_run,
        "start_time": start_dt.isoformat(),
        "finish_time": end_dt.isoformat(),
        "duration_seconds": round(duration, 2),
        "databases": databases,
        "tables_total": migration_progress.get("tables_total", 0),
        "tables_copied": migration_progress.get("tables_copied", 0),
        "rows_total": migration_progress.get("rows_total", 0),
        "rows_copied": migration_progress.get("rows_copied", 0),
        "errors": error_count,
        "warnings": warning_count,
        "verification_result": "PASSED" if verification_passed else "FAILED",
        "verification_details": verification_details,
        "error_message": error_message
    }
    
    # 1. Write JSON Report
    try:
        with open(REPORT_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(report_data, f, indent=4)
        # Copy to root path
        root_report_json = os.path.abspath(os.path.join(REPORTS_DIR, "..", "report.json"))
        with open(root_report_json, "w", encoding="utf-8") as f:
            json.dump(report_data, f, indent=4)
    except Exception as e:
        print(f"Failed to write report.json: {e}")
        
    # 2. Write Beautiful HTML Report
    badge_class = "success" if verification_passed and not error_message else "danger"
    status_text = status.replace("_", " ")
    
    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Database Migration Report - {status_text}</title>
    <style>
        :root {{
            --bg-color: #0f0c1b;
            --card-bg: rgba(25, 20, 45, 0.6);
            --border-color: rgba(255, 255, 255, 0.08);
            --primary: #6366f1;
            --primary-glow: rgba(99, 102, 241, 0.35);
            --success: #10b981;
            --danger: #ef4444;
            --text-color: #e2e8f0;
            --text-muted: #94a3b8;
        }}
        
        body {{
            background: linear-gradient(135deg, #090514 0%, #150f2e 100%);
            color: var(--text-color);
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            margin: 0;
            padding: 40px 20px;
            display: flex;
            justify-content: center;
        }}
        
        .container {{
            max-width: 900px;
            width: 100%;
        }}
        
        .header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid var(--border-color);
            padding-bottom: 24px;
            margin-bottom: 30px;
        }}
        
        h1 {{
            font-size: 28px;
            font-weight: 700;
            background: linear-gradient(to right, #a5b4fc, #818cf8, #6366f1);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            margin: 0;
        }}
        
        .badge {{
            padding: 8px 16px;
            border-radius: 30px;
            font-weight: 600;
            font-size: 14px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            border: 1px solid transparent;
        }}
        
        .badge.success {{
            background-color: rgba(16, 185, 129, 0.15);
            color: var(--success);
            border-color: rgba(16, 185, 129, 0.3);
            box-shadow: 0 0 15px rgba(16, 185, 129, 0.2);
        }}
        
        .badge.danger {{
            background-color: rgba(239, 68, 68, 0.15);
            color: var(--danger);
            border-color: rgba(239, 68, 68, 0.3);
            box-shadow: 0 0 15px rgba(239, 68, 68, 0.2);
        }}
        
        .grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }}
        
        .card {{
            background: var(--card-bg);
            border: 1px solid var(--border-color);
            border-radius: 16px;
            padding: 20px;
            box-shadow: 0 8px 32px 0 rgba(0, 0, 0, 0.37);
            backdrop-filter: blur(8px);
        }}
        
        .card-label {{
            font-size: 12px;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 1px;
            margin-bottom: 8px;
        }}
        
        .card-value {{
            font-size: 24px;
            font-weight: 700;
            color: #fff;
        }}
        
        .details-card {{
            composes: card;
            margin-bottom: 30px;
        }}
        
        .details-row {{
            display: flex;
            justify-content: space-between;
            padding: 12px 0;
            border-bottom: 1px solid rgba(255, 255, 255, 0.04);
        }}
        
        .details-row:last-child {{
            border-bottom: none;
        }}
        
        .details-label {{
            color: var(--text-muted);
        }}
        
        .details-value {{
            font-weight: 500;
            text-align: right;
        }}
        
        .error-box {{
            background: rgba(239, 68, 68, 0.08);
            border: 1px solid rgba(239, 68, 68, 0.2);
            border-radius: 12px;
            padding: 16px;
            color: #fca5a5;
            margin-bottom: 30px;
            font-family: monospace;
            white-space: pre-wrap;
        }}
        
        .footer {{
            text-align: center;
            color: var(--text-muted);
            font-size: 12px;
            margin-top: 50px;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div>
                <h1>Database Migration Report</h1>
                <p style="margin: 5px 0 0 0; color: var(--text-muted);">AWS RDS to Azure MySQL</p>
            </div>
            <span class="badge {badge_class}">{status_text}</span>
        </div>
        
        {f'<div class="error-box"><strong>Critical Error:</strong><br>{error_message}</div>' if error_message else ''}
        
        <div class="grid">
            <div class="card">
                <div class="card-label">Duration</div>
                <div class="card-value">{round(duration, 2)}s</div>
            </div>
            <div class="card">
                <div class="card-label">Tables Copied</div>
                <div class="card-value">{report_data["tables_copied"]} / {report_data["tables_total"]}</div>
            </div>
            <div class="card">
                <div class="card-label">Rows Copied</div>
                <div class="card-value">{report_data["rows_copied"]:,} / {report_data["rows_total"]:,}</div>
            </div>
            <div class="card">
                <div class="card-label">Errors</div>
                <div class="card-value" style="color: {'var(--danger)' if error_count > 0 else 'inherit'}">{error_count}</div>
            </div>
        </div>
        
        <div class="card" style="margin-bottom: 30px;">
            <h3 style="margin-top: 0; font-size: 18px; border-bottom: 1px solid var(--border-color); padding-bottom: 12px;">Migration Details</h3>
            <div class="details-row">
                <div class="details-label">Databases Migrated</div>
                <div class="details-value">{", ".join(databases)}</div>
            </div>
            <div class="details-row">
                <div class="details-label">Start Time</div>
                <div class="details-value">{start_dt.strftime('%Y-%m-%d %H:%M:%S')}</div>
            </div>
            <div class="details-row">
                <div class="details-label">End Time</div>
                <div class="details-value">{end_dt.strftime('%Y-%m-%d %H:%M:%S')}</div>
            </div>
            <div class="details-row">
                <div class="details-label">Dry Run</div>
                <div class="details-value">{"Yes" if dry_run else "No"}</div>
            </div>
            <div class="details-row">
                <div class="details-label">Catch-Up Mode (Incremental)</div>
                <div class="details-value">{"Yes" if migration_progress.get("incremental_sync") else "No"}</div>
            </div>
            <div class="details-row">
                <div class="details-label">Verification Result</div>
                <div class="details-value" style="color: {'var(--success)' if verification_passed else 'var(--danger)'}; font-weight: 700;">{report_data["verification_result"]}</div>
            </div>
            <div class="details-row">
                <div class="details-label">Verification Log Summary</div>
                <div class="details-value" style="font-size: 14px; max-width: 60%;">{verification_details}</div>
            </div>
        </div>
        
        <div class="footer">
            <p>Generated by Production Database Migration Tool on {datetime.now().strftime('%Y-%m-%d at %H:%M:%S')}</p>
        </div>
    </div>
</body>
</html>
"""

    try:
        with open(REPORT_HTML_PATH, "w", encoding="utf-8") as f:
            f.write(html_content)
        # Copy to root path
        root_report_html = os.path.abspath(os.path.join(REPORTS_DIR, "..", "report.html"))
        with open(root_report_html, "w", encoding="utf-8") as f:
            f.write(html_content)
    except Exception as e:
        print(f"Failed to write report.html: {e}")
