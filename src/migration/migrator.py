import os
import time
import re
import threading
from datetime import datetime
import pymysql
from src.services.db_client import get_connection
from src.services.state import get_db_checkpoint, update_table_checkpoint, update_object_checkpoint, clear_db_checkpoint
from src.utils.logger import log_migration, log_error, log_performance, log_verification
from src.reporting.reporter import generate_report

# Global dictionary to track active migration progress
migration_progress = {
    "status": "IDLE",            # IDLE, RUNNING, PAUSED, SUCCESS, FAILED, CANCELLED, SOURCE_CHANGED
    "database": "",
    "current_table": "",
    "tables_total": 0,
    "tables_copied": 0,
    "rows_total": 0,
    "rows_copied": 0,
    "current_table_rows_total": 0,
    "current_table_rows_copied": 0,
    "speed_bps": 0,             # Bytes per second
    "speed_rps": 0,             # Rows per second
    "eta_seconds": 0,
    "error_message": "",
    "dry_run": False,
    "cancel_requested": False
}

class MigrationCancelled(Exception):
    pass

def clean_sql_definer(sql):
    """Replaces DEFINER=`user`@`host` with DEFINER=CURRENT_USER to avoid Azure permission errors."""
    if not sql:
        return ""
    # Matches: DEFINER = `user`@`host` or DEFINER = user@host
    pattern = r'DEFINER\s*=\s*(?:\`[^\`]+\`|[^\s`@]+)@(?:\`[^\`]+\`|[^\s`@]+)'
    return re.sub(pattern, 'DEFINER = CURRENT_USER', sql, flags=re.IGNORECASE)

def strip_foreign_keys_from_ddl(ddl):
    """
    Strips CONSTRAINT ... FOREIGN KEY clauses from CREATE TABLE statement.
    Returns (cleaned_ddl, list_of_fk_alter_statements).
    """
    if not ddl:
        return "", []
    
    # We will find constraint foreign key lines.
    # Typical DDL has:
    #   CONSTRAINT `fk_name` FOREIGN KEY (`col`) REFERENCES `ref_table` (`ref_col`) ON DELETE ...
    fk_lines = []
    lines = ddl.split("\n")
    cleaned_lines = []
    
    # Extract table name to construct alter statement later
    table_match = re.search(r'CREATE\s+TABLE\s+(?:\`([^\`]+)\`|([^\s\(]+))', ddl, re.IGNORECASE)
    table_name = table_match.group(1) or table_match.group(2) if table_match else None
    
    for i, line in enumerate(lines):
        # Match lines with FOREIGN KEY
        if "FOREIGN KEY" in line and "CONSTRAINT" in line:
            # Keep track of the constraint line
            fk_lines.append(line.strip().rstrip(","))
        else:
            cleaned_lines.append(line)
            
    # Clean up trailing commas in table creation statement if constraint was the last item
    # Example:
    #   col1 int,
    #   col2 varchar(10),
    # ) ENGINE=InnoDB
    # If we removed the constraint at the end, we need to strip the comma after col2.
    
    # Reassemble cleaned lines
    cleaned_ddl = "\n".join(cleaned_lines)
    
    # Fix trailing comma before the closing parenthesis
    # Matches: , \s* )
    cleaned_ddl = re.sub(r',\s*\)', '\n)', cleaned_ddl, flags=re.IGNORECASE)
    
    fk_alters = []
    if table_name and fk_lines:
        for fk in fk_lines:
            # Build statement: ALTER TABLE `table_name` ADD CONSTRAINT ...
            # Make sure to remove leading/trailing spaces and trailing comma
            fk_statement = fk.strip().rstrip(",")
            fk_alters.append(f"ALTER TABLE `{table_name}` ADD {fk_statement}")
            
    return cleaned_ddl, fk_alters

def get_table_row_count(conn, db_name, table_name):
    """Gets row count for a table on the connection."""
    with conn.cursor() as cursor:
        cursor.execute(f"SELECT COUNT(*) as cnt FROM `{db_name}`.`{table_name}`")
        res = cursor.fetchone()
        return res["cnt"] if res else 0

