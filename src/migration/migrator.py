import os
import time
import re
import threading
from datetime import datetime
import pymysql
from src.services.db_client import get_connection
from src.services.state import get_db_checkpoint, update_table_checkpoint, update_object_checkpoint, clear_db_checkpoint, update_checkpoint_meta
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
    """Strips DEFINER=... clauses completely to avoid Azure MySQL permission errors."""
    if not sql:
        return ""
    pattern = r'DEFINER\s*=\s*(?:\`[^\`]+\`|[^\s`@]+)@(?:\`[^\`]+\`|[^\s`@]+)|DEFINER\s*=\s*CURRENT_USER'
    return re.sub(pattern, '', sql, flags=re.IGNORECASE).strip()

def strip_foreign_keys_from_ddl(ddl):
    """
    Strips FOREIGN KEY clauses from CREATE TABLE statement regardless of whether CONSTRAINT keyword is present.
    Returns (cleaned_ddl, list_of_fk_alter_statements).
    """
    if not ddl:
        return "", []
    
    fk_lines = []
    lines = ddl.split("\n")
    cleaned_lines = []
    
    table_match = re.search(r'CREATE\s+TABLE\s+(?:\`([^\`]+)\`|([^\s\(]+))', ddl, re.IGNORECASE)
    table_name = table_match.group(1) or table_match.group(2) if table_match else None
    
    for i, line in enumerate(lines):
        if "FOREIGN KEY" in line.upper():
            fk_lines.append(line.strip().rstrip(","))
        else:
            cleaned_lines.append(line)
            
    cleaned_ddl = "\n".join(cleaned_lines)
    cleaned_ddl = re.sub(r',\s*\n\s*\)', '\n)', cleaned_ddl, flags=re.IGNORECASE)
    
    fk_alters = []
    if table_name and fk_lines:
        for fk in fk_lines:
            fk_statement = fk.strip().rstrip(",")
            if not fk_statement.upper().startswith("CONSTRAINT") and not fk_statement.upper().startswith("FOREIGN KEY"):
                fk_statement = f"FOREIGN KEY {fk_statement}"
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
            try:
                with conn.cursor() as _init_cur:
                    _init_cur.execute("SET SESSION sql_generate_invisible_primary_key = 0")
            except Exception:
                pass
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

def parse_mismatches(details):
    """Parses verification failure details into structured object lists for delta repairs."""
    failed_tables = set()
    failed_triggers = set()
    failed_views = set()
    failed_procedures = set()
    failed_functions = set()
    failed_events = set()
    
    if not details:
        return [], [], [], [], [], []
        
    match = re.search(r"Tables missing in Azure:\s*([^\n]+)", details, re.IGNORECASE)
    if match:
        tables = [t.strip() for t in match.group(1).split(",")]
        for t in tables:
            if t:
                failed_tables.add(t)
                
    table_pattern = r"Table\s+'([^']+)'\s+(?:row count mismatch|data checksum mismatch|columns mismatch|engine mismatch|collation mismatch|indexes mismatch|Foreign Keys mismatch)"
    matches = re.finditer(table_pattern, details, re.IGNORECASE)
    for m in matches:
        failed_tables.add(m.group(1))
        
    for obj_type in ["views", "procedures", "functions", "triggers", "events"]:
        pattern = rf"{obj_type.capitalize()}\s+mismatch\.\s+Missing:\s*\{{([^}}]+)\}}"
        obj_match = re.search(pattern, details, re.IGNORECASE)
        if obj_match:
            items = obj_match.group(1).split(",")
            for item in items:
                name = item.strip().strip("'").strip('"')
                if name and name != "set()":
                    if obj_type == "triggers":
                        failed_triggers.add(name)
                    elif obj_type == "views":
                        failed_views.add(name)
                    elif obj_type == "procedures":
                        failed_procedures.add(name)
                    elif obj_type == "functions":
                        failed_functions.add(name)
                    elif obj_type == "events":
                        failed_events.add(name)
                        
    return list(failed_tables), list(failed_triggers), list(failed_views), list(failed_procedures), list(failed_functions), list(failed_events)

def get_azure_max_pk(conn, db_name, table_name, pks):
    if not pks:
        return None
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT 1 FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s", (db_name, table_name))
            if not cursor.fetchone():
                return None
            if len(pks) == 1:
                pk = pks[0]
                cursor.execute(f"SELECT MAX(`{pk}`) as max_val FROM `{db_name}`.`{table_name}`")
                res = cursor.fetchone()
                return [res["max_val"]] if res and res["max_val"] is not None else None
            else:
                pk_order = ", ".join([f"`{pk}` DESC" for pk in pks])
                cursor.execute(f"SELECT {', '.join([f'`{pk}`' for pk in pks])} FROM `{db_name}`.`{table_name}` ORDER BY {pk_order} LIMIT 1")
                res = cursor.fetchone()
                return [res[pk] for pk in pks] if res else None
    except Exception:
        return None

