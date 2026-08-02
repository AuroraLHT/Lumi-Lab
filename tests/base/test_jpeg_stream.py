"""The chamber camera's MJPEG stream. Tier T0: no broker, no camera.

The chamber stream carries JPEG rather than the raw array the RHEED camera streams,
and rather than the H.264 rheed.video carries. These pin the three things that make
that safe to rely on: the bytes really are a decodable JPEG with the colour channels
the right way round, a bad frame costs one frame instead of the encoder thread, and
the `image` op stays lossless so nothing that needs true pixels is affected.
"""

from __future__ import annotations

import queue
import time

import cv2
import numpy as np
import pytest

from lumi.base.camera.handlers import JpegCameraHandler
from lumi.base.camera.jpeg_stream import JpegEncoder, JpegEncoderConfig
from lumi.base.mq.codec import decode, encode
from lumi.contracts import Codec
from lumi.contracts.chamber import CHAMBER
from lumi.contracts.payloads.camera import JpegMeta


def make_encoder(**kwargs) -> JpegEncoder:
    cfg = JpegEncoderConfig(**{"quality": 90, "idle_time": 0.01, **kwargs})
    return JpegEncoder(camera=None, camera_queue=queue.Queue(), config=cfg)


def header(t: float = 1.0, uid: str = "u") -> dict:
    # The cameras write `time` as a *string* (SimCamera.on_grab), which is why the
    # handler cannot pass it straight into a float field.
    return {"time": str(t), "uuid": uid, "time_stamp": "2026-07-30 12:00:00"}


# --- encoding ---------------------------------------------------------------


def test_encodes_a_decodable_jpeg():
    blob, width, height, channels = make_encoder().encode(
        np.zeros((48, 64, 3), dtype=np.uint8)
    )
    assert blob[:2] == b"\xff\xd8" and blob[-2:] == b"\xff\xd9"  # SOI / EOI
    assert (width, height, channels) == (64, 48, 3)
    decoded = cv2.imdecode(np.frombuffer(blob, np.uint8), cv2.IMREAD_COLOR)
    assert decoded.shape == (48, 64, 3)


def test_red_stays_red():
    """The cameras hand out RGB and cv2.imencode wants BGR.

    Getting this wrong does not raise -- it swaps red and blue, which on a chamber
    view reads as a plasma colour change rather than as a bug. Hence a test.
    """
    frame = np.zeros((16, 16, 3), dtype=np.uint8)
    frame[:, :, 0] = 255  # pure red, in RGB

    blob, *_ = make_encoder(quality=95).encode(frame)
    b, g, r = cv2.imdecode(np.frombuffer(blob, np.uint8), cv2.IMREAD_COLOR)[8, 8]

    assert r > 200 and b < 60, f"channels swapped: decoded BGR {(b, g, r)}"


def test_greyscale_is_not_widened_to_rgb():
    grey, _, _, channels = make_encoder(quality=95).encode(np.full((16, 16), 128, np.uint8))
    colour, _, _, _ = make_encoder(quality=95).encode(np.full((16, 16, 3), 128, np.uint8))
    assert channels == 1
    assert len(grey) < len(colour)


def test_deep_pixels_are_scaled_not_truncated():
    """A 12-bit sensor reads 0-4095; a plain astype(uint8) would wrap full scale to 255
    only by luck and turn most of the range into noise."""
    blob, *_ = make_encoder(quality=95, max_intensity=4095).encode(
        np.full((16, 16), 4095, dtype=np.uint16)
    )
    back = cv2.imdecode(np.frombuffer(blob, np.uint8), cv2.IMREAD_GRAYSCALE)
    assert back[8, 8] > 245


def test_a_frame_it_cannot_encode_raises():
    with pytest.raises(ValueError):
        make_encoder().encode(np.zeros((4, 4, 7), dtype=np.uint8))


def test_jpeg_is_much_smaller_than_the_raw_array():
    """The whole point of the change, with the bound the chamber view actually gets.

    A flat frame would compress absurdly well and prove nothing, so this is a scene:
    smooth structure plus sensor noise. Against the real sim camera the measured
    figure is ~17x (23.0 MB/s -> 1.3 MB/s at 640x480x3, 25fps); 10x is the floor.
    """
    rng = np.random.default_rng(0)
    ys, xs = np.mgrid[0:480, 0:640]
    scene = ((ys + xs) / 4 % 256).astype(np.float32)
    frame = np.clip(scene[..., None] + rng.normal(0, 6, (480, 640, 3)), 0, 255).astype(np.uint8)

    blob, *_ = make_encoder(quality=80).encode(frame)
    assert len(blob) < frame.nbytes / 10


def test_even_pure_noise_beats_the_raw_array():
    """Uniform noise is JPEG's pathological case -- no spatial correlation to exploit,
    so it compresses ~4x rather than ~17x. Pinned so a future quality change that
    quietly destroys the worst case shows up here rather than on the lab network."""
    rng = np.random.default_rng(0)
    frame = rng.integers(0, 256, (480, 640, 3), dtype=np.uint8)

    blob, *_ = make_encoder(quality=80).encode(frame)
    assert len(blob) < frame.nbytes / 4


# --- the thread -------------------------------------------------------------


def run_encoder(encoder: JpegEncoder, timeout: float = 2.0):
    encoder.start()
    try:
        yield
    finally:
        encoder.stop()
        encoder.join(timeout=timeout)


