
from pathlib import Path
import subprocess

def main():
    print("AI Film Factory 啟動成功！")

    output = Path("output")
    output.mkdir(exist_ok=True)

    subprocess.run([
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", "color=c=black:s=720x1280:r=24:d=60",
        "-f", "lavfi",
        "-i", "anullsrc=r=44100:cl=stereo",
        "-t", "60",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-shortest",
        str(output / "test.mp4")
    ], check=True)

    print("60秒 MP4 製作完成！")

if __name__ == "__main__":
    main()
