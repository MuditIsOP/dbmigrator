import time
import pymysql
from src.services.db_client import get_connection
from src.utils.logger import log_verification, log_error

def get_db_metadata(conn, db_name):
    """
    Gathers detailed metadata about the tables, columns, indexes, FKs, and counts.
    """
    metadata = {
        "tables": {},
        "views": [],
        "procedures": [],
        "functions": [],
        "triggers": [],
        "events": []
    }
    
    with conn.cursor() as cursor:
        # 1. Fetch tables and views list
        cursor.execute("""
            SELECT TABLE_NAME, TABLE_TYPE, ENGINE, TABLE_COLLATION 
            FROM information_schema.TABLES 
            WHERE TABLE_SCHEMA = %s
        """, (db_name,))
        for row in cursor.fetchall():
            t_name = row["TABLE_NAME"]
            if row["TABLE_TYPE"] == "BASE TABLE":
                metadata["tables"][t_name] = {
                    "engine": row["ENGINE"],
                    "collation": row["TABLE_COLLATION"],
                    "columns": {},
                    "indexes": {},
                    "foreign_keys": {},
                    "row_count": 0,
                    "checksum": None
                }
            elif row["TABLE_TYPE"] == "VIEW":
                metadata["views"].append(t_name)

        # 2. Fetch columns metadata
        cursor.execute("""
            SELECT 
                TABLE_NAME, COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH,
                NUMERIC_PRECISION, NUMERIC_SCALE, IS_NULLABLE, COLUMN_DEFAULT, EXTRA, COLUMN_TYPE
            FROM information_schema.COLUMNS 
            WHERE TABLE_SCHEMA = %s
        """, (db_name,))
        for row in cursor.fetchall():
            t_name = row["TABLE_NAME"]
            if t_name in metadata["tables"]:
                col_name = row["COLUMN_NAME"]
                metadata["tables"][t_name]["columns"][col_name] = {
                    "data_type": row["DATA_TYPE"],
                    "length": row["CHARACTER_MAXIMUM_LENGTH"],
                    "precision": row["NUMERIC_PRECISION"],
                    "scale": row["NUMERIC_SCALE"],
                    "nullable": row["IS_NULLABLE"],
                    "default": row["COLUMN_DEFAULT"],
                    "extra": row["EXTRA"], # holds auto_increment, generated expressions
                    "column_type": row["COLUMN_TYPE"]
                }

        # 3. Fetch indexes metadata
        cursor.execute("""
            SELECT TABLE_NAME, INDEX_NAME, NON_UNIQUE, COLUMN_NAME, SEQ_IN_INDEX
            FROM information_schema.STATISTICS
            WHERE TABLE_SCHEMA = %s
            ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX
        """, (db_name,))
        for row in cursor.fetchall():
            t_name = row["TABLE_NAME"]
            if t_name in metadata["tables"]:
                idx_name = row["INDEX_NAME"]
                col_name = row["COLUMN_NAME"]
                non_unique = row["NON_UNIQUE"]
                
                if idx_name not in metadata["tables"][t_name]["indexes"]:
                    metadata["tables"][t_name]["indexes"][idx_name] = {
                        "unique": not non_unique,
                        "columns": []
                    }
                metadata["tables"][t_name]["indexes"][idx_name]["columns"].append(col_name)

        # 4. Fetch foreign keys metadata
        cursor.execute("""
            SELECT 
                CONSTRAINT_NAME, TABLE_NAME, COLUMN_NAME, 
                REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME
            FROM information_schema.KEY_COLUMN_USAGE
            WHERE TABLE_SCHEMA = %s AND REFERENCED_TABLE_NAME IS NOT NULL
            ORDER BY TABLE_NAME, CONSTRAINT_NAME, ORDINAL_POSITION
        """, (db_name,))
        for row in cursor.fetchall():
            t_name = row["TABLE_NAME"]
            if t_name in metadata["tables"]:
                fk_name = row["CONSTRAINT_NAME"]
                if fk_name not in metadata["tables"][t_name]["foreign_keys"]:
                    metadata["tables"][t_name]["foreign_keys"][fk_name] = {
                        "columns": [],
                        "ref_table": row["REFERENCED_TABLE_NAME"],
                        "ref_columns": []
                    }
                metadata["tables"][t_name]["foreign_keys"][fk_name]["columns"].append(row["COLUMN_NAME"])
                metadata["tables"][t_name]["foreign_keys"][fk_name]["ref_columns"].append(row["REFERENCED_COLUMN_NAME"])

        # 5. Fetch routines (Procedures / Functions)
        cursor.execute("""
            SELECT ROUTINE_NAME, ROUTINE_TYPE 
            FROM information_schema.ROUTINES 
            WHERE ROUTINE_SCHEMA = %s
        """, (db_name,))
        for row in cursor.fetchall():
            name = row["ROUTINE_NAME"]
            if row["ROUTINE_TYPE"] == "PROCEDURE":
                metadata["procedures"].append(name)
            elif row["ROUTINE_TYPE"] == "FUNCTION":
                metadata["functions"].append(name)

        # 6. Fetch triggers (fetch name and parent table)
        cursor.execute("""
            SELECT TRIGGER_NAME, EVENT_OBJECT_TABLE 
            FROM information_schema.TRIGGERS 
            WHERE TRIGGER_SCHEMA = %s
        """, (db_name,))
        metadata["triggers"] = [(row["TRIGGER_NAME"], row["EVENT_OBJECT_TABLE"]) for row in cursor.fetchall()]

        # 7. Fetch events
        cursor.execute("""
            SELECT EVENT_NAME 
            FROM information_schema.EVENTS 
            WHERE EVENT_SCHEMA = %s
        """, (db_name,))
        metadata["events"] = [row["EVENT_NAME"] for row in cursor.fetchall()]
        
    return metadata

