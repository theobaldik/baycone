import os
from pathlib import Path

from baycone.config import config

# TODO: Find out which parameters are really necessary and optimize them for both passes
X264_PARAMS_MOVIE_PASS_1 = [
    "rc_lookahead=60",
    "mbtree=1",
    "vbv_maxrate=31250",
    "vbv_bufsize=31250",
]

X264_PARAMS_MOVIE_PASS_2 = [
    "cabac=1",
    "ref=4",
    "deblock=-1,-1",
    "analyse=0x3,0x133",
    "me=umh",
    "subme=9",
    "psy=1",
    "psy_rd=1.00,0.15",
    "mixed_ref=1",
    "me_range=24",
    "chroma_me=1",
    "trellis=2",
    "8x8dct=1",
    "deadzone=21,11",
    "chroma_qp_offset=-3",
    "threads=0",
    "sliced_threads=0",
    "decimate=1",
    "interlaced=0",
    "constrained_intra=0",
    "b_bias=0",
    "direct=3",
    "weightb=1",
    "open_gop=0",
    "weightp=2",
    "scenecut=40",
    "intra_refresh=0",
    "rc_lookahead=60",
    "mbtree=1",
    "qcomp=0.60",
    "qpmin=0",
    "qpmax=69",
    "qpstep=4",
    "cplxblur=20.0",
    "qblur=0.5",
    "vbv_maxrate=31250",
    "vbv_bufsize=31250",
    "filler=0",
    "aq=1,1.00",
]

CMD_FFMPEG_ENCODE_MOVIE_PASS_1 = [
    "{ffmpeg}",
    "-y",
    "-hide_banner",
    "-i",
    "{input_file}",
    "-c:v",
    "libx264",
    "-x264-params",
    f"{':'.join(X264_PARAMS_MOVIE_PASS_1)}",
    "-b:v",
    "{video_bitrate}k",
    "-pass",
    "1",
    "-an",
    "-f",
    "null",
    "-passlogfile",
    "{passlog_file}",
    "-progress",
    "pipe:1",
    os.devnull,
]

# TODO: Remove metadata and add audio track metadata
# TODO: Handle multiple audio tracks
CMD_FFMPEG_ENCODE_MOVIE_PASS_2 = [
    "{ffmpeg}",
    "-hide_banner",
    "-i",
    "{input_file}",
    "-c:v",
    "libx264",
    "-x264-params",
    f"{':'.join(X264_PARAMS_MOVIE_PASS_2)}",
    "-preset",
    "slow",
    "-tune",
    "film",
    "-profile:v",
    "high",
    "-level:v",
    "4.1",
    "-b:v",
    "{video_bitrate}k",
    "-pass",
    "2",
    "-c:a",
    "aac",
    "-b:a",
    "{audio_bitrate}k",
    "-movflags",
    "+faststart",
    "-max_interleave_delta",
    "0",
    "-fflags",
    "+genpts",
    "-passlogfile",
    "{passlog_file}",
    "-progress",
    "pipe:1",
    "{output_file}",
]

# TODO: Merge with 2nd pass of encoding
CMD_FFMPEG_OPTIMIZE = [
    "{ffmpeg}",
    "-y",
    "-hide_banner",
    "-i",
    "{input_file}",
    "-map",
    "0:v",
    "-map",
    "0:a",
    "-c",
    "copy",
    "-map_metadata",
    "-1",
    "-metadata:s:a:0",
    "language={language}",
    "-metadata:s:a:0",
    "title={track_name}",
    "-movflags",
    "+faststart",
    "{output_file}",
]

CMD_FFPROBE = [
    "{ffprobe}",
    "-v",
    "quiet",
    "-print_format",
    "json",
    "-show_format",
    "-show_streams",
    "{input_file}",
]


def get_cmd_encode_movie_pass_1(
    input_file: Path,
    passlog_file: Path,
    video_bitrate: int,
) -> list[str]:
    cmd = [
        arg.format(
            ffmpeg=config.ffmpeg_path,
            input_file=input_file.absolute(),
            passlog_file=passlog_file.absolute(),
            video_bitrate=video_bitrate,
        )
        for arg in CMD_FFMPEG_ENCODE_MOVIE_PASS_1
    ]
    return cmd


def get_cmd_encode_movie_pass_2(
    input_file: Path,
    output_file: Path,
    passlog_file: Path,
    video_bitrate: int,
    audio_bitrate: int,
) -> list[str]:
    cmd = [
        arg.format(
            ffmpeg=config.ffmpeg_path,
            input_file=input_file.absolute(),
            output_file=output_file.absolute(),
            passlog_file=passlog_file.absolute(),
            video_bitrate=video_bitrate,
            audio_bitrate=audio_bitrate,
        )
        for arg in CMD_FFMPEG_ENCODE_MOVIE_PASS_2
    ]
    return cmd


def get_cmd_optimize(
    input_file: Path,
    output_file: Path,
    language: str,
    track_name: str,
) -> list[str]:
    cmd = [
        arg.format(
            ffmpeg=config.ffmpeg_path,
            input_file=input_file.absolute(),
            output_file=output_file.absolute(),
            language=language,
            track_name=track_name,
        )
        for arg in CMD_FFMPEG_OPTIMIZE
    ]
    return cmd


def get_cmd_ffprobe(input_file: Path) -> list[str]:
    cmd = [arg.format(ffprobe=config.ffprobe_path, input_file=input_file.absolute()) for arg in CMD_FFPROBE]
    return cmd
