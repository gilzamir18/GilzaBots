import os
import json

GILZABOTS_DIR = os.path.join(os.path.expanduser("~"), ".gilzabots")

HARDWARE_FILE = os.path.join(GILZABOTS_DIR, "hardware.json")

def load_hardware_info():
    if os.path.exists(HARDWARE_FILE):
        try:
            with open(HARDWARE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return None

def save_hardware_info(data):
    os.makedirs(GILZABOTS_DIR, exist_ok=True)
    with open(HARDWARE_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)

def get_or_detect_hardware():
    info = load_hardware_info()
    if not info:
        from gilzabots.hardware_detect import get_hardware_info
        info = get_hardware_info()
        save_hardware_info(info)
    return info
