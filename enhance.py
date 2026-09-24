"""
enhance.py — PRXNO video enhancement pass
Boosts sharpness, resolution and color punch on a clip.
"""

import subprocess
from pathlib import Path

ENHANCE_TIMEOUT = 1800

def build_filter_chain(target_height: int = 1080,
                        sharpen_amount: float = 0.4,
                        contrast: float = 1.08,
                        saturation: float = 1.15) -> str:
    return (
        f"scale=-2:{target_height}:flags=lanczos,"
        f"unsharp=5:5:0.8:5:5:{sharpen_amount},"
        f"eq=contrast={contrast}:saturation={saturation}"
    )


def enhance_clip(input_path: str,
                  output_path: str,
                  crf: int = 18,
                  preset: str = "slow",
                  target_height: int = 1080,
                  timeout: int = ENHANCE_TIMEOUT):
    in_p = Path(input_path)
    out_p = Path(output_path)

    if not in_p.exists():
        return False, f"Input not found: {in_p}"

    out_p.parent.mkdir(parents=True, exist_ok=True)

    vf = build_filter_chain(target_height=target_height)

    cmd = [
        "ffmpeg", "-y",
        "-i", str(in_p),
        "-vf", vf,
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
        "-c:a", "aac", "-b:a", "192k",
        str(out_p),
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"FFmpeg timed out after {timeout}s"

    if result.returncode != 0:
        tail = "\n".join(result.stderr.strip().splitlines()[-15:])
        return False, f"FFmpeg failed (code {result.returncode}):\n{tail}"

    if not out_p.exists() or out_p.stat().st_size == 0:
        return False, "FFmpeg reported success but output file is missing/empty"

    return True, f"Enhanced clip written to {out_p} ({out_p.stat().st_size / 1_000_000:.1f} MB)"


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 3:
        print("Usage: python enhance.py <input.mp4> <output.mp4>")
        sys.exit(1)

    ok, msg = enhance_clip(sys.argv[1], sys.argv[2])
    print(msg)
    sys.exit(0 if ok else 1)
