import os

log_path = os.path.expanduser("~/.gilzabots/logs/llm_debug.log")
try:
    with open(log_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
        tail = "".join(lines[-1500:])
    
    out_path = os.path.join(os.getcwd(), "debug_tail.log")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(tail)
    print(f"Saved {len(lines[-1500:])} lines to debug_tail.log")
except Exception as e:
    print(f"Error: {e}")
