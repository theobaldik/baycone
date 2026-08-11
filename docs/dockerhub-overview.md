# Baycone

**Media pipeline for self-hosted libraries.**

Baycone watches one or more media directories for new video files and automatically transcodes them with `ffmpeg` into a
streaming-friendly MP4 — h264 / AAC with the moov atom up front. Built for Jellyfin, but the output is plain MP4 that
any player handles.

Source, configuration reference and documentation: https://github.com/theobaldik/baycone

## Tags

| Tag      | Meaning                            |
|----------|------------------------------------|
| `0.1.0`  | Exact release                      |
| `0.1`    | Latest patch of that minor version |
| `latest` | Latest release                     |

## Quick start

```yaml
# compose.yml
services:
  baycone:
    image: theobaldik/baycone:0.1.0
    user: "1000:1000"
    volumes:
      - ./config.yml:/config/config.yml:ro
      - /your/movies:/locations/movies
```

The container runs as a non-root user, uid/gid **1000** by default, and needs read and write access to the mounted
directories — set `user:` to the host account that owns your media if that isn't uid 1000.

| Variable            | Default              | Description                                |
|---------------------|----------------------|--------------------------------------------|
| `BAYCONE_CONFIG`    | `/config/config.yml` | Path to the YAML config file               |
| `BAYCONE_LOCATIONS` | `/locations`         | Base directory for relative location paths |

## License

Baycone itself is licensed under the **MIT License**:
https://github.com/theobaldik/baycone/blob/master/LICENSE

### Third-party components in this image

This image bundles Debian packages that are **not** covered by the MIT License and remain under their own terms — most
notably **ffmpeg / ffprobe**, which Debian builds with `--enable-gpl` (including libx264 and libx265) and distributes
under the **GNU General Public License, version 2 or later**.

Baycone invokes `ffmpeg` and `ffprobe` as external processes and does not link against them. The MIT License therefore
covers Baycone's own code only; this image as a whole is a collection of separately licensed works.

The license text of every bundled package is retained inside the image under `/usr/share/doc/<package>/copyright`, and a
summary is included at `/usr/share/doc/baycone/NOTICE`.

### Written offer of source code

All bundled Debian packages are unmodified binary packages from the official Debian archive. Complete corresponding
source code for the GPL-licensed components is made publicly available by Debian at https://sources.debian.org and can
be retrieved for any bundled package with `apt-get source <package>` — for example, from a container started from this
image:

```bash
apt-get update && apt-get source ffmpeg
```

### Patent notice

The bundled ffmpeg build includes AVC/H.264 and HEVC/H.265 encoders (libx264, libx265). Distributing or using those
encoders may require patent licenses in some jurisdictions, independently of the copyright licenses above.
