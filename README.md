<div align="center"><img width="128" height="128" alt="Burnout2_Recomp_Logo_128_Corrected" src="https://github.com/user-attachments/assets/56cdde6c-ce6f-41a2-913c-63189fa3dbcf" /></div>

# <div align="center">Burnout 2: Point of Impact — Static Recompilation</div>

An unofficial Windows version of the original Xbox game, focused on fast gameplay
and preserving its look and feel.

<i>Still in development: Slowdown, bugs, and crashes are possible. Open an issue if you experience a problem, and
I'll try to respond in a timely manner. This is an independent fan project, unaffiliated with the game's owners.</i>

## AI Disclosure

Yes, AI (Codex) assisted in the development of this project. No, this project was not blindly thrown together.
If you're skeptical, I implore you to try the recompilation for yourself. I take quality control quite seriously!👀

## What's new in 1.5

- Improved HUD/text interpolation with less repeated graphics work.
- Correctness and efficiency fixes for game timing, memory, file loading,
  thread cleanup, sound processing and controller rumble.
- One **Draw distance** setting replaces the LOD and render-distance overrides:
  choose Original, **2×** or **4×**, including traffic detail at greater distances.
- Improved car reflections and reduced building flicker with extended draw
  distance. Some scenery issues remain, including on **Sunrise Valley Springs**.
- Fixed snow particle size and texturing, and improved upscaling inputs.
- Refreshed settings with the recomp's logo, clearer categories and custom icons.
  The game now opens without a separate terminal window.

