# Launcher

customtkinter app that lists the apps next to it, opens them, and keeps their
versions and change logs.

## Features

- **Apps:** every `<number>.<name>` folder with a `main.py` shown as a card with version and an editable description. Open starts the app and the launcher closes (switch it off with "Close after opening"). If the app crashes on start, the launcher stays open and shows the error.
- **Versions:** bump an app by 0.1 (1.8, 1.9, 2.0, 2.1 ...). A change log note is required. Old 3-part versions such as 1.0.0 become 1.1 on their first bump.
- **Commit Log:** every bump of every app, newest first, with filter by app and note search.
- Dark and light theme.

## Requirements

- Python 3.11 or newer
- tkinter (Linux: `sudo apt install python3-tk`)
- `pip install -r requirements.txt` (customtkinter)

## Run

```
python main.py
```

## What a bump writes (in the app's own folder)

| File | Change |
|---|---|
| `version.toml` | `version = "<new>"` (only that line changes) |
| `changelog.toml` | a `[[commit]]` entry appended (time, user, from, to, note). The Commit Log tab reads this. |
| `CHANGELOG.md` | the entry added at the top. A new file is created from `changelog.toml` if missing. Existing text is never rewritten. |

If one write fails, the others are restored. Edits made by hand in `CHANGELOG.md` are not read back.

## Repository layout

```
.
├── main.py                 entry point
├── ui.py                   interface (App)
├── core/core.py            config, app scan, launch, version bump, change logs
├── version.toml
├── requirements.txt
└── Service/Service_configs/launcher/launcher_config.toml
```

## Configuration

`Service/Service_configs/launcher/launcher_config.toml`:

- `[paths] apps_dir`: empty means the folder above the launcher.
- `[settings]`: theme, close after opening, startup check time, number of commits shown.
- `[descriptions]`: app descriptions, written when you press Edit on a card.

A missing section or key stops the app with a message naming it.
