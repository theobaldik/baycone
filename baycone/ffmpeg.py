import os
from pathlib import Path

from baycone.audio import AudioTrack
from baycone.config import config

# Spliced in place of these elements: one argument group per output audio track, the video filters
AUDIO_ARGS = "{audio_args}"
VIDEO_ARGS = "{video_args}"

# 8-bit 4:2:0, the only format libx264's 'high' profile and every player accept; a no-op for such sources
TARGET_PIX_FMT = "yuv420p"

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
    "-v",
    "error",
    "-i",
    "{input_file}",
    "-map",
    "0:v:0",
    VIDEO_ARGS,
    "-pix_fmt",
    TARGET_PIX_FMT,
    "-c:v",
    "libx264",
    "-x264-params",
    ":".join(X264_PARAMS_MOVIE_PASS_1),
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

CMD_FFMPEG_ENCODE_MOVIE_PASS_2 = [
    "{ffmpeg}",
    "-hide_banner",
    "-v",
    "error",
    "-i",
    "{input_file}",
    "-map",
    "0:v:0",
    VIDEO_ARGS,
    "-pix_fmt",
    TARGET_PIX_FMT,
    "-c:v",
    "libx264",
    "-x264-params",
    ":".join(X264_PARAMS_MOVIE_PASS_2),
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
    AUDIO_ARGS,
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

CMD_FFMPEG_ENCODE_AUDIO = [
    "{ffmpeg}",
    "-y",
    "-hide_banner",
    "-v",
    "error",
    "-i",
    "{input_file}",
    "-map",
    "0:v:0",
    AUDIO_ARGS,
    "-c:v",
    "copy",
    "-progress",
    "pipe:1",
    "{output_file}",
]

# TODO: Merge with 2nd pass of encoding
CMD_FFMPEG_OPTIMIZE = [
    "{ffmpeg}",
    "-y",
    "-hide_banner",
    "-v",
    "error",
    "-fflags",
    "+genpts",
    "-i",
    "{input_file}",
    "-map",
    "0:v",
    "-c",
    "copy",
    "-map_metadata",
    "-1",
    AUDIO_ARGS,
    "-movflags",
    "+faststart",
    "-progress",
    "pipe:1",
    "{output_file}",
]

ARGS_VIDEO_FILTERS = ["-vf", "{filters}"]

FILTER_CROP = "crop=iw:{height}:0:{top}"
FILTER_SCALE = "scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2"
# zscale fails on untagged input, so the source transfer is stated explicitly; HDR is always BT.2020
FILTER_TONEMAP = ",".join(
    [
        "zscale=tin={transfer}:pin=bt2020:min=bt2020nc:rin=tv:t=linear:npl=100",
        "format=gbrpf32le",
        "zscale=p=bt709",
        "tonemap=tonemap=hable:desat=0",
        "zscale=t=bt709:m=bt709:r=tv",
    ]
)

ARGS_AUDIO_MAP = ["-map", "0:a:{source}"]
ARGS_AUDIO_COPY = ["-c:a:{output}", "copy"]
ARGS_AUDIO_ENCODE = ["-c:a:{output}", "aac", "-b:a:{output}", "{bitrate}k"]
ARGS_AUDIO_METADATA = [
    "-metadata:s:a:{output}",
    "language={language}",
    "-metadata:s:a:{output}",
    "title={title}",
    "-disposition:a:{output}",
    "{disposition}",
]

# Keyframes only; without 'reset' cropdetect reports the bounding box of the content across the whole file
CMD_FFMPEG_CROPDETECT = [
    "{ffmpeg}",
    "-hide_banner",
    "-nostats",
    "-skip_frame",
    "nokey",
    "-i",
    "{input_file}",
    "-map",
    "0:v:0",
    "-vf",
    "cropdetect=limit=0.094:round=2:skip=0",
    "-an",
    "-f",
    "null",
    "-",
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


def _render(template: list[str], splices: dict[str, list[str]] | None = None, **values: object) -> list[str]:
    splices = splices or {}
    cmd = []
    for arg in template:
        if arg in splices:
            cmd.extend(splices[arg])
        else:
            cmd.append(arg.format(ffmpeg=config.ffmpeg_path, ffprobe=config.ffprobe_path, **values))
    return cmd


def get_filter_crop(height: int, top: int) -> str:
    return FILTER_CROP.format(height=height, top=top)


def get_filter_scale(width: int, height: int) -> str:
    return FILTER_SCALE.format(width=width, height=height)


def get_filter_tonemap(transfer: str) -> str:
    return FILTER_TONEMAP.format(transfer=transfer)


def _video_args(video_filters: list[str]) -> list[str]:
    return _render(ARGS_VIDEO_FILTERS, filters=",".join(video_filters)) if video_filters else []


def _audio_encode_args(audio_tracks: list[AudioTrack]) -> list[str]:
    args = []
    for output, track in enumerate(audio_tracks):
        args += _render(ARGS_AUDIO_MAP, source=track.stream.index)
        if track.bitrate:
            args += _render(ARGS_AUDIO_ENCODE, output=output, bitrate=track.bitrate)
        else:
            args += _render(ARGS_AUDIO_COPY, output=output)
    return args


def _audio_metadata_args(audio_tracks: list[AudioTrack], encoded: bool) -> list[str]:
    """An encoded file already holds just `audio_tracks`, in output order."""
    args = []
    for output, track in enumerate(audio_tracks):
        args += _render(ARGS_AUDIO_MAP, source=output if encoded else track.stream.index)
        args += _render(
            ARGS_AUDIO_METADATA,
            output=output,
            language=track.stream.lang,
            title=track.title,
            disposition=track.disposition,
        )
    return args


def get_cmd_encode_movie_pass_1(
    input_file: Path,
    passlog_file: Path,
    video_bitrate: int,
    video_filters: list[str],
) -> list[str]:
    return _render(
        CMD_FFMPEG_ENCODE_MOVIE_PASS_1,
        {VIDEO_ARGS: _video_args(video_filters)},
        input_file=input_file.absolute(),
        passlog_file=passlog_file.absolute(),
        video_bitrate=video_bitrate,
    )


def get_cmd_encode_movie_pass_2(
    input_file: Path,
    output_file: Path,
    passlog_file: Path,
    video_bitrate: int,
    video_filters: list[str],
    audio_tracks: list[AudioTrack],
) -> list[str]:
    return _render(
        CMD_FFMPEG_ENCODE_MOVIE_PASS_2,
        {VIDEO_ARGS: _video_args(video_filters), AUDIO_ARGS: _audio_encode_args(audio_tracks)},
        input_file=input_file.absolute(),
        output_file=output_file.absolute(),
        passlog_file=passlog_file.absolute(),
        video_bitrate=video_bitrate,
    )


def get_cmd_encode_audio(input_file: Path, output_file: Path, audio_tracks: list[AudioTrack]) -> list[str]:
    return _render(
        CMD_FFMPEG_ENCODE_AUDIO,
        {AUDIO_ARGS: _audio_encode_args(audio_tracks)},
        input_file=input_file.absolute(),
        output_file=output_file.absolute(),
    )


def get_cmd_optimize(input_file: Path, output_file: Path, audio_tracks: list[AudioTrack], encoded: bool) -> list[str]:
    return _render(
        CMD_FFMPEG_OPTIMIZE,
        {AUDIO_ARGS: _audio_metadata_args(audio_tracks, encoded)},
        input_file=input_file.absolute(),
        output_file=output_file.absolute(),
    )


def get_cmd_cropdetect(input_file: Path) -> list[str]:
    return _render(CMD_FFMPEG_CROPDETECT, input_file=input_file.absolute())


def get_cmd_ffprobe(input_file: Path) -> list[str]:
    return _render(CMD_FFPROBE, input_file=input_file.absolute())
