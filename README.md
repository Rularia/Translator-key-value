# Translator JSON Tool

Desktop JSON translation helper for game text workflows.

## Features

- Loads source JSON and project files (`*.tzproj.json`)
- Autosaves current state to `translator_autosave.tzproj.json`
- Stores API profiles in `api_profiles.json`
- Left side: `Worklist`, `Translated`, `Skipped`
- Right side: `Manual`, `Batch`, `Auto`
- Search supports wildcards: `*` and `?`
- Batch tools support copy, multi-line paste, find, replace
- Auto web workflow uses numbered blocks like `[1]`, `[2]`, `[3]`
- Auto API workflow supports OpenAI-compatible `chat/completions`
- Save As JSON only writes back rows that are not skipped
- JSON structure is preserved logically; output formatting is normalized

## Files

- `app.py`: main desktop app
- `api_profiles.json`: saved API profiles
- `api_profiles.example.json`: example API profile template
- `translator_autosave.tzproj.json`: autosaved project state
- `build_release.py`: PyInstaller build script
- `app.ico`: optional app icon used during packaging

## Run

```bat
conda activate <your-conda-env>
cd /d <project-folder>
pip install -e .
set PYTHONPATH=src
python app.py
```

## Packaging

Put your icon at:

```text
<project-folder>\app.ico
```

Then install PyInstaller and run the build script:

```bat
conda activate <your-conda-env>
cd /d <project-folder>
pip install pyinstaller
python build_release.py
```

The script will:

- build a windowed executable with PyInstaller
- include `src` in the import path
- include `api_profiles.example.json` in the output
- use `app.ico` automatically if it exists

Build output:

- `dist\\TranslatorJsonTool\\`
- `build\\`
- `app.spec`

If `app.ico` is missing, the script will still build the app without an icon.