def test_a_bad_frame_costs_one_frame_not_the_thread():
    """An exception escaping run() would kill the thread while the capability went on
    advertising a healthy stream -- the failure mode that took out the RHEED encoder."""
    encoder = make_encoder()
    encoder.start()
    try:
        encoder.camera_queue.put((np.zeros((4, 4, 7), np.uint8), header()))  # unencodable
        encoder.camera_queue.put((np.zeros((16, 16, 3), np.uint8), header()))  # fine
        deadline = time.time() + 3.0
        while time.time() < deadline and encoder.n_encoded < 1:
            time.sleep(0.02)

        assert encoder.is_alive()
        assert encoder.n_failures == 1
        assert encoder.n_encoded == 1, "the good frame after the bad one was dropped"
    finally:
        encoder.stop()
        encoder.join(timeout=2.0)


def test_a_full_queue_drops_the_oldest_frame():
    """A live view must never park the encoder waiting on a slow consumer: the stale
    frame is the one to discard, not the new one."""
    encoder = make_encoder(queue_size=2)
    for i in range(6):
        encoder._put((f"f{i}".encode(), {}))

    assert encoder.frames.qsize() == 2
    assert [encoder.frames.get()[0] for _ in range(2)] == [b"f4", b"f5"]
    assert encoder.n_dropped == 4


def test_is_producing_is_false_for_a_dead_thread():
    """is_alive() alone is not the health signal -- see VideoReadout.n_fragments."""
    encoder = make_encoder()
    assert not encoder.is_producing()  # never started


def test_is_producing_goes_stale_without_new_frames():
    encoder = make_encoder()
    encoder.start()
    try:
        encoder._put((b"x", {}))
        assert encoder.is_producing(stale_after=5.0)
        encoder.last_frame_at = time.time() - 60
        assert not encoder.is_producing(stale_after=5.0)
    finally:
        encoder.stop()
        encoder.join(timeout=2.0)


# --- the handler ------------------------------------------------------------


class FakeCamera:
    frame_dims = (480, 640)

    class config:
        fps = 25

    def get_frame(self):
        return np.zeros((480, 640, 3), dtype=np.uint8), header()


async def test_handler_labels_the_stream_with_real_metadata():
    encoder = make_encoder()
    blob, width, height, channels = encoder.encode(np.zeros((48, 64, 3), np.uint8))
    encoder.frames.put((blob, {**header(t=1234.5, uid="abc"), "width": width,
                               "height": height, "channels": channels}))

    meta, payload = await JpegCameraHandler(FakeCamera(), encoder).next()

    assert isinstance(meta, JpegMeta)
    assert payload == blob
    assert (meta.width, meta.height, meta.channels) == (64, 48, 3)
    assert meta.time == 1234.5 and meta.uuid == "abc"
    assert meta.quality == encoder.config.quality


async def test_handler_returns_none_on_an_empty_queue():
    """`next()` runs on the node's event loop and must never block it."""
    assert await JpegCameraHandler(FakeCamera(), make_encoder()).next() is None


async def test_readout_exposes_the_encoders_progress():
    encoder = make_encoder()
    encoder.n_encoded, encoder.n_dropped = 17, 3
    readout = JpegCameraHandler(FakeCamera(), encoder).readout()

    assert readout.n_encoded == 17
    assert readout.n_dropped == 3
    # The readout deliberately has no lifecycle fields to clobber them with.
    assert not hasattr(readout, "is_streaming")


def test_n_encoded_advances_with_nobody_consuming():
    """The counter that looks like a liveness signal and is not.

    `_put` evicts the oldest frame to make room, so a queue nobody drains still counts
    up at full rate -- n_encoded proves the encoder thread is alive and nothing more.
    Observed live: n_encoded had reached 1792 while is_streaming was still False.
    n_dropped is what reveals the stalled consumer, which is why both are on the wire.
    """
    encoder = make_encoder(queue_size=4)
    for _ in range(100):
        encoder._put((b"frame", {}))

    assert encoder.n_encoded == 100, "n_encoded does NOT stall when nothing consumes"
    assert encoder.n_dropped == 96
    assert encoder.frames.qsize() == 4


async def test_the_image_op_is_still_a_lossless_array():
    """The split is the point: the stream is lossy because it is for looking at, and
    `image` stays lossless because that is what you call when the pixels matter."""
    meta, frame = await JpegCameraHandler(FakeCamera(), make_encoder()).image(None)

    assert isinstance(frame, np.ndarray)
    assert meta.dtype == "uint8" and meta.shape == [480, 640, 3]
    assert CHAMBER.capability("camera").op("image").response_codec is Codec.NPY


# --- the contract -----------------------------------------------------------


def test_the_chamber_stream_is_declared_raw_jpeg():
    stream = CHAMBER.capability("camera").stream
    assert stream.codec is Codec.RAW
    assert stream.payload is JpegMeta


def test_the_rheed_camera_still_streams_arrays():
    """Only the chamber changed. RHEED's frame stream feeds the integrator and the
    detection model, which need the real pixels."""
    from lumi.contracts.rheed import RHEED

    assert RHEED.capability("camera").stream.codec is Codec.NPY


def test_a_jpeg_frame_survives_the_raw_codec():
    blob, width, height, channels = make_encoder().encode(np.zeros((48, 64, 3), np.uint8))
    meta = JpegMeta(time=1.0, uuid="u", time_stamp="ts", width=width, height=height,
                    channels=channels, quality=90)

    body, headers = encode(meta, Codec.RAW, blob)
    back_meta, back_payload = decode(JpegMeta, Codec.RAW, body, headers)

    assert back_payload == blob
    assert back_meta == meta
    # The bytes go on the wire as-is, not base64'd into a JSON body.
    assert body == blob
