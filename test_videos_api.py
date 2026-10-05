"""Offline tests for the /v1/videos routes over a stub backend; no grok needed.

Run:  venv\\Scripts\\python -m unittest -v test_videos_api
"""
import base64
import os
import struct
import tempfile
import threading
import unittest

import agentry
from backends import Backend


def _box(kind, payload):
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def write_minimal_mp4(path, width=448, height=672, seconds=6.0):
    """ftyp + moov{mvhd, trak{tkhd}}: enough for _mp4_info."""
    mvhd = (b"\x00" + b"\x00\x00\x00" + struct.pack(">II", 0, 0)
            + struct.pack(">I", 1000) + struct.pack(">I", int(seconds * 1000))
            + b"\x00" * 80)
    tkhd = (b"\x00" + b"\x00\x00\x0f" + struct.pack(">IIII", 0, 0, 1, 0)
            + struct.pack(">I", int(seconds * 1000)) + b"\x00" * 8
            + b"\x00" * 8 + b"\x00" * 36
            + struct.pack(">II", width << 16, height << 16))
    moov = _box(b"moov", _box(b"mvhd", mvhd) + _box(b"trak", _box(b"tkhd", tkhd)))
    with open(path, "wb") as f:
        f.write(_box(b"ftyp", b"isom\x00\x00\x02\x00isomiso2mp41") + moov
                + _box(b"mdat", b"\x00" * 64))


class StubGrok(Backend):
    def __init__(self, clip_path):
        self.default_model = "grok-4.7"
        self.session_id = "s1"
        self.session_fresh = True
        self.turn_lock = threading.Lock()
        self.clip_path = clip_path
        self.calls = []
        self.fail = False

    def new_session(self, cwd=None, model=None, effort=None):
        return self.session_id

    def prompt(self, text, images=None, timeout=900, model=None, effort=None):
        yield "unused"

    def video_turn(self, prompt, image, seconds=6, size_text="", timeout=900,
                   model=None, effort=None):
        self.calls.append({"prompt": prompt, "image": image, "seconds": seconds,
                           "size_text": size_text})
        yield "I'll make the clip."
        if self.fail:
            yield "\n[grok video error] Provide at least one input"
            return
        yield ("video", "video/mp4", self.clip_path, "rewritten " + prompt)
        yield "Done."

    def cancel(self):
        return False

    def is_alive(self):
        return True

    def close(self):
        pass


class VideosApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clip = os.path.join(self.tmp.name, "1.mp4")
        write_minimal_mp4(self.clip)
        self.stub = agentry._backend = StubGrok(self.clip)
        self.kind = agentry.BACKEND_KIND
        agentry.BACKEND_KIND = "grok"
        self.addCleanup(lambda: setattr(agentry, "BACKEND_KIND", self.kind))
        agentry._VIDEOS.clear()
        self.client = agentry.app.test_client()
        self.png = "data:image/png;base64," + base64.b64encode(b"\x89PNGfake").decode()

    def test_mp4_info(self):
        self.assertEqual(agentry._mp4_info(self.clip), (448, 672, 6.0))
        self.assertIsNone(agentry._mp4_info(os.path.join(self.tmp.name, "nope.mp4")))

    def test_create_retrieve_content_list_delete(self):
        r = self.client.post("/v1/videos", json={
            "prompt": "the dog wags its tail", "seconds": "6", "size": "720x1280",
            "input_reference": {"image_url": self.png}})
        self.assertEqual(r.status_code, 200, r.get_json())
        v = r.get_json()
        self.assertEqual(v["object"], "video")
        self.assertEqual(v["status"], "completed")
        self.assertEqual(v["progress"], 100)
        self.assertEqual(v["size"], "448x672")          # delivered, not asked
        self.assertEqual(v["seconds"], "6")
        self.assertEqual(v["revised_prompt"], "rewritten the dog wags its tail")
        self.assertTrue(v["id"].startswith("video_"))
        self.assertNotIn("_path", v)
        self.assertEqual(r.headers["X-Device"], "grok")
        call = self.stub.calls[0]
        self.assertEqual(call["image"], ("image/png", base64.b64encode(b"\x89PNGfake").decode()))
        self.assertEqual(call["seconds"], 6)
        self.assertIn("portrait", call["size_text"])
        self.assertIn("720p", call["size_text"])

        vid = v["id"]
        self.assertEqual(self.client.get(f"/v1/videos/{vid}").get_json()["id"], vid)
        c = self.client.get(f"/v1/videos/{vid}/content")
        self.assertEqual(c.status_code, 200)
        self.assertEqual(c.mimetype, "video/mp4")
        self.assertEqual(c.data, open(self.clip, "rb").read())
        self.assertEqual(self.client.get(f"/v1/videos/{vid}/content?variant=thumbnail")
                         .status_code, 400)
        self.assertEqual([x["id"] for x in self.client.get("/v1/videos").get_json()["data"]],
                         [vid])
        d = self.client.delete(f"/v1/videos/{vid}")
        self.assertEqual(d.get_json()["deleted"], True)
        self.assertEqual(self.client.get(f"/v1/videos/{vid}").status_code, 404)
        self.assertTrue(os.path.isfile(self.clip))     # delete forgets, never removes

    def test_multipart_reference_and_short_seconds_floor(self):
        r = self.client.post("/v1/videos", data={
            "prompt": "wag", "seconds": "3",
            "input_reference": (open(self.clip, "rb"), "ref.jpg", "image/jpeg")},
            content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.stub.calls[0]["seconds"], 6)
        self.assertEqual(self.stub.calls[0]["image"][0], "image/jpeg")

    def test_missing_reference_and_prompt(self):
        r = self.client.post("/v1/videos", json={"prompt": "wag"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"]["param"], "input_reference")
        r = self.client.post("/v1/videos", json={"input_reference": {"image_url": self.png}})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"]["param"], "prompt")

    def test_failed_generation_is_a_failed_object(self):
        self.stub.fail = True
        r = self.client.post("/v1/videos", json={"prompt": "wag",
                                                 "input_reference": {"image_url": self.png}})
        self.assertEqual(r.status_code, 502)
        v = r.get_json()
        self.assertEqual(v["status"], "failed")
        self.assertIn("Provide at least one input", v["error"]["message"])
        self.assertEqual(self.client.get(f"/v1/videos/{v['id']}/content").status_code, 409)

    def test_other_backends_get_501(self):
        agentry.BACKEND_KIND = "codex"
        r = self.client.post("/v1/videos", json={"prompt": "wag",
                                                 "input_reference": {"image_url": self.png}})
        self.assertEqual(r.status_code, 501)


if __name__ == "__main__":
    unittest.main()
