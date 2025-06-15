"""Touch‑controlled media viewer for Raspberry Pi.

Tap the screen (or click with a mouse) to advance through images
stored in *MEDIA_DIR*. Images are rendered with pygame. Press Esc or close the window to quit.
"""

from __future__ import annotations

import os
import sys
import pygame
import subprocess
from typing import Tuple  # add after existing imports
import shutil
import threading
import queue

DEBUG = True  # set to False for normal operation

# Queue used by an optional evdev listener thread to signal touches
touch_queue: "queue.Queue[bool]" = queue.Queue()


def dbg(msg: str) -> None:
    """Print *msg* with a millisecond timestamp when DEBUG is on."""
    if DEBUG:
        print(f"{pygame.time.get_ticks():>7} ms | {msg}")


def blackout(scr: pygame.Surface, ms: int = 50) -> None:
    """Fill the current screen black, flip, and pause *ms* milliseconds."""
    scr.fill((0, 0, 0))
    pygame.display.flip()
    pygame.time.wait(ms)


def reset_display() -> pygame.Surface:
    """Re‑initialise the pygame display and return a new full‑screen surface
       while keeping the desktop hidden behind a black root window."""
    dbg("Re‑initialising display")
    # Paint the X11 root window black so anything behind our window stays dark
    try:
        subprocess.call(["xsetroot", "-solid", "black"])
    except FileNotFoundError:
        dbg("xsetroot not found – background flash may be visible")
    pygame.display.quit()
    pygame.display.init()
    return pygame.display.set_mode((0, 0), pygame.FULLSCREEN)


def start_evdev_listener() -> None:
    """Spawn a background thread that watches the first touchscreen evdev device
       for BTN_TOUCH presses and drops a token into *touch_queue*.
       Falls back silently if python-evdev or a touchscreen device is absent."""
    try:
        import evdev
    except ImportError:
        dbg("python-evdev not installed; external touch monitor disabled")
        return

    # Pick the first device that adverts BTN_TOUCH
    devices = [
        d for d in map(evdev.InputDevice, evdev.list_devices())
        if evdev.ecodes.BTN_TOUCH in d.capabilities().get(evdev.ecodes.EV_KEY, [])
    ]
    if not devices:
        dbg("No evdev touchscreen device found")
        return

    dev = devices[0]
    dbg(f"evdev listener bound to {dev.path} ({dev.name})")

    def _worker(device: "evdev.InputDevice") -> None:
        for ev in device.read_loop():
            if ev.type == evdev.ecodes.EV_KEY and ev.code == evdev.ecodes.BTN_TOUCH and ev.value == 1:
                dbg("evdev touch press detected")
                touch_queue.put(True)

    th = threading.Thread(target=_worker, args=(dev,), daemon=True, name="EvdevTouch")
    th.start()


MEDIA_DIR = '/home/calen/projects/rpi-touch-app/media/'

# Supported media extensions
IMG_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".gif"}
VID_EXTS = {".mp4", ".mov", ".mkv", ".avi"}

def load_media_list() -> list[str]:
    """Return absolute paths of all supported media image files found in MEDIA_DIR."""
    return [
        os.path.join(MEDIA_DIR, f)
        for f in sorted(os.listdir(MEDIA_DIR))
        if os.path.splitext(f)[1].lower() in IMG_EXTS | VID_EXTS
    ]


def load_surface(path: str) -> pygame.Surface:
    """Load an image file into a pygame Surface.
       Falls back to Pillow if SDL_image lacks PNG/JPEG support."""
    try:
        return pygame.image.load(path)
    except pygame.error:
        from PIL import Image  # lazy import
        pil_img = Image.open(path).convert("RGBA")
        mode = pil_img.mode
        size: Tuple[int, int] = pil_img.size
        data = pil_img.tobytes()
        return pygame.image.frombuffer(data, size, mode).convert_alpha()


