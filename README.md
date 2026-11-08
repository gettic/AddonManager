# Guild Addon Manager

A Windows app that installs and updates the guild's addons for WoW Forever with one click. It is one file, `AddonManager.exe`. You don't need to install anything else.

It is not an in-game addon. It downloads the guild's addons and puts them into your WoW Forever `Interface\AddOns` folder.

Current version: v0.0.1

## Get it

The download link comes with the first release.

## First start

Windows may show "Windows protected your PC" the first times, because the app isn't signed. This is normal. Click **More info**, then **Run anyway**.

## How to use it

1. Open `AddonManager.exe`. It looks for your WoW Forever folder by itself. If it can't find it, click **Choose folder** and pick the folder where WoW Forever is installed.
2. The list shows each guild addon with the version you have and the latest version (for example v1.2.3).
3. Click **Install** or **Update** next to an addon, or **Install/update all** to do every addon that needs it.
4. If the status says "Installed (version unknown)", the button says **Reinstall**.
5. To use a different WoW Forever folder, click **Change...**.
6. Click **Check again** to reload the list, for example after your internet came back.

If WoW is running, the app warns you. You can still install. Afterwards type `/reload` in the game, or restart it, to use the new versions.

If something fails, the app tells you in plain words and nothing is half-installed: your old version stays. The app says "No addon list is set up yet" until the guild's addon list is available.

## Settings and log

Both are in `%APPDATA%\AddonManager` (paste it into the Windows Explorer address bar):

| File | What it holds |
| --- | --- |
| `settings.json` | the WoW Forever folder you chose |
| `log.txt` | details of errors; show it to the guild officer if something goes wrong |

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
