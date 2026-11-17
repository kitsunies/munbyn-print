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
  UUID `00001101-0000-1000-8000-00805f9b34fb`) and pairing PIN `0000`.
  Static inspection of the Munbyn Print Android app also found BLE transports
  and TSPL support. The transport used by each ITPP130B variant is not yet verified.
- Over USB it's a standard USB printer class device (`5958:0130`), so the
  kernel's `usblp` gives you `/dev/usb/lp0`.

This tool renders PDFs and images to a 1-bit bitmap, wraps it in TSPL and sends
it over an RFCOMM socket (Python stdlib) or to `/dev/usb/lp0`.

Tested on an ITPP130B using BLE from Debian 13 with BlueZ 5.82: a physical TSPL
test label was confirmed on 2026-11-17. Classic SPP and USB remain unverified on
this hardware. Sending bytes successfully does not confirm a physical label printed.

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
`49535343-8841-43f4-a8d4-ecbe34729bb3`. Writes are acknowledged and split into
chunks bounded by the characteristic's reported write size. No automatic retry
is performed, because a failed job may already have printed partly.
The current acknowledged-write path is conservative: a full 100x150 mm bitmap
transfer took several minutes in testing.

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

Landscape artwork is rotated and everything is scaled to fit the label.
Options: `--size 100x150` (mm, default 4x6 in), `--density 0-15`, `--channel`
(RFCOMM channel, default 1). `MUNBYN_PRINTER` and `MUNBYN_CHANNEL` env vars work too.

## Print server

```bash
munbyn-print --printer AA:BB:CC:DD:EE:FF serve --port 8130
# For a BLE printer, add --ble before serve.
curl --data-binary @label.pdf http://printhost:8130/print
```

`POST /print` takes a PDF, PNG/JPEG or raw TSPL body and returns `200` once the
transport write completes, `503` if the printer is unreachable (off, out of range,
connected to a phone) and `400` if the file can't be rendered. Jobs print one at
a time. There is no queue: the caller decides whether to retry. `GET /health`
returns `{"ok": true}`.

The server has no authentication. Run it on a trusted network with access limited
to intended clients. `/health` reports server availability, not printer readiness.

`munbyn-print.service` is a systemd unit for running it permanently.
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
