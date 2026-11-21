import io
import zlib
import os
import socket
import sys
import threading
import time
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


def _decode(payload, size=(800, 1199)):
    """Rebuild the printed dots from a TSPL job: mode 0 BITMAP, 0 = black."""
    _, _, rest = payload.partition(b"BITMAP ")
    x, y, wb, h, mode, rest = rest.split(b",", 5)
    x, y, wb, h = int(x), int(y), int(wb), int(h)
    assert mode == b"0" and x % 8 == 0
    canvas = Image.new("1", size, 1)
    canvas.paste(Image.frombytes("1", (wb * 8, h), rest[: wb * h]), (x, y))
    return canvas.convert("L"), (x, y, wb, h), rest[wb * h:]


def test_black_pixel_is_zero_bit_and_padding_is_white():
    img = Image.new("L", (10, 2), 255)
    img.putpixel((0, 0), 0)
    job = mp.to_tspl(img, label_mm=(10, 10))
    _, box, tail = _decode(job, (16, 2))
    assert box == (0, 0, 1, 1)  # cropped to the single inked byte
    assert job.endswith(b",0," + bytes([0b01111111]) + tail)  # pad bits stay white
    assert tail == b"\r\nPRINT 1,1\r\n"


def _full_width_dots(img):
    """What an uncropped job prints: the padded label converted to 1-bit."""
    padded = Image.new("L", (800, img.height), 255)
    padded.paste(img, (0, 0))
    return padded.convert("1").convert("L").tobytes()


def test_crop_sends_only_ink_and_prints_identical_dots():
    img = Image.new("L", (799, 1199), 255)
    draw = ImageDraw.Draw(img)
    draw.rectangle((101, 230, 530, 700), outline=0, width=3)
    draw.text((140, 300), "4x6", fill=0)
    job = mp.to_tspl(img)
    printed, (x, y, wb, h), _ = _decode(job)
    assert printed.tobytes() == _full_width_dots(img)
    assert (x, y, wb * 8, h) == (96, 230, 440, 471)
    assert len(job) < 27_000  # a full-width bitmap is 120 KB


def test_crop_keeps_dithered_grey_dots():
    img = Image.new("L", (799, 1199), 255)
    ImageDraw.Draw(img).rectangle((600, 900, 700, 1000), fill=200)  # pale area
    printed, _, _ = _decode(mp.to_tspl(img))
    assert printed.tobytes() == _full_width_dots(img)
    assert printed.getextrema() == (0, 255)  # the grey did produce dots


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
    printed, _, _ = _decode(payload)
    assert printed.point(lambda v: 255 if v < 128 else 0).getbbox() is None  # blank page


def test_pdf_label_on_a4_page_keeps_barcode_scale():
    page = Image.new("RGB", (600, 840), "white")
    ImageDraw.Draw(page).rectangle((20, 50, 299, 449), fill="black")
    buf = io.BytesIO()
    page.save(buf, "PDF")
    rendered, _, _ = _decode(mp.file_to_tspl(buf.getvalue()))
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
    printed, _, _ = _decode(job, (64, 64))
    black = [(i % 64, i // 64) for i, v in enumerate(printed.tobytes()) if v == 0]
    assert black == [(12, 20)]


def test_warm_rfcomm_reuses_link_reconnects_after_hangup_and_idles_out(monkeypatch):
    links = []

    def connect(mac, channel, timeout):
        ours, printer = socket.socketpair()
        links.append(printer)
        return ours

    monkeypatch.setattr(mp, "_rfcomm_connect", connect)
    monkeypatch.setattr(mp, "_rfcomm_send", lambda s, payload: s.sendall(payload))
    warm = mp.WarmRfcomm("AA:BB:CC:DD:EE:FF", idle=0.2)
    warm.send(b"one")
    warm.send(b"two")
    assert len(links) == 1  # a batch pays the connect once
    assert links[0].recv(10) == b"onetwo"
    links[0].close()  # printer power-cycled between jobs
    warm.send(b"three")
    assert len(links) == 2 and links[1].recv(10) == b"three"
    time.sleep(0.4)  # idle: link released so the phone app can connect
    assert warm.sock is None and links[1].recv(10) == b""


def test_compressed_job_decodes_to_the_same_dots():
    img = Image.new("L", (799, 1199), 255)
    draw = ImageDraw.Draw(img)
    draw.rectangle((101, 230, 530, 700), outline=0, width=3)
    draw.rectangle((600, 900, 700, 1000), fill=200)
    plain = mp.to_tspl(img)
    packed = mp.to_tspl(img, compress=True)
    _, _, rest = packed.partition(b"BITMAP ")
    x, y, wb, h, mode, length, rest = rest.split(b",", 6)
    assert mode == b"3" and rest[int(length):] == b"\r\nPRINT 1,1\r\n"
    # The printer inflates, applies the table again, and burns 1 bits.
    rows = bytes(b ^ 0xFF for b in zlib.decompress(rest[:int(length)]).translate(mp.CODE941))
    head = b"BITMAP " + b",".join((x, y, wb, h, b"0")) + b","
    rebuilt = plain[:plain.index(b"BITMAP ")] + head + rows + b"\r\nPRINT 1,1\r\n"
    assert rebuilt == plain
    assert len(packed) < len(plain) // 4


def test_code941_table_matches_the_vendor_library():
    assert sorted(mp.CODE941) == list(range(256))
    assert mp.CODE941.translate(mp.CODE941) == bytes(range(256))  # self-inverse
    assert (mp.CODE941[0x00], mp.CODE941[0xFF]) == (0xDD, 0x5A)  # the streak bytes seen
