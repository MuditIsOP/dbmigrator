import os
import json
import pymysql
import pymysql.cursors
from pymysql.constants import CLIENT
from src.utils.logger import log_migration, log_error

class CaseInsensitiveDict(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._lower_map = {k.lower() if isinstance(k, str) else k: k for k in self}

    def __contains__(self, key):
        if isinstance(key, str):
            return key.lower() in self._lower_map
        return super().__contains__(key)

    def __getitem__(self, key):
        if isinstance(key, str):
            lower_key = key.lower()
            if lower_key in self._lower_map:
                return super().__getitem__(self._lower_map[lower_key])
        return super().__getitem__(key)

    def get(self, key, default=None):
        if isinstance(key, str):
            lower_key = key.lower()
            if lower_key in self._lower_map:
                return super().__getitem__(self._lower_map[lower_key])
        return super().get(key, default)

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self._lower_map[key.lower() if isinstance(key, str) else key] = key

    def __delitem__(self, key):
        super().__delitem__(key)
        self._lower_map.pop(key.lower() if isinstance(key, str) else key, None)

class CaseInsensitiveDictCursor(pymysql.cursors.DictCursor):
    dict_type = CaseInsensitiveDict

STATE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "state"))
os.makedirs(STATE_DIR, exist_ok=True)
DISCOVERY_PATH = os.path.join(STATE_DIR, "discovery.json")

def get_connection(config, db_name=None):
    """
    Establishes a connection to MySQL based on a configuration dictionary.
    config should have: host, port, user, password, and optional ssl.
    """
    ssl_config = None
    if config.get("ssl_enabled"):
        ssl_config = {}
        if config.get("ssl_ca"):
            ssl_config["ca"] = config["ssl_ca"]
            
    # Parse port safely
    port = config.get("port", 3306)
    if isinstance(port, str):
        port = int(port) if port.strip() else 3306
        
    return pymysql.connect(
        host=config["host"],
        port=port,
        user=config["user"],
        password=config["password"],
        database=db_name,
        ssl=ssl_config,
        charset="utf8mb4",
        autocommit=True,
        cursorclass=CaseInsensitiveDictCursor,
        client_flag=CLIENT.MULTI_RESULTS,  # Allow multiple statements/results
        connect_timeout=10,
        read_timeout=300,   # 5 min read timeout for large batch fetches
        write_timeout=300,  # 5 min write timeout for large batch inserts
        # Clear restrictive sql_mode so that AWS zero-dates ('0000-00-00 00:00:00')
        # are accepted by Azure Flexible Server without error 1292.
        # Disable max_execution_time so long queries aren't killed during migration.
        init_command="SET SESSION sql_mode = '', SESSION max_execution_time = 0"
    )

def test_connection(config):
    """Tests if a connection can be established. Returns (success, message)."""
    conn = None
    try:
        conn = get_connection(config)
        with conn.cursor() as cursor:
            cursor.execute("SELECT 1")
        return True, "Connection successful"
    except Exception as e:
        return False, str(e)
    finally:
        if conn:
            conn.close()

def discover_databases(aws_config):
    """
    Discovers all non-system databases from AWS RDS.
    Collects charsets, collations, sizes, and object counts.
    Saves and returns discovery.json content.
    """
    system_dbs = {"mysql", "information_schema", "performance_schema", "sys"}
    conn = None
    discovery = {}
    
    try:
        conn = get_connection(aws_config)
        with conn.cursor() as cursor:
            # 1. Fetch schemata
            cursor.execute("SELECT schema_name, default_character_set_name, default_collation_name FROM information_schema.schemata")
            dbs = cursor.fetchall()
            
            for db in dbs:
                name = db["schema_name"]
                if name in system_dbs:
                    continue
                
                discovery[name] = {
                    "name": name,
                    "charset": db["default_character_set_name"],
                    "collation": db["default_collation_name"],
                    "size_bytes": 0,
                    "table_count": 0,
                    "view_count": 0,
                    "procedure_count": 0,
                    "function_count": 0,
                    "trigger_count": 0,
                    "event_count": 0
                }
            
            # 2. Gather sizes, tables, and views counts per schema (optimized to avoid full scans)
            if discovery:
                db_names = tuple(discovery.keys())
                
                cursor.execute("""
                    SELECT 
                        table_schema, 
                        SUM(COALESCE(data_length + index_length, 0)) as size_bytes,
                        SUM(CASE WHEN table_type = 'BASE TABLE' THEN 1 ELSE 0 END) as tables,
                        SUM(CASE WHEN table_type = 'VIEW' THEN 1 ELSE 0 END) as views
                    FROM information_schema.tables 
                    WHERE table_schema IN %s
                    GROUP BY table_schema
                """, (db_names,))
                table_stats = cursor.fetchall()
                for stat in table_stats:
                    db_name = stat["table_schema"]
                    if db_name in discovery:
                        discovery[db_name]["size_bytes"] = int(stat["size_bytes"] or 0)
                        discovery[db_name]["table_count"] = int(stat["tables"] or 0)
                        discovery[db_name]["view_count"] = int(stat["views"] or 0)
                
                # 3. Gather routines (procedures and functions) count
                cursor.execute("""
                    SELECT routine_schema, routine_type, COUNT(*) as cnt 
                    FROM information_schema.routines 
                    WHERE routine_schema IN %s
                    GROUP BY routine_schema, routine_type
                """, (db_names,))
                routine_stats = cursor.fetchall()
                for stat in routine_stats:
                    db_name = stat["routine_schema"]
                    if db_name in discovery:
                        r_type = stat["routine_type"].lower()
                        if r_type == "procedure":
                            discovery[db_name]["procedure_count"] = stat["cnt"]
                        elif r_type == "function":
                            discovery[db_name]["function_count"] = stat["cnt"]
                
                # 4. Gather triggers count
                cursor.execute("""
                    SELECT trigger_schema, COUNT(*) as cnt 
                    FROM information_schema.triggers 
                    WHERE trigger_schema IN %s
                    GROUP BY trigger_schema
                """, (db_names,))
                trigger_stats = cursor.fetchall()
                for stat in trigger_stats:
                    db_name = stat["trigger_schema"]
                    if db_name in discovery:
                        discovery[db_name]["trigger_count"] = stat["cnt"]
                        
                # 5. Gather events count
                cursor.execute("""
                    SELECT event_schema, COUNT(*) as cnt 
                    FROM information_schema.events 
                    WHERE event_schema IN %s
                    GROUP BY event_schema
                """, (db_names,))
                event_stats = cursor.fetchall()
                for stat in event_stats:
                    db_name = stat["event_schema"]
                    if db_name in discovery:
                        discovery[db_name]["event_count"] = stat["cnt"]
                    
        # Save as discovery.json in state folder
        with open(DISCOVERY_PATH, "w", encoding="utf-8") as f:
            json.dump(discovery, f, indent=4)
            
        # Also copy to root directory if we want it there as per EDS
        root_discovery_path = os.path.abspath(os.path.join(STATE_DIR, "..", "discovery.json"))
        with open(root_discovery_path, "w", encoding="utf-8") as f:
            json.dump(discovery, f, indent=4)
            
        return discovery
    except Exception as e:
        log_error(None, None, "Database Discovery", 0, "FAILED", e)
        raise e
    finally:
        if conn:
            conn.close()
