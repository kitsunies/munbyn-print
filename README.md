# munbyn-print

Print to MUNBYN ITPP130 / ITPP130B thermal label printers from Linux, over
**Bluetooth** or USB. No vendor driver, no CUPS, no Raspberry Pi print box.
Any Linux machine with a Bluetooth radio near the printer becomes a network
print server.

MUNBYN ships drivers for Windows, macOS and ChromeOS only. You don't need any of them:

- The printer speaks **TSPL**. The macOS driver is only a CUPS raster filter
  (`rastertolabel`) that writes `SIZE` / `GAP` / `DIRECTION` / `BITMAP` /
  `PRINT` commands to `usb://`.
- The Windows Bluetooth driver uses **classic Bluetooth SPP** (Serial Port Profile,
  UUID `00001101-0000-1000-8000-00805f9b34fb`) and pairing PIN `0000`. The Munbyn
  Print Android app also prints to the ITPP130B over classic SPP (RFCOMM channel 1).
- Over USB it's a standard USB printer class device (`5958:0130`), so the
  kernel's `usblp` gives you `/dev/usb/lp0`.

This tool renders PDFs and images to a 1-bit bitmap, wraps it in TSPL and sends
it over an RFCOMM socket (Python stdlib) or to `/dev/usb/lp0`.

Tested on an ITPP130B from Debian 13 with BlueZ 5.82: full 100x150 mm bitmap
labels over BLE (Bleak 3.1.0) on 2026-11-17 and over classic SPP on 2026-11-21.
USB remains unverified on this hardware. A successful write alone does not confirm
a physical label printed.

The ITPP130B is dual-mode with **two addresses**: classic SPP answers on one
(`DC:1D:...` on the tested unit, advertising `ITPP130B-XXXX`) and BLE on another
(`DC:0D:...`, advertising `ITPP130B-XXXX-LE`). Find the classic one with
`hcitool scan`. The tested unit accepted RFCOMM without pairing.

Classic SPP is the faster path: the printer drains about 20 KB/s over SPP against
about 11.5 KB/s over BLE. Bitmaps are cropped to their inked area (pixel-identical
output), so a typical shipping label is about 90 KB: about 4.5 s over SPP once
connected. Connecting takes up to about 4.5 s, so the print server keeps the link
open between jobs (see below).

## Install

```bash
sudo apt-get install -y poppler-utils bluez   # pdftoppm for PDFs; bluez for pairing
pip install git+https://github.com/kitsunies/munbyn-print
```

The only Python dependency is Pillow.

For BLE, install the optional extra instead:

```bash
pip install 'munbyn-print[ble] @ git+https://github.com/kitsunies/munbyn-print'
```

BLE uses Bleak and the printer's serial service
`49535343-fe7d-4ae5-8fa9-9fafd205e455`, writing TSPL to characteristic
`49535343-8841-43f4-a8d4-ecbe34729bb3`. Writes go in 20-byte chunks without
acknowledgement, except the last chunk, whose response confirms every earlier
chunk left the adapter before disconnecting. A full 100x150 mm label takes about
11 s, against 214 s with every chunk acknowledged. The printer can negotiate a
185-byte MTU, but it has no flow control for unacknowledged writes: 182-byte
chunks garbled labels. Its serial link drains about 11.5 KB/s either way. No
automatic retry is performed, because a failed job may already have printed partly.