Download the new **Installation.zip** and run **Build.cmd** again to update.
See [Updating between releases](#updating-between-releases) to keep your saves,
settings and imported music.

## Screenshots

<p>
  <img src="https://raw.githubusercontent.com/timftw21/burnout2-recompilation/main/screenshots/single-player.jpg" alt="Single Player menu" width="49%">
  <img src="https://raw.githubusercontent.com/timftw21/burnout2-recompilation/main/screenshots/driving-lesson.jpg" alt="First driving lesson" width="49%">
</p>

## What happened to the original release?

The original recompilation worked, but its poor performance and growing complexity
led to a fresh start. Its implementation was replaced with a smaller version
focused on speed and simplicity. I couldn't bear to keep the slop version public😅

## Current status

### What works

- Startup, menus and many levels, including driving, collisions and
  tested lesson transitions.
- Background music, menu sounds and in-game sound effects.
- Creating, saving and loading profiles through the game's Load/Save menu.
- Gamepad and keyboard input.
- Saved settings for display, audio and controls, with a selectable refresh rate
  that defaults to 60 Hz.
- Custom keyboard bindings, 1×–4× internal resolution, and optional frame
  interpolation for higher display rates.
- Native widescreen camera and screen layout, presented at 16:9.
- Optional MSAA or FXAA antialiasing and anisotropic filtering up to 16×.
- Imported custom music through the original Music Selection menu.
- Optional intro skipping and 2×/4× draw distance, including traffic detail.
- Experimental DLSS, FSR and XeSS upscaling.

### What still needs work

- Testing all modes, tracks and saved-game progression.
- Checking sound balance and consistently smooth performance across the whole game.
- Experimental upscaling has small visual issues and can reduce performance.

## Getting started

You need a 64-bit Windows PC, a graphics card that supports DirectX 11, and <i>your
own copy of the US Xbox release. Game files are not included</i>.

1. Install [Visual Studio 2026 Community](https://visualstudio.microsoft.com/downloads/)
   with **Desktop development with C++**, **C++ CMake tools for Windows** and the included Windows SDK,
   [CMake 4.2 or newer](https://cmake.org/download/), and
   [Git for Windows](https://git-scm.com/downloads/win).
   Also install the Windows x64 version of [LLVM 22.1 or newer](https://github.com/llvm/llvm-project/releases).
   **[Ninja](https://github.com/ninja-build/ninja/releases/latest)** (`ninja.exe`)
   is also required. Visual Studio includes it with **C++ CMake tools for Windows**.
2. Download [Installation.zip](https://github.com/timftw21/burnout2-recompilation/releases/latest/download/Installation.zip)
   and extract it, or clone the repository.
3. Double-click **Build.cmd** and select your US Xbox XISO disc image (`.iso` or `.xiso`).
   Use an XISO; full-disc dumps need conversion to XISO first.
   The builder prepares the game files, downloads dependencies and builds the app.
   The first build can take a while; later builds reuse previous work.
4. Click **Open game folder**, then double-click **b2.exe**.

There are no commands to type. If building fails, click **Open build log**.
The [developer build instructions](https://github.com/timftw21/burnout2-recompilation/blob/main/BUILDING.txt)
also cover offline debugging.

If the builder reports **ninja.exe is missing**, open **Visual Studio Installer**,
click **Modify** for Visual Studio 2026, and install **C++ CMake tools for Windows**
under **Individual components**. Alternatively, download **ninja-win.zip** from
the [official Ninja releases](https://github.com/ninja-build/ninja/releases/latest),
extract it, and add the folder containing `ninja.exe` to your Windows **Path**
environment variable. Close and reopen **Build.cmd** after installing it.

### Updating between releases

Use these steps when updating from an earlier release:

1. Close the game and builder.
2. Back up `data/user` and `data/local/settings.cfg` beside your current `b2.exe`.
   These contain your saves and settings. If you imported music, also back up
   `data/local/custom-music`.
3. Check the prerequisites above. Updates from releases before 1.3 require
   LLVM 22.1 or newer; Visual Studio and its Windows SDK are still required.
4. Download the latest **Installation.zip** and extract it into your existing
   source folder—the folder containing **Build.cmd**—replacing the source files.
   Keep the existing `build` and `data` folders.
5. Run **Build.cmd** again and select your XISO. It rebuilds the game and updates
   game-file locations while preserving your other settings, saves and imported
   music. Open the game folder when the build finishes.

For a new-folder installation, build it first, then restore your backups to the
same locations beside the new `b2.exe`. Run **Build.cmd** once more to refresh
game-file locations while keeping the restored settings.

### Game settings

Click **Start game** to play; press **Esc** to open or close settings. Changes
save automatically. Hover over options for details, and use **Restart game**
to apply graphics and enhancement changes during play.

- **General:** Choose the refresh rate (60 Hz by default) and FPS overlay.
  Rates above 60 Hz automatically enable **Frame interpolation** to keep gameplay
  at its normal speed. Disabling it at higher rates speeds up gameplay.
  Interpolation adds some display delay and can distort fine details.
- **Graphics:** Select 1×–4× resolution, widescreen (16:9) and V-sync.
  Higher resolutions use more graphics power.
- **Enhancements:** Adjust MSAA/FXAA, anisotropic filtering and **Draw distance**
  (Original, 2× or 4×). Increased distance uses more resources and may cause
  rendering issues. Experimental DLSS, FSR and XeSS also have visual issues and
  may reduce performance; they replace separate antialiasing and use the selected
  output resolution. **Skip intro splash screens** is under **Miscellaneous**.
- **Audio:** To use custom songs, **Browse** for a music folder, click
  **Import music**, then select the collection in the game's **Music Selection**
  menu. MP3, WAV, WMA, FLAC, M4A and AAC are supported through Windows codecs.
  Importing replaces the previous collection and restarts an active game.
- **Controls:** Click a binding and press its replacement; use **Clear** or
  **Restore keyboard defaults** as needed. Esc cancels rebinding.
- **Log:** View startup, capture and error messages.

Press **F9** to start or stop a performance recording. Reports are saved under
`build/captures/performance-*`; include the whole folder when reporting a slowdown.
Saves are in `data/user` beside `b2.exe`; back up that folder to keep your profiles.

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
| Start / Pause | Enter |
| Toggle settings | Esc (or F1) |
| Capture frame | F8 |
| Record / save performance | F9 |

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
- [Lucas and Kanade's image-registration paper](https://publications.ri.cmu.edu/storage/publications/pub_files/pub3/lucas_bruce_d_1981_1/lucas_bruce_d_1981_1.pdf):
  the motion-estimation method used for optional frame interpolation.

See [third-party notices](THIRD_PARTY_NOTICES.txt) for dependency licenses and
[research notes](https://github.com/timftw21/burnout2-recompilation/tree/main/research)
for detailed findings and source links.
