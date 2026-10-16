"""Shared ffmpeg helpers for the demo scripts."""
import subprocess

_ENC = None


def video_encoder(quality=19):
    """Best H.264 encoder this ffmpeg has that actually works here (LGPL builds have no libx264)."""
    global _ENC
    if _ENC is None:
        have = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
        for name in ("h264_nvenc", "h264_qsv", "h264_amf", "libx264", "h264_mf"):
            if name in have and subprocess.run(
                    ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=s=256x256:d=0.2", "-c:v", name, "-f", "null", "-"],
                    capture_output=True).returncode == 0:
                _ENC = name
                break
        else:
            _ENC = "mpeg4"
    return {
        "h264_nvenc": ["-c:v", "h264_nvenc", "-preset", "p5", "-cq", str(quality)],
        "h264_qsv": ["-c:v", "h264_qsv", "-global_quality", str(quality)],
        "h264_amf": ["-c:v", "h264_amf", "-quality", "quality", "-rc", "cqp", "-qp_i", str(quality), "-qp_p", str(quality)],
        "libx264": ["-c:v", "libx264", "-preset", "fast", "-crf", str(quality)],
        "h264_mf": ["-c:v", "h264_mf", "-b:v", "12M"],
        "mpeg4": ["-c:v", "mpeg4", "-q:v", "3"],
    }[_ENC] + ["-pix_fmt", "yuv420p"]