def find_aws_matched_pk(aws_conn, az_conn, db_name, table, pks, aws_columns):
    if not pks:
        return None
    try:
        # 1. Get the last row on Azure
        pk_desc_order = ", ".join([f"`{pk}` DESC" for pk in pks])
        with az_conn.cursor() as az_cur:
            az_cur.execute(f"SELECT * FROM `{db_name}`.`{table}` ORDER BY {pk_desc_order} LIMIT 1")
            last_az_row = az_cur.fetchone()
            if not last_az_row:
                return None

        # 2. Find this row on AWS RDS
        match_cols = []
        for col_name, col_meta in aws_columns.items():
            if "auto_increment" in col_meta.get("extra", "").lower():
                continue
            d_type = col_meta.get("data_type", "").lower()
            if d_type in ("geometry", "point", "linestring", "polygon", "blob", "longblob", "mediumblob", "tinyblob", "json", "longtext"):
                continue
            if col_name in last_az_row:
                match_cols.append(col_name)

        if match_cols:
            conds = []
            params = []
            for col in match_cols:
                val = last_az_row[col]
                if val is None:
                    conds.append(f"`{col}` IS NULL")
                else:
                    conds.append(f"`{col}` = %s")
                    params.append(val)
                    
            with aws_conn.cursor() as aws_cur:
                aws_cur.execute(f"SELECT {', '.join([f'`{pk}`' for pk in pks])} FROM `{db_name}`.`{table}` WHERE {' AND '.join(conds)} LIMIT 1", params)
                res = aws_cur.fetchone()
                if res:
                    return [res[pk] for pk in pks]

        # Fallback: Match by PK columns directly
        conds = []
        params = []
        for pk in pks:
            val = last_az_row.get(pk)
            if val is None:
                conds.append(f"`{pk}` IS NULL")
            else:
                conds.append(f"`{pk}` = %s")
                params.append(val)
                
        with aws_conn.cursor() as aws_cur:
            aws_cur.execute(f"SELECT {', '.join([f'`{pk}`' for pk in pks])} FROM `{db_name}`.`{table}` WHERE {' AND '.join(conds)} LIMIT 1", params)
            res = aws_cur.fetchone()
            if res:
                return [res[pk] for pk in pks]
    except Exception as e:
        print(f"Error matching AWS PK for table {table}: {e}")
        return None
    return None

