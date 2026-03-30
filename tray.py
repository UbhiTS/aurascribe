"""
System tray icon for AuraScribe.
Starts the backend in a thread, shows tray icon with Open/Quit options.
Run this instead of main.py for the tray experience.
"""
import warnings
import logging

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

logging.getLogger("lightning.pytorch.utilities.migration").setLevel(logging.ERROR)
logging.getLogger("lightning.pytorch.core.saving").setLevel(logging.ERROR)
logging.getLogger("pytorch_lightning").setLevel(logging.ERROR)

import threading
import webbrowser
import subprocess
import sys
import time
from pathlib import Path

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:
    print("pystray/Pillow not installed. Run: pip install pystray Pillow")
    sys.exit(1)


def create_icon_image(size=64, recording=False):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    color = (220, 50, 50) if recording else (90, 114, 242)
    draw.ellipse([4, 4, size - 4, size - 4], fill=color)
    # Lightning bolt shape
    pts = [
        (size * 0.55, size * 0.15),
        (size * 0.30, size * 0.52),
        (size * 0.50, size * 0.52),
        (size * 0.45, size * 0.85),
        (size * 0.70, size * 0.48),
        (size * 0.50, size * 0.48),
    ]
    draw.polygon(pts, fill="white")
    return img


def start_server():
    import uvicorn
    from backend.config import APP_HOST, APP_PORT
    uvicorn.run("backend.api:app", host=APP_HOST, port=APP_PORT, log_level="warning")


def main():
    # Start backend in background thread
    server_thread = threading.Thread(target=start_server, daemon=True)
    server_thread.start()
    time.sleep(1.5)  # Let server boot

    from backend.config import APP_PORT
    url = f"http://localhost:{APP_PORT}"

    icon_img = create_icon_image()

    def open_ui(icon, item):
        webbrowser.open(url)

    def quit_app(icon, item):
        icon.stop()
        sys.exit(0)

    menu = pystray.Menu(
        pystray.MenuItem("Open AuraScribe", open_ui, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", quit_app),
    )

    icon = pystray.Icon("AuraScribe", icon_img, "AuraScribe", menu)
    webbrowser.open(url)  # Open on first launch
    icon.run()


if __name__ == "__main__":
    main()
