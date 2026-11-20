#!/usr/bin/env python3
"""Print to MUNBYN ITPP130 / ITPP130B label printers from Linux, no driver, no CUPS.

The printer speaks TSPL. The vendor macOS driver is only a CUPS filter that turns
pages into TSPL, and the Windows Bluetooth driver only pairs the printer (PIN 0000)
and opens a Serial Port Profile (SPP) channel. This module does both jobs itself:

    PDF / image -> 1-bit bitmap -> TSPL -> Bluetooth SPP / BLE or /dev/usb/lp0
"""

import argparse
import asyncio
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image

DPI = 203
LABEL_MM = (100, 150)  # 4x6 in shipping label
BLE_WRITE_UUID = "49535343-8841-43f4-a8d4-ecbe34729bb3"


# ── Rendering ────────────────────────────────────────────────────────────────


def render_pdf(data: bytes, page: int = 1) -> Image.Image:
    """Rasterise one PDF page at printer resolution with poppler's pdftoppm."""
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "in.pdf")
        with open(src, "wb") as f:
            f.write(data)
        subprocess.run(
            ["pdftoppm", "-r", str(DPI), "-f", str(page), "-l", str(page),
             "-png", "-singlefile", src, os.path.join(tmp, "out")],
            check=True, capture_output=True, timeout=60,
        )
        img = Image.open(os.path.join(tmp, "out.png")).convert("L")
        # Shipping PDFs can place a 4x6 label inside an otherwise blank A4 page.
        bounds = img.point(lambda value: 255 if value < 240 else 0).getbbox()
        return img.crop(bounds) if bounds else img


def fit(img: Image.Image, label_mm=LABEL_MM) -> Image.Image:
    """Rotate landscape art to portrait, scale it to fit the label, centre it."""
    w, h = (round(mm * DPI / 25.4) for mm in label_mm)
    img = img.convert("L")
    if (img.width > img.height) != (w > h):
        img = img.rotate(90, expand=True)
    img.thumbnail((w, h), Image.LANCZOS)
    canvas = Image.new("L", (w, h), 255)
    canvas.paste(img, ((w - img.width) // 2, (h - img.height) // 2))
    return canvas


def to_tspl(img: Image.Image, label_mm=LABEL_MM, gap_mm: int = 3,
            density: int = 8, copies: int = 1, offset_mm=(0, 0)) -> bytes:
    """Build one TSPL job. PIL mode "1" stores black as 0, which is exactly what
    TSPL BITMAP mode 0 wants (0 = burn a dot), so the packed bytes go straight in."""
    img = img.convert("L")
    if any(offset_mm):  # per-printer calibration: +x right, +y down, in mm
        shifted = Image.new("L", img.size, 255)
        shifted.paste(img, tuple(round(mm * DPI / 25.4) for mm in offset_mm))
        img = shifted
    if img.width % 8:  # pad rows to whole bytes with white, never stray black dots
        padded = Image.new("L", (img.width + 8 - img.width % 8, img.height), 255)
        padded.paste(img, (0, 0))
        img = padded
    bits = img.convert("1").tobytes()
    header = (
        f"SIZE {label_mm[0]} mm,{label_mm[1]} mm\r\n"
        f"GAP {gap_mm} mm,0 mm\r\n"
        f"DIRECTION 0,0\r\n"
        f"DENSITY {density}\r\n"
        f"CLS\r\n"
        f"BITMAP 0,0,{img.width // 8},{img.height},0,"
    ).encode("ascii")
    return header + bits + f"\r\nPRINT 1,{copies}\r\n".encode("ascii")


def file_to_tspl(data: bytes, **opts) -> bytes:
    """PDF or any Pillow-readable image -> TSPL. Raw TSPL passes through."""
    if data.startswith(b"%PDF"):
        img = render_pdf(data)
    elif data.startswith((b"SIZE", b"CLS")):
        return data
    else:
        img = Image.open(io.BytesIO(data))
    label_mm = opts.pop("label_mm", LABEL_MM)
    return to_tspl(fit(img, label_mm), label_mm=label_mm, **opts)


# ── Transports ───────────────────────────────────────────────────────────────


def send_bluetooth(payload: bytes, mac: str, channel: int = 1, timeout: float = 20) -> None:
    """Send over classic Bluetooth SPP (RFCOMM). Pair once first, PIN 0000."""
    with socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM) as s:
        s.settimeout(timeout)
        s.connect((mac, channel))
        s.sendall(payload)
        # ponytail: fixed drain delay so the printer reads the tail before the link
        # drops; replace with a TSPL status read-back if labels ever come out cut.
        time.sleep(1.0)


def send_usb(payload: bytes, device: str = "/dev/usb/lp0") -> None:
    """Write to the kernel usblp node. No O_CREAT: unplugged must fail, not
    silently create a plain file where the device belongs."""
    with open(os.open(device, os.O_WRONLY), "wb", buffering=0) as dev:
        dev.write(payload)


def send_ble(payload: bytes, mac: str, timeout: float = 20) -> None:
    """Write TSPL to the ITPP130B BLE serial characteristic at an exact address."""
    try:
        from bleak import BleakClient
    except ImportError as exc:
        raise OSError("BLE requires: pip install 'munbyn-print[ble]'") from exc

    async def write():
        async with BleakClient(mac, timeout=timeout) as client:
            characteristic = client.services.get_characteristic(BLE_WRITE_UUID)
            if characteristic is None:
                raise OSError(f"printer lacks BLE characteristic {BLE_WRITE_UUID}")
            # An acknowledged write per chunk took minutes per label. Unacknowledged
            # writes return once queued locally, so the last chunk is acknowledged:
            # ATT is ordered, so its response means every earlier chunk went out.
            fast = "write-without-response" in characteristic.properties
            # The printer has no flow control for unacknowledged writes: 182-byte
            # chunks (MTU 185, when negotiated) garbled labels; 20-byte chunks print
            # clean. Its ~11.5 KB/s serial drain caps throughput either way.
            chunk_size = 20 if fast else min(512, characteristic.max_write_without_response_size)
            last = max(0, len(payload) - chunk_size)
            for start in range(0, len(payload), chunk_size):
                await asyncio.wait_for(
                    client.write_gatt_char(
                        characteristic, payload[start:start + chunk_size],
                        response=not fast or start >= last,
                    ), timeout=timeout if start < last else max(timeout, 120),
                )
            # shortcut: fixed drain delay; use printer status if jobs are truncated.
            await asyncio.sleep(1)

    try:
        asyncio.run(write())
    except Exception as exc:
        # Never retry a partial job automatically: it could print duplicate labels.
        raise OSError(f"BLE write failed for {mac}: {exc}") from exc


def send(payload: bytes, target: str, channel: int = 1, ble: bool = False) -> None:
    """target is a Bluetooth MAC (AA:BB:CC:DD:EE:FF) or a device path."""
    if ble:
        send_ble(payload, target)
    elif target.startswith("/"):
        send_usb(payload, target)
    else:
        send_bluetooth(payload, target, channel)


# ── HTTP print server ────────────────────────────────────────────────────────


def serve(target: str, host: str, port: int, channel: int = 1,
          ble: bool = False, **opts) -> None:
    """POST a PDF, image or TSPL body to /print. One job at a time; the response
    says whether it reached the printer, so callers own their retry policy."""
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, code: int, body: dict) -> None:
            raw = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if self.path != "/health":
                return self._reply(404, {"error": "not found"})
            self._reply(200, {"ok": True, "target": target})

        def do_POST(self):
            if self.path != "/print":
                return self._reply(404, {"error": "not found"})
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= 20_000_000:
                return self._reply(400, {"error": "body must be 1 byte to 20 MB"})
            data = self.rfile.read(length)
            try:
                payload = file_to_tspl(data, **opts)
            except Exception as exc:
                return self._reply(400, {"error": f"could not render: {exc}"})
            try:
                with lock:
                    send(payload, target, channel, ble=ble)
            except OSError as exc:
                self.log_error("printer unreachable: %s", exc)
                return self._reply(503, {"error": f"printer unreachable: {exc}"})
            self._reply(200, {"ok": True, "bytes": len(payload)})

    print(f"munbyn-print: http://{host}:{port}/print -> {target}", file=sys.stderr)
    ThreadingHTTPServer((host, port), Handler).serve_forever()