def display_image(screen: pygame.Surface, path: str) -> None:
    """Draw *path* centred and scaled to fit the current screen."""
    try:
        img = load_surface(path)
        img = img.convert_alpha() if img.get_alpha() else img.convert()
        sw, sh = screen.get_size()
        iw, ih = img.get_size()
        scale = min(sw / iw, sh / ih)
        img = pygame.transform.smoothscale(img, (int(iw * scale), int(ih * scale)))
        rect = img.get_rect(center=(sw // 2, sh // 2))
        screen.fill((0, 0, 0))
        screen.blit(img, rect)
        pygame.display.flip()
    except Exception as e:
        dbg(f"ERROR loading image {path}: {e}")
        screen.fill((255, 0, 0))  # red screen to signal error
        pygame.display.flip()


def play_video(path: str) -> subprocess.Popen | None:
    """Play *path* full‑screen using the first video player found.
       Returns the subprocess handle, or None if no player is available."""
    # Prefer omxplayer → VLC (cvlc) → mpv
    if shutil.which("omxplayer"):
        cmd = ["omxplayer", "--no-osd", "--aspect-mode", "fill", path]
    elif shutil.which("cvlc"):
        cmd = ["cvlc", "--fullscreen", "--no-video-title-show", "--play-and-exit", path]
    elif shutil.which("mpv"):
        cmd = ["mpv", "--fs", "--quiet", path]
    else:
        print("No supported video player (omxplayer, VLC, or mpv) found in PATH.", file=sys.stderr)
        return None

    dbg(f"Launching video player: {' '.join(cmd)}")
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main() -> None:
    media = load_media_list()
    dbg(f"Loaded {len(media)} media files:")
    for i, p in enumerate(media):
        dbg(f"  {i}: {p}")
    if not media:
        print(f"No supported media found in {MEDIA_DIR}", file=sys.stderr)
        sys.exit(1)

    pygame.init()
    start_evdev_listener()
    screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
    pygame.display.set_caption("Touch Media Viewer")
    clock = pygame.time.Clock()
    current_proc: subprocess.Popen | None = None
    last_was_video = False

    MIN_VIEW_MS = 1000  # minimum time (in ms) each item stays before next advance
    last_show_ms = pygame.time.get_ticks()

    idx = 0

    def show(idx: int) -> None:
        dbg(f"show({idx}) called")
        nonlocal current_proc, last_was_video, screen
        nonlocal last_show_ms
        last_show_ms = pygame.time.get_ticks()
        path = media[idx]
        dbg(f"  path={path}")
        ext = os.path.splitext(path)[1].lower()
        dbg(f"  ext={ext}  (image={ext in IMG_EXTS})")

        # stop any running video
        if current_proc and current_proc.poll() is None:
            dbg("Terminating current video")
            current_proc.terminate()
            current_proc.wait(timeout=2)
            current_proc = None
            # flag so that next image triggers a display reset
            last_was_video = True

        if ext in IMG_EXTS:
            if last_was_video:
                blackout(screen)
                screen = reset_display()
            display_image(screen, path)
            last_was_video = False
        else:
            current_proc = play_video(path)
            if current_proc is None:
                # fallback: display a black screen if video can't play
                blackout(screen)
                screen.fill((0, 0, 0))
                pygame.display.flip()
            last_was_video = True

    show(idx)

    running = True
    while running:
        for event in pygame.event.get():
            dbg(f"Event: {pygame.event.event_name(event.type)}")

            if event.type == pygame.QUIT:
                running = False

            elif event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                running = False

            # ――― Touch / mouse handling ―――
            elif event.type in (pygame.FINGERDOWN, pygame.MOUSEBUTTONDOWN):
                if pygame.time.get_ticks() - last_show_ms >= MIN_VIEW_MS:
                    idx = (idx + 1) % len(media)
                    show(idx)
                else:
                    dbg("Press ignored (under minimum view time)")

        # ――― Auto‑advance if the current video finished ―――
        if current_proc and current_proc.poll() is not None:
            current_proc = None
            last_was_video = True
            idx = (idx + 1) % len(media)
            show(idx)

        # ――― Handle evdev touches ―――
        while not touch_queue.empty():
            touch_queue.get_nowait()
            if pygame.time.get_ticks() - last_show_ms >= MIN_VIEW_MS:
                idx = (idx + 1) % len(media)
                show(idx)
            else:
                dbg("Evdev press ignored (under minimum view time)")

        clock.tick(30)

    if current_proc and current_proc.poll() is None:
        current_proc.terminate()

    pygame.quit()
    sys.exit()


if __name__ == "__main__":
    main()