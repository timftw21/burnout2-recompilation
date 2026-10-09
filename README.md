# Burnout 2: Point of Impact — Static Recompilation

An unofficial Windows version of the original Xbox game, focused on fast gameplay
and preserving its look and feel.

<i>Still in development: menus and the first driving lesson work. The rest of the
game is still being tested.</i>

## Screenshots

<p>
  <img src="screenshots/single-player.jpg" alt="Single Player menu" width="49%">
  <img src="screenshots/driving-lesson.jpg" alt="First driving lesson" width="49%">
</p>

## What happened to the original release?

The original recompilation worked, but its poor performance and growing complexity
led to a fresh start. Its implementation was replaced with a smaller version
focused on speed and simplicity. I couldn't bear to keep the slop version public😅

## Current status

### What works

- Startup, menus and the first driving lesson, including driving, collisions and
  tested lesson transitions.
- Background music, menu sounds and in-game sound effects.
- Creating, saving and loading profiles through the game's Load/Save menu.
- Gamepad and keyboard input.
- Saved settings for display, audio and controls, with a selectable refresh rate
  that defaults to 60 Hz.

### What still needs work

- Remaining graphics glitches, including artifacts around car shadows and further
  checks of reflections against the original game.
- Testing all modes, tracks and saved-game progression.
- Checking sound balance and consistently smooth performance across the whole game.
- Custom control bindings and other settings.

## Getting started

You need a 64-bit Windows PC, a graphics card that supports DirectX 11, and your
own copy of the US Xbox release. Game files are not included.

1. Install [Visual Studio 2026 Community](https://visualstudio.microsoft.com/downloads/)
   with **Desktop development with C++** and the included Windows SDK,
   [CMake 4.2 or newer](https://cmake.org/download/), and
   [Git for Windows](https://git-scm.com/downloads/win).
2. Download the source ZIP from [Releases](https://github.com/timftw21/burnout2-recompilation/releases)
   and extract it, or clone the repository.
3. Double-click **Build.cmd** and select your US Xbox disc image (`.iso` or `.xiso`).
   The builder prepares the game files, downloads dependencies and builds the app.
   The first build can take a while; later builds reuse previous work.
4. Click **Open game folder**, then double-click **b2.exe**.

There are no commands to type. If building fails, click **Open build log**.
The [developer build instructions](BUILDING.txt) also cover offline debugging.

The builder fills in the game-file locations on first setup. Click **Start game**
in the settings window. Press **Esc** to open or close settings, with
**General**, **Audio**, **Graphics** and **Controls** tabs. The game defaults to
**60 Hz**; choose another rate in General. Click **Save settings** to keep changes.

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

See [third-party notices](THIRD_PARTY_NOTICES.txt) for dependency licenses and
[research notes](research/) for detailed findings and source links.

<i>This is an independent fan project, unaffiliated with the game's owners.</i>
