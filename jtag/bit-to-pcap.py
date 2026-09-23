#!/usr/bin/env python3
"""Convert Xilinx .bit containers to the word-swapped byte stream for Zynq PCAP."""

import argparse
import gzip
import hashlib
from pathlib import Path

SYNC = bytes.fromhex("aa995566")
PCAP_SYNC = bytes.fromhex("665599aa")


def read_input(path: Path) -> bytes:
    with path.open("rb") as stream:
        prefix = stream.read(2)
    if prefix == b"\x1f\x8b":
        with gzip.open(path, "rb") as stream:
            return stream.read()
    return path.read_bytes()


def parse_bit_payload(data: bytes) -> bytes:
    if len(data) < 12:
        raise ValueError("input is too short to be a Xilinx .bit file")
    magic_size = int.from_bytes(data[:2], "big")
    pos = 2 + magic_size
    if magic_size != 9 or data[pos : pos + 2] != b"\x00\x01":
        raise ValueError("invalid Xilinx .bit header")
    pos += 2

    while pos < len(data):
        tag = data[pos]
        pos += 1
        if tag == ord("e"):
            if pos + 4 > len(data):
                raise ValueError("truncated Xilinx .bit payload length")
            payload_size = int.from_bytes(data[pos : pos + 4], "big")
            pos += 4
            if pos + payload_size != len(data):
                raise ValueError(
                    f".bit payload length says {payload_size} bytes, "
                    f"but only {len(data) - pos} remain"
                )
            return data[pos:]

        if tag not in b"abcd" or pos + 2 > len(data):
            raise ValueError("invalid field in Xilinx .bit header")
        field_size = int.from_bytes(data[pos : pos + 2], "big")
        pos += 2 + field_size
    raise ValueError("Xilinx .bit file has no e payload field")


def convert(data: bytes) -> bytes:
    # Accept an already converted PCAP .bin unchanged.
    if data.startswith(b"\xff" * 8 + PCAP_SYNC):
        if len(data) % 4:
            raise ValueError("PCAP .bin size must be divisible by four")
        return data

    payload = parse_bit_payload(data)
    sync_at = payload.find(SYNC)
    if sync_at < 8 or payload[sync_at - 8 : sync_at] != b"\xff" * 8:
        raise ValueError(".bit payload has no aligned PCAP sync preamble")
    pcap_data = payload[sync_at - 8 :]
    if len(pcap_data) % 4:
        raise ValueError("PCAP payload size must be divisible by four")

    swapped = bytearray(len(pcap_data))
    for offset in range(0, len(pcap_data), 4):
        swapped[offset : offset + 4] = pcap_data[offset : offset + 4][::-1]
    if not swapped.startswith(b"\xff" * 8 + PCAP_SYNC):
        raise ValueError("converted stream has an invalid PCAP sync word")
    return bytes(swapped)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        image = convert(read_input(args.input))
        args.output.write_bytes(image)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    digest = hashlib.sha256(image).hexdigest()
    print(f"PCAP image: {len(image)} bytes sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
