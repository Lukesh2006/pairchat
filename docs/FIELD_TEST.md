# Field test: three physical devices

**Date:** 2026-11-25
**Result:** ✅ Windows laptop, Linux laptop and Android phone chatting in one room through the offline hub, after three fixes found during the test.

## Setup

| Role | Device | Browser | Address on the hotspot |
|---|---|---|---|
| Hub and host | Windows 11 laptop | Chrome / Edge | 10.247.230.242 |
| Guest | Linux laptop | Firefox 154 | 10.247.230.152 |
| Guest | Android phone (also providing the hotspot) | Chrome | 10.247.230.128 |

All three were connected to the **phone's Wi-Fi hotspot**. Pairing went through `serve.py` on the Windows laptop, not the internet.

## What happened, step by step

### 1. Phone stuck on "Connecting…" → fixed
The phone loaded the page from the laptop (the green *Offline mode* badge showed), but stayed on "Connecting…", and the host never saw a join request.

- **Cause:** browsers hide their local IP behind random `.local` (mDNS) names. Across a phone hotspot the devices couldn't resolve each other's names, so the direct WebRTC link never opened, and the guest only sends its join request once that link is open.
- **Fix:**
  1. The hub now rewrites `.local` candidates to the real LAN address it sees for each device.
  2. If the direct link still isn't open after 6 s, chat falls back to a **relay through the hub**.
  3. The hub logs each step.

### 2. Linux laptop (Firefox): "Lost contact with the laptop running serve.py" → fixed
- **Cause:** Python's `BaseHTTPRequestHandler` answered the WebSocket upgrade with `HTTP/1.0 101`. Chrome tolerates that; **Firefox rejects it**, because RFC 6455 requires HTTP/1.1.
- **Fix:** the hub writes `HTTP/1.1 101 Switching Protocols` itself. Covered by `test_handshake_is_http11_and_says_open`.
- Also added: `check.html` self-test page, the reason each pairing connection ended in the hub log, and the browser name in the log.

### 3. "No room with that code" between the two laptops → fixed
- **Cause:** **two hubs were running** (ports 8765 and 8767). The Windows laptop created its room on one and the Linux laptop looked on the other. Rooms only exist on the hub that created them.
- **Fix:** run one hub. `serve.py` now warns at startup if another hub is running on this computer, and the app's error names the hub it checked.

## Self-test result from the Linux laptop

`check.html`, reported to the hub console:

```
===== Connection check from 10.247.230.152 =====
[INFO] Browser: Mozilla/5.0 (X11; Linux x86_64; rv:154.0) Gecko/20100101 Firefox/154.0
[PASS] WebSocket support: available
[PASS] WebRTC support: available
[PASS] Reach the laptop hub: served by serve.py at 10.247.230.242:8767
[PASS] Get a device ID: 685b28074859d157
[PASS] Pairing connection (WebSocket): opened in 37 ms, hub said hello
[PASS] Connection stays up (12 s): stable
[PASS] Hub relay (backup path): messages can travel through the laptop
[PASS] WebRTC on this device: data channel opened · 5 candidate(s): 4 hidden (.local), visible IPs: none
```

## Hub log excerpt (real devices)

```
[hub] device 10.247.230.152 connected for pairing (Firefox on Linux)
[hub] join attempt for unknown room 142762 from 10.247.230.152        <- room was on the other hub (issue 3)
[hub] room 146271 created on 10.247.230.152                           <- Linux laptop hosts a room
[hub] device 10.247.230.128 opened the app
[hub] device 10.247.230.128 connected for pairing (Chrome on Android)
[hub] join request for room 146271 from 10.247.230.128
[hub] device 02419a <-> room 146271: direct link unavailable, relaying chat through this laptop
```

The last line shows the hotspot blocking the direct link and the relay taking over automatically, which is exactly what fix 1 was built for.

## Final check
With a single hub on port 8765: the Windows laptop created a room, and the Linux laptop and the phone both joined and chatted. All three devices worked.
