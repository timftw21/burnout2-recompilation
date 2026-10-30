<div align="center"><img width="128" alt="Burnout2_Recomp_Logo_128_Corrected" src="https://github.com/user-attachments/assets/56cdde6c-ce6f-41a2-913c-63189fa3dbcf" /></div>

# <div align="center">Burnout 2: Point of Impact — Static Recompilation</div>

An unofficial version of the original Xbox game for Windows, with experimental
Linux and macOS ports, focused on fast gameplay, preserving the original's look
and feel, and a variety of enhancements.

**Linux and macOS builds need testing.** Linux still needs gameplay, sound and
performance checks on native desktop hardware. macOS needs those checks on both
Intel and Apple Silicon.

<i>Still in development: Slowdown, bugs, and crashes are possible. Open an issue if you experience a problem, and
I'll try to respond in a timely manner. This is an independent fan project, unaffiliated with the game's owners.</i>

## AI Disclosure

Yes, AI (Codex) assisted in the development of this project. No, this project was not blindly thrown together.
If you're skeptical, I implore you to try the recompilation for yourself. I take quality control quite seriously!👀

## What's new in 1.11

- Added ten achievements covering Crash Zones, races, Driving 101, Championships,
  starting boosts, Burnout chains and Time Attack.
- Added an **Achievements** settings tab with saved progress, icons, notifications
  that fade in and out, and an unlock sound. Disable tracking at any time, or
  reset progress after confirmation.
- Fixed lingering trails behind animated text at high refresh rates.
- Fixed Steam Deck Game Mode startup by keeping X11 available alongside Wayland.

