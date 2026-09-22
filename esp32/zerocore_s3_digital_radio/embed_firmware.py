"""Generate a C++ source from the upstream Si468x firmware blobs before build."""

from pathlib import Path

Import("env")

root = Path(env.subst("$PROJECT_DIR"))
images = {
    "fm_patch": "rom00_patch_mini.003.bin",
    "fm_firmware": "fmhd_radio_5_3_3.bin",
    "dab_patch": "rom00_patch.016.bin",
    "dab_firmware": "dab_radio_6_0_9.bin",
}
target = root / "src" / "firmware_images.cpp"
parts = ["#include <stdint.h>\n", '#include "firmware_images.h"\n']
for name, filename in images.items():
    data = (root / "data" / filename).read_bytes()
    parts.append(f"const uint8_t {name}[] = {{\n")
    for offset in range(0, len(data), 24):
        parts.append("  " + ", ".join(f"0x{value:02x}" for value in data[offset:offset + 24]) + ",\n")
    parts.append("};\n")
    parts.append(f"const uint32_t {name}_size = {len(data)};\n")
contents = "".join(parts)
if not target.exists() or target.read_text(encoding="utf-8") != contents:
    target.write_text(contents, encoding="utf-8")
