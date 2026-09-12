from pathlib import Path
import subprocess
import sys

from smart_moments import analyze_video


def smart_cut(input_video, output_video=None):
    input_video = Path(input_video).resolve()

    if not input_video.exists():
        raise FileNotFoundError(input_video)

    print("Analyzing video for best moment...")
    result = analyze_video(input_video)

    moments = result.get("moments", [])

    if not moments:
        raise RuntimeError(
            "No suitable moment was detected."
        )

    best = moments[0]

    start = float(best["recommended_start"])
    end = float(best["recommended_end"])

    if end <= start:
        raise RuntimeError(
            "Invalid detected time range."
        )

    if output_video is None:
        output_video = (
            input_video.parent /
            f"smart_{input_video.stem}.mp4"
        )

    output_video = Path(output_video).resolve()

    print("")
    print("========================================")
    print(" SMART CUT")
    print("========================================")
    print(f"Score:       {best['score']}")
    print(f"Detected:    {best['start']}s - {best['end']}s")
    print(f"Recommended: {start}s - {end}s")
    print(f"Output:      {output_video}")
    print("")

    duration = end - start

    command = [
        "ffmpeg",
        "-y",
        "-ss",
        str(start),
        "-i",
        str(input_video),
        "-t",
        str(duration),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "20",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        str(output_video)
    ]

    subprocess.run(
        command,
        check=True
    )

    if not output_video.exists():
        raise RuntimeError(
            "Smart cut output was not created."
        )

    print("")
    print("SMART CUT COMPLETE")
    print(output_video)

    return output_video


if __name__ == "__main__":

    if len(sys.argv) < 2:
        print(
            "Usage: python smart_cut.py VIDEO.mp4"
        )
        raise SystemExit(1)

    input_video = sys.argv[1]

    output_video = None

    if len(sys.argv) >= 3:
        output_video = sys.argv[2]

    smart_cut(
        input_video,
        output_video
    )
