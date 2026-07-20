import unittest

from legacy.dab_radio_i2c_safe2 import CMD_FM_RDS_STATUS, Si468xDabRadio
from raspiaudio_radio.backend import RadioBackend
from raspiaudio_radio.rds import RdsDecoder


def _word(first: str, second: str) -> int:
    return (ord(first) << 8) | ord(second)


def _consume_2a_text(decoder: RdsDecoder, text: str, ab: int = 0) -> None:
    padded = (text + "\r").ljust(64)
    last_segment = text.index("\r") // 4 if "\r" in text else len(text) // 4
    for segment in range(last_segment + 1):
        chunk = padded[segment * 4 : (segment + 1) * 4]
        decoder.consume(
            {
                "block_a": 0x1234,
                "block_b": (2 << 12) | ((ab & 1) << 4) | segment,
                "block_c": _word(chunk[0], chunk[1]),
                "block_d": _word(chunk[2], chunk[3]),
                "ble": (0, 0, 0, 0),
            }
        )


def _rt_plus_group(
    content_type_1: int,
    start_1: int,
    length_1: int,
    content_type_2: int,
    start_2: int,
    length_2: int,
) -> dict:
    return {
        "block_a": 0x1234,
        "block_b": (11 << 12) | (1 << 3) | ((content_type_1 >> 3) & 0x07),
        "block_c": (
            ((content_type_1 & 0x07) << 13)
            | ((start_1 & 0x3F) << 7)
            | ((length_1 & 0x3F) << 1)
            | ((content_type_2 >> 5) & 0x01)
        ),
        "block_d": (
            ((content_type_2 & 0x1F) << 11)
            | ((start_2 & 0x3F) << 5)
            | (length_2 & 0x1F)
        ),
        "ble": (0, 0, 0, 0),
    }


class RdsDecoderTests(unittest.TestCase):
    def test_assembles_program_service_name(self) -> None:
        decoder = RdsDecoder()
        for segment, pair in enumerate(("FR", "AN", "CE", "IN")):
            decoder.consume(
                {
                    "block_a": 0xF123,
                    "block_b": (10 << 5) | segment,
                    "block_c": 0,
                    "block_d": _word(*pair),
                    "ble": (0, 0, 0, 0),
                }
            )

        snapshot = decoder.snapshot()
        self.assertEqual(snapshot["station_name"], "FRANCEIN")
        self.assertEqual(snapshot["program_identification_hex"], "F123")
        self.assertEqual(snapshot["program_type"], "Pop Music")

    def test_assembles_radio_text_and_resets_on_ab_change(self) -> None:
        decoder = RdsDecoder()
        chunks = ("BONJ", "OUR ", "PARI", "S\r  ")
        for segment, chunk in enumerate(chunks):
            decoder.consume(
                {
                    "block_a": 0x1234,
                    "block_b": (2 << 12) | segment,
                    "block_c": _word(chunk[0], chunk[1]),
                    "block_d": _word(chunk[2], chunk[3]),
                    "ble": (0, 0, 0, 0),
                }
            )

        self.assertEqual(decoder.snapshot()["text"], "BONJOUR PARIS")

        decoder.consume(
            {
                "block_a": 0x1234,
                "block_b": (2 << 12) | (1 << 4),
                "block_c": _word("N", "O"),
                "block_d": _word("U", "V"),
                "ble": (0, 0, 0, 0),
            }
        )
        self.assertEqual(decoder.snapshot()["text"], "")
        self.assertEqual(decoder.snapshot()["text_ab"], 1)

    def test_rejects_group_with_uncorrectable_block_b(self) -> None:
        decoder = RdsDecoder()
        accepted = decoder.consume(
            {
                "block_a": 0x1234,
                "block_b": 0,
                "block_c": 0,
                "block_d": _word("B", "A"),
                "ble": (0, 3, 0, 0),
            }
        )
        self.assertFalse(accepted)
        self.assertEqual(decoder.snapshot()["groups_decoded"], 0)

    def test_waits_for_all_segments_before_publishing_radio_text(self) -> None:
        decoder = RdsDecoder()
        decoder.consume(
            {
                "block_a": 0x1234,
                "block_b": (2 << 12) | 2,
                "block_c": _word("T", "E"),
                "block_d": _word("\r", " "),
                "ble": (0, 0, 0, 0),
            }
        )
        self.assertEqual(decoder.snapshot()["text"], "")

        for segment, chunk in enumerate(("BONJ", "OUR ")):
            decoder.consume(
                {
                    "block_a": 0x1234,
                    "block_b": (2 << 12) | segment,
                    "block_c": _word(chunk[0], chunk[1]),
                    "block_d": _word(chunk[2], chunk[3]),
                    "ble": (0, 0, 0, 0),
                }
            )
        self.assertEqual(decoder.snapshot()["text"], "BONJOUR TE")

    def test_decodes_rt_plus_title_and_artist(self) -> None:
        decoder = RdsDecoder()
        text = "Now: GOLD - SPANDAU BALLET\r"
        _consume_2a_text(decoder, text)
        decoder.consume(
            {
                "block_a": 0x1234,
                "block_b": (3 << 12) | (11 << 1),
                "block_c": 0,
                "block_d": 0x4BD7,
                "ble": (0, 0, 0, 0),
            }
        )
        decoder.consume(_rt_plus_group(1, 5, 3, 4, 12, 13))

        snapshot = decoder.snapshot()
        self.assertTrue(snapshot["rt_plus"])
        self.assertEqual(snapshot["rt_plus_group_type"], "11A")
        self.assertEqual(snapshot["title"], "GOLD")
        self.assertEqual(snapshot["artist"], "SPANDAU BALLET")
        self.assertEqual(snapshot["group_type_counts"]["11A"], 1)


