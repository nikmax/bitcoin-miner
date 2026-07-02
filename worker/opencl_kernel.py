# Compatibility shim: der eigentliche Kernel liegt ab v0.3.5 zentral in shared/kernels.
import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parents[1]))
from shared.kernels.opencl_sha256 import OPENCL_KERNEL
