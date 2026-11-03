import sys
import subprocess
import os

def detect_ram():
    """Returns total system RAM in GB."""
    ram_gb = 0.0
    try:
        if sys.platform == "win32":
            import ctypes
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]
            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            ram_gb = stat.ullTotalPhys / (1024**3)
        elif sys.platform == "darwin":
            output = subprocess.check_output(["sysctl", "-n", "hw.memsize"]).decode().strip()
            ram_gb = int(output) / (1024**3)
        else: # linux
            with open("/proc/meminfo", "r") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        kb_str = "".join([c for c in line if c.isdigit()])
                        ram_gb = int(kb_str) / (1024**2)
                        break
    except Exception as e:
        print(f"Error detecting RAM: {e}")
    return round(ram_gb, 1)

def detect_vram():
    """Returns dedicated VRAM in GB and GPU type."""
    vram_gb = 0.0
    gpu_type = "unknown"
    
    # Try nvidia-smi
    try:
        output = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            stderr=subprocess.DEVNULL
        ).decode().strip()
        lines = output.split('\n')
        if lines:
            vram_gb = int(lines[0]) / 1024
            gpu_type = "nvidia"
            return round(vram_gb, 1), gpu_type
    except Exception:
        pass

    # Apple Silicon
    if sys.platform == "darwin":
        try:
            output = subprocess.check_output(["sysctl", "-n", "hw.memsize"]).decode().strip()
            # Mac unified memory -> VRAM is effectively up to ~70% of system RAM
            total_ram_gb = int(output) / (1024**3)
            vram_gb = total_ram_gb * 0.70
            gpu_type = "apple_unified"
            return round(vram_gb, 1), gpu_type
        except Exception:
            pass
            
    # Windows fallback (AMD or Intel)
    if sys.platform == "win32":
        try:
            output = subprocess.check_output("wmic path win32_VideoController get AdapterRAM", shell=True, stderr=subprocess.DEVNULL).decode()
            lines = [line.strip() for line in output.split('\n') if line.strip() and line.strip().isdigit()]
            if lines:
                vram_bytes = int(lines[0])
                if vram_bytes > 0:
                    vram_gb = vram_bytes / (1024**3)
                    gpu_type = "integrated_or_amd"
                    return round(vram_gb, 1), gpu_type
        except Exception:
            pass
            
    return 0.0, "unknown"

def get_hardware_info():
    ram = detect_ram()
    vram, gpu_type = detect_vram()
    return {
        "ram_gb": ram,
        "vram_gb": vram,
        "gpu_type": gpu_type
    }
