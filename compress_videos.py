# ===================== IMPORTS ============================================== #
import os
import time
import shutil
import tempfile
import argparse
import subprocess
from datetime import datetime

from rich.console import Console, Group
from rich.live import Live
from rich.prompt import Confirm
from rich.progress import (
    Progress,
    SpinnerColumn,
    TextColumn,
    BarColumn,
    MofNCompleteColumn,
    TaskProgressColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

# ===================== CONSTANTS ============================================ #
INPUT_FORMATS = [".mp4", ".mkv", ".avi", ".mov"]
OUTPUT_FORMAT = ".mp4"
TMP_FILE_PREFIX = "compressed_video_"
FILE_ERROR_LOG = "video_compression_errors.log"

console = Console()

# ===================== AUXILIARY FUNCTIONS ================================== #
def list_files_by_extension_recursive(directory, extension):
    output_list = []
    list_extensions = []
    if type(extension) == type(""):
        list_extensions = [extension]
    elif type(extension) == type([]):
        list_extensions = extension
    for root, dirs, files in os.walk(directory):
        for file_item in files:
            file_path = os.path.join(root, file_item)
            for ext in list_extensions:
                if file_path.lower().endswith(ext.lower()):
                    output_list.append(file_path)
    return output_list

def parse_datetime_arg(value):
    for date_format in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, date_format)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(
        f"Invalid date/time '{value}'. Use YYYY-MM-DD or YYYY-MM-DD HH:MM:SS"
    )

def data_size_string(num_bytes):
    units = ['', 'Ki', 'Mi', 'Gi']
    i = 0
    while abs(num_bytes) >= 1024.0 and i < len(units) - 1:
        num_bytes /= 1024.0
        i += 1
    return f"{num_bytes:,.3f} {units[i]}B"

def get_video_duration_seconds(video_path):
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", video_path],
            capture_output=True, text=True, check=True,
        )
        return float(result.stdout.strip())
    except (subprocess.CalledProcessError, ValueError, FileNotFoundError):
        return None

# ===================== MAIN SCRIPT ========================================== #
def main():
    # --------------------- Argument validation
    parser = argparse.ArgumentParser(description="Compress videos using ffmpeg (libx265).")
    parser.add_argument("path", help="Path to a video file or a directory to search recursively")
    parser.add_argument(
        "--modified-before",
        type=parse_datetime_arg,
        default=None,
        help="Process only videos modified before this date/time (YYYY-MM-DD or YYYY-MM-DD HH:MM:SS)",
    )
    args = parser.parse_args()

    if shutil.which("ffmpeg") is None:
        console.print("[red]ffmpeg not found in PATH[/red]")
        return

    input_arg = os.path.abspath(args.path)
    if os.path.isfile(input_arg):
        list_videos = [input_arg]
    elif os.path.isdir(input_arg):
        list_videos = list_files_by_extension_recursive(input_arg, INPUT_FORMATS)
        list_videos.sort()
    else:
        console.print("[red]Invalid path[/red]")
        return

    if args.modified_before is not None:
        cutoff_timestamp = args.modified_before.timestamp()
        list_videos = [v for v in list_videos if os.path.getmtime(v) < cutoff_timestamp]

    if not list_videos:
        console.print("No videos to process")
        return

    if os.path.isdir(input_arg):
        for item in list_videos:
            console.print(item, style="dim")
        console.print("\nListed items will be converted")
        if not Confirm.ask("Do you accept?"):
            return

    # --------------------- Video conversion
    storage_saving = 0
    overall_progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
    )
    current_progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeRemainingColumn(),
        console=console,
    )
    with tempfile.TemporaryDirectory(prefix=TMP_FILE_PREFIX) as temp_dir:
        temp_video_path = os.path.join(temp_dir, "output" + OUTPUT_FORMAT)
        with Live(Group(overall_progress, current_progress), console=console):
            overall_task = overall_progress.add_task("Compressing videos", total=len(list_videos))
            current_task = current_progress.add_task("Current file", total=None)
            for input_video_path in list_videos:
                overall_progress.update(overall_task, description=os.path.basename(input_video_path))
                video_duration = get_video_duration_seconds(input_video_path)
                current_progress.reset(
                    current_task,
                    total=video_duration,
                    description=os.path.basename(input_video_path),
                )
                output_video_path = os.path.splitext(input_video_path)[0] + OUTPUT_FORMAT
                with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr_file:
                    proc = subprocess.Popen(
                        ["ffmpeg", "-y", "-nostdin", "-i", input_video_path,
                         "-vcodec", "libx265", "-crf", "28", "-nostats",
                         "-progress", "pipe:1", temp_video_path],
                        stdout=subprocess.PIPE,
                        stderr=stderr_file,
                        text=True,
                    )
                    try:
                        for line in proc.stdout:
                            if line.startswith("out_time_us="):
                                out_time_seconds = int(line.split("=", 1)[1]) / 1_000_000
                                if video_duration is not None:
                                    out_time_seconds = min(out_time_seconds, video_duration)
                                current_progress.update(current_task, completed=out_time_seconds)
                        cmd_result = proc.wait()
                    except KeyboardInterrupt:
                        proc.terminate()
                        proc.wait()
                        console.print(
                            f"\n[yellow]Cancelled by user. You have just saved {data_size_string(storage_saving)}[/yellow]"
                        )
                        return
                    conversion_succeeded = cmd_result == 0 and os.path.isfile(temp_video_path)
                    if conversion_succeeded: # Success
                        saving = os.path.getsize(input_video_path) - os.path.getsize(temp_video_path)
                        try:
                            shutil.move(temp_video_path, output_video_path)
                        except OSError as move_error:
                            conversion_succeeded = False
                            cmd_result = str(move_error)
                        else:
                            storage_saving += saving
                            if input_video_path != output_video_path:
                                os.remove(input_video_path)
                    if not conversion_succeeded: # Error
                        if os.path.isfile(temp_video_path):
                            os.remove(temp_video_path)
                        stderr_file.seek(0)
                        ffmpeg_output = stderr_file.read()
                        error_log = (
                            "[" + time.strftime("%Y-%m-%d %H:%M:%S") + "] Returned " + str(cmd_result)
                            + " for file \"" + input_video_path + "\"\n" + ffmpeg_output + "\n"
                        )
                        with open(FILE_ERROR_LOG, "a", encoding="utf-8") as fp:
                            fp.write(error_log)
                overall_progress.advance(overall_task)
    console.print(f"================\n[green]You have just saved {data_size_string(storage_saving)}[/green]")

if __name__ == "__main__":
    main()
