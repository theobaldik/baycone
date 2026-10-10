import os
from pathlib import Path

from baycone.audio import AudioTrack
from baycone.config import config

# Spliced in place of this element, one argument group per output audio track
AUDIO_ARGS = "{audio_args}"

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
    "-map",
    "0:v:0",
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
    "-i",
    "{input_file}",
    "-map",
    "0:v:0",
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
    "{output_file}",
]

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


def _render(template: list[str], audio_args: list[str] | None = None, **values: object) -> list[str]:
    cmd = []
    for arg in template:
        if arg == AUDIO_ARGS:
            cmd.extend(audio_args or [])
        else:
            cmd.append(arg.format(ffmpeg=config.ffmpeg_path, ffprobe=config.ffprobe_path, **values))
    return cmd


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


def get_cmd_encode_movie_pass_1(input_file: Path, passlog_file: Path, video_bitrate: int) -> list[str]:
    return _render(
        CMD_FFMPEG_ENCODE_MOVIE_PASS_1,
        input_file=input_file.absolute(),
        passlog_file=passlog_file.absolute(),
        video_bitrate=video_bitrate,
    )


def get_cmd_encode_movie_pass_2(
    input_file: Path,
    output_file: Path,
    passlog_file: Path,
    video_bitrate: int,
    audio_tracks: list[AudioTrack],
) -> list[str]:
    return _render(
        CMD_FFMPEG_ENCODE_MOVIE_PASS_2,
        _audio_encode_args(audio_tracks),
        input_file=input_file.absolute(),
        output_file=output_file.absolute(),
        passlog_file=passlog_file.absolute(),
        video_bitrate=video_bitrate,
    )


def get_cmd_encode_audio(input_file: Path, output_file: Path, audio_tracks: list[AudioTrack]) -> list[str]:
    return _render(
        CMD_FFMPEG_ENCODE_AUDIO,
        _audio_encode_args(audio_tracks),
        input_file=input_file.absolute(),
        output_file=output_file.absolute(),
    )


def get_cmd_optimize(input_file: Path, output_file: Path, audio_tracks: list[AudioTrack], encoded: bool) -> list[str]:
    return _render(
        CMD_FFMPEG_OPTIMIZE,
        _audio_metadata_args(audio_tracks, encoded),
        input_file=input_file.absolute(),
        output_file=output_file.absolute(),
    )


def get_cmd_ffprobe(input_file: Path) -> list[str]:
    return _render(CMD_FFPROBE, input_file=input_file.absolute())
