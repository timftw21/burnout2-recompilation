# Burnout 2: Point of Impact — Static Recompilation

An unofficial Windows version of the original Xbox game, focused on fast gameplay
and preserving its look and feel.

<i>Still in development: While gameplay is quite stable, the rest of the
game is still being tested.</i>

## AI Disclosure

Yes, AI was used in the development of this project. No, this project was not blindly thrown together.
If you're skeptical, I implore you to try the recompilation for yourself. I take quality control quite seriously!😊

## What's new in 1.1

**Major performance improvements:** typical frame time fell from **23.12 to
16.67 ms**, slow-frame time (99th percentile) from **30.84 to 17.95 ms**, and
peak RAM from **530 to 204 MiB** in comparable manual race recordings.
Average frame rate rose from **43.75 to 60.02 FPS** on a Ryzen 5 5600X and RTX 5060 Ti
at 60 Hz and native resolution. Heavy scenes still have occasional dips;
testing across the whole game continues.

This release also adds custom keyboard bindings, internal resolution scaling,
frame interpolation for higher refresh rates, a frame rate counter, performance
recording and an application icon.

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

### What still needs work

- Testing all modes, tracks and saved-game progression.
- Checking sound balance and consistently smooth performance across the whole game.
- Checking upscaled rendering and frame interpolation across all modes and PCs.

## Getting started

You need a 64-bit Windows PC, a graphics card that supports DirectX 11, and your
own copy of the US Xbox release. Game files are not included.

1. Install [Visual Studio 2026 Community](https://visualstudio.microsoft.com/downloads/)
   with **Desktop development with C++** and the included Windows SDK,
   [CMake 4.2 or newer](https://cmake.org/download/), and
   [Git for Windows](https://git-scm.com/downloads/win).
2. Download [Installation.zip](https://github.com/timftw21/burnout2-recompilation/releases/latest/download/Installation.zip)
   and extract it, or clone the repository.
3. Double-click **Build.cmd** and select your US Xbox disc image (`.iso` or `.xiso`).
   The builder prepares the game files, downloads dependencies and builds the app.
   The first build can take a while; later builds reuse previous work.
4. Click **Open game folder**, then double-click **b2.exe**.

There are no commands to type. If building fails, click **Open build log**.
The [developer build instructions](https://github.com/timftw21/burnout2-recompilation/blob/main/BUILDING.txt)
also cover offline debugging.

The builder fills in the game-file locations on first setup. Click **Start game**
in the settings window. Press **Esc** to open or close settings, with
**General**, **Audio**, **Graphics** and **Controls** tabs. The game defaults to
**60 Hz**; choose another rate in General. Click **Save settings** to keep changes.

In **Controls**, click a key beside an action and press its replacement. **Clear**
removes a binding; **Restore keyboard defaults** resets them. Esc cancels a key
change. Esc, F1, F8 and F9 stay reserved for settings and captures.

In **Graphics**, choose **Internal resolution** from 1× (640 × 480) to
4× (2560 × 1920). Higher values give sharper edges and use more graphics power.
Resolution changes apply when you start the game. If you change it while playing,
save your settings and restart the app.
In **General**, selecting a rate above 60 Hz turns on **Frame interpolation**.
Uncheck it if you prefer to keep it off, then save your settings. Gameplay stays
at 60 Hz. Interpolation adds about one frame of display delay and may show
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