# ── CLI ──────────────────────────────────────────────────────────────────────


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="munbyn-print", description=__doc__.splitlines()[0])
    p.add_argument("--printer", default=os.environ.get("MUNBYN_PRINTER", "/dev/usb/lp0"),
                   help="Bluetooth MAC or USB device path (env MUNBYN_PRINTER)")
    p.add_argument("--channel", type=int, default=int(os.environ.get("MUNBYN_CHANNEL", "1")),
                   help="RFCOMM channel (default 1)")
    p.add_argument("--ble", action="store_true", help="use BLE instead of classic Bluetooth SPP")
    p.add_argument("--size", default="100x150", help="label size in mm, WxH (default 100x150)")
    p.add_argument("--density", type=int, default=8, help="print darkness 0-15 (default 8)")
    p.add_argument("--offset", default=os.environ.get("MUNBYN_OFFSET", "0,0"),
                   help="calibration shift in mm, X,Y; +X right, +Y down (env MUNBYN_OFFSET)")
    sub = p.add_subparsers(dest="cmd", required=True)
    pr = sub.add_parser("print", help="print a PDF, image or TSPL file ('-' for stdin)")
    pr.add_argument("file")
    pr.add_argument("--copies", type=int, default=1)
    pr.add_argument("--dry-run", metavar="OUT", help="write the TSPL here instead of printing")
    sv = sub.add_parser("serve", help="run the HTTP print server")
    sv.add_argument("--host", default="0.0.0.0")
    sv.add_argument("--port", type=int, default=8130)
    a = p.parse_args(argv)

    label_mm = tuple(int(x) for x in a.size.lower().split("x"))
    offset_mm = tuple(float(x) for x in a.offset.split(","))
    opts = {"label_mm": label_mm, "density": a.density, "offset_mm": offset_mm}
    if a.cmd == "serve":
        return serve(a.printer, a.host, a.port, a.channel, ble=a.ble, **opts)
    data = sys.stdin.buffer.read() if a.file == "-" else open(a.file, "rb").read()
    payload = file_to_tspl(data, copies=a.copies, **opts)
    if a.dry_run:
        with open(a.dry_run, "wb") as f:
            f.write(payload)
    else:
        send(payload, a.printer, a.channel, ble=a.ble)


if __name__ == "__main__":
    main()