def sync_database_objects(aws_config, azure_config, db_name, tables, views, procedures, functions, triggers, events, dry_run, resume, batch_size, checkpoint, selected_tables, start_time, incremental_sync=False):
    """Internal helper to execute schema, data copy, and object replication for specified lists."""
    global migration_progress
    
    if incremental_sync:
        views = []
        procedures = []
        functions = []
        triggers = []
        events = []
        
    # Step 1: Create Database on Azure
    op_start = time.time()
    def get_db_meta(conn):
        with conn.cursor() as cursor:
            cursor.execute("SELECT default_character_set_name, default_collation_name FROM information_schema.schemata WHERE schema_name = %s", (db_name,))
            return cursor.fetchone()
    
    db_meta = execute_with_retry(aws_config, None, get_db_meta)
    charset = db_meta["default_character_set_name"]
    collation = db_meta["default_collation_name"]
    
    create_db_sql = f"CREATE DATABASE IF NOT EXISTS `{db_name}` CHARACTER SET {charset} COLLATE {collation}"
    
    if not dry_run:
        def create_db_azure(conn):
            with conn.cursor() as cursor:
                cursor.execute(create_db_sql)
        execute_with_retry(azure_config, None, create_db_azure)

    all_fk_alters = []

    # Step 2: Tables Schema Copy (and extract FKs)
    for table in tables:
        if migration_progress["cancel_requested"]:
            raise MigrationCancelled()
        
        def get_table_ddl(conn):
            with conn.cursor() as cursor:
                cursor.execute(f"SHOW CREATE TABLE `{table}`")
                return cursor.fetchone()["Create Table"]
        
        ddl = execute_with_retry(aws_config, db_name, get_table_ddl)
        ddl = clean_sql_definer(ddl)
        clean_ddl, fk_alters = strip_foreign_keys_from_ddl(ddl)
        all_fk_alters.extend(fk_alters)
        
        table_exists_on_azure = False
        if not dry_run:
            def check_table_exists(conn):
                with conn.cursor() as cursor:
                    cursor.execute("SELECT 1 FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s", (db_name, table))
                    return cursor.fetchone() is not None
            table_exists_on_azure = execute_with_retry(azure_config, db_name, check_table_exists)

        if resume and table in checkpoint.get("completed_tables", []):
            log_migration(db_name, table, "Skip Table Schema (Already Completed)", 0, "SKIPPED")
            migration_progress["tables_copied"] += 1
            continue
        
        if incremental_sync and table_exists_on_azure:
            log_migration(db_name, table, "Skip Table Schema Creation (Table Exists in Catch-Up Mode)", 0, "SUCCESS")
        else:
            migration_progress["current_table"] = table
            log_migration(db_name, table, "Generate Clean Table DDL", 0, "SUCCESS")
            
            if not dry_run:
                def create_table_azure(conn):
                    with conn.cursor() as cursor:
                        cursor.execute("SET FOREIGN_KEY_CHECKS = 0")
                        try:
                            cursor.execute("SET SESSION sql_generate_invisible_primary_key = 0")
                        except Exception:
                            pass
                        cursor.execute(f"DROP TABLE IF EXISTS `{table}`")
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
            
        if resume and table in checkpoint.get("completed_tables", []):
            continue
        
        migration_progress["current_table"] = table
        
        def get_pk(conn):
            return fetch_table_primary_key(conn, db_name, table)
        pks = execute_with_retry(aws_config, db_name, get_pk)
        
        if not pks:
            def get_fallback_pks(conn):
                with conn.cursor() as cursor:
                    # 1. Look for auto_increment column
                    cursor.execute("""
                        SELECT COLUMN_NAME 
                        FROM information_schema.COLUMNS 
                        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND EXTRA LIKE '%%auto_increment%%'
                    """, (db_name, table))
                    row = cursor.fetchone()
                    if row:
                        return [row["COLUMN_NAME"]]
                    
                    # 2. Look for unique key constraint columns
                    cursor.execute("""
                        SELECT COLUMN_NAME 
                        FROM information_schema.STATISTICS 
                        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND NON_UNIQUE = 0
                        ORDER BY INDEX_NAME, SEQ_IN_INDEX
                    """, (db_name, table))
                    rows = cursor.fetchall()
                    if rows:
                        return [r["COLUMN_NAME"] for r in rows]
                        
                    # 3. Look for column named 'id'
                    cursor.execute("""
                        SELECT COLUMN_NAME 
                        FROM information_schema.COLUMNS 
                        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND COLUMN_NAME = 'id'
                    """, (db_name, table))
                    if cursor.fetchone():
                        return ["id"]
                    return []
            pks = execute_with_retry(aws_config, db_name, get_fallback_pks)
        
        table_exists_on_azure = False
        if not dry_run:
            def check_table_exists(conn):
                with conn.cursor() as cursor:
                    cursor.execute("SELECT 1 FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s", (db_name, table))
                    return cursor.fetchone() is not None
            table_exists_on_azure = execute_with_retry(azure_config, db_name, check_table_exists)

        offset = 0
        last_pk_values = None
        
        if incremental_sync and table_exists_on_azure and pks:
            def get_matched_pk(conn):
                from src.services.db_client import get_connection
                aws_temp_conn = None
                try:
                    aws_temp_conn = get_connection(aws_config, db_name)
                    with aws_temp_conn.cursor() as cursor:
                        cursor.execute("SELECT COLUMN_NAME, DATA_TYPE, EXTRA FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s", (db_name, table))
                        cols = {}
                        for r in cursor.fetchall():
                            cols[r["COLUMN_NAME"]] = {
                                "data_type": r["DATA_TYPE"],
                                "extra": r["EXTRA"]
                            }
                    matched = find_aws_matched_pk(aws_temp_conn, conn, db_name, table, pks, cols)
                    if matched:
                        return matched
                except Exception as e:
                    print(f"Error resolving matched PK: {e}")
                finally:
                    if aws_temp_conn:
                        aws_temp_conn.close()
                return get_azure_max_pk(conn, db_name, table, pks)
                
            last_pk_values = execute_with_retry(azure_config, db_name, get_matched_pk)
            # Save the starting max PK for delta verifications
            if last_pk_values:
                update_checkpoint_meta(db_name, "starting_max_pks", table, last_pk_values)
                
            def get_delta_count(conn):
                with conn.cursor() as cursor:
                    if last_pk_values:
                        conds = []
                        for idx in range(len(pks)):
                            sub_conds = []
                            for prev_idx in range(idx):
                                sub_conds.append(f"`{pks[prev_idx]}` = %s")
                            sub_conds.append(f"`{pks[idx]}` > %s")
                            conds.append("(" + " AND ".join(sub_conds) + ")")
                        params = []
                        for idx in range(len(pks)):
                            for prev_idx in range(idx):
                                params.append(last_pk_values[prev_idx])
                            params.append(last_pk_values[idx])
                        where_clause = " OR ".join(conds)
                        cursor.execute(f"SELECT COUNT(*) as count_val FROM `{table}` WHERE {where_clause}", params)
                    else:
                        cursor.execute(f"SELECT COUNT(*) as count_val FROM `{table}`")
                    res = cursor.fetchone()
                    return res["count_val"] if res else 0
            row_count = execute_with_retry(aws_config, db_name, get_delta_count)
            log_migration(db_name, table, f"Catch-Up Mode active: starting after PK {last_pk_values} (delta rows to copy: {row_count})", 0, "SUCCESS")
        else:
            def get_count(conn):
                return get_table_row_count(conn, db_name, table)
            row_count = execute_with_retry(aws_config, db_name, get_count)
            
        migration_progress["current_table_rows_total"] = row_count
        migration_progress["current_table_rows_copied"] = 0
        
        if row_count == 0:
            log_migration(db_name, table, "Table is Up to Date (0 delta rows to copy)" if incremental_sync else "Copy Table Data (Empty Table)", 0, "SUCCESS")
            if not dry_run and not incremental_sync:
                def get_auto_inc(conn):
                    with conn.cursor() as cursor:
                        cursor.execute("SELECT AUTO_INCREMENT FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s", (db_name, table))
                        res = cursor.fetchone()
                        return res.get("AUTO_INCREMENT") if res else None
                
                auto_inc = execute_with_retry(aws_config, db_name, get_auto_inc)
                if auto_inc:
                    def set_auto_inc(conn):
                        with conn.cursor() as cursor:
                            cursor.execute(f"ALTER TABLE `{table}` AUTO_INCREMENT = {auto_inc}")
                    execute_with_retry(azure_config, db_name, set_auto_inc)
                    
            update_table_checkpoint(db_name, table)
            migration_progress["tables_copied"] += 1
            continue
        
        def build_select_query():
            if pks:
                pk_order = ", ".join([f"`{pk}`" for pk in pks])
                if last_pk_values:
                    conds = []
                    for idx in range(len(pks)):
                        sub_conds = []
                        for prev_idx in range(idx):
                            sub_conds.append(f"`{pks[prev_idx]}` = %s")
                        sub_conds.append(f"`{pks[idx]}` > %s")
                        conds.append("(" + " AND ".join(sub_conds) + ")")
                    
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
                return f"SELECT * FROM `{table}` LIMIT {batch_size} OFFSET {offset}", []

        while offset < row_count:
            if migration_progress["cancel_requested"]:
                raise MigrationCancelled()
                
            select_sql, select_params = build_select_query()
            
            def fetch_batch(conn):
                with conn.cursor() as cursor:
                    cursor.execute(select_sql, select_params)
                    return cursor.fetchall()
            
            batch_start = time.time()
            rows = execute_with_retry(aws_config, db_name, fetch_batch)
            fetch_duration = time.time() - batch_start
            
            if not rows:
                break
                
            if not dry_run:
                def insert_batch(conn):
                    with conn.cursor() as cursor:
                        cursor.execute("SET FOREIGN_KEY_CHECKS = 0")
                        cursor.execute("SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s", (db_name, table))
                        az_cols = {r["COLUMN_NAME"] for r in cursor.fetchall()}
                        if az_cols:
                            columns = [c for c in rows[0].keys() if c in az_cols]
                        else:
                            columns = list(rows[0].keys())
                            
                        col_names_str = ", ".join([f"`{c}`" for c in columns])
                        placeholders = ", ".join(["%s"] * len(columns))
                        insert_verb = "INSERT IGNORE" if incremental_sync else "INSERT"
                        insert_sql = f"{insert_verb} INTO `{table}` ({col_names_str}) VALUES ({placeholders})"
                        
                        insert_data = [tuple(row[col] for col in columns) for row in rows]
                        cursor.executemany(insert_sql, insert_data)
                        conn.commit()
                
                insert_start = time.time()
                execute_with_retry(azure_config, db_name, insert_batch)
                insert_duration = time.time() - insert_start
                log_performance(db_name, table, "Batch Copy (Write)", insert_duration, f"Copied {len(rows)} rows")
            else:
                insert_duration = 0
                log_migration(db_name, table, f"[DRY RUN] Batch Copy (Write)", 0, f"Simulated {len(rows)} rows")

            log_performance(db_name, table, "Batch Copy (Read)", fetch_duration, f"Fetched {len(rows)} rows")

            offset += len(rows)
            migration_progress["rows_copied"] += len(rows)
            migration_progress["current_table_rows_copied"] = offset
            
            elapsed = time.time() - start_time
            if elapsed > 0:
                rows_copied = int(migration_progress["rows_copied"])
                rows_total  = int(migration_progress["rows_total"])
                speed = rows_copied / elapsed
                migration_progress["speed_rps"] = speed
                remaining = max(rows_total - rows_copied, 0)
                migration_progress["eta_seconds"] = remaining / speed if speed > 0 else 0
                
            if pks:
                last_row = rows[-1]
                last_pk_values = [last_row[pk] for pk in pks]

        if not dry_run:
            def get_auto_inc(conn):
                with conn.cursor() as cursor:
                    cursor.execute("SELECT AUTO_INCREMENT FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s", (db_name, table))
                    res = cursor.fetchone()
                    return res.get("AUTO_INCREMENT") if res else None
            
            auto_inc = execute_with_retry(aws_config, db_name, get_auto_inc)
            if auto_inc:
                def set_auto_inc(conn):
                    with conn.cursor() as cursor:
                        cursor.execute(f"ALTER TABLE `{table}` AUTO_INCREMENT = {auto_inc}")
                execute_with_retry(azure_config, db_name, set_auto_inc)

            def analyze_table_azure(conn):
                with conn.cursor() as cursor:
                    cursor.execute(f"ANALYZE TABLE `{table}`")
            op_start = time.time()
            execute_with_retry(azure_config, db_name, analyze_table_azure)
            log_performance(db_name, table, "Analyze Table Statistics", time.time() - op_start, "SUCCESS")

        if not dry_run:
            def check_azure_count(conn):
                starting_max_pk = checkpoint.get("starting_max_pks", {}).get(table)
                if incremental_sync and starting_max_pk and pks:
                    with conn.cursor() as cursor:
                        conds = []
                        for idx in range(len(pks)):
                            sub_conds = []
                            for prev_idx in range(idx):
                                sub_conds.append(f"`{pks[prev_idx]}` = %s")
                            sub_conds.append(f"`{pks[idx]}` > %s")
                            conds.append("(" + " AND ".join(sub_conds) + ")")
                        params = []
                        for idx in range(len(pks)):
                            for prev_idx in range(idx):
                                params.append(starting_max_pk[prev_idx])
                            params.append(starting_max_pk[idx])
                        where_clause = " OR ".join(conds)
                        cursor.execute(f"SELECT COUNT(*) as count_val FROM `{table}` WHERE {where_clause}", params)
                        res = cursor.fetchone()
                        return res["count_val"] if res else 0
                else:
                    return get_table_row_count(conn, db_name, table)
            az_count = execute_with_retry(azure_config, db_name, check_azure_count)
            
            if az_count != row_count:
                err_msg = f"Row count mismatch! Source: {row_count}, Dest: {az_count}"
                log_error(db_name, table, "Row Count Validation", 0, "FAILED", err_msg)
                raise ValueError(f"Table data copy verification failed for table {table}: {err_msg}")
            else:
                log_verification(db_name, table, "Row Count Validation", 0, "SUCCESS")

        update_table_checkpoint(db_name, table)
        migration_progress["tables_copied"] += 1
        log_migration(db_name, table, "Table Copy Completed & Verified", 0, "SUCCESS")

    # Step 4: Add Foreign Keys on Azure
    if all_fk_alters:
        log_migration(db_name, None, f"Adding {len(all_fk_alters)} Foreign Key constraints", 0, "START")
        if not dry_run:
            existing_azure_fks = set()
            existing_azure_tables = set()
            def get_azure_meta(conn):
                with conn.cursor() as cursor:
                    cursor.execute("""
                        SELECT CONSTRAINT_NAME 
                        FROM information_schema.REFERENTIAL_CONSTRAINTS 
                        WHERE CONSTRAINT_SCHEMA = %s
                    """, (db_name,))
                    fks = [r["CONSTRAINT_NAME"] for r in cursor.fetchall()]
                    
                    cursor.execute("""
                        SELECT TABLE_NAME 
                        FROM information_schema.TABLES 
                        WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
                    """, (db_name,))
                    tbls = [r["TABLE_NAME"] for r in cursor.fetchall()]
                    return fks, tbls
            try:
                azure_fks, azure_tbls = execute_with_retry(azure_config, db_name, get_azure_meta)
                existing_azure_fks = set(azure_fks)
                existing_azure_fks_lower = {f.lower() for f in azure_fks}
                existing_azure_tables = set(azure_tbls)
                existing_azure_tables_lower = {t.lower() for t in azure_tbls}
            except Exception as e:
                log_migration(db_name, None, "Warning: Could not fetch existing Azure schema metadata", 0, "WARNING", e)

            for alter in all_fk_alters:
                if migration_progress["cancel_requested"]:
                    raise MigrationCancelled()
                    
                match = re.search(r'CONSTRAINT\s+(?:\`([^\`]+)\`|([a-zA-Z0-9_]+))', alter, re.IGNORECASE)
                fk_name = match.group(1) or match.group(2) if match else None
                
                if fk_name and (fk_name in existing_azure_fks or fk_name.lower() in existing_azure_fks_lower):
                    log_migration(db_name, None, f"Skip Foreign Key {fk_name} (Already Exists on Azure)", 0, "SKIPPED")
                    continue
                    
                ref_match = re.search(r'REFERENCES\s+(?:\`([^\`]+)\`|([a-zA-Z0-9_]+))', alter, re.IGNORECASE)
                ref_table = ref_match.group(1) or ref_match.group(2) if ref_match else None
                
                if ref_table:
                    if selected_tables and ref_table not in selected_tables and ref_table not in existing_azure_tables and ref_table.lower() not in existing_azure_tables_lower:
                        log_migration(db_name, None, f"Skip Foreign Key {fk_name} (Referenced table {ref_table} does not exist on Azure)", 0, "WARNING")
                        continue
                    
                def run_alter(conn):
                    with conn.cursor() as cursor:
                        cursor.execute("SET FOREIGN_KEY_CHECKS = 0")
                        cursor.execute(alter)
                
                op_start = time.time()
                try:
                    execute_with_retry(azure_config, db_name, run_alter)
                    log_migration(db_name, None, f"Execute Alter: {alter[:60]}...", time.time() - op_start, "SUCCESS")
                except Exception as ex:
                    log_migration(db_name, None, f"Warning: Foreign Key creation failed: {alter[:60]}... Details: {ex}", 0, "WARNING")

    # Step 5-9: Objects Copy
    for view in views:
        if migration_progress["cancel_requested"]:
            raise MigrationCancelled()
        def get_view_ddl(conn):
            with conn.cursor() as cursor:
                cursor.execute(f"SHOW CREATE VIEW `{view}`")
                return cursor.fetchone()["Create View"]
        try:
            view_ddl = clean_sql_definer(execute_with_retry(aws_config, db_name, get_view_ddl))
            if not dry_run:
                def create_view(conn):
                    with conn.cursor() as cursor:
                        cursor.execute(f"DROP VIEW IF EXISTS `{view}`")
                        cursor.execute(view_ddl)
                op_start = time.time()
                execute_with_retry(azure_config, db_name, create_view)
                log_migration(db_name, view, "Copy View", time.time() - op_start, "SUCCESS")
            update_object_checkpoint(db_name, "views", view)
        except Exception as ex:
            log_migration(db_name, view, f"Warning: View copy failed: {ex}", 0, "WARNING")

    for func in functions:
        if migration_progress["cancel_requested"]:
            raise MigrationCancelled()
        def get_func_ddl(conn):
            with conn.cursor() as cursor:
                cursor.execute(f"SHOW CREATE FUNCTION `{func}`")
                return cursor.fetchone()["Create Function"]
        try:
            func_ddl = clean_sql_definer(execute_with_retry(aws_config, db_name, get_func_ddl))
            if not dry_run:
                def create_func(conn):
                    with conn.cursor() as cursor:
                        cursor.execute(f"DROP FUNCTION IF EXISTS `{func}`")
                        cursor.execute(func_ddl)
                op_start = time.time()
                execute_with_retry(azure_config, db_name, create_func)
                log_migration(db_name, func, "Copy Function", time.time() - op_start, "SUCCESS")
            update_object_checkpoint(db_name, "functions", func)
        except Exception as ex:
            log_migration(db_name, func, f"Warning: Function copy failed: {ex}", 0, "WARNING")

    for proc in procedures:
        if migration_progress["cancel_requested"]:
            raise MigrationCancelled()
        def get_proc_ddl(conn):
            with conn.cursor() as cursor:
                cursor.execute(f"SHOW CREATE PROCEDURE `{proc}`")
                return cursor.fetchone()["Create Procedure"]
        try:
            proc_ddl = clean_sql_definer(execute_with_retry(aws_config, db_name, get_proc_ddl))
            if not dry_run:
                def create_proc(conn):
                    with conn.cursor() as cursor:
                        cursor.execute(f"DROP PROCEDURE IF EXISTS `{proc}`")
                        cursor.execute(proc_ddl)
                op_start = time.time()
                execute_with_retry(azure_config, db_name, create_proc)
                log_migration(db_name, proc, "Copy Procedure", time.time() - op_start, "SUCCESS")
            update_object_checkpoint(db_name, "procedures", proc)
        except Exception as ex:
            log_migration(db_name, proc, f"Warning: Procedure copy failed: {ex}", 0, "WARNING")

    for trigger in triggers:
        if migration_progress["cancel_requested"]:
            raise MigrationCancelled()
        def get_trigger_ddl(conn):
            with conn.cursor() as cursor:
                cursor.execute("""
                    SELECT TRIGGER_NAME, ACTION_TIMING, EVENT_MANIPULATION, EVENT_OBJECT_TABLE, ACTION_STATEMENT 
                    FROM information_schema.TRIGGERS 
                    WHERE TRIGGER_SCHEMA = %s AND TRIGGER_NAME = %s
                """, (db_name, trigger))
                r = cursor.fetchone()
                if r and r.get("ACTION_STATEMENT"):
                    timing = r["ACTION_TIMING"]
                    event = r["EVENT_MANIPULATION"]
                    tbl = r["EVENT_OBJECT_TABLE"]
                    stmt = r["ACTION_STATEMENT"]
                    return f"CREATE TRIGGER `{trigger}` {timing} {event} ON `{tbl}` FOR EACH ROW {stmt}"
                cursor.execute(f"SHOW CREATE TRIGGER `{trigger}`")
                res = cursor.fetchone()
                if res:
                    for k, v in res.items():
                        if isinstance(v, str) and "TRIGGER" in v.upper():
                            return v
                return ""
        try:
            trigger_ddl = clean_sql_definer(execute_with_retry(aws_config, db_name, get_trigger_ddl))
            if not dry_run:
                def create_trigger(conn):
                    with conn.cursor() as cursor:
                        cursor.execute(f"DROP TRIGGER IF EXISTS `{trigger}`")
                        cursor.execute(trigger_ddl)
                op_start = time.time()
                execute_with_retry(azure_config, db_name, create_trigger)
                log_migration(db_name, trigger, "Copy Trigger", time.time() - op_start, "SUCCESS")
            update_object_checkpoint(db_name, "triggers", trigger)
        except Exception as ex:
            log_migration(db_name, trigger, f"Warning: Trigger copy failed: {ex}", 0, "WARNING")

    for event in events:
        if migration_progress["cancel_requested"]:
            raise MigrationCancelled()
        def get_event_ddl(conn):
            with conn.cursor() as cursor:
                cursor.execute(f"SHOW CREATE EVENT `{event}`")
                return cursor.fetchone()["Create Event"]
        try:
            event_ddl = clean_sql_definer(execute_with_retry(aws_config, db_name, get_event_ddl))
            if not dry_run:
                def create_event(conn):
                    with conn.cursor() as cursor:
                        cursor.execute(f"DROP EVENT IF EXISTS `{event}`")
                        cursor.execute(event_ddl)
                op_start = time.time()
                execute_with_retry(azure_config, db_name, create_event)
                log_migration(db_name, event, "Copy Event", time.time() - op_start, "SUCCESS")
            update_object_checkpoint(db_name, "events", event)
        except Exception as ex:
            log_migration(db_name, event, f"Warning: Event copy failed: {ex}", 0, "WARNING")