`--compress` (env `MUNBYN_COMPRESS=1`) sends the vendor `BITMAP` mode 3 that the
Munbyn Print app uses: `BITMAP x,y,wBytes,h,3,<len>,<data>`, where the rows
(1 = black) pass through a fixed self-inverse 256-byte table (`Code941` in the
app's `libPrinterNative.so`) and then zlib. A shipping label shrinks from about
98 KB to about 15 KB. Plain zlib without the table prints vertical streaks,
because the printer applies the table after inflating.

Power-cycle the printer after any aborted job: it can be left waiting for the
rest of a bitmap and would swallow the next label.

Disconnect the phone before scanning or printing. For a printer advertising as
`ITPP130B-…-LE`, use its exact BLE MAC with `--ble`. The tested BLE endpoint did
not require pairing or a PIN. The next section applies to classic SPP only.

If you connected manually with `bluetoothctl`, disconnect that session before
using the CLI: `bluetoothctl disconnect AA:BB:CC:DD:EE:FF`. An existing connection
can stop advertising, causing Bleak's address scan to report the device missing.

## Pair once (Bluetooth)

Turn the printer on and disconnect it from any phone (it stops advertising while
a phone is connected), then:

```bash
bluetoothctl
  power on
  agent on
  scan on                 # identify your printer by its exact MAC, not its name
  pair AA:BB:CC:DD:EE:FF  # PIN 0000 if asked
  trust AA:BB:CC:DD:EE:FF
  quit
```

Multiple printers can advertise the same model name. Verify the MAC belongs to
your intended printer before pairing or printing.

## Print

```bash
munbyn-print --printer AA:BB:CC:DD:EE:FF print label.pdf
munbyn-print --ble --printer AA:BB:CC:DD:EE:FF print label.pdf
munbyn-print --printer /dev/usb/lp0 print photo.png --copies 2
munbyn-print print label.pdf --dry-run out.tspl     # inspect the TSPL, print nothing
```

PDF page whitespace is cropped to the visible label content, including shipping
labels placed on A4 pages. Landscape artwork is rotated and everything is scaled
to fit the label. Image files preserve their supplied margins.
Options: `--size 100x150` (mm, default 4x6 in), `--density 0-15`, `--channel`
(RFCOMM channel, default 1), `--offset X,Y` (calibration shift in mm, +X right,
+Y down). `MUNBYN_PRINTER`, `MUNBYN_CHANNEL` and `MUNBYN_OFFSET` env vars work too.

## Print server

```bash
munbyn-print --printer AA:BB:CC:DD:EE:FF serve --port 8130
# For a BLE printer, add --ble before serve.
curl --data-binary @label.pdf http://printhost:8130/print
```

`POST /print` takes a PDF, PNG/JPEG or raw TSPL body and returns `200` once the
transport write completes (over SPP: once the kernel send queue has drained), `503` if the printer is unreachable (off, out of range,
connected to a phone) and `400` if the file can't be rendered. Jobs print one at
a time. There is no queue: the caller decides whether to retry. `GET /health`
returns `{"ok": true}`.

The server has no authentication. Run it on a trusted network with access limited
to intended clients. `/health` reports server availability, not printer readiness.

Over classic SPP the server keeps the RFCOMM link open for 30 s after each job,
so a batch of labels pays the connect once; it then disconnects so a phone app
can reach the printer. A link the printer dropped (power-cycle) is detected and
reopened before the next job. Failed writes are never retried automatically.

`munbyn-print.service` is a systemd unit for running it permanently.
Create its dedicated account before enabling it:

```bash
sudo useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin munbyn-print
```

Use a static system account for BLE: a DynamicUser account failed system D-Bus
authentication on the tested Debian host, before any print bytes were sent.
For BLE, add `--ble` before `serve` in its `ExecStart`. Bleak requires access to
the system D-Bus and permission to use BlueZ; container BLE setups also need
access to the host's system D-Bus socket.

### In a container

Linux only allows Bluetooth sockets in the host network namespace, so a
container or Kubernetes pod needs `--network host` / `hostNetwork: true`. USB
needs the device passed in (`--device /dev/usb/lp0`).

## Other printers

The same approach should work for other TSPL label printers that use SPP or
usblp, such as other MUNBYN models and many Chinese 4x6 shipping printers. If you try
one, please open an issue with the result.

## Development

```bash
pip install Pillow pytest && pytest -q
```

MIT licensed. Not affiliated with MUNBYN.
