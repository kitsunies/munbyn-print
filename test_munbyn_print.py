import io
import os
import socket
import threading
import urllib.request

import pytest
from PIL import Image, ImageDraw

import munbyn_print as mp


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
