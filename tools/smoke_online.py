"""Smoke-test the published site (online mode, PeerJS cloud broker).

    pip install -r tools/requirements-demo.txt
    python tools/smoke_online.py [URL]      # default: https://lukesh2006.github.io/pairchat/

Two separate browser sessions (like two people on different networks) open the site:
the host creates a room, the guest opens the invite link and joins, the host accepts,
then they exchange a message and a split block of commands. Exits non-zero on failure.
"""
from __future__ import annotations

import sys

from playwright.sync_api import sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "https://lukesh2006.github.io/pairchat/"
sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # Windows consoles default to cp1252


def main() -> int:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="msedge")
        host = browser.new_context().new_page()
        guest = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True).new_page()

        host.goto(URL)
        badge = host.inner_text("#modeBadge")
        assert "Online" in badge, f"expected online mode, badge says {badge!r}"
        host.fill("#nameInput", "Host")
        host.click("#createBtn")
        host.wait_for_selector("#invite:not(.hidden)", timeout=20000)
        invite = host.inner_text("#inviteLink").strip()
        assert invite.startswith(URL.rstrip("/")) and "#" in invite, f"bad invite link {invite!r}"
        print("room created, invite:", invite)

        guest.goto(invite)
        guest.fill("#nameInput", "Friend")
        guest.click("#joinBtn")
        host.wait_for_selector("#modal:not(.hidden)", timeout=30000)
        host.click("#acceptBtn")
        guest.wait_for_selector("#chat:not(.hidden)", timeout=20000)
        host.click("#inviteClose")
        print("guest joined:", guest.inner_text("#membersLine"))

        host.fill("#msgInput", "Hello from far away!")
        host.click("#sendBtn")
        guest.wait_for_selector(".msg:not(.mine) >> text=Hello from far away!", timeout=15000)

        guest.fill("#msgInput", "git pull\npython3 app/serve.py")
        guest.click("#sendBtn")
        guest.wait_for_selector("#splitBar:not(.hidden)")
        guest.click("#splitYes")
        host.wait_for_selector(".msg:not(.mine) pre >> text=python3 app/serve.py", timeout=15000)
        received = host.locator(".msg:not(.mine) pre").all_inner_texts()
        assert received[-2:] == ["git pull", "python3 app/serve.py"], received
        host.wait_for_selector(".msg.mine .tick.ok", timeout=10000)
        print("messages, split commands and delivered ticks OK:", received[-2:])
        browser.close()
    print("ONLINE SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