def calculate_table_checksum_sql(columns):
    """
    Builds a checksum SQL expression using BIT_XOR(CAST(CONV(SUBSTRING(MD5(...)))))
    which is order-independent and executes entirely inside the DB engine.
    """
    col_exprs = []
    for col in columns:
        c_name = f"`{col['name']}`"
        d_type = col["data_type"].lower()
        
        # Handle spatial types (e.g. geometry) by converting to WKT (Well-Known Text)
        if d_type in ("geometry", "point", "linestring", "polygon", "multipoint", "multilinestring", "multipolygon", "geometrycollection"):
            expr = f"COALESCE(ST_AsText({c_name}), 'NULL')"
        # Handle BLOBs/Binaries by converting to HEX string
        elif "blob" in d_type or "binary" in d_type or d_type == "varbinary":
            expr = f"COALESCE(HEX({c_name}), 'NULL')"
        # Handle JSON (convert to char)
        elif d_type == "json":
            expr = f"COALESCE(CAST({c_name} AS CHAR CHARACTER SET utf8mb4), 'NULL')"
        # Normal columns
        else:
            expr = f"COALESCE(CAST({c_name} AS CHAR CHARACTER SET utf8mb4), 'NULL')"
            
        col_exprs.append(expr)
        
    # Join columns with a separator
    concat_expr = f"CONCAT_WS('|', {', '.join(col_exprs)})"
    
    # Hash formula: BIT_XOR(CAST(CONV(SUBSTRING(MD5(row_str), 1, 16), 16, 10) AS UNSIGNED))
    checksum_sql = f"SELECT COALESCE(BIT_XOR(CAST(CONV(SUBSTRING(MD5({concat_expr}), 1, 16), 16, 10) AS UNSIGNED)), 0) AS checksum FROM"
    return checksum_sql

