# hot-cues-automation

Adds Serato-compatible hot cues to drum-and-bass MP3 files.

The script scans one folder for `.mp3` files, detects DNB tempo tracks, skips
files that already contain Serato hot cues, asks which detected drop to use,
then writes three hot cues into the MP3's ID3 tags.

## Install as a command

Recommended, from this repo:

```bash
pipx install --editable .
```

That installs a command you can run from any folder:

```bash
hot-cues
```

If you do not have `pipx`, install it first:

```bash
python3 -m pip install --user pipx
python3 -m pipx ensurepath
```

Restart your terminal after `ensurepath` if `hot-cues` is not found.

## Development install

For local development from this repo:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

That installs the same command while the virtualenv is active:

```bash
hot-cues
```

## Usage

Run it from a folder that contains MP3s:

```bash
cd /path/to/serato-folder
hot-cues
```

Or pass the folder explicitly:

```bash
hot-cues /path/to/serato-folder
```

The scan is non-recursive. It only processes MP3 files directly inside the
selected folder.

## Notes

- The script writes ID3 tags directly into MP3 files, so test on copies first.
- During processing, enter a drop number to write cues, `p` to preview the file
  in the system audio player, or `s` to skip the track.
- Files that already have Serato hot cues are skipped.
