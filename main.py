"""
AuraScribe — entry point.
"""
import warnings
import logging

# Suppress noisy third-party warnings that don't affect functionality
warnings.filterwarnings("ignore", message="torchcodec is not installed correctly")
warnings.filterwarnings("ignore", message=".*TensorFloat-32.*")
warnings.filterwarnings("ignore", message=".*task-dependent loss function.*")
warnings.filterwarnings("ignore", message=".*loss_func.W.*")
warnings.filterwarnings("ignore", message=".*ModelCheckpoint.*colliding.*")
warnings.filterwarnings("ignore", message=".*std().*degrees of freedom.*")
warnings.filterwarnings("ignore", category=UserWarning, module="pyannote")
warnings.filterwarnings("ignore", category=UserWarning, module="lightning")
warnings.filterwarnings("ignore", message=".*Mean of empty slice.*")
warnings.filterwarnings("ignore", message=".*invalid value encountered in divide.*")
warnings.filterwarnings("ignore", category=RuntimeWarning, module="numpy")

# Silence the Lightning checkpoint upgrade spam
logging.getLogger("lightning.pytorch.utilities.migration").setLevel(logging.ERROR)
logging.getLogger("lightning.pytorch.core.saving").setLevel(logging.ERROR)
logging.getLogger("pytorch_lightning").setLevel(logging.ERROR)

import uvicorn
from backend.config import APP_HOST, APP_PORT


def main():
    print("Starting AuraScribe...")
    print(f"Open http://localhost:{APP_PORT} in your browser")
    uvicorn.run(
        "backend.api:app",
        host=APP_HOST,
        port=APP_PORT,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
