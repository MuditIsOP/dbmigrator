import os
import json

CONFIG_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "config"))
STATE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "state"))

os.makedirs(CONFIG_DIR, exist_ok=True)
os.makedirs(STATE_DIR, exist_ok=True)

CONNECTIONS_PATH = os.path.join(CONFIG_DIR, "connections.json")
CHECKPOINTS_PATH = os.path.join(STATE_DIR, "checkpoints.json")

# Simple base64 or custom basic obfuscation for passwords in connections file
# (just to prevent accidental plain-text viewing, as requested)
import base64

def obfuscate(val):
    if not val:
        return ""
    return base64.b64encode(val.encode("utf-8")).decode("utf-8")

def deobfuscate(val):
    if not val:
        return ""
    try:
        return base64.b64decode(val.encode("utf-8")).decode("utf-8")
    except Exception:
        return val  # Fallback if not base64 encoded

# ==========================================
# Connection Profiles Management
# ==========================================

def load_profiles():
    if not os.path.exists(CONNECTIONS_PATH):
        return {}
    try:
        with open(CONNECTIONS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            # Deobfuscate passwords
            for name, profile in data.items():
                if "aws" in profile and "password" in profile["aws"]:
                    profile["aws"]["password"] = deobfuscate(profile["aws"]["password"])
                if "azure" in profile and "password" in profile["azure"]:
                    profile["azure"]["password"] = deobfuscate(profile["azure"]["password"])
            return data
    except Exception as e:
        print(f"Error loading profiles: {e}")
        return {}

def save_profiles(profiles):
    try:
        # Obfuscate passwords before writing to disk
        data_to_save = {}
        for name, profile in profiles.items():
            profile_copy = json.loads(json.dumps(profile))  # deep copy
            if "aws" in profile_copy and "password" in profile_copy["aws"]:
                profile_copy["aws"]["password"] = obfuscate(profile_copy["aws"]["password"])
            if "azure" in profile_copy and "password" in profile_copy["azure"]:
                profile_copy["azure"]["password"] = obfuscate(profile_copy["azure"]["password"])
            data_to_save[name] = profile_copy
            
        with open(CONNECTIONS_PATH, "w", encoding="utf-8") as f:
            json.dump(data_to_save, f, indent=4)
        return True
    except Exception as e:
        print(f"Error saving profiles: {e}")
        return False

def add_profile(name, aws_config, azure_config):
    profiles = load_profiles()
    existing = profiles.get(name, {})
    
    if aws_config.get("password") == "********" and "aws" in existing:
        aws_config["password"] = existing["aws"].get("password", "")
    if azure_config.get("password") == "********" and "azure" in existing:
        azure_config["password"] = existing["azure"].get("password", "")
        
    profiles[name] = {
        "aws": aws_config,
        "azure": azure_config
    }
    return save_profiles(profiles)

def delete_profile(name):
    profiles = load_profiles()
    if name in profiles:
        del profiles[name]
        return save_profiles(profiles)
    return False

# ==========================================
# Checkpoints (Migration State) Management
# ==========================================

def load_checkpoints():
    if not os.path.exists(CHECKPOINTS_PATH):
        return {}
    try:
        with open(CHECKPOINTS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"Error loading checkpoints: {e}")
        return {}

def save_checkpoints(checkpoints):
    try:
        with open(CHECKPOINTS_PATH, "w", encoding="utf-8") as f:
            json.dump(checkpoints, f, indent=4)
        return True
    except Exception as e:
        print(f"Error saving checkpoints: {e}")
        return False

def get_db_checkpoint(db_name):
    checkpoints = load_checkpoints()
    return checkpoints.get(db_name, {
        "completed_tables": [],
        "completed_objects": {
            "views": [],
            "functions": [],
            "procedures": [],
            "triggers": [],
            "events": []
        }
    })

def update_table_checkpoint(db_name, table_name):
    checkpoints = load_checkpoints()
    if db_name not in checkpoints:
        checkpoints[db_name] = {
            "completed_tables": [],
            "completed_objects": {
                "views": [],
                "functions": [],
                "procedures": [],
                "triggers": [],
                "events": []
            }
        }
    if table_name not in checkpoints[db_name]["completed_tables"]:
        checkpoints[db_name]["completed_tables"].append(table_name)
    save_checkpoints(checkpoints)

def update_object_checkpoint(db_name, obj_type, obj_name):
    checkpoints = load_checkpoints()
    if db_name not in checkpoints:
        checkpoints[db_name] = {
            "completed_tables": [],
            "completed_objects": {
                "views": [],
                "functions": [],
                "procedures": [],
                "triggers": [],
                "events": []
            }
        }
    if obj_name not in checkpoints[db_name]["completed_objects"][obj_type]:
        checkpoints[db_name]["completed_objects"][obj_type].append(obj_name)
    save_checkpoints(checkpoints)

def clear_db_checkpoint(db_name):
    checkpoints = load_checkpoints()
    if db_name in checkpoints:
        del checkpoints[db_name]
        save_checkpoints(checkpoints)
