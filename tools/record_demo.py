"""Record the PairChat demo video: three devices chatting through the offline hub.

    pip install -r tools/requirements-demo.txt
    python tools/record_demo.py            # uses Microsoft Edge (or --browser chrome)

What it does
  1. Starts a real hub (app/serve.py) and saves its console log.
  2. Opens three real browser windows, each recorded to video:
       * "Windows laptop" - creates the room and accepts join requests
       * "Linux laptop"   - joins with the invite link over the LAN address
       * "Phone"          - phone-sized screen, joins through the hub relay (?relay),
                             the same path a phone hotspot falls back to
  3. Chats, pastes a block of commands (split prompt -> one message per command),
     copies one with the Copy button, sends a link and a photo, leaves - then stitches the three recordings side by side
     into docs/demo/pairchat-demo.mp4 (+ a GIF preview) and saves screenshots.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import imageio_ffmpeg
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "docs" / "demo"
SHOTS = ROOT / "docs" / "screenshots"
PORT = 8790
LAPTOP = {"width": 960, "height": 640}
PHONE = {"width": 390, "height": 844}


def wait_for_hub(port: int) -> dict:
    for _ in range(100):
        try:
            return json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/pairchat-local.json", timeout=0.5).read())
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("hub did not start")


def pause(seconds: float) -> None:
    time.sleep(seconds)


def type_slowly(page, selector: str, text: str, delay: int = 55) -> None:
    page.click(selector)
    page.type(selector, text, delay=delay)


def send(page, text: str) -> None:
    type_slowly(page, "#msgInput", text)
    pause(0.4)
    page.click("#sendBtn")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--browser", choices=["msedge", "chrome"], default="msedge")
    ap.add_argument("--headed", action="store_true", help="show the windows while recording")
    args = ap.parse_args()

    DEMO.mkdir(parents=True, exist_ok=True)
    SHOTS.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="pairchat-rec-"))
    log_path = DEMO / "demo-hub-log.txt"

    raw_log = work / "hub.log"
    hub = subprocess.Popen([sys.executable, "-u", str(ROOT / "app" / "serve.py"), "--port", str(PORT),
                            "--no-browser", "--skip-hub-check"],
                           stdout=open(raw_log, "w", encoding="utf-8"), stderr=subprocess.STDOUT)
    try:
        info = wait_for_hub(PORT)
        host_url = f"http://localhost:{PORT}/"
        lan = (info.get("lanUrls") or [host_url])[0]          # other devices use the laptop's network address
        print("hub up; other devices will use", lan)

        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel=args.browser, headless=not args.headed)
            starts = {}

            def device(name, size, **extra):
                ctx = browser.new_context(viewport=size, color_scheme="dark", record_video_dir=str(work / name),
                                          record_video_size=size, **extra)
                page = ctx.new_page()
                starts[name] = time.time()
                return ctx, page

            win_ctx, win = device("windows", LAPTOP)
            lin_ctx, lin = device("linux", LAPTOP)
            ph_ctx, ph = device("phone", PHONE, is_mobile=True, has_touch=True, device_scale_factor=2)

            for p in (win, lin, ph):
                p.goto(host_url if p is win else lan)
            pause(1.5)
            # Sync flash: a red frame shown in all three windows at the same moment. compose()
            # finds it in each recording, lines the three videos up on it, and cuts it out.
            for p in (win, lin, ph):
                p.evaluate("""() => { const d = document.createElement('div');
                    d.style.cssText = 'position:fixed;inset:0;background:#ff0000;z-index:2147483647';
                    document.body.append(d); setTimeout(() => d.remove(), 700); }""")
            pause(2)
            win.screenshot(path=str(SHOTS / "home.png"))

            # 1. Windows laptop creates the room
            type_slowly(win, "#nameInput", "Windows laptop")
            pause(0.5)
            win.click("#createBtn")
            win.wait_for_selector("#invite:not(.hidden)")
            code = win.inner_text("#inviteCode").strip()
            invite = win.inner_text("#inviteLink").strip()
            print("room", code, "invite", invite)
            pause(3)
            win.screenshot(path=str(SHOTS / "invite.png"))

            # 2. Linux laptop opens the invite link and joins
            lin.goto(invite)
            pause(1.2)
            type_slowly(lin, "#nameInput", "Linux laptop")
            pause(0.6)
            lin.click("#joinBtn")
            win.wait_for_selector("#modal:not(.hidden)")
            pause(2)
            win.screenshot(path=str(SHOTS / "join-request.png"))
            win.click("#acceptBtn")
            lin.wait_for_selector("#chat:not(.hidden)")
            pause(1.5)

            # 3. Phone opens the invite (relay path, as on a phone hotspot) and joins
            relay_invite = invite.replace(f":{PORT}/#", f":{PORT}/?relay#")
            ph.goto(relay_invite)
            pause(1.5)
            ph.screenshot(path=str(SHOTS / "phone-invited.png"))
            type_slowly(ph, "#nameInput", "Phone")
            pause(0.6)
            ph.click("#joinBtn")
            win.wait_for_selector("#modal:not(.hidden)")
            pause(1.8)
            win.click("#acceptBtn")
            ph.wait_for_selector("#chat:not(.hidden)")
            pause(1)
            win.click("#inviteClose")
            pause(1.5)

            # 4. Chat
            send(win, "Hi both! This room has no internet - it runs on my laptop.")
            pause(1.5)
            send(lin, "Linux laptop here, joined from the invite link.")
            pause(1.5)
            send(ph, "Phone here! I came in through the hub relay.")
            pause(1.5)

            # 5. Paste a block of commands -> split prompt -> one message per command
            send(win, "Run these on the Linux laptop:")
            pause(1)
            win.fill("#msgInput", "cd ~/projects/pairchat\ngit pull\n\ndocker run \\\n  -p 8080:80 \\\n  nginx\npython3 app/serve.py --port 8765")
            pause(1.8)
            win.click("#sendBtn")
            win.wait_for_selector("#splitBar:not(.hidden)")
            pause(3.2)
            win.screenshot(path=str(SHOTS / "split-prompt.png"))
            win.click("#splitYes")
            pause(2)

            # 6. Linux laptop copies one command with the Copy button
            lin.locator(".msg:not(.mine)", has_text="git pull").locator(".copy-btn").click()
            pause(2.2)
            send(lin, "Copied 'git pull' - running it now")
            pause(1.5)

            # 7. A link and a photo from the phone
            send(ph, "Docs are here: https://github.com/Lukesh2006/pairchat")
            pause(1.5)
            photo = win.screenshot(type="png")
            ph.set_input_files("#fileInput", files=[{"name": "laptop-screen.png", "mimeType": "image/png", "buffer": photo}])
            pause(2.5)
            send(lin, "Got the photo on Linux too.")
            pause(2.5)
            win.screenshot(path=str(SHOTS / "chat-laptop.png"))
            ph.locator(".msg", has_text="Run these on").evaluate("el => el.scrollIntoView({block: 'start'})")
            pause(0.5)
            ph.screenshot(path=str(SHOTS / "commands-phone.png"))
            ph.evaluate("const m = document.querySelector('#messages'); m.scrollTop = m.scrollHeight")
            pause(1)
            ph.screenshot(path=str(SHOTS / "chat-phone.png"))

            # 8. Phone leaves
            ph.click("#leaveBtn")
            pause(3)

            videos = {}
            for name, ctx, page in (("windows", win_ctx, win), ("linux", lin_ctx, lin), ("phone", ph_ctx, ph)):
                video = page.video
                ctx.close()
                videos[name] = Path(video.path())
            browser.close()
    finally:
        hub.terminate()
        hub.wait(5)

    # Keep only the LAN address the devices used (drops VPN/Tailscale addresses from the published log)
    lines = raw_log.read_text(encoding="utf-8").splitlines()
    log_path.write_text("\n".join(l for l in lines if not l.strip().startswith("Other devices") or lan in l) + "\n",
                        encoding="utf-8")
    compose(videos, starts, work)
    shutil.rmtree(work, ignore_errors=True)
    print("done:", DEMO / "pairchat-demo.mp4")


def flash_time(video: Path) -> float | None:
    """Seconds into `video` where the red sync flash first appears."""
    reader = imageio_ffmpeg.read_frames(str(video), pix_fmt="rgb24")
    meta = next(reader)
    w, h = meta["size"]
    fps = meta.get("fps") or 25
    try:
        for i, frame in enumerate(reader):
            if i > fps * 30:
                break
            px = (h // 2 * w + w // 2) * 3                      # centre pixel
            r, g, b = frame[px], frame[px + 1], frame[px + 2]
            if r > 200 and g < 70 and b < 70:
                return i / fps
    finally:
        reader.close()
    return None


def compose(videos: dict, starts: dict, work: Path) -> None:
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    flashes = {name: flash_time(path) for name, path in videos.items()}
    print("sync flash found at", {k: (round(v, 2) if v is not None else None) for k, v in flashes.items()})
    if all(v is not None for v in flashes.values()):
        trims = {name: t + 0.8 for name, t in flashes.items()}      # start right after the flash
    else:                                                          # fall back to wall-clock alignment
        latest = max(starts.values())
        trims = {name: latest - starts[name] for name in videos}
    font = "C\\:/Windows/Fonts/segoeuib.ttf" if Path("C:/Windows/Fonts/segoeuib.ttf").exists() else ""
    label = lambda text, x, y: (f"drawtext=text='{text}':x={x}:y={y}:fontsize=22:fontcolor=white"
                                + (f":fontfile='{font}'" if font else ""))
    graph = ";".join([
        "[0:v]scale=720:480,setsar=1[a]",
        "[1:v]scale=720:480,setsar=1[b]",
        "[2:v]scale=452:980,setsar=1[c]",
        "color=c=0x0b0d24:s=1280x1080:r=25[bg]",
        "[bg][a]overlay=24:52:shortest=1[t1]",
        "[t1][b]overlay=24:584:shortest=1[t2]",
        "[t2][c]overlay=800:72:shortest=1[t3]",
        "[t3]" + ",".join([
            label("Windows laptop - creates the room (runs serve.py)", 24, 16),
            label("Linux laptop - joins with the invite link", 24, 548),
            label("Phone - joins by QR (hub relay)", 800, 36),
        ]) + "[out]",
    ])
    script = work / "graph.txt"
    script.write_text(graph, encoding="utf-8")
    inputs = []
    for name in ("windows", "linux", "phone"):
        inputs += ["-ss", f"{trims[name]:.2f}", "-i", str(videos[name])]
    mp4 = DEMO / "pairchat-demo.mp4"
    subprocess.run([ff, "-y", *inputs, "-filter_complex_script", str(script), "-map", "[out]",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "26", "-preset", "slow", "-movflags", "+faststart",
                    str(mp4)], check=True)
    gif = DEMO / "pairchat-demo.gif"
    subprocess.run([ff, "-y", "-i", str(mp4), "-vf",
                    # README preview: 1.5x speed, fewer colours -> stays well under GitHub's 10 MB image limit
                    "setpts=PTS/1.5,fps=6,scale=760:-1:flags=lanczos,split[s0][s1];[s0]palettegen=max_colors=64[p];[s1][p]paletteuse=dither=bayer:bayer_scale=4",
                    str(gif)], check=True)


if __name__ == "__main__":
    main()