Download the new **Installation.zip**. On Windows, run **Install.cmd** again to
update. On Linux, open **Install.desktop**; on macOS, open **Install.command**.
See [Updating between releases](#updating-between-releases) to keep your saves,
settings and imported music.

## Screenshots

<table>
  <tr>
    <td align="center" width="50%">
      <a href="https://raw.githubusercontent.com/timftw21/burnout2-recompilation/main/screenshots/single-player.jpg"><img src="https://raw.githubusercontent.com/timftw21/burnout2-recompilation/main/screenshots/single-player.jpg" alt="Single Player menu" width="360"></a><br>
      <sub>Single Player menu</sub>
    </td>
    <td align="center" width="50%">
      <a href="https://raw.githubusercontent.com/timftw21/burnout2-recompilation/main/screenshots/driving-lesson.jpg"><img src="https://raw.githubusercontent.com/timftw21/burnout2-recompilation/main/screenshots/driving-lesson.jpg" alt="Driving lesson" width="360"></a><br>
      <sub>Driving lesson</sub>
    </td>
  </tr>
  <tr>
    <td align="center" width="50%">
      <a href="https://raw.githubusercontent.com/timftw21/burnout2-recompilation/main/screenshots/snowy-track.jpg"><img src="https://raw.githubusercontent.com/timftw21/burnout2-recompilation/main/screenshots/snowy-track.jpg" alt="Police car drifting on a snowy track" width="360"></a><br>
      <sub>Snowy track</sub>
    </td>
    <td align="center" width="50%">
      <a href="https://raw.githubusercontent.com/timftw21/burnout2-recompilation/main/screenshots/airport-terminal.jpg"><img src="https://raw.githubusercontent.com/timftw21/burnout2-recompilation/main/screenshots/airport-terminal.jpg" alt="Cars at Airport Terminal 1 and 2" width="360"></a><br>
      <sub>Airport Terminal 1 &amp; 2</sub>
    </td>
  </tr>
</table>

## What happened to the original release?

The original recompilation worked, but its poor performance and growing complexity
led to a fresh start. Its implementation was replaced with a smaller version
focused on speed and simplicity. I couldn't bear to keep the slop version public😅

## Current status

The features below have been validated primarily on Windows. Linux and macOS
support is still being tested.

### What works

- Startup, menus, and many levels/modes.
- Background music, menu sounds and in-game sound effects.
- Creating, saving and loading profiles through the game's Load/Save menu.
- Gamepad and keyboard input.
- Saved settings for display, audio and controls, with refresh rates from 60 Hz
  through **Unlimited**. High-refresh gameplay and menus keep normal game speed.
- Custom keyboard bindings and 1×–4× internal resolution.
- Native 16:9 and ultrawide 21:9 cameras with the HUD's original proportions
  and edge placement.
- Optional MSAA or FXAA antialiasing and anisotropic filtering up to 16×.
- Optional ambient occlusion with adjustable quality and strength.
- Imported custom music through the original Music Selection menu, with a saved
  **Loop Track** option in Music Selection and **Pause → Options**.
- Optional motion blur from the original game.
- Optional intro skipping and 2×/4× draw distance, including traffic detail.
- Clean HUD redraws the original low-resolution interface assets with smooth edges.
- Optional fast loading skips loading transitions and frame delays.
- Ten achievements with saved progress and animated unlock notifications.
- Asset replacement mods with saved load order and conflict checks.
- Optional automatic muting when the game loses focus.

### What still needs work

- Testing all modes, tracks and saved-game progression.
- Checking sound balance and consistently smooth performance across the whole game.

## Getting started

All platforms require **your own copy of the US Xbox release** as an XISO disc
image. Game files are not included. Keep the source and build folders together.

### Linux x86-64

Use a glibc-based 64-bit Linux distribution and a compatible Vulkan 1.4 graphics
driver. Extract **Installation.zip** into a folder you will keep, then open
**Install.desktop**. Your desktop may first require **Allow Launching** or
**Run as Program**. You can also start setup from a terminal:

```bash
bash Install.sh
```

Setup installs missing dependencies on Ubuntu 24.04+/Debian 13+, Fedora and
Arch Linux, opens a disc picker and builds the game. Installing system packages
may require your password. Start the game from your application menu or run
`bash Play.sh`. Rerun setup in the same folder to update. For other distributions
and manual builds, see [BUILDING.txt](BUILDING.txt).

Linux uses Vulkan through DXVK Native and runs without Wine. Desktop gameplay,
sound and graphics performance still need testing; the existing WSL software
rendering result does not measure native GPU performance.

### macOS Intel and Apple Silicon

Building requires macOS 15.2+ and Xcode 16.3+; the game targets macOS 14+ and
a compatible Metal 3 GPU. Install Xcode and open it once to finish its setup.
Extract **Installation.zip** into a folder you will keep, then double-click
**Install.command**. Setup installs Homebrew and the missing build tools, opens
a disc picker, builds for your Mac and adds **Burnout 2 Recompilation.app** to
your home **Applications** folder. You can also start setup from a terminal:

```bash
bash Install.command
```

Intel and Apple Silicon use separate native builds. Graphics run through DXMT
and Metal, without Wine or Rosetta. Both architectures still need real gameplay,
visual, audio and performance testing. No Mac gameplay result is available yet.
CI compiles the full game on both architectures. Its Intel virtual GPU lacks
the features required by the Metal backend, so Intel graphics need testing on
a compatible physical Mac. Run `./build/macos/Release/b2-tool inspect-metal` to
check GPU support.

Linux and macOS do not support WARP, D3D11 debug
layers or hardware memory watches. Custom music imports require FFmpeg.

### Steam Deck / SteamOS

In Desktop Mode, extract **Installation.zip** into a permanent folder, then open
**Install.desktop**. Setup detects SteamOS and installs its build tools and
runtime through Flatpak in your user account. It leaves SteamOS's system files
alone. The first installation needs internet access and several GB of free space.

Launch from the application menu, or add **Burnout 2: Point of Impact** to Steam
as a non-Steam game for Gaming Mode. Leave the compatibility-tool override off.
You can also run `bash Install.sh --steamdeck` and later `bash Play.sh --steamdeck`.
Keep the installation folder and XISO in place. Steam Deck support is experimental;
gameplay, sound and performance still need checking on physical hardware.

### Windows x64

You need a 64-bit Windows PC and a graphics card that supports DirectX 11.

1. Download [Installation.zip](https://github.com/timftw21/burnout2-recompilation/releases/latest/download/Installation.zip)
   and extract it into a folder you will keep.
2. Double-click **Install.cmd** and select your US Xbox XISO disc image (`.iso` or `.xiso`).
   Use an XISO; full-disc dumps need conversion to XISO first.
   Setup automatically downloads missing tools and builds the game. If Microsoft
   C++ tools are missing, approve the Windows permission prompt for their installer.
   The first installation needs internet access and can take a while; progress
   and download sizes appear in the setup window.
3. Click **Launch game**. For later launches, open `build/windows/Release/b2.exe`.

There are no separate tool downloads, commands to type or PATH changes to make.
Compatible installed tools are reused; portable tools stay in `build/deps/windows`.
The full Visual Studio editor is not required. **Build.cmd** also opens the same
installer. If setup asks for a restart, restart Windows and run **Install.cmd**
again. Verified downloads and completed build work are kept for retries.
If building fails, click **Open build log**.
The [developer build instructions](https://github.com/timftw21/burnout2-recompilation/blob/main/BUILDING.txt)
also cover offline debugging.

### Updating between releases

These installer steps apply to Windows. On Linux and macOS, extract the updated
source over your existing folder, keeping `build` and `data`. Rerun
**Install.desktop** (Linux) or **Install.command** (macOS) and choose your XISO;
settings, saves and imported music are preserved. Keep that folder in place so
the application shortcut remains valid. After moving it, rerun setup to rebuild
and refresh the game paths. `bash Install.sh --shortcuts-only` recreates a
deleted shortcut without rebuilding an installation that remains in place.

1. Close the game and builder.
2. Back up `data/user`, `data/local/settings.cfg` and, if present,
   `data/local/achievements.cfg` beside your current `b2.exe`. These contain your
   saves, settings and achievements. If you imported music, also back up
   `data/local/custom-music`. Back up the `mods` folder as well if you added mods.
3. Download the latest **Installation.zip** and extract it into your existing
   source folder—the folder containing **Build.cmd**—replacing the source files.
   Keep the existing `build` and `data` folders.
4. Run **Install.cmd** again and select your XISO. It prepares any missing tools,
   rebuilds the game and updates game-file locations while preserving your other
   settings, saves and imported music. Click **Launch game** when the build finishes.

For a new-folder installation, build it first, then restore your backups to the
same locations beside the new `b2.exe`. Run **Install.cmd** once more to refresh
game-file locations while keeping the restored settings.

### Game settings

Click **Start game** to play; press **Esc** to open or close settings. Changes
save automatically. Hover over options for details, and use **Restart game**
to apply graphics and enhancement changes during play.

- **General:** Choose the refresh rate (60 Hz by default) or **Unlimited**, and
  enable the FPS overlay. Higher rates smooth gameplay and menus without changing
  game speed. Unlimited removes the frame cap; V-sync still follows your display.
- **Graphics:** Select 1×–4× resolution, Original (4:3), Widescreen (16:9),
  Ultrawide (21:9), and V-sync.
  Higher resolutions use more graphics power.
- **Enhancements:** Adjust MSAA/FXAA, anisotropic filtering and **Draw distance**
  (Original, 2× or 4×). **Ambient occlusion** adds contact shading, with three
  quality levels and a strength slider; Off preserves the original appearance.
  Increased distance and shading quality use more resources. Extended draw distance
  fades scenery and traffic detail changes for smoother transitions.
  **Clean HUD**, **Fast load**, **Motion blur** and **Skip intro splash screens**
  are under **Miscellaneous**. Clean HUD is enabled by default and redraws the
  original lettering, icons and gauges. Fast load is optional and removes loading
  transitions and frame limits while the game's assets load.
  Motion blur controls the original game's scene blur during boosting and other
  speed effects; it is enabled by default.
- **Audio:** Adjust volume, mute the game, or enable **Mute on focus loss** to
  silence it while another window has focus. To use custom songs, **Browse** for a music folder, click
  **Import music**, then select the collection in the game's **Music Selection**
  menu. MP3, WAV, WMA, FLAC, M4A and AAC are supported through Windows codecs
  on Windows and FFmpeg on Linux and macOS.
  Importing replaces the previous collection and restarts an active game.
  In-game music volume and Crash-mode music stopping also apply to Burnout 1 and
  imported songs. **Loop Track** repeats the selected song until you turn it off;
  change it in **Music Selection** or **Pause → Options** and confirm your choice.
- **Controls:** Click a binding and press its replacement; use **Clear** or
  **Restore keyboard defaults** as needed. Esc cancels rebinding.
- **Achievements:** View progress and the requirements for each achievement.
  **Disable achievements** pauses new unlocks and notifications while keeping
  progress; **Enable achievements** resumes tracking. **Reset achievements** asks
  for confirmation before clearing achievement progress. Game saves are kept.
- **Mods:** Open the mods folder, add a mod, then **Refresh**. Enable the mods you
  want and use the arrows to set their order; lower enabled mods override earlier
  ones when files overlap. **Check mods** reports conflicts. Changes apply when
  you start or restart the game. Setup includes **Test Mod**, disabled by default,
  which recolors the game's logos using files generated from your own disc.
  *Scripts and executable patches are not yet supported.* See
  [BUILDING.txt](BUILDING.txt) for the mod format and creation steps.
- **Diagnostics:** View startup and error messages, capture a frame, or record performance.

Press **F9** to start or stop a performance recording. Reports are saved under
`build/captures/performance-*`; include the whole folder when reporting a slowdown.
Saves are in `data/user` beside the game executable (`b2.exe` on Windows, `b2` on
Linux and macOS); back up that folder to keep your profiles.

### Achievements

Achievements unlock as you play and are shared across game profiles in the same
installation. Progress is saved in `data/local/achievements.cfg` beside the game
executable. Loading an existing save does not immediately award achievements;
previous medals and completions count toward aggregate goals when you next finish
a relevant event.

| Achievement | Requirement |
| --- | --- |
| Crash + Burn | Earn bronze or better on any Crash Zone. |
| See You in Hell | Earn gold on all Crash Zones. |
| Dangerous Driving | Earn gold on all Offensive Driving 101 lessons. |
| You're Winner ! | Earn first place in any race. |
| Copped Cars | Complete Pursuit 1, Pursuit 2, and Pursuit 3 in Championship mode. |
| Burnout Big Leagues | Complete Championship mode. |
| Burnt Out | Complete Custom Series Championship mode. |
| Started in Second Gear | Activate a starting boost. |
| Burnout x Achievement | Achieve a Burnout x 15 or higher. |
| Stay Frosty | Achieve a time of 0:40.00 or faster on Crystal Summit Peak in Time Attack mode. |

## Controls

Connected gamepads work automatically. Keyboard controls are enabled by default:

| Action | Key |
| --- | --- |
| Accelerate | W |
| Brake | S |
| Steer | A / D |
| Move through menus | Arrow keys |
| A button / Confirm | Space |
| B button / Back | Left Shift |
| Black button | R |
| White button | F |
| Start / Pause | Enter |
| Toggle settings | Esc (or F1) |
| Capture frame | F8 |
| Record / save performance | F9 |

**Black** and **White** refer to the original Xbox controller's extra face buttons.
On modern gamepads, Black maps to **RB / R1** and White maps to **LB / L1**.

## Resources and credits

The project builds on preserved research from the first recompilation and checks
discoveries against the game's original executable. Original Xbox video screenshots
provide the visual reference.

- [XboxDevWiki](https://xboxdevwiki.net/Kernel) and
  [nxdk](https://github.com/XboxDev/nxdk): Xbox hardware and system interfaces.
- [xemu](https://github.com/xemu-project/xemu) and
  [Cxbx-Reloaded](https://github.com/Cxbx-Reloaded/Cxbx-Reloaded): references for
  graphics, sound and Xbox system behavior.
- [DSP56300 manual](https://www.nxp.com/docs/en/reference-manual/DSP56300FM.pdf) and
  [mborgerson's DSP56300 toolkit](https://github.com/mborgerson/dsp56300): the
  original sound processor and its instructions.
- [SDL3](https://wiki.libsdl.org/SDL3/FrontPage),
  [Dear ImGui](https://github.com/ocornut/imgui) and
  [Zydis](https://github.com/zyantific/zydis): windows, input, audio, settings and
  instruction decoding during the build.
- [Microsoft's Direct3D 11 documentation](https://learn.microsoft.com/en-us/windows/win32/direct3d11/atoc-dx-graphics-direct3d-11)
  and [Windows API documentation](https://learn.microsoft.com/en-us/windows/win32/api/):
  the Windows graphics and system backend.
- [DXVK Native](https://github.com/doitsujin/dxvk) and
  [DXMT](https://github.com/3Shain/dxmt): native Vulkan and Metal graphics backends.
- [Berkeley SoftFloat](https://www.jhauser.us/arithmetic/SoftFloat.html) and
  [MPFR](https://www.mpfr.org/): floating-point support for Apple Silicon.
- [XeGTAO](https://github.com/GameTechDev/XeGTAO): reference for ambient occlusion.

See [third-party notices](THIRD_PARTY_NOTICES.txt) for dependency licenses and
[research notes](https://github.com/timftw21/burnout2-recompilation/tree/main/research)
for detailed findings and source links.
