import platform
import subprocess
import sys
import logging
from pathlib import Path
from shutil import which

import psutil
import colorlog

DIVIDER = "[---------] SYSTEM INFO [---------]"


def print_system_info():
    lines = ['\n']
    lines.append(DIVIDER)
    lines.append("")
    lines.append("Python Version:")
    lines.append(f"  {sys.version}")

    lines.append("\nPlatform Information:")
    lines.append(f"  System: {platform.system()}")
    lines.append(f"  Release: {platform.release()}")
    lines.append(f"  Version: {platform.version()}")
    lines.append(f"  Machine: {platform.machine()}")
    lines.append(f"  Processor: {platform.processor()}")

    lines.append("\nCPU Information:")
    lines.append(f"  Physical cores: {psutil.cpu_count(logical=False)}")
    lines.append(f"  Total cores: {psutil.cpu_count(logical=True)}")
    cpu_freq = psutil.cpu_freq()
    lines.append(f"  Max Frequency: {cpu_freq.max:.2f} Mhz")
    lines.append(f"  Current Frequency: {cpu_freq.current:.2f} Mhz")

    lines.append("\nMemory Information:")
    vm = psutil.virtual_memory()
    lines.append(f"  Total: {vm.total / (1024**3):.2f} GB")
    lines.append(f"  Available: {vm.available / (1024**3):.2f} GB")
    lines.append(f"  Used: {vm.used / (1024**3):.2f} GB")

    lines.append("\nDisk Information (>80%):")
    partitions = psutil.disk_partitions()
    for partition in partitions:
        try:
            disk_usage = psutil.disk_usage(partition.mountpoint)
            if disk_usage.used / disk_usage.total > 0.8:
                lines.append(f"  Device: {partition.device}")
                lines.append(f"    Mount point: {partition.mountpoint}")
                lines.append(f"      Total size: {disk_usage.total / (1024**3):.2f} GB")
                lines.append(f"      Used: {disk_usage.used / (1024**3):.2f} GB")
                lines.append(f"      Free: {disk_usage.free / (1024**3):.2f} GB")
        except PermissionError:
            lines.append("      Permission denied")

    lines.append("")

    # Check for NVIDIA GPU
    if which('nvidia-smi'):
        try:
            lines.append("\nNVIDIA GPU Information:")
            nvidia_output = subprocess.check_output(['nvidia-smi', '--query-gpu=gpu_name,memory.total,memory.used,memory.free,temperature.gpu,utilization.gpu', '--format=csv,noheader,nounits']).decode()
            for gpu_line in nvidia_output.strip().split('\n'):
                name, total, used, free, temp, util = gpu_line.split(', ')
                lines.append(f"  GPU: {name}")
                lines.append(f"    Memory Total: {total} MB")
                lines.append(f"    Memory Used: {used} MB")
                lines.append(f"    Memory Free: {free} MB")
                lines.append(f"    Temperature: {temp}°C")
                lines.append(f"    Utilization: {util}%")
        except (subprocess.CalledProcessError, FileNotFoundError):
            lines.append("  Error getting NVIDIA GPU information")

    # Check for AMD GPU
    if which('rocm-smi'):
        try:
            lines.append("\nAMD GPU Information:")
            rocm_output = subprocess.check_output(['rocm-smi', '--showproductname', '--showmeminfo', '--showtemp']).decode()
            lines.extend(f"  {line}" for line in rocm_output.strip().split('\n'))
        except (subprocess.CalledProcessError, FileNotFoundError):
            lines.append("  Error getting AMD GPU information")

    # Check for Apple Silicon
    if platform.system() == 'Darwin' and platform.machine() == 'arm64':
        try:
            lines.append("\nApple Silicon Information:")
            # Use sysctl to get basic M1/M2 information
            cpu_brand = subprocess.check_output(['sysctl', '-n', 'machdep.cpu.brand_string']).decode().strip()
            lines.append(f"  Processor: {cpu_brand}")
            # Check if Metal Performance Shaders (MPS) backend is available
            try:
                import torch
                if torch.backends.mps.is_available():
                    lines.append("  MPS: Available")
                    lines.append(f"  MPS Device: {torch.device('mps')}")
                else:
                    lines.append("  MPS: Not available")
            except ImportError:
                lines.append("  PyTorch not installed, cannot check MPS availability")
        except (subprocess.CalledProcessError, FileNotFoundError):
            lines.append("  Error getting Apple Silicon information")

    lines.append("")
    lines.append(DIVIDER)
    return "\n".join(lines)


class TeeHandler(logging.Handler):
    """A handler that writes to both a file and the console while preserving print statements."""
    def __init__(self, filename, mode='a', console_level=logging.INFO, file_level=logging.DEBUG):
        super().__init__()
        
        # Console handler - only shows INFO and above
        self.console_handler = colorlog.StreamHandler()
        self.console_handler.setLevel(console_level)
        self.console_handler.setFormatter(
            colorlog.ColoredFormatter("%(green)s%(levelname)s:%(name)s:%(message)s")
        )
        
        # File handler - shows all levels
        Path(filename).parent.mkdir(parents=True, exist_ok=True)
        self.file_handler = logging.FileHandler(filename, mode=mode)
        self.file_handler.setLevel(file_level)
        self.file_handler.setFormatter(
            logging.Formatter("%(asctime)s - %(levelname)s - %(name)s:%(lineno)d - %(message)s")
        )

    def emit(self, record):
        if record.levelno >= self.console_handler.level:
            self.console_handler.emit(record)
        if record.levelno >= self.file_handler.level:
            self.file_handler.emit(record)


class PrintCapture:
    """Captures print statements and redirects them to logging while preserving console output."""
    def __init__(self, logger):
        self.logger = logger
        self.original_stdout = sys.stdout

    def write(self, buf):
        self.original_stdout.write(buf)
        for line in buf.rstrip().splitlines():
            if line.strip():  
                self.logger.debug(f"[PRINT] {line.rstrip()}")

    def flush(self):
        self.original_stdout.flush()
    
    # Forward any other attributes to the original stdout
    def __getattr__(self, attr):
        return getattr(self.original_stdout, attr)