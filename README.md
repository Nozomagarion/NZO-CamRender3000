# NZO CamRender3000

A Blender 5.1 add-on that automates sequential rendering of multiple cameras, each using its own animation keyframe range — in a single click.

![Blender](https://img.shields.io/badge/Blender-5.1%2B-orange?logo=blender) ![License](https://img.shields.io/badge/license-MIT-blue)

---

## The Problem

Rendering a continuous sequence involving multiple animated cameras normally requires manually switching the active camera, adjusting `frame_start` / `frame_end`, and launching each render separately.

## The Solution

**NZO CamRender3000** automates this entirely:

1. It reads each camera's keyframe range automatically
2. Places timeline markers to bind each camera to its start frame
3. Launches **a single render job** — Blender switches cameras natively at each marker
4. Restores your original scene state when done

The result: a perfectly contiguous, absolutely-numbered frame sequence (e.g. `0000.png` → `0120.png`) ready for editing.

---

## Features

- **N-Panel UI** — dedicated *MultiCam* tab in the 3D Viewport sidebar
- **Dynamic camera list** — scan all scene cameras with one click, checkbox each one in/out
- **Auto keyframe detection** — extracts `fmin`/`fmax` from each camera's action (supports Blender 4.4+ layered actions and legacy FCurves)
- **Live progress bar** — frame counter + per-camera status during render
- **Non-destructive** — original camera, frame range and timeline markers are fully restored after render
- **Reliable** — marker-based approach avoids the `G.is_rendering` race condition that breaks handler/timer chaining in Blender 5.x

---

## Installation

1. Download `multicam_render.zip` from the [Releases](../../releases) page
2. In Blender: **Edit → Preferences → Add-ons → Install…**
3. Select the `.zip` file and enable **Render: NZO CamRender3000**

---

## Usage

1. Open the **N-Panel** (`N` key) in the 3D Viewport → **MultiCam** tab
2. Click **Refresh Camera List** — all scene cameras appear with checkboxes
3. Check the cameras you want to include (they must have keyframes)
4. Verify the **Output path** at the bottom of the panel
5. Click **Render Selected Cameras** — done

> **Note:** Camera keyframe ranges should be non-overlapping for correct results  
> (e.g. Cam1: frames 0–60, Cam2: frames 61–120, Cam3: frames 121–180).

---

## How It Works (Technical)

Instead of chaining multiple `bpy.ops.render.render()` calls (which silently fail in Blender 5.x because `G.is_rendering` hasn't been cleared between calls), the add-on:

1. Saves existing timeline markers
2. Clears markers and places one per camera at its `fmin` frame, binding `marker.camera`
3. Sets `scene.frame_start` / `scene.frame_end` to cover the full combined range
4. Calls `bpy.ops.render.render('INVOKE_DEFAULT', animation=True)` **once**
5. Blender's native marker-camera system handles all camera switches
6. `render_complete` handler restores everything when the job finishes

---

## Compatibility

| Blender | Status |
|---------|--------|
| 5.1     | ✅ Tested |
| 4.4     | ✅ Compatible (layered actions supported) |
| < 4.4   | ⚠️ Should work (legacy FCurve API fallback) |

---

## License

MIT — free to use, modify and distribute.
