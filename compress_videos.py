# ===================== IMPORTS ============================================== #
import os
import time
import shutil
import tempfile
import argparse
import subprocess
from datetime import datetime

from rich.console import Console
from rich.live import Live
from rich.prompt import Confirm
from rich.tree import Tree
from rich.rule import Rule
from rich.table import Table
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
OUTPUT_CODEC = "hevc"
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

class PairedTask:
    """Drives one task across several Progress instances kept in lockstep,
    so a bar and its metrics can live in separate table cells."""
    def __init__(self, *progress_bars):
        self.progress_bars = progress_bars
        self.task_ids = []

    def add_task(self, *args, **kwargs):
        self.task_ids = [progress_bar.add_task(*args, **kwargs) for progress_bar in self.progress_bars]

    def update(self, **kwargs):
        for progress_bar, task_id in zip(self.progress_bars, self.task_ids):
            progress_bar.update(task_id, **kwargs)

    def reset(self, **kwargs):
        for progress_bar, task_id in zip(self.progress_bars, self.task_ids):
            progress_bar.reset(task_id, **kwargs)

    def advance(self, amount=1):
        for progress_bar, task_id in zip(self.progress_bars, self.task_ids):
            progress_bar.advance(task_id, amount)

def build_file_tree(root_directory, file_paths):
    tree = Tree(f"[bold]{root_directory}[/bold]")
    branch_by_dir = {root_directory: tree}
    for file_path in sorted(file_paths):
        relative_path = os.path.relpath(file_path, root_directory)
        parent_branch = tree
        current_dir = root_directory
        for part in os.path.dirname(relative_path).split(os.sep):
            if not part:
                continue
            current_dir = os.path.join(current_dir, part)
            if current_dir not in branch_by_dir:
                branch_by_dir[current_dir] = parent_branch.add(f"[bold]{part}[/bold]")
            parent_branch = branch_by_dir[current_dir]
        parent_branch.add(os.path.basename(file_path), style="dim")
    return tree

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

def get_video_codec(video_path):
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=codec_name", "-of", "default=noprint_wrappers=1:nokey=1", video_path],
            capture_output=True, text=True, check=True,
        )
        return result.stdout.strip().lower() or None
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None

def is_already_compressed(video_path):
    video_ext = os.path.splitext(video_path)[1].lower()
    return video_ext == OUTPUT_FORMAT and get_video_codec(video_path) == OUTPUT_CODEC

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
    parser.add_argument(
        "--filter-codec",
        action="store_true",
        help=f"Skip videos that already appear compressed ({OUTPUT_FORMAT} + {OUTPUT_CODEC} codec)",
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

    rejected_videos = []
    if args.modified_before is not None:
        cutoff_timestamp = args.modified_before.timestamp()
        kept_videos = []
        for video_path in list_videos:
            if os.path.getmtime(video_path) < cutoff_timestamp:
                kept_videos.append(video_path)
            else:
                rejected_videos.append(video_path)
        list_videos = kept_videos

    if args.filter_codec:
        kept_videos = []
        for video_path in list_videos:
            if is_already_compressed(video_path):
                rejected_videos.append(video_path)
            else:
                kept_videos.append(video_path)
        list_videos = kept_videos

    if not list_videos:
        console.print("No videos to process")
        return

    if os.path.isdir(input_arg):
        if rejected_videos:
            console.print(Rule(f"[bold yellow]Rejected videos ({len(rejected_videos)})[/bold yellow]"))
            console.print("Videos excluded by the active filters.")
            console.print(build_file_tree(input_arg, rejected_videos))
        console.print(Rule(f"[bold green]Videos to convert ({len(list_videos)})[/bold green]"))
        console.print("Videos that matched the supported extensions and passed all active filters.")
        console.print(build_file_tree(input_arg, list_videos))
        console.print("\nListed items will be converted")
        if not Confirm.ask("Do you accept?"):
            return

    # --------------------- Video conversion
    storage_saving = 0

    overall_bar_progress = Progress(SpinnerColumn(), BarColumn(), console=console)
    overall_count_progress = Progress(MofNCompleteColumn(), console=console)
    overall_time_progress = Progress(TimeElapsedColumn(), console=console)

    current_bar_progress = Progress(SpinnerColumn(), BarColumn(), console=console)
    current_percent_progress = Progress(TaskProgressColumn(), console=console)
    current_time_progress = Progress(TimeRemainingColumn(), console=console)

    video_name_progress = Progress(TextColumn("{task.description}"), console=console)

    progress_table = Table.grid(padding=(0, 2))
    for _ in range(4):
        progress_table.add_column(justify="left")
    progress_table.add_row(
        overall_bar_progress, overall_count_progress, overall_time_progress, "Total elapsed time"
    )
    progress_table.add_row(
        current_bar_progress, current_percent_progress, current_time_progress,
        "Estimated time remaining for the current video",
    )
    progress_table.add_row(video_name_progress, "", "", "")

    overall_task = PairedTask(overall_bar_progress, overall_count_progress, overall_time_progress)
    current_task = PairedTask(current_bar_progress, current_percent_progress, current_time_progress)
    video_name_task = PairedTask(video_name_progress)

    with tempfile.TemporaryDirectory(prefix=TMP_FILE_PREFIX) as temp_dir:
        temp_video_path = os.path.join(temp_dir, "output" + OUTPUT_FORMAT)
        with Live(progress_table, console=console):
            overall_task.add_task("Compressing videos", total=len(list_videos))
            current_task.add_task("Current file", total=None)
            video_name_task.add_task("", total=None)
            for input_video_path in list_videos:
                video_name_task.update(description=os.path.basename(input_video_path))
                video_duration = get_video_duration_seconds(input_video_path)
                current_task.reset(total=video_duration)
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
                                current_task.update(completed=out_time_seconds)
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
                overall_task.advance()
    console.print(f"================\n[green]You have just saved {data_size_string(storage_saving)}[/green]")

if __name__ == "__main__":
    main()
