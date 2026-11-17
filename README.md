# munbyn-print

Print to MUNBYN ITPP130 / ITPP130B thermal label printers from Linux, over
**Bluetooth** or USB. No vendor driver, no CUPS, no Raspberry Pi print box.
Any Linux machine with a Bluetooth radio near the printer becomes a network
print server.

MUNBYN ships drivers for Windows, macOS and ChromeOS only. You don't need any of them:

- The printer speaks **TSPL**. The macOS driver is only a CUPS raster filter
  (`rastertolabel`) that writes `SIZE` / `GAP` / `DIRECTION` / `BITMAP` /
  `PRINT` commands to `usb://`.
- The ITPP130B's Bluetooth is **classic Bluetooth SPP** (Serial Port Profile,
  UUID `00001101-0000-1000-8000-00805f9b34fb`), not BLE. The Windows Bluetooth
  driver only pairs with PIN `0000` and sends the same TSPL down that serial channel.
- Over USB it's a standard USB printer class device (`5958:0130`), so the
  kernel's `usblp` gives you `/dev/usb/lp0`.

This tool renders PDFs and images to a 1-bit bitmap, wraps it in TSPL and sends
it over an RFCOMM socket (Python stdlib) or to `/dev/usb/lp0`.

## Install

```bash
sudo apt-get install -y poppler-utils bluez   # pdftoppm for PDFs; bluez for pairing
pip install git+https://github.com/kitsunies/munbyn-print
```

The only Python dependency is Pillow.

## Pair once (Bluetooth)

Turn the printer on and disconnect it from any phone (it stops advertising while
a phone is connected), then:

```bash
bluetoothctl
  power on
  agent on
  scan on                 # wait for the ITPP130B to appear, note its MAC
  pair AA:BB:CC:DD:EE:FF  # PIN 0000 if asked
  trust AA:BB:CC:DD:EE:FF
  quit
```

## Print

```bash
munbyn-print --printer AA:BB:CC:DD:EE:FF print label.pdf
munbyn-print --printer /dev/usb/lp0 print photo.png --copies 2
munbyn-print print label.pdf --dry-run out.tspl     # inspect the TSPL, print nothing
```

Landscape artwork is rotated and everything is scaled to fit the label.
Options: `--size 100x150` (mm, default 4x6 in), `--density 0-15`, `--channel`
(RFCOMM channel, default 1). `MUNBYN_PRINTER` and `MUNBYN_CHANNEL` env vars work too.

## Print server

```bash
munbyn-print --printer AA:BB:CC:DD:EE:FF serve --port 8130
curl --data-binary @label.pdf http://printhost:8130/print
```

`POST /print` takes a PDF, PNG/JPEG or raw TSPL body and returns `200` once the
job reached the printer, `503` if the printer is unreachable (off, out of range,
connected to a phone) and `400` if the file can't be rendered. Jobs print one at
a time. There is no queue: the caller decides whether to retry. `GET /health`
returns `{"ok": true}`.

`munbyn-print.service` is a systemd unit for running it permanently.

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
