import io
import os
import socket
import sys
import threading
from types import SimpleNamespace
import urllib.request

import pytest
from PIL import Image, ImageDraw

import munbyn_print as mp


def test_ble_uses_exact_address_and_preserves_chunk_order(monkeypatch):
    calls, responses = [], []
    characteristic = SimpleNamespace(
        max_write_without_response_size=20, properties=["write", "write-without-response"],
    )

    class Client:
        def __init__(self, address, timeout):
            calls.append(address)
            self.services = SimpleNamespace(get_characteristic=lambda uuid: characteristic)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            calls.append("disconnected")

        async def write_gatt_char(self, char, data, response):
            assert char is characteristic
            assert 0 < len(data) <= 20
            calls.append(data)
            responses.append(response)

    monkeypatch.setitem(sys.modules, "bleak", SimpleNamespace(BleakClient=Client))
    payload = bytes(range(57))
    mp.send(payload, "AA:BB:CC:DD:EE:FF", ble=True)
    assert calls[0] == "AA:BB:CC:DD:EE:FF"
    assert b"".join(calls[1:-1]) == payload
    assert calls[-1] == "disconnected"
    # Only the final chunk is acknowledged: it proves the queued chunks were sent
    # before disconnecting, without a round trip per chunk.
    assert responses == [False, False, True]


def test_ble_unacknowledged_chunks_stay_20_bytes_at_large_mtu(monkeypatch):
    sizes, responses = [], []
    characteristic = SimpleNamespace(
        max_write_without_response_size=182, properties=["write", "write-without-response"],
    )

    class Client:
        def __init__(self, address, timeout):
            self.services = SimpleNamespace(get_characteristic=lambda uuid: characteristic)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def write_gatt_char(self, char, data, response):
            sizes.append(len(data))
            responses.append(response)

    monkeypatch.setitem(sys.modules, "bleak", SimpleNamespace(BleakClient=Client))
    mp.send(bytes(50), "AA:BB:CC:DD:EE:FF", ble=True)
    assert sizes == [20, 20, 10]  # 182-byte unacknowledged writes garbled labels
    assert responses == [False, False, True]


def test_ble_failure_is_reported_without_retry(monkeypatch):
    calls = []

    def fail(address, timeout):
        calls.append(address)
        raise RuntimeError("connection failed")

    monkeypatch.setitem(sys.modules, "bleak", SimpleNamespace(BleakClient=fail))
    with pytest.raises(OSError, match="connection failed"):
        mp.send(b"PRINT 1,1", "AA:BB:CC:DD:EE:FF", ble=True)
    assert calls == ["AA:BB:CC:DD:EE:FF"]


def test_cli_ble_print_routes_to_ble_transport(monkeypatch, tmp_path):
    label = tmp_path / "label.tspl"
    label.write_bytes(b"SIZE 100 mm,150 mm\r\nPRINT 1,1\r\n")
    calls = []
    monkeypatch.setattr(mp, "send_ble", lambda data, mac: calls.append((data, mac)))
    mp.main(["--ble", "--printer", "AA:BB:CC:DD:EE:FF", "print", str(label)])
    assert calls == [(label.read_bytes(), "AA:BB:CC:DD:EE:FF")]


def _bitmap(payload):
    _, _, rest = payload.partition(b"BITMAP ")
    x, y, wb, h, mode, rest = rest.split(b",", 5)
    wb, h = int(wb), int(h)
    assert (x, y, mode) == (b"0", b"0", b"0")
    return wb, h, rest[: wb * h], rest[wb * h:]


def test_black_pixel_is_zero_bit_and_padding_is_white():
    img = Image.new("L", (10, 2), 255)
    img.putpixel((0, 0), 0)
    wb, h, bits, tail = _bitmap(mp.to_tspl(img, label_mm=(10, 10)))
    assert (wb, h) == (2, 2)
    assert bits == bytes([0b01111111, 0xFF, 0xFF, 0xFF])  # 6 pad bits stay white
    assert tail == b"\r\nPRINT 1,1\r\n"


def test_landscape_rotates_and_fits_4x6():
    img = Image.new("L", (1200, 800), 255)
    ImageDraw.Draw(img).rectangle((0, 0, 1199, 799), outline=0, width=20)
    out = mp.fit(img)
    assert out.size == (799, 1199)  # 100x150 mm at 203 dpi
    assert out.getpixel((out.width // 2, out.height // 2)) == 255


def test_pdf_renders_to_label():
    buf = io.BytesIO()
    Image.new("RGB", (400, 600), "white").save(buf, "PDF")
    payload = mp.file_to_tspl(buf.getvalue())
    assert payload.startswith(b"SIZE 100 mm,150 mm\r\n")
    wb, h, _, _ = _bitmap(payload)
    assert (wb, h) == (100, 1199)


def test_pdf_label_on_a4_page_keeps_barcode_scale():
    page = Image.new("RGB", (600, 840), "white")
    ImageDraw.Draw(page).rectangle((20, 50, 299, 449), fill="black")
    buf = io.BytesIO()
    page.save(buf, "PDF")
    wb, h, bits, _ = _bitmap(mp.file_to_tspl(buf.getvalue()))
    rendered = Image.frombytes("1", (wb * 8, h), bits).convert("L")
    bounds = rendered.point(lambda value: 255 if value < 128 else 0).getbbox()
    assert bounds is not None
    assert bounds[2] - bounds[0] > 750
    assert bounds[3] - bounds[1] > 1100


def test_raw_tspl_passes_through():
    assert mp.file_to_tspl(b"SIZE 50 mm,30 mm\r\nCLS\r\n") == b"SIZE 50 mm,30 mm\r\nCLS\r\n"


def test_usb_unplugged_fails_without_creating_file(tmp_path):
    dev = tmp_path / "lp0"
    with pytest.raises(FileNotFoundError):
        mp.send(b"x", str(dev))
    assert not dev.exists()
    dev.touch()
    mp.send(b"SIZE", str(dev))
    assert dev.read_bytes() == b"SIZE"


def test_http_server_prints_and_reports_unreachable(tmp_path):
    dev = tmp_path / "lp0"
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    threading.Thread(target=mp.serve, args=(str(dev), "127.0.0.1", port), daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            urllib.request.urlopen(url + "/health")
            break
        except OSError:
            threading.Event().wait(0.05)

    post = lambda: urllib.request.urlopen(urllib.request.Request(url + "/print", data=b"SIZE 1 mm,1 mm\r\n"))
    with pytest.raises(urllib.error.HTTPError) as err:
        post()
    assert err.value.code == 503
    dev.touch()
    assert post().status == 200
    assert dev.read_bytes() == b"SIZE 1 mm,1 mm\r\n"


def test_offset_shifts_image_left_and_down():
    img = Image.new("L", (64, 64), 255)
    img.putpixel((20, 4), 0)
    job = mp.to_tspl(img, label_mm=(8, 8), offset_mm=(-1, 2))  # 1 mm = 8 dots
    head, _, rest = job.partition(b"BITMAP ")
    bits = rest.split(b",", 5)[5][:-len(b"\r\nPRINT 1,1\r\n")]
    assert len(bits) == 8 * 64
    black = [(i % 8 * 8 + 7 - b, i // 8) for i, byte in enumerate(bits)
             for b in range(8) if not byte >> b & 1]
    assert black == [(12, 20)]
