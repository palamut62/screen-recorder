from __future__ import annotations

import logging
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.core.state import AppState, Region
from app.utils.env import detect_display_name, detect_session_type, ensure_output_dir


class RecorderError(RuntimeError):
    pass


class FFmpegRecorder:
    def __init__(self) -> None:
        self._logger = logging.getLogger(__name__)
        self._process: Optional[subprocess.Popen[str]] = None
        self._current_output: Optional[Path] = None
        self._last_output: Optional[Path] = None
        self._last_error: Optional[str] = None

    @property
    def is_recording(self) -> bool:
        return self._process is not None and self._process.poll() is None

    @property
    def current_output(self) -> Optional[Path]:
        return self._current_output

    @property
    def last_output(self) -> Optional[Path]:
        return self._last_output

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    def validate_environment(self) -> None:
        if shutil.which("ffmpeg") is None:
            raise RecorderError("FFmpeg bulunamadı. Lütfen `ffmpeg` kurun.")

        session_type = detect_session_type()
        if session_type == "wayland":
            raise RecorderError(
                "Wayland algılandı. Portal + PipeWire entegrasyonu henüz tamamlanmadı."
            )

    def start(self, state: AppState) -> Path:
        if self.is_recording:
            raise RecorderError("Kayıt zaten devam ediyor.")

        self.validate_environment()
        output_dir = ensure_output_dir(state.output_dir)
        output_path = output_dir / self._build_filename(state.output_format)
        command = self._build_command(
            fps=state.fps,
            region=state.selected_region,
            output_path=output_path,
        )
        self._logger.info(
            "Starting recording | fps=%s | format=%s | output=%s | command=%s",
            state.fps,
            state.output_format,
            output_path,
            command,
        )

        try:
            self._process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError as exc:
            raise RecorderError(f"FFmpeg başlatılamadı: {exc}") from exc

        self._last_error = None
        self._current_output = output_path
        self._last_output = output_path
        self._logger.info("Recording process started | pid=%s", self._process.pid if self._process else None)
        return output_path

    def stop(self) -> Path:
        if self._process is None or self._current_output is None:
            self._logger.warning("Stop requested but no active process exists")
            raise RecorderError("Aktif kayıt bulunmuyor.")
        if self._process.poll() is not None:
            error_text = self._read_process_error() or "Kayit sureci beklenmedik sekilde sonlandi."
            self._logger.error("Stop requested after process had already exited | error=%s", error_text)
            self._process = None
            self._current_output = None
            self._last_error = error_text
            raise RecorderError(error_text)

        try:
            if self._process.stdin is not None:
                self._process.stdin.write("q\n")
                self._process.stdin.flush()
            self._process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._process.terminate()
            self._process.wait(timeout=5)
        finally:
            if self._process.stdin is not None:
                self._process.stdin.close()

        output_path = self._current_output
        self._read_process_error()
        self._logger.info("Recording stopped successfully | output=%s", output_path)
        self._process = None
        self._current_output = None
        return output_path

    def poll_failure(self) -> Optional[str]:
        if self._process is None or self._process.poll() is None:
            return None
        error_text = self._read_process_error() or "Kayit sureci beklenmedik sekilde sonlandi."
        self._logger.error("Recording process exited unexpectedly | error=%s", error_text)
        self._process = None
        self._current_output = None
        self._last_error = error_text
        return error_text

    def _build_filename(self, extension: str) -> str:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"recording_{timestamp}.{extension}"

    def _build_command(
        self,
        fps: int,
        region: Optional[Region],
        output_path: Path,
    ) -> list[str]:
        display_name = detect_display_name()
        input_target = display_name
        command = [
            "ffmpeg",
            "-y",
            "-framerate",
            str(fps),
        ]

        if region is not None:
            if not region.is_valid():
                raise RecorderError("Geçersiz bölge bilgisi.")
            command.extend(
                [
                    "-video_size",
                    f"{region.width}x{region.height}",
                    "-f",
                    "x11grab",
                    "-i",
                    f"{input_target}+{region.x},{region.y}",
                ]
            )
        else:
            command.extend(
                [
                    "-f",
                    "x11grab",
                    "-i",
                    input_target,
                ]
            )

        command.extend(self._codec_args(output_path.suffix.lstrip(".")))
        command.append(str(output_path))
        return command

    def _codec_args(self, output_format: str) -> list[str]:
        if output_format == "webm":
            return ["-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-c:v", "libvpx-vp9", "-crf", "30", "-b:v", "0"]
        return ["-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p"]

    def get_media_info(self, input_path: Path) -> str:
        if shutil.which("ffprobe") is None:
            return f"Selected: {input_path.name}"

        command = [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration,size,format_name",
            "-of",
            "default=noprint_wrappers=1:nokey=0",
            str(input_path),
        ]
        try:
            result = subprocess.run(command, capture_output=True, text=True, check=True)
        except subprocess.SubprocessError:
            return f"Selected: {input_path.name}"

        info = {}
        for line in result.stdout.splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            info[key] = value

        duration = float(info.get("duration", "0") or "0")
        size = int(info.get("size", "0") or "0")
        size_mb = size / (1024 * 1024) if size else 0
        return (
            f"{input_path.name} | {info.get('format_name', 'unknown')} | "
            f"{duration:.1f}s | {size_mb:.1f} MB"
        )

    def export_media(
        self,
        input_path: Path,
        output_dir: Path,
        output_format: str,
        speed_factor: float,
    ) -> Path:
        if shutil.which("ffmpeg") is None:
            raise RecorderError("FFmpeg bulunamadı. Lütfen `ffmpeg` kurun.")
        if not input_path.exists():
            raise RecorderError("Seçilen medya dosyası bulunamadı.")

        output_dir = ensure_output_dir(output_dir)
        output_path = output_dir / self._build_media_filename(input_path.stem, output_format, speed_factor)
        command = self._build_export_command(input_path, output_path, output_format, speed_factor)
        try:
            subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        except subprocess.CalledProcessError as exc:
            error_text = exc.stderr.strip().splitlines()[-1] if exc.stderr else "FFmpeg export hatası."
            raise RecorderError(error_text) from exc
        self._last_output = output_path
        return output_path

    def _build_media_filename(self, stem: str, extension: str, speed_factor: float) -> str:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        speed_tag = str(speed_factor).replace(".", "_")
        return f"{stem}_x{speed_tag}_{timestamp}.{extension}"

    def _build_export_command(
        self,
        input_path: Path,
        output_path: Path,
        output_format: str,
        speed_factor: float,
    ) -> list[str]:
        command = ["ffmpeg", "-y", "-i", str(input_path)]
        video_filters = []

        if speed_factor != 1.0:
            video_filters.append(f"setpts=PTS/{speed_factor}")

        if output_format == "gif":
            if speed_factor == 1.0:
                video_filters.append("fps=12,scale=960:-1:flags=lanczos")
            else:
                video_filters.append("fps=12,scale=960:-1:flags=lanczos")
            command.extend(["-vf", ",".join(video_filters), "-an"])
            command.append(str(output_path))
            return command

        if video_filters:
            command.extend(["-vf", ",".join(video_filters)])

        if speed_factor != 1.0:
            command.extend(["-filter:a", self._build_atempo_chain(speed_factor)])

        command.extend(self._codec_args(output_format))
        command.append(str(output_path))
        return command

    def _build_atempo_chain(self, speed_factor: float) -> str:
        factors = []
        remaining = speed_factor

        while remaining > 2.0:
            factors.append("atempo=2.0")
            remaining /= 2.0

        while remaining < 0.5:
            factors.append("atempo=0.5")
            remaining /= 0.5

        factors.append(f"atempo={remaining:.3f}")
        return ",".join(factors)

    def _read_process_error(self) -> Optional[str]:
        if self._process is None or self._process.stderr is None:
            return self._last_error
        try:
            error_output = self._process.stderr.read().strip()
        except OSError:
            return self._last_error
        if not error_output:
            return self._last_error
        self._last_error = error_output.splitlines()[-1]
        self._logger.error("FFmpeg stderr captured | full_output=%s", error_output.replace("\n", " || "))
        return self._last_error