class RadioBackendRdsTests(unittest.TestCase):
    def test_persists_rds_station_name_in_fm_scan_cache(self) -> None:
        backend = object.__new__(RadioBackend)
        backend._current_station = {
            "station_id": "fm:98100",
            "band": "fm",
            "hd_available": False,
            "label": "FM 98.1",
        }
        backend._stations = {"fm": [dict(backend._current_station)]}
        backend._scan_key = lambda mode=None: "fm"
        saved = []
        backend._save_scan_file_locked = lambda key, stations: saved.append((key, [dict(item) for item in stations]))

        backend._apply_rds_station_name_locked("NOSTALGI")

        self.assertEqual(backend._current_station["label"], "NOSTALGI")
        self.assertEqual(backend._stations["fm"][0]["station_name"], "NOSTALGI")
        self.assertEqual(saved[0][0], "fm")
        self.assertEqual(saved[0][1][0]["label"], "NOSTALGI")


class Si468xRdsStatusTests(unittest.TestCase):
    def test_parses_hardware_fifo_reply(self) -> None:
        radio = object.__new__(Si468xDabRadio)
        commands = []
        radio._write_command = lambda command: commands.append(command)
        reply = bytearray(20)
        reply[0] = 0x84
        reply[5] = 0x1B
        reply[6] = 0x2A
        reply[8:10] = (0x34, 0x12)
        reply[10] = 3
        reply[11] = 0b00011011
        reply[12:14] = (0x34, 0x12)
        reply[14:16] = (0x78, 0x56)
        reply[16:18] = (0xBC, 0x9A)
        reply[18:20] = (0xF0, 0xDE)
        radio._read_reply = lambda length: list(reply)

        status = radio.fm_rds_status(status_only=False, intack=True)

        self.assertEqual(commands, [[CMD_FM_RDS_STATUS, 0x01]])
        self.assertTrue(status["rds_interrupt"])
        self.assertTrue(status["rds_sync"])
        self.assertEqual(status["program_identification"], 0x1234)
        self.assertEqual(status["program_type_code"], 10)
        self.assertEqual(status["fifo_used"], 3)
        self.assertEqual(status["ble"], (0, 1, 2, 3))
        self.assertEqual(status["block_b"], 0x5678)
        self.assertEqual(status["block_d"], 0xDEF0)


if __name__ == "__main__":
    unittest.main()
