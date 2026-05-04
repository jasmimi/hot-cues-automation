import base64
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mutagen.id3 import COMM, GEOB, ID3

import hot_cues_automation as hot_cues


def _markers2_payload(entry_type: bytes, entry_data: bytes) -> bytes:
    raw = (
        hot_cues._MARKERS2_VERSION
        + entry_type
        + b"\x00"
        + len(entry_data).to_bytes(4, "big")
        + entry_data
        + b"\x00"
    )
    return base64.b64encode(raw)


def _serato_markers2_frame(data: bytes) -> GEOB:
    return GEOB(
        encoding=0,
        mime="application/octet-stream",
        filename="",
        desc="Serato Markers2",
        data=data,
    )


class SeratoMetadataTests(unittest.TestCase):
    def test_markers2_encode_decode_roundtrip(self) -> None:
        payload = hot_cues._encode_markers2(
            [
                (0, 1234, (0xCC, 0x00, 0x00), "Downbeat"),
                (1, 5678, (0xCC, 0x88, 0x00), "16 bars"),
            ]
        )

        self.assertEqual(
            hot_cues._decode_markers2(payload),
            [
                {"type": "CUE", "index": 0, "position_ms": 1234},
                {"type": "CUE", "index": 1, "position_ms": 5678},
            ],
        )

    def test_detects_existing_non_cue_serato_markers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "track.mp3"
            path.write_bytes(b"")

            tag = ID3()
            tag.add(_serato_markers2_frame(_markers2_payload(b"LOOP", b"existing")))
            tag.save(path, v2_version=3)

            self.assertTrue(hot_cues.has_serato_markers(str(path)))
            self.assertFalse(hot_cues.has_hot_cues(str(path)))

    def test_write_hot_cues_refuses_existing_serato_markers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "track.mp3"
            path.write_bytes(b"")

            tag = ID3()
            tag.add(_serato_markers2_frame(_markers2_payload(b"LOOP", b"existing")))
            tag.save(path, v2_version=3)
            original_data = ID3(path)["GEOB:Serato Markers2"].data

            with self.assertRaisesRegex(RuntimeError, "Refusing to overwrite"):
                hot_cues.write_hot_cues(str(path), [(1000, "Downbeat")])

            updated = ID3(path)
            self.assertEqual(updated["GEOB:Serato Markers2"].data, original_data)
            self.assertIsNone(updated.get("COMM:hot-cues-automation:eng"))

    def test_write_hot_cues_preserves_existing_blank_comment(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "track.mp3"
            path.write_bytes(b"")

            tag = ID3()
            tag.add(
                COMM(
                    encoding=3,
                    lang="eng",
                    desc="",
                    text="existing user comment",
                )
            )
            tag.save(path, v2_version=3)

            hot_cues.write_hot_cues(str(path), [(1000, "Downbeat")])

            updated = ID3(path)
            self.assertEqual(updated["COMM::eng"].text, ["existing user comment"])
            self.assertEqual(
                updated["COMM:hot-cues-automation:eng"].text,
                ["hot cues generated"],
            )
            self.assertTrue(hot_cues.has_hot_cues(str(path)))

    def test_process_file_skips_serato_markers_before_loading_audio(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "track.mp3"
            path.write_bytes(b"")

            tag = ID3()
            tag.add(_serato_markers2_frame(_markers2_payload(b"LOOP", b"existing")))
            tag.save(path, v2_version=3)

            with mock.patch.object(
                hot_cues.librosa,
                "load",
                side_effect=AssertionError("audio should not load"),
            ):
                result = hot_cues.process_file(str(path))

            self.assertEqual(result["status"], "skipped_has_serato_markers")


if __name__ == "__main__":
    unittest.main()