def fetch_table_primary_key(conn, db_name, table_name):
    """Fetches primary key column name(s) for a table."""
    with conn.cursor() as cursor:
        cursor.execute("""
            SELECT COLUMN_NAME 
            FROM information_schema.KEY_COLUMN_USAGE 
            WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND CONSTRAINT_NAME = 'PRIMARY'
            ORDER BY ORDINAL_POSITION
        """, (db_name, table_name))
        rows = cursor.fetchall()
        return [r["COLUMN_NAME"] for r in rows]

def execute_with_retry(config, db_name, action_fn, max_retries=3, retry_delay=2):
    """Executes a database function with retry logic for transient connection drops."""
    conn = None
    for attempt in range(1, max_retries + 1):
        if migration_progress["cancel_requested"]:
            raise MigrationCancelled("Migration cancelled by user.")
        try:
            conn = get_connection(config, db_name)
            res = action_fn(conn)
            return res
        except (pymysql.MySQLError, ConnectionError) as e:
            if attempt == max_retries:
                raise e
            time.sleep(retry_delay)
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

def run_migration_process(aws_config, azure_config, databases, dry_run=False, resume=False, batch_size=5000):
    """
    Main migration runner. Connects, inspects, copies schemas, streams data,
    verifies, and logs progress.
    """
    global migration_progress
    migration_progress.update({
        "status": "RUNNING",
        "rows_total": 0,
        "rows_copied": 0,
        "tables_total": 0,
        "tables_copied": 0,
        "speed_bps": 0,
        "speed_rps": 0,
        "eta_seconds": 0,
        "error_message": "",
        "dry_run": dry_run,
        "cancel_requested": False
    })
    
    start_time = time.time()
    
    try:
        # Determine total tables and rows across all chosen databases for overall progress
        for db_name in databases:
            if migration_progress["cancel_requested"]:
                raise MigrationCancelled()
                
            def count_stats(conn):
                with conn.cursor() as cursor:
                    # Table count
                    cursor.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = %s AND table_type = 'BASE TABLE'", (db_name,))
                    tables = [r["table_name"] for r in cursor.fetchall()]
                    migration_progress["tables_total"] += len(tables)
                    
                    # Total row count
                    for table in tables:
                        cursor.execute(f"SELECT COUNT(*) as cnt FROM `{db_name}`.`{table}`")
                        migration_progress["rows_total"] += cursor.fetchone()["cnt"]
            
            execute_with_retry(aws_config, db_name, count_stats)

        # Run database by database
        for db_name in databases:
            migration_progress["database"] = db_name
            checkpoint = get_db_checkpoint(db_name) if resume else {"completed_tables": [], "completed_objects": {"views": [], "functions": [], "procedures": [], "triggers": [], "events": []}}
            
            # Step 1: Create Database on Azure
            op_start = time.time()
            # Fetch collation/charset from AWS
            def get_db_meta(conn):
                with conn.cursor() as cursor:
                    cursor.execute("SELECT default_character_set_name, default_collation_name FROM information_schema.schemata WHERE schema_name = %s", (db_name,))
                    return cursor.fetchone()
            
            db_meta = execute_with_retry(aws_config, None, get_db_meta)
            charset = db_meta["default_character_set_name"]
            collation = db_meta["default_collation_name"]
            
            create_db_sql = f"CREATE DATABASE IF NOT EXISTS `{db_name}` CHARACTER SET {charset} COLLATE {collation}"
            log_migration(db_name, None, "Create Database Statement Generate", 0, "SUCCESS")
            
            if not dry_run:
                def create_db_azure(conn):
                    with conn.cursor() as cursor:
                        cursor.execute(create_db_sql)
                execute_with_retry(azure_config, None, create_db_azure)
                log_migration(db_name, None, "Create Database on Azure", time.time() - op_start, "SUCCESS")
            else:
                log_migration(db_name, None, "[DRY RUN] Create Database on Azure", 0, "SUCCESS")

            # Discover objects on AWS RDS
            def get_db_objects(conn):
                with conn.cursor() as cursor:
                    # Tables
                    cursor.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = %s AND table_type = 'BASE TABLE'", (db_name,))
                    tables = [r["table_name"] for r in cursor.fetchall()]
                    # Views
                    cursor.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = %s AND table_type = 'VIEW'", (db_name,))
                    views = [r["table_name"] for r in cursor.fetchall()]
                    # Procedures
                    cursor.execute("SELECT routine_name FROM information_schema.routines WHERE routine_schema = %s AND routine_type = 'PROCEDURE'", (db_name,))
                    procedures = [r["routine_name"] for r in cursor.fetchall()]
                    # Functions
                    cursor.execute("SELECT routine_name FROM information_schema.routines WHERE routine_schema = %s AND routine_type = 'FUNCTION'", (db_name,))
                    functions = [r["routine_name"] for r in cursor.fetchall()]
                    # Triggers
                    cursor.execute("SELECT trigger_name FROM information_schema.triggers WHERE trigger_schema = %s", (db_name,))
                    triggers = [r["trigger_name"] for r in cursor.fetchall()]
                    # Events
                    cursor.execute("SELECT event_name FROM information_schema.events WHERE event_schema = %s", (db_name,))
                    events = [r["event_name"] for r in cursor.fetchall()]
                    
                    return tables, views, procedures, functions, triggers, events
            
            tables, views, procedures, functions, triggers, events = execute_with_retry(aws_config, db_name, get_db_objects)

            # Hold all foreign key alters to execute after tables and data are copied
            all_fk_alters = []

            # Step 2: Tables Schema Copy (and extract FKs)
            for table in tables:
                if migration_progress["cancel_requested"]:
                    raise MigrationCancelled()
                    
                if resume and table in checkpoint["completed_tables"]:
                    log_migration(db_name, table, "Skip Table Schema (Already Completed)", 0, "SKIPPED")
                    migration_progress["tables_copied"] += 1
                    continue
                
                migration_progress["current_table"] = table
                
                # Fetch AWS create DDL
                def get_table_ddl(conn):
                    with conn.cursor() as cursor:
                        cursor.execute(f"SHOW CREATE TABLE `{table}`")
                        return cursor.fetchone()["Create Table"]
                
                ddl = execute_with_retry(aws_config, db_name, get_table_ddl)
                
                # Strip definer (just in case) and extract foreign keys
                ddl = clean_sql_definer(ddl)
                clean_ddl, fk_alters = strip_foreign_keys_from_ddl(ddl)
                all_fk_alters.extend(fk_alters)
                
                log_migration(db_name, table, "Generate Clean Table DDL", 0, "SUCCESS")
                
                if not dry_run:
                    def create_table_azure(conn):
                        with conn.cursor() as cursor:
                            # Disable FK checks to safely create tables
                            cursor.execute("SET FOREIGN_KEY_CHECKS = 0")
                            cursor.execute(clean_ddl)
                    
                    op_start = time.time()
                    execute_with_retry(azure_config, db_name, create_table_azure)
                    log_migration(db_name, table, "Create Table Schema on Azure", time.time() - op_start, "SUCCESS")
                else:
                    log_migration(db_name, table, "[DRY RUN] Create Table Schema on Azure", 0, "SUCCESS")

            # Step 3: Copy Data in Batches
            for table in tables:
                if migration_progress["cancel_requested"]:
                    raise MigrationCancelled()
                    
                if resume and table in checkpoint["completed_tables"]:
                    # Row counts already factored
                    continue
                
                migration_progress["current_table"] = table
                
                # Fetch row count
                def get_count(conn):
                    return get_table_row_count(conn, db_name, table)
                
                row_count = execute_with_retry(aws_config, db_name, get_count)
                migration_progress["current_table_rows_total"] = row_count
                migration_progress["current_table_rows_copied"] = 0
                
                if row_count == 0:
                    log_migration(db_name, table, "Copy Table Data (Empty Table)", 0, "SUCCESS")
                    if not dry_run:
                        # Set auto_increment for empty tables
                        def get_auto_inc(conn):
                            with conn.cursor() as cursor:
                                cursor.execute("SELECT AUTO_INCREMENT FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s", (db_name, table))
                                return cursor.fetchone().get("AUTO_INCREMENT")
                        
                        auto_inc = execute_with_retry(aws_config, db_name, get_auto_inc)
                        if auto_inc:
                            def set_auto_inc(conn):
                                with conn.cursor() as cursor:
                                    cursor.execute(f"ALTER TABLE `{table}` AUTO_INCREMENT = {auto_inc}")
                            execute_with_retry(azure_config, db_name, set_auto_inc)
                            log_migration(db_name, table, f"Restore Auto Increment pointer to {auto_inc}", 0, "SUCCESS")
                            
                    update_table_checkpoint(db_name, table)
                    migration_progress["tables_copied"] += 1
                    continue
                
                # Determine primary key for paginated streaming
                def get_pk(conn):
                    return fetch_table_primary_key(conn, db_name, table)
                pks = execute_with_retry(aws_config, db_name, get_pk)
                
                # Copy loop
                offset = 0
                last_pk_values = None
                
                # Query building helper
                def build_select_query():
                    if pks:
                        pk_order = ", ".join([f"`{pk}`" for pk in pks])
                        if last_pk_values:
                            # Compound primary key condition support
                            # Example for (col1, col2) : WHERE col1 > val1 OR (col1 = val1 AND col2 > val2)
                            conds = []
                            for idx in range(len(pks)):
                                sub_conds = []
                                for prev_idx in range(idx):
                                    sub_conds.append(f"`{pks[prev_idx]}` = %s")
                                sub_conds.append(f"`{pks[idx]}` > %s")
                                conds.append("(" + " AND ".join(sub_conds) + ")")
                            
                            # Flatten values
                            params = []
                            for idx in range(len(pks)):
                                for prev_idx in range(idx):
                                    params.append(last_pk_values[prev_idx])
                                params.append(last_pk_values[idx])
                                
                            where_clause = " OR ".join(conds)
                            return f"SELECT * FROM `{table}` WHERE {where_clause} ORDER BY {pk_order} LIMIT {batch_size}", params
                        else:
                            return f"SELECT * FROM `{table}` ORDER BY {pk_order} LIMIT {batch_size}", []
                    else:
                        # Fallback for tables without PK
                        return f"SELECT * FROM `{table}` LIMIT {batch_size} OFFSET {offset}", []

                while offset < row_count:
                    if migration_progress["cancel_requested"]:
                        raise MigrationCancelled()
                        
                    select_sql, select_params = build_select_query()
                    
                    # Fetch batch from AWS
                    def fetch_batch(conn):
                        with conn.cursor() as cursor:
                            cursor.execute(select_sql, select_params)
                            return cursor.fetchall()
                    
                    batch_start = time.time()
                    rows = execute_with_retry(aws_config, db_name, fetch_batch)
                    fetch_duration = time.time() - batch_start
                    
                    if not rows:
                        break
                        
                    # Insert batch to Azure
                    if not dry_run:
                        def insert_batch(conn):
                            with conn.cursor() as cursor:
                                # Disable FK checks for data insertion speed & constraint bypass
                                cursor.execute("SET FOREIGN_KEY_CHECKS = 0")
                                # Build insert statement
                                columns = rows[0].keys()
                                col_names_str = ", ".join([f"`{c}`" for c in columns])
                                placeholders = ", ".join(["%s"] * len(columns))
                                insert_sql = f"INSERT INTO `{table}` ({col_names_str}) VALUES ({placeholders})"
                                
                                # Convert dictionaries to value tuples
                                insert_data = [tuple(row[col] for col in columns) for row in rows]
                                cursor.executemany(insert_sql, insert_data)
                        
                        insert_start = time.time()
                        execute_with_retry(azure_config, db_name, insert_batch)
                        insert_duration = time.time() - insert_start
                        
                        log_performance(db_name, table, "Batch Copy (Write)", insert_duration, f"Copied {len(rows)} rows")
                    else:
                        insert_duration = 0
                        log_migration(db_name, table, f"[DRY RUN] Batch Copy (Write)", 0, f"Simulated {len(rows)} rows")

                    # Log fetch speed
                    log_performance(db_name, table, "Batch Copy (Read)", fetch_duration, f"Fetched {len(rows)} rows")

                    # Update status
                    offset += len(rows)
                    migration_progress["rows_copied"] += len(rows)
                    migration_progress["current_table_rows_copied"] = offset
                    
                    # Calculate speeds
                    elapsed = time.time() - start_time
                    if elapsed > 0:
                        speed = migration_progress["rows_copied"] / elapsed
                        migration_progress["speed_rps"] = speed
                        remaining = migration_progress["rows_total"] - migration_progress["rows_copied"]
                        migration_progress["eta_seconds"] = remaining / speed if speed > 0 else 0
                        
                    # Keep track of last primary key values for the next query iteration
                    if pks:
                        last_row = rows[-1]
                        last_pk_values = [last_row[pk] for pk in pks]

                # Update Auto Increment Pointer on target (preserve original state!)
                if not dry_run:
                    def get_auto_inc(conn):
                        with conn.cursor() as cursor:
                            cursor.execute("SELECT AUTO_INCREMENT FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s", (db_name, table))
                            return cursor.fetchone().get("AUTO_INCREMENT")
                    
                    auto_inc = execute_with_retry(aws_config, db_name, get_auto_inc)
                    if auto_inc:
                        def set_auto_inc(conn):
                            with conn.cursor() as cursor:
                                cursor.execute(f"ALTER TABLE `{table}` AUTO_INCREMENT = {auto_inc}")
                        execute_with_retry(azure_config, db_name, set_auto_inc)
                        log_migration(db_name, table, f"Restore Auto Increment pointer to {auto_inc}", time.time() - op_start, "SUCCESS")

                # Verify table copy immediately for checkpoint safety
                if not dry_run:
                    def check_azure_count(conn):
                        return get_table_row_count(conn, db_name, table)
                    az_count = execute_with_retry(azure_config, db_name, check_azure_count)
                    
                    if az_count != row_count:
                        # Mismatch
                        err_msg = f"Row count mismatch! Source: {row_count}, Dest: {az_count}"
                        log_error(db_name, table, "Row Count Validation", 0, "FAILED", err_msg)
                        raise ValueError(f"Table data copy verification failed for table {table}: {err_msg}")
                    else:
                        log_verification(db_name, table, "Row Count Validation", 0, "SUCCESS")

                # Checkpoint saving
                update_table_checkpoint(db_name, table)
                migration_progress["tables_copied"] += 1
                log_migration(db_name, table, "Table Copy Completed & Verified", 0, "SUCCESS")

            # Step 4: Add Foreign Keys on Azure
            if all_fk_alters:
                log_migration(db_name, None, f"Adding {len(all_fk_alters)} Foreign Key constraints", 0, "START")
                if not dry_run:
                    for alter in all_fk_alters:
                        if migration_progress["cancel_requested"]:
                            raise MigrationCancelled()
                            
                        def run_alter(conn):
                            with conn.cursor() as cursor:
                                cursor.execute("SET FOREIGN_KEY_CHECKS = 0")
                                cursor.execute(alter)
                        
                        op_start = time.time()
                        execute_with_retry(azure_config, db_name, run_alter)
                        log_migration(db_name, None, f"Execute Alter: {alter[:60]}...", time.time() - op_start, "SUCCESS")
                else:
                    log_migration(db_name, None, "[DRY RUN] Foreign Keys applied", 0, "SUCCESS")

            # Step 5: Copy Views
            for view in views:
                if migration_progress["cancel_requested"]:
                    raise MigrationCancelled()
                if resume and view in checkpoint["completed_objects"]["views"]:
                    continue
                    
                def get_view_ddl(conn):
                    with conn.cursor() as cursor:
                        cursor.execute(f"SHOW CREATE VIEW `{view}`")
                        # Field is "Create View"
                        return cursor.fetchone()["Create View"]
                        
                view_ddl = execute_with_retry(aws_config, db_name, get_view_ddl)
                view_ddl = clean_sql_definer(view_ddl)
                
                if not dry_run:
                    def create_view(conn):
                        with conn.cursor() as cursor:
                            # Drop if exists in case of retry/overwrite
                            cursor.execute(f"DROP VIEW IF EXISTS `{view}`")
                            cursor.execute(view_ddl)
                    op_start = time.time()
                    execute_with_retry(azure_config, db_name, create_view)
                    log_migration(db_name, view, "Copy View", time.time() - op_start, "SUCCESS")
                else:
                    log_migration(db_name, view, "[DRY RUN] Copy View", 0, "SUCCESS")
                    
                update_object_checkpoint(db_name, "views", view)

            # Step 6: Copy Functions
            for func in functions:
                if migration_progress["cancel_requested"]:
                    raise MigrationCancelled()
                if resume and func in checkpoint["completed_objects"]["functions"]:
                    continue
                    
                def get_func_ddl(conn):
                    with conn.cursor() as cursor:
                        cursor.execute(f"SHOW CREATE FUNCTION `{func}`")
                        return cursor.fetchone()["Create Function"]
                
                func_ddl = execute_with_retry(aws_config, db_name, get_func_ddl)
                func_ddl = clean_sql_definer(func_ddl)
                
                if not dry_run:
                    def create_func(conn):
                        with conn.cursor() as cursor:
                            cursor.execute(f"DROP FUNCTION IF EXISTS `{func}`")
                            cursor.execute(func_ddl)
                    op_start = time.time()
                    execute_with_retry(azure_config, db_name, create_func)
                    log_migration(db_name, func, "Copy Function", time.time() - op_start, "SUCCESS")
                else:
                    log_migration(db_name, func, "[DRY RUN] Copy Function", 0, "SUCCESS")
                    
                update_object_checkpoint(db_name, "functions", func)

            # Step 7: Copy Procedures
            for proc in procedures:
                if migration_progress["cancel_requested"]:
                    raise MigrationCancelled()
                if resume and proc in checkpoint["completed_objects"]["procedures"]:
                    continue
                    
                def get_proc_ddl(conn):
                    with conn.cursor() as cursor:
                        cursor.execute(f"SHOW CREATE PROCEDURE `{proc}`")
                        return cursor.fetchone()["Create Procedure"]
                
                proc_ddl = execute_with_retry(aws_config, db_name, get_proc_ddl)
                proc_ddl = clean_sql_definer(proc_ddl)
                
                if not dry_run:
                    def create_proc(conn):
                        with conn.cursor() as cursor:
                            cursor.execute(f"DROP PROCEDURE IF EXISTS `{proc}`")
                            cursor.execute(proc_ddl)
                    op_start = time.time()
                    execute_with_retry(azure_config, db_name, create_proc)
                    log_migration(db_name, proc, "Copy Procedure", time.time() - op_start, "SUCCESS")
                else:
                    log_migration(db_name, proc, "[DRY RUN] Copy Procedure", 0, "SUCCESS")
                    
                update_object_checkpoint(db_name, "procedures", proc)

            # Step 8: Copy Triggers
            for trigger in triggers:
                if migration_progress["cancel_requested"]:
                    raise MigrationCancelled()
                if resume and trigger in checkpoint["completed_objects"]["triggers"]:
                    continue
                    
                def get_trigger_ddl(conn):
                    with conn.cursor() as cursor:
                        cursor.execute(f"SHOW CREATE TRIGGER `{trigger}`")
                        return cursor.fetchone()["SQL Original Statement"] # Or SQL Original Statement/SQL statement
                
                trigger_ddl = execute_with_retry(aws_config, db_name, get_trigger_ddl)
                trigger_ddl = clean_sql_definer(trigger_ddl)
                
                if not dry_run:
                    def create_trigger(conn):
                        with conn.cursor() as cursor:
                            cursor.execute(f"DROP TRIGGER IF EXISTS `{trigger}`")
                            cursor.execute(trigger_ddl)
                    op_start = time.time()
                    execute_with_retry(azure_config, db_name, create_trigger)
                    log_migration(db_name, trigger, "Copy Trigger", time.time() - op_start, "SUCCESS")
                else:
                    log_migration(db_name, trigger, "[DRY RUN] Copy Trigger", 0, "SUCCESS")
                    
                update_object_checkpoint(db_name, "triggers", trigger)

            # Step 9: Copy Events
            for event in events:
                if migration_progress["cancel_requested"]:
                    raise MigrationCancelled()
                if resume and event in checkpoint["completed_objects"]["events"]:
                    continue
                    
                def get_event_ddl(conn):
                    with conn.cursor() as cursor:
                        cursor.execute(f"SHOW CREATE EVENT `{event}`")
                        return cursor.fetchone()["Create Event"]
                
                event_ddl = execute_with_retry(aws_config, db_name, get_event_ddl)
                event_ddl = clean_sql_definer(event_ddl)
                
                if not dry_run:
                    def create_event(conn):
                        with conn.cursor() as cursor:
                            cursor.execute(f"DROP EVENT IF EXISTS `{event}`")
                            cursor.execute(event_ddl)
                    op_start = time.time()
                    execute_with_retry(azure_config, db_name, create_event)
                    log_migration(db_name, event, "Copy Event", time.time() - op_start, "SUCCESS")
                else:
                    log_migration(db_name, event, "[DRY RUN] Copy Event", 0, "SUCCESS")
                    
                update_object_checkpoint(db_name, "events", event)

            # Verify Database schema, counts, etc. using verifier
            if not dry_run:
                # Import verification here to avoid circular imports
                from src.verification.verifier import verify_database
                verify_start = time.time()
                verification_ok, ver_details = verify_database(aws_config, azure_config, db_name)
                
                log_verification(db_name, None, "Full Database Verification Run", time.time() - verify_start, "SUCCESS" if verification_ok else "FAILED")
                
                if not verification_ok:
                    raise ValueError(f"Post-migration verification failed for database {db_name}. Details: {ver_details[:300]}")
            else:
                log_verification(db_name, None, "[DRY RUN] Full Database Verification Run", 0, "SUCCESS")

            # Database migration done, clean checkpoints
            if not dry_run:
                clear_db_checkpoint(db_name)

        # Generate Reports
        duration_total = time.time() - start_time
        generate_report(databases, start_time, time.time(), dry_run, None)
        migration_progress["status"] = "SUCCESS"
        log_migration(None, None, "Migration Process Completed Successfully", duration_total, "SUCCESS")
        
    except MigrationCancelled as e:
        migration_progress["status"] = "CANCELLED"
        migration_progress["error_message"] = "Cancelled by user."
        log_migration(None, None, "Migration Process Cancelled", time.time() - start_time, "CANCELLED")
    except Exception as e:
        migration_progress["status"] = "FAILED"
        migration_progress["error_message"] = str(e)
        log_error(None, None, "Migration Process", time.time() - start_time, "FAILED", e)
        # Generate report with error
        generate_report(databases, start_time, time.time(), dry_run, str(e))

def start_async_migration(aws_config, azure_config, databases, dry_run=False, resume=False, batch_size=5000):
    """Starts the migration process in a background thread."""
    t = threading.Thread(
        target=run_migration_process,
        args=(aws_config, azure_config, databases, dry_run, resume, batch_size),
        daemon=True
    )
    t.start()
    return t
