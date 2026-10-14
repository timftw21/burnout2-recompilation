# Burnout 2: Point of Impact — Static Recompilation

An unofficial Windows version of the original Xbox game, focused on fast gameplay
and preserving its look and feel.

<i>Still in development: Slowdown, bugs, and crashes are possible. Open an issue if you experience a problem, and
I'll try to respond in a timely manner.</i>

## AI Disclosure

Yes, AI was used in the development of this project. No, this project was not blindly thrown together.
If you're skeptical, I implore you to try the recompilation for yourself. I take quality control quite seriously!😊

## What's new in 1.4

- Faster game code and less repeated graphics work, with improved frame pacing.
- Fixed competing frame-rate limits with **V-sync**, including when frame
  interpolation is enabled. Lower selected frame rates still keep their cap.
- Fixed the square in the sky, incorrect trackside wall textures on
  **Heartbreak Hills [R]**, and a crash caused by graphics resource handling.
- More efficient frame interpolation and upscaling. Upscaling now uses your
  selected output resolution instead of doubling an already enlarged image.
- **Custom music** imports your songs for the game's Music Selection menu.
- Optional **MSAA/FXAA antialiasing**, **anisotropic filtering**, **LOD override**,
  and a **Render distance override** that extends the original range by 50%.
- Experimental **DLSS, FSR and XeSS**, with a visible warning about visual issues
  and possible performance loss.
- **Skip intro splash screens** now stops at the **PRESS START** title screen.
- The mouse cursor hides after two idle seconds and returns on mouse input.

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
- Optional intro skipping and increased track detail and draw distance.
- Experimental DLSS, FSR and XeSS upscaling.

### What still needs work

- Testing all modes, tracks and saved-game progression.
- Checking sound balance and consistently smooth performance across the whole game.
- Experimental upscaling has visual issues and can reduce performance.
- Frame interpolation can still distort fine details and transparent overlays.

## Getting started

You need a 64-bit Windows PC, a graphics card that supports DirectX 11, and your
own copy of the US Xbox release. Game files are not included.

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

Use these steps when updating from an earlier release to 1.4 or a later release:

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

The builder updates the game-file locations after each successful build while
keeping your other settings and saves. Click **Start game**
in the settings window. Press **Esc** to open or close settings, with
**Game Files**, **General**, **Audio**, **Graphics**, **Enhancements**, **Controls** and **Log** tabs. The game
defaults to **60 Hz**; choose another rate in General. Changes save automatically
when you finish editing. Hover over a setting to read its description.
Startup, capture and error messages appear in Log, with the newest messages first.
The mouse cursor hides after two seconds without mouse input. Moving, clicking
or scrolling restores it; it stays visible while a mouse button is held.

In **Controls**, click a key beside an action and press its replacement. **Clear**
removes a binding; **Restore keyboard defaults** resets them. Esc cancels a key
change. Esc, F1, F8 and F9 stay reserved for settings and captures.

In **Audio**, click **Browse** beside **Music folder**, select a folder, then
click **Import music**. Importing enables **Custom music** automatically. Start
the game and choose the imported collection in its **Music Selection** menu.
The folder name becomes the collection name; filenames become song names.
Each import replaces the previous collection and accepts up to 500 songs directly
inside the selected folder. Subfolders are not included.

MP3, WAV, WMA, FLAC, M4A and AAC files use the audio codecs available in Windows.
Imported copies are stored in `data/local/custom-music` beside `b2.exe`; your
original files stay untouched. If Windows reports that its media components are
missing, install the Media Feature Pack. Importing during play restarts the game
from the beginning. Toggling **Custom music** applies when you start or restart
the game. Cancelling an import keeps the previous collection.
Importing preserves the recording's volume; different songs can be louder or
quieter than the built-in music. Use the game's music volume controls to adjust it.

In **Graphics**, choose **Internal resolution** from 1× (640 × 480) to
4× (2560 × 1920). Higher values give sharper edges and use more graphics power.
Enable **Widescreen (16:9)** for the game's native widescreen view. It uses the
original camera and screen layout, with the image fitted to 16:9 within your
window or display.
Widescreen keeps the same internal resolution: it renders a wider view into the
original 640 × 480 buffer, then stretches that image to 16:9 for display.
Resolution changes apply when you start the game. If you change it while playing,
click **Restart game** in settings to apply them. Restart game reloads the game
from the beginning while keeping the window open.
Widescreen changes also apply when you start or restart the game.
**V-sync** prevents tearing and limits presentation to your display's refresh
rate. Selecting a lower frame rate keeps that lower cap. It defaults to disabled.

In **Enhancements**, choose **Antialiasing** from Off (original), MSAA 2×/4×/8×,
or FXAA Low/Medium/High. MSAA smooths geometry edges while keeping texture detail
sharp; higher sample counts use more GPU power and memory. Unsupported MSAA
levels are disabled. FXAA smooths the whole image and can soften fine detail
and in-game text. **Anisotropic filtering** offers Original, Off (1×), 2×, 4×, 8× and 16×;
higher values keep textures sharper at an angle. Original preserves the game's
per-texture filtering, including its 2× anisotropic filtering. Both options
apply when you start or restart the game. They default to the original settings;
increasing them uses more GPU power.

**LOD override** keeps textures at their highest available detail and uses the
track geometry selected for nearby viewing. **Render distance override** extends
the original track visibility, camera clipping and scenery fade distances by
50%. Extra scenery is loaded during startup and culled outside the extended
range. These options increase GPU use; forcing texture detail can also cause
shimmer. **Skip intro splash screens** goes to the **PRESS START** title screen
after the required loading screen. All three default to disabled.

**Upscaling technique** offers experimental DLSS, FSR and XeSS. All three have
visual issues and can reduce performance. With upscaling enabled, **Graphics**
shows **Output resolution**: choose the final image size from 1× to 4×.
The game renders at half that scale, rounded up to a whole native scale, and
reconstructs the chosen output size. At 1×, the technique provides antialiasing
at native resolution. Separate MSAA/FXAA is disabled while upscaling is selected.
DLSS requires an NVIDIA RTX card; FSR and XeSS support additional compatible
cards. Upscaling defaults to Off. Enhancement changes apply on Start/Restart.

The game buttons share a centered row. **Exit game** saves pending setting
changes and closes the app.
In **General**, selecting a rate above 60 Hz turns on **Frame interpolation**.
Uncheck it if you prefer to keep it off. With interpolation
on, gameplay stays at 60 Hz; with it off, higher rates speed up gameplay.
Interpolation adds about one frame of display delay and may show
artifacts around fast movement. **Show frame rate** displays the current game
and display FPS in the upper-right corner.

To record a slowdown, press **F9**, reproduce the problem, then press **F9**
again to save the report. Recording stops after one minute. Reports save under
`build/captures/performance-*`; send the whole folder when reporting performance
issues. **F8** also includes performance data in a frame capture while recording.

Profiles are stored in `data/user` beside `b2.exe`. Back up that whole folder to
keep your saves.

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

<i>This is an independent fan project, unaffiliated with the game's owners.</i>