def run_migration_process(aws_config, azure_config, databases, dry_run=False, resume=False, batch_size=5000, verify_only=False, fix_mismatches=False, exclude_directus=True, incremental_sync=False):
    """
    Main migration runner. Connects, inspects, copies schemas, streams data,
    verifies, and logs progress. Supports Auto-Healing sync loop.
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
        "cancel_requested": False,
        "incremental_sync": incremental_sync
    })
    
    start_time = time.time()
    
    try:
        for db_name, selected_tables in databases.items():
            if migration_progress["cancel_requested"]:
                raise MigrationCancelled()
                
            def count_stats(conn):
                with conn.cursor() as cursor:
                    cursor.execute(
                        """SELECT table_name, COALESCE(table_rows, 0) AS row_estimate
                           FROM information_schema.tables
                           WHERE table_schema = %s AND table_type = 'BASE TABLE'""",
                        (db_name,)
                    )
                    rows = cursor.fetchall()
                    if exclude_directus:
                        rows = [r for r in rows if not r["table_name"].lower().startswith("directus_")]
                    if selected_tables:
                        filtered_rows = [r for r in rows if r["table_name"] in selected_tables]
                        migration_progress["tables_total"] += len(filtered_rows)
                        migration_progress["rows_total"] += sum(int(r["row_estimate"]) for r in filtered_rows)
                    else:
                        migration_progress["tables_total"] += len(rows)
                        migration_progress["rows_total"] += sum(int(r["row_estimate"]) for r in rows)
            
            execute_with_retry(aws_config, db_name, count_stats)

        for db_name in databases.keys():
            migration_progress["database"] = db_name
            selected_tables = databases[db_name]
            checkpoint = get_db_checkpoint(db_name) if resume else {"completed_tables": [], "completed_objects": {"views": [], "functions": [], "procedures": [], "triggers": [], "events": []}}
            
            def get_db_objects(conn):
                with conn.cursor() as cursor:
                    cursor.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = %s AND table_type = 'BASE TABLE'", (db_name,))
                    tables = [r["table_name"] for r in cursor.fetchall()]
                    cursor.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = %s AND table_type = 'VIEW'", (db_name,))
                    views = [r["table_name"] for r in cursor.fetchall()]
                    cursor.execute("SELECT routine_name FROM information_schema.routines WHERE routine_schema = %s AND routine_type = 'PROCEDURE'", (db_name,))
                    procedures = [r["routine_name"] for r in cursor.fetchall()]
                    cursor.execute("SELECT routine_name FROM information_schema.routines WHERE routine_schema = %s AND routine_type = 'FUNCTION'", (db_name,))
                    functions = [r["routine_name"] for r in cursor.fetchall()]
                    cursor.execute("SELECT trigger_name, event_object_table FROM information_schema.triggers WHERE trigger_schema = %s", (db_name,))
                    triggers_meta = [(r["trigger_name"], r["event_object_table"]) for r in cursor.fetchall()]
                    cursor.execute("SELECT event_name FROM information_schema.events WHERE event_schema = %s", (db_name,))
                    events = [r["event_name"] for r in cursor.fetchall()]
                    return tables, views, procedures, functions, triggers_meta, events
            
            tables, views, procedures, functions, triggers_meta, events = execute_with_retry(aws_config, db_name, get_db_objects)

            if exclude_directus:
                tables = [t for t in tables if not t.lower().startswith("directus_")]
                triggers_meta = [t for t in triggers_meta if not t[1].lower().startswith("directus_")]

            if selected_tables:
                tables = [t for t in tables if t in selected_tables]
                triggers = [t[0] for t in triggers_meta if t[1] in selected_tables]
            else:
                triggers = [t[0] for t in triggers_meta]

            verification_ok = True
            ver_details = "All verification steps completed."

            if verify_only:
                log_migration(db_name, None, "Verify Only mode active. Skipping schema creation and data copying.", 0, "SUCCESS")
            elif fix_mismatches:
                log_migration(db_name, None, "Fix Mismatches (Auto-Healing Loop) active.", 0, "START")
                attempt = 0
                max_attempts = 3
                object_retries = {}
                persistent_failures = set()
                
                from src.verification.verifier import verify_database
                
                while attempt < max_attempts:
                    attempt += 1
                    log_migration(db_name, None, f"--- Starting Auto-Healing Audit Loop (Attempt {attempt}/{max_attempts}) ---", 0, "START")
                    
                    audit_ok, audit_details = verify_database(aws_config, azure_config, db_name, selected_tables, exclude_directus)
                    verification_ok, ver_details = audit_ok, audit_details
                    
                    if audit_ok:
                        log_migration(db_name, None, f"All database objects match 100% on Attempt {attempt}! Delta sync completed.", 0, "SUCCESS")
                        if not dry_run:
                            clear_db_checkpoint(db_name)
                        break
                    
                    failed_tbls, failed_trigs, failed_vws, failed_procs, failed_funcs, failed_evts = parse_mismatches(audit_details)
                    
                    current_failed = set(failed_tbls + failed_trigs + failed_vws + failed_procs + failed_funcs + failed_evts)
                    for obj in current_failed:
                        object_retries[obj] = object_retries.get(obj, 0) + 1
                        if object_retries[obj] >= max_attempts:
                            persistent_failures.add(obj)
                            
                    active_tbls = [t for t in failed_tbls if t in tables and t not in persistent_failures]
                    active_trigs = [t for t in failed_trigs if t in triggers and t not in persistent_failures]
                    active_vws = [v for v in failed_vws if v in views and v not in persistent_failures]
                    active_procs = [p for p in failed_procs if p in procedures and p not in persistent_failures]
                    active_funcs = [f for f in failed_funcs if f in functions and f not in persistent_failures]
                    active_evts = [e for e in failed_evts if e in events and e not in persistent_failures]
                    
                    if persistent_failures:
                        log_migration(db_name, None, f"Warning: The following objects persistently failed after {max_attempts} retries and will be reported: {', '.join(persistent_failures)}", 0, "WARNING")
                        
                    if not (active_tbls or active_trigs or active_vws or active_procs or active_funcs or active_evts):
                        log_migration(db_name, None, "No further retryable delta objects remaining to sync.", 0, "WARNING")
                        break
                        
                    log_migration(db_name, None, f"Syncing delta: {len(active_tbls)} tables, {len(active_trigs)} triggers...", 0, "START")
                    for t in active_tbls:
                        if t in checkpoint["completed_tables"]:
                            checkpoint["completed_tables"].remove(t)
                    sync_database_objects(aws_config, azure_config, db_name, active_tbls, active_vws, active_procs, active_funcs, active_trigs, active_evts, dry_run, resume, batch_size, checkpoint, selected_tables, start_time, incremental_sync)

            else:
                # Standard Migration Pass
                sync_database_objects(aws_config, azure_config, db_name, tables, views, procedures, functions, triggers, events, dry_run, resume, batch_size, checkpoint, selected_tables, start_time, incremental_sync)

            # Final Verification Pass for standard or verify_only runs
            if not dry_run and not fix_mismatches:
                from src.verification.verifier import verify_database
                verify_start = time.time()
                starting_max_pks = checkpoint.get("starting_max_pks") if checkpoint else None
                verification_ok, ver_details = verify_database(aws_config, azure_config, db_name, selected_tables, exclude_directus, incremental_sync, starting_max_pks)
                log_verification(db_name, None, "Full Database Verification Run", time.time() - verify_start, "SUCCESS" if verification_ok else "FAILED")
                
                if not verification_ok:
                    raise ValueError(f"Post-migration verification failed for database {db_name}. Details: {ver_details[:300]}")
            
            if not dry_run and not verify_only and verification_ok:
                clear_db_checkpoint(db_name)

        duration_total = time.time() - start_time
        generate_report(databases, start_time, time.time(), dry_run, None, verification_ok, ver_details)
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
        generate_report(databases, start_time, time.time(), dry_run, str(e), False, f"Aborted due to error: {e}")

def start_async_migration(aws_config, azure_config, databases, dry_run=False, resume=False, batch_size=5000, verify_only=False, fix_mismatches=False, exclude_directus=True, incremental_sync=False):
    """Starts the migration process in a background thread."""
    t = threading.Thread(
        target=run_migration_process,
        args=(aws_config, azure_config, databases, dry_run, resume, batch_size, verify_only, fix_mismatches, exclude_directus, incremental_sync),
        daemon=True
    )
    t.start()
    return t
