# Guild Addon Manager

A Windows app that installs and updates the guild's addons for WoW Forever with one click. It is one file, `AddonManager.exe`. You don't need to install anything else.

It is not an in-game addon. It downloads the guild's addons and puts them into your WoW Forever `Interface\AddOns` folder.

Current version: v0.1.0

## Get it

Download the latest version here: https://github.com/gettic/AddonManager/releases/latest/download/AddonManager.exe

This link always gives you the newest `AddonManager.exe`.

## First start

Windows may show "Windows protected your PC" the first times, because the app isn't signed. This is normal. Click **More info**, then **Run anyway**.

## How to use it

1. Open `AddonManager.exe`. It looks for your WoW Forever folder by itself. If it can't find it, click **Choose folder** and pick the folder where WoW Forever is installed.
2. The list shows each guild addon with the version you have and the latest version (for example v1.2.3). The window is dark (black and grey with red). The status is colour-coded:

   | Status | Colour |
   | --- | --- |
   | Update available | red |
   | Not installed | light grey |
   | Up to date | dim grey |
   | Failed install | red "Failed" block |

   The title bar is dark too on Windows 10 (20H1 or newer) and Windows 11. The **Choose folder** window follows your Windows light or dark setting.
3. Click **Install** or **Update** next to an addon, or **Install/update all** to do every addon that needs it.
4. If the status says "Installed (version unknown)", the button says **Reinstall**.
5. To use a different WoW Forever folder, click **Change...**.
6. Click **Check again** to reload the list, for example after your internet came back.

If WoW is running, the app warns you. You can still install. Afterwards type `/reload` in the game, or restart it, to use the new versions.

If something fails, the app tells you in plain words and nothing is half-installed: your old version stays.

## Where the addons come from

Each guild addon has its own public page on GitHub. The app takes the latest normal release from there. Drafts and pre-releases are never offered.

The guild's list of addons is the file `addons.json` in the AddonManager repo. The app also has a built-in copy of the list, in case it can't read the file.

GitHub allows about 60 update checks per hour from one PC. That is plenty. The app remembers the answers for 10 minutes, and if you reach the limit it tells you when to try again.

## Settings and log

Both are in `%APPDATA%\AddonManager` (paste it into the Windows Explorer address bar):

| File | What it holds |
| --- | --- |
| `settings.json` | the WoW Forever folder you chose |
| `cache.json` | the latest versions found, kept for 10 minutes |
| `log.txt` | details of errors; show it to the guild officer if something goes wrong |

## For addon makers

To add an addon to the guild's list, add an entry to `addons.json` and push it. No new `.exe` is needed:

```
{"folder": "TaxiTime", "name": "Taxi Time", "repo": "gettic/TaxiTime"}
```

| Field | Meaning |
| --- | --- |
| `folder` | the addon's folder name in `Interface\AddOns` |
| `name` | the name shown in the app |
| `repo` | the public GitHub repo, as `owner/name` |

Each release of the addon needs a zip named `<Folder>-<version>.zip` or `<Folder>-v<version>.zip`, for example `TaxiTime-1.2.0.zip`. The version comes from the release tag (`v1.2.0` or `1.2.0`). Inside the zip must be the `<Folder>\` folder with `<Folder>_Forever.toc` in it. If the release has only one zip, the app uses that one.

## For developers

The app is written in Python (standard library only) and lives in `AddonManager\`.

Run the tests from the `AddonManager` folder:

```
python -m unittest discover -s tests -t .
```

Build `dist\AddonManager.exe` (needs Python on the build PC; it makes its own build environment with a pinned PyInstaller):

```
powershell -ExecutionPolicy Bypass -File build.ps1
```

The build compiles and tests the code, builds the .exe, then runs a hidden self-test of the .exe on fake data and prints its size and sha256. Use `-TestOnly` to stop after the tests.

## Not affiliated with Blizzard

Not affiliated with or endorsed by Blizzard Entertainment.
