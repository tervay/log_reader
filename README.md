# wpilog

A small command-line tool that reads WPILib `.wpilog` files, and CTRE `.hoot` files, and prints JSON. It is built for AI agents (and people) who debug robot code from logs. All output can be piped straight into `jq`.

## Install

Download the binary for your OS from the [Releases](../../releases) page. It is a single file with nothing else to install.

Or run it from source with [uv](https://docs.astral.sh/uv/):

```sh
uvx --from git+https://github.com/tervay/log_reader wpilog --help
```

On macOS, a downloaded binary may be blocked the first time it runs. To unblock it, run `xattr -d com.apple.quarantine wpilog-macos-*`.

## Usage

```sh
wpilog info LOG
wpilog read LOG [-e GLOB ...] [--start SEC] [--end SEC] [--limit N]
```

`info` prints one JSON object that lists every entry in the log:

```json
{"file":"match.wpilog","extraHeader":"","start":5.29,"end":487.33,"entries":[
  {"name":"/Drive/Pose","type":"struct:Pose2d","metadata":"","count":22409,"first":5.29,"last":487.33}]}
```

`read` prints one JSON object per record. `t` is the time in seconds.

```json
{"t":12.34,"name":"/Drive/Pose","value":{"translation":{"x":1.2,"y":3.4},"rotation":{"value":0.5}}}
```

- `-e` takes a glob (`*`, `?`, `[...]`) that is matched against the full entry name. You can repeat it. Without `-e`, the tool prints every entry.
- `--start` and `--end` limit the output to a time window, in seconds.
- `--limit` stops after N records.

### Examples

```sh
wpilog info match.wpilog | jq -r '.entries[] | "\(.name)\t\(.type)\t\(.count)"'
wpilog read match.wpilog -e '/Drive/*' --start 20 --end 25
wpilog read match.wpilog -e '/RealOutputs/Odometry/RobotPose' | jq -c '[.t, .value.translation.x, .value.translation.y]'
wpilog read match.wpilog -e '/DriverStation/Enabled' | jq -s 'map(select(.value)) | first'
```

### How values are printed

| Log type | JSON value |
|---|---|
| `boolean`, `int64`, `float`, `double`, `string` | the JSON scalar |
| `boolean[]`, `int64[]`, `float[]`, `double[]`, `string[]` | an array |
| `json` | the parsed JSON |
| `struct:X`, `struct:X[]` | an object (or an array of objects), decoded with the schemas in the log, including nested structs, enums and bit-fields |
| anything else (`raw`, `proto:*`, `msgpack`, ...) | a hex string |

Values that JSON can't represent (NaN and ±Infinity) are printed as `null`. Errors go to stderr, so stdout always holds valid JSON.

## Hoot files

When you pass a `.hoot` file, `wpilog` converts it to wpilog with CTRE's [owlet](https://docs.ctr-electronics.com/cli-tools.html), then reads the result. The tool picks the owlet version that matches the log and downloads it from CTRE the first time you need it. The download goes into the same folder as the `wpilog` executable, as `owlet-<version>-C<compliancy>`, and later runs reuse it. To get a newer owlet, delete that file. Owlet is CTRE software, covered by [CTRE's license](https://raw.githubusercontent.com/CrossTheRoadElec/Phoenix-Releases/refs/heads/master/CTRE_LICENSE.txt).

## Development

```sh
uv run pytest
uv run pyinstaller --onefile --name wpilog --collect-binaries native.wpiutil wpilog.py
```

Push a tag such as `v0.2.0` to build binaries for Linux (x86_64 and arm64), Windows, and macOS (arm64 and x86_64) and attach them to a GitHub release.

### Upgrading WPILib

Change the `robotpy-wpiutil==` version in `pyproject.toml`, then run `uv run pytest` and tag a release. No other file depends on the WPILib version.