def verify_database(aws_config, azure_config, db_name, selected_tables=None, exclude_directus=False, incremental_sync=False, starting_max_pks=None):
    """
    Main verification entrypoint. Compares AWS RDS and Azure MySQL metadata.
    Returns (success_boolean, mismatch_details_string).
    """
    mismatches = []
    
    aws_conn = None
    az_conn = None
    try:
        aws_conn = get_connection(aws_config, db_name)
        az_conn = get_connection(azure_config, db_name)
        
        # 1. Fetch metadata dictionaries
        aws_meta = get_db_metadata(aws_conn, db_name)
        az_meta = get_db_metadata(az_conn, db_name)
        
        # 2. Compare Table Counts (case-insensitively)
        aws_tables = set(aws_meta["tables"].keys())
        az_tables = set(az_meta["tables"].keys())
        
        if exclude_directus:
            aws_tables = {t for t in aws_tables if not t.lower().startswith("directus_")}
            az_tables = {t for t in az_tables if not t.lower().startswith("directus_")}
            aws_meta["triggers"] = [t for t in aws_meta["triggers"] if not t[1].lower().startswith("directus_")]
            az_meta["triggers"] = [t for t in az_meta["triggers"] if not t[1].lower().startswith("directus_")]

        # Filter tables if selection is active
        if selected_tables:
            selected_set_lower = {t.lower() for t in selected_tables}
            aws_tables = {t for t in aws_tables if t.lower() in selected_set_lower}
            az_tables = {t for t in az_tables if t.lower() in selected_set_lower}
            
            # Filter triggers by event table
            aws_meta["triggers"] = [t[0] for t in aws_meta["triggers"] if t[1].lower() in selected_set_lower]
            az_meta["triggers"] = [t[0] for t in az_meta["triggers"] if t[1].lower() in selected_set_lower]
        else:
            # Flatten triggers list of tuples to string names
            aws_meta["triggers"] = [t[0] for t in aws_meta["triggers"]]
            az_meta["triggers"] = [t[0] for t in az_meta["triggers"]]
            
        aws_tables_lower = {t.lower() for t in aws_tables}
        az_tables_lower = {t.lower() for t in az_tables}
        
        if aws_tables_lower != az_tables_lower:
            missing_in_az = {t for t in aws_tables if t.lower() not in az_tables_lower}
            extra_in_az = {t for t in az_tables if t.lower() not in aws_tables_lower}
            if missing_in_az:
                mismatches.append(f"Tables missing in Azure: {', '.join(missing_in_az)}")
            if extra_in_az:
                mismatches.append(f"Unexpected tables in Azure: {', '.join(extra_in_az)}")
                
        if not incremental_sync:
            # Compare other object lists (case-insensitively)
            for obj_type in ["views", "procedures", "functions", "triggers", "events"]:
                aws_objs = set(aws_meta[obj_type])
                az_objs = set(az_meta[obj_type])
                
                aws_objs_lower = {o.lower() for o in aws_objs}
                az_objs_lower = {o.lower() for o in az_objs}
                
                if aws_objs_lower != az_objs_lower:
                    missing = {o for o in aws_objs if o.lower() not in az_objs_lower}
                    extra = {o for o in az_objs if o.lower() not in aws_objs_lower}
                    if missing or extra:
                        mismatches.append(f"{obj_type.capitalize()} mismatch. Missing: {missing}. Extra: {extra}")

        # Map tables for case-insensitive lookup
        aws_table_map = {t.lower(): t for t in aws_tables}
        az_table_map = {t.lower(): t for t in az_tables}
        
        common_tables_lower = aws_tables_lower & az_tables_lower

        # 3. Compare Table Column schemas and Row Counts
        for table_lower in common_tables_lower:
            aws_table_name = aws_table_map[table_lower]
            az_table_name = az_table_map[table_lower]
            
            aws_tbl = aws_meta["tables"][aws_table_name]
            az_tbl = az_meta["tables"][az_table_name]
            
            # Row Counts
            starting_max_pk = (starting_max_pks or {}).get(aws_table_name)
            pks = aws_tbl["indexes"].get("PRIMARY", {}).get("columns", [])
            
            if not pks:
                # 1. Look for auto_increment column
                for col_name, col_meta in aws_tbl["columns"].items():
                    if "auto_increment" in col_meta.get("extra", "").lower():
                        pks = [col_name]
                        break
            if not pks:
                # 2. Look for unique indexes
                for idx_name, idx_meta in aws_tbl["indexes"].items():
                    if idx_meta.get("unique"):
                        pks = idx_meta.get("columns", [])
                        break
            if not pks:
                # 3. Look for 'id' column
                if "id" in aws_tbl["columns"]:
                    pks = ["id"]
            
            aws_where = ""
            aws_params = []
            if incremental_sync and starting_max_pk and pks:
                conds = []
                for idx in range(len(pks)):
                    sub_conds = []
                    for prev_idx in range(idx):
                        sub_conds.append(f"`{pks[prev_idx]}` = %s")
                    sub_conds.append(f"`{pks[idx]}` > %s")
                    conds.append("(" + " AND ".join(sub_conds) + ")")
                aws_params = []
                for idx in range(len(pks)):
                    for prev_idx in range(idx):
                        aws_params.append(starting_max_pk[prev_idx])
                    aws_params.append(starting_max_pk[idx])
                aws_where = " WHERE " + " OR ".join(conds)
                
            with aws_conn.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) as cnt FROM `{aws_table_name}`{aws_where}", aws_params)
                aws_row_count = cur.fetchone()["cnt"]
            with az_conn.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) as cnt FROM `{az_table_name}`{aws_where}", aws_params)
                az_row_count = cur.fetchone()["cnt"]
                
            aws_tbl["row_count"] = aws_row_count
            az_tbl["row_count"] = az_row_count
            
            if aws_row_count != az_row_count:
                mismatches.append(f"Table '{aws_table_name}' row count mismatch! AWS: {aws_row_count}, Azure: {az_row_count}")
                
            # Engine and collation check
            if aws_tbl["engine"] != az_tbl["engine"]:
                mismatches.append(f"Table '{aws_table_name}' engine mismatch. AWS: {aws_tbl['engine']}, Azure: {az_tbl['engine']}")
            if aws_tbl["collation"] != az_tbl["collation"]:
                mismatches.append(f"Table '{aws_table_name}' collation mismatch. AWS: {aws_tbl['collation']}, Azure: {az_tbl['collation']}")
                
            # Columns metadata check
            aws_cols = set(aws_tbl["columns"].keys())
            az_cols = set(az_tbl["columns"].keys())
            
            # Remove Azure's auto-generated invisible primary key column "my_row_id" if it doesn't exist on AWS
            if "my_row_id" in az_cols and "my_row_id" not in aws_cols:
                az_cols.discard("my_row_id")
                
            if aws_cols != az_cols:
                mismatches.append(f"Table '{aws_table_name}' columns mismatch. Missing in Azure: {aws_cols - az_cols}. Extra: {az_cols - aws_cols}")
            
            for col in (aws_cols & az_cols):
                ac = aws_tbl["columns"][col]
                zc = az_tbl["columns"][col]
                
                # Check data type, nullable, default value
                if ac["data_type"] != zc["data_type"]:
                    mismatches.append(f"Table '{aws_table_name}' Column '{col}' data type mismatch: AWS={ac['data_type']}, Azure={zc['data_type']}")
                if ac["nullable"] != zc["nullable"]:
                    mismatches.append(f"Table '{aws_table_name}' Column '{col}' nullability mismatch: AWS={ac['nullable']}, Azure={zc['nullable']}")
                if ac["default"] != zc["default"]:
                    mismatches.append(f"Table '{aws_table_name}' Column '{col}' default mismatch: AWS={ac['default']}, Azure={zc['default']}")
                    
            # Primary / Unique Indexes check
            aws_idxs = set(aws_tbl["indexes"].keys())
            az_idxs = set(az_tbl["indexes"].keys())
            
            # Remove Azure's auto-generated invisible primary key index "PRIMARY" (indexing "my_row_id") if it doesn't exist on AWS
            if "PRIMARY" in az_idxs and "PRIMARY" not in aws_idxs:
                if az_tbl["indexes"]["PRIMARY"]["columns"] == ["my_row_id"]:
                    az_idxs.discard("PRIMARY")
                    
            if aws_idxs != az_idxs:
                mismatches.append(f"Table '{aws_table_name}' indexes mismatch. AWS keys: {aws_idxs}. Azure keys: {az_idxs}")
            else:
                for idx in aws_idxs:
                    ai = aws_tbl["indexes"][idx]
                    zi = az_tbl["indexes"][idx]
                    if ai["unique"] != zi["unique"] or ai["columns"] != zi["columns"]:
                        mismatches.append(f"Table '{aws_table_name}' Index '{idx}' definition mismatch. AWS={ai}, Azure={zi}")

            # Foreign Keys check
            aws_fks = set(aws_tbl["foreign_keys"].keys())
            az_fks = set(az_tbl["foreign_keys"].keys())
            if aws_fks != az_fks:
                # Some Azure configurations alter constraint names slightly, so we can verify by columns/ref table rather than name alone
                # Let's map constraints by column tuple and target table to be resilient to name differences
                aws_fk_mappings = {(tuple(fk["columns"]), fk["ref_table"]): fk["ref_columns"] for fk in aws_tbl["foreign_keys"].values()}
                az_fk_mappings = {(tuple(fk["columns"]), fk["ref_table"]): fk["ref_columns"] for fk in az_tbl["foreign_keys"].values()}
                if aws_fk_mappings != az_fk_mappings:
                    mismatches.append(f"Table '{aws_table_name}' Foreign Keys mismatch. AWS mapping: {aws_fk_mappings}, Azure mapping: {az_fk_mappings}")

            # 4. Checksums (deterministic data hashing)
            if aws_row_count > 0:
                # Prepare column details for SQL generator
                table_columns = []
                for c_name, c_meta in aws_tbl["columns"].items():
                    table_columns.append({"name": c_name, "data_type": c_meta["data_type"]})
                    
                checksum_sql = calculate_table_checksum_sql(table_columns)
                
                # Fetch AWS checksum
                with aws_conn.cursor() as cur:
                    cur.execute(f"{checksum_sql} `{aws_table_name}`{aws_where}", aws_params)
                    aws_checksum = cur.fetchone()["checksum"]
                    
                # Fetch Azure checksum
                with az_conn.cursor() as cur:
                    cur.execute(f"{checksum_sql} `{az_table_name}`{aws_where}", aws_params)
                    az_checksum = cur.fetchone()["checksum"]
                    
                aws_tbl["checksum"] = aws_checksum
                az_tbl["checksum"] = az_checksum
                
                if aws_checksum != az_checksum:
                    mismatches.append(f"Table '{aws_table_name}' data checksum mismatch! AWS: {aws_checksum}, Azure: {az_checksum} (Data integrity compromise)")
                    log_error(db_name, aws_table_name, "Data Checksum", 0, "FAILED", f"AWS Hash={aws_checksum}, Azure Hash={az_checksum}")
                else:
                    log_verification(db_name, aws_table_name, "Data Checksum Match", 0, f"Checksum = {aws_checksum}")
            else:
                log_verification(db_name, aws_table_name, "Data Checksum Match", 0, "SUCCESS (Up to date / No delta rows)" if incremental_sync else "SUCCESS (Empty Table)")

        # Result report
        if mismatches:
            details = "\n".join(mismatches)
            log_verification(db_name, None, "Verification Status: FAILED", 0, f"{len(mismatches)} mismatches")
            return False, details
        else:
            log_verification(db_name, None, "Verification Status: SUCCESS", 0, "All verification checks passed.")
            return True, "ALL OK"
            
    except Exception as e:
        log_error(db_name, None, "Verification Engine Exception", 0, "FAILED", e)
        return False, f"Verification failed with system exception: {e}"
    finally:
        if aws_conn:
            try:
                aws_conn.close()
            except Exception:
                pass
        if az_conn:
            try:
                az_conn.close()
            except Exception:
                pass
