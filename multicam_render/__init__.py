# ============================================================
#  Multi-Camera Sequence Render — Blender 5.1  (v1.4)
#
#  STRATEGY: instead of chaining multiple render.render() calls
#  (which silently fail because G.is_rendering is still True),
#  we:
#    1. Place one Timeline Marker per camera at its fmin frame
#       and bind the camera to that marker.
#    2. Launch a SINGLE render.render(animation=True) over the
#       combined frame range.
#    Blender switches the active camera at each marker
#    automatically — no chaining needed.
#    3. restore_state() cleans up markers/camera/frames after.
#
#  Progress is updated via render_post (per frame) + a
#  persistent redraw timer so the N-Panel bar updates live.
# ============================================================

bl_info = {
    "name":        "NZO CamRender3000",
    "author":      "NZO",
    "version":     (1, 4, 0),
    "blender":     (5, 1, 0),
    "location":    "View3D › Sidebar › MultiCam",
    "description": "Batch-render multiple cameras sequentially using their own keyframe ranges",
    "category":    "Render",
}

import bpy
import os
import time as _time
from bpy.props import (StringProperty, BoolProperty,
                       CollectionProperty, IntProperty, EnumProperty)
from bpy.types import PropertyGroup, UIList, Operator, Panel


# ──────────────────────────────────────────────────────────────
#  Global state
# ──────────────────────────────────────────────────────────────

_render_state: dict = {}   # backup of scene before batch

# Progress dict — read by the panel's draw()
_progress = {
    "is_running":      False,
    "total_frames":    0,
    "rendered_frames": 0,
    "cameras":         [],   # [(name, fmin, fmax), ...]
    "current_cam":     "",
}

_redraw_running = [False]  # whether the redraw timer is active
_range_sync_guard = [False]

# ──────────────────────────────────────────────────────────────
#  Parallel render — subprocess script + state
# ──────────────────────────────────────────────────────────────

# Script written to disk and executed by each background Blender process.
# Completely self-contained — must not import multicam_render.
_PARALLEL_RENDER_SCRIPT = r'''
import bpy, json, sys, os

cfg_path = sys.argv[sys.argv.index("--") + 1]
with open(cfg_path, "r") as _f:
    _cfg = json.load(_f)

_status_file = _cfg["status_file"]

def _write(cam, frame, done):
    try:
        with open(_status_file, "w") as f:
            json.dump({"cam": cam, "frame": frame, "done": done}, f)
    except Exception:
        pass

scene = bpy.context.scene

for cam_info in _cfg["cameras"]:
    cam_obj = bpy.data.objects.get(cam_info["name"])
    if not cam_obj:
        continue
    scene.camera      = cam_obj
    scene.frame_start = cam_info["fmin"]
    scene.frame_end   = cam_info["fmax"]
    out_dir = os.path.join(_cfg["output_dir"], cam_info["name"])
    os.makedirs(out_dir, exist_ok=True)
    scene.render.filepath = os.path.join(out_dir, "")

    _name = cam_info["name"]

    def _on_post(sc, dg=None, _n=_name):
        _write(_n, sc.frame_current, False)

    bpy.app.handlers.render_post.append(_on_post)
    bpy.ops.render.render(animation=True)
    bpy.app.handlers.render_post.remove(_on_post)

_write("", 0, True)
'''

_parallel_state: dict = {
    "is_running": False,
    "processes":  [],   # list of {proc, status_path, cameras, index}
    "status":     [],   # list of {cam, frame, done}
}

# ── Thumbnail preview collection (lazy-initialised in register()) ──
_preview_coll = None


def _thumb_key(cam_name):
    return f"thumb_{cam_name}"


def _thumb_path(cam_name):
    import tempfile, re
    safe = re.sub(r'[^\w\-]', '_', cam_name)
    return os.path.join(tempfile.gettempdir(), f"nzo_thumb_{safe}.png")


def _load_thumb(cam_name):
    """Load (or force-reload) one thumbnail into the preview collection.
    Returns True if the file exists and was loaded."""
    import os as _os
    pcoll = _preview_coll
    if pcoll is None:
        return False
    path = _thumb_path(cam_name)
    if not _os.path.isfile(path):
        return False
    key = _thumb_key(cam_name)
    try:
        pcoll.load(key, path, 'IMAGE', force_reload=True)
        return True
    except Exception:
        return False


def _thumb_icon_id(cam_name):
    """Return the icon_id for this camera's thumbnail, or 0 if not available."""
    if _preview_coll is None:
        return 0
    key = _thumb_key(cam_name)
    if key not in _preview_coll:
        _load_thumb(cam_name)
    if key in _preview_coll:
        return _preview_coll[key].icon_id
    return 0


# Extra live stats updated by the redraw timer
_render_stats = {
    "elapsed_s": 0.0,
    "eta_s":     None,   # None = not enough data yet
    "fps":       0.0,    # frames per second
}


# ──────────────────────────────────────────────────────────────
#  Render history helpers
# ──────────────────────────────────────────────────────────────

def _history_path():
    """JSON file sitting next to the .blend, or in the system temp dir."""
    import os
    blend = bpy.data.filepath
    if blend:
        return os.path.splitext(blend)[0] + "_render_history.json"
    import tempfile
    return os.path.join(tempfile.gettempdir(), "nzo_render_history.json")


def _load_history():
    import json, os
    path = _history_path()
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _append_history(entry):
    import json
    entries = _load_history()
    entries.append(entry)
    try:
        with open(_history_path(), "w", encoding="utf-8") as f:
            json.dump(entries[-20:], f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[MultiCam] Could not save history: {e}")


def _format_duration(seconds):
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


# ──────────────────────────────────────────────────────────────
#  Keyframe range extraction  (Blender 4.4+ layered + legacy)
# ──────────────────────────────────────────────────────────────

def get_keyframe_range(cam_obj):
    """(fmin, fmax) from the camera's action, or (None, None)."""
    anim = cam_obj.animation_data
    if not anim or not anim.action:
        return None, None

    action = anim.action
    frames = []

    # Blender 4.4+ : layers → strips → channelbags → fcurves
    if hasattr(action, 'layers') and len(action.layers) > 0:
        for layer in action.layers:
            for strip in layer.strips:
                if hasattr(strip, 'channelbags'):
                    for cb in strip.channelbags:
                        for fc in cb.fcurves:
                            for kp in fc.keyframe_points:
                                frames.append(kp.co[0])
    # Legacy (< 4.4)
    elif hasattr(action, 'fcurves'):
        for fc in action.fcurves:
            for kp in fc.keyframe_points:
                frames.append(kp.co[0])

    return (int(min(frames)), int(max(frames))) if frames else (None, None)


# ──────────────────────────────────────────────────────────────
#  State save / restore  (camera, frames, markers)
# ──────────────────────────────────────────────────────────────

def _save_state(scene):
    _render_state.clear()
    _render_state.update({
        "camera":      scene.camera,
        "frame_start": scene.frame_start,
        "frame_end":   scene.frame_end,
        "window":      bpy.context.window,
        # Save all existing timeline markers
        "markers": [
            (m.name, m.frame, m.camera)
            for m in scene.timeline_markers
        ],
    })


def _update_cam_range_from_selection(self, context):
    """Called when the active index in the UIList changes.
    Selects the camera in the viewport (so its keyframes show in
    the timeline) and updates the Create Camera Start/End fields."""
    col = self.multicam_cameras       # self = Scene
    idx = self.multicam_active_index
    if not (0 <= idx < len(col)):
        return
    cam_obj = bpy.data.objects.get(col[idx].cam_name)
    if cam_obj is None:
        return

    # Select the camera object in the viewport
    try:
        for obj in context.selected_objects:
            obj.select_set(False)
        cam_obj.select_set(True)
        context.view_layer.objects.active = cam_obj
    except Exception:
        pass

    _range_sync_guard[0] = True
    try:
        fmin, fmax = get_keyframe_range(cam_obj)
        if fmin is not None:
            duration = fmax - fmin
            self.multicam_new_cam_start = fmax + 1
            self.multicam_new_cam_end   = fmax + 1 + duration
        elif self.multicam_new_cam_start <= 0:
            self.multicam_new_cam_start = context.scene.frame_start
    finally:
        _range_sync_guard[0] = False


def _apply_range_to_selected_camera(scene, context):
    if _range_sync_guard[0]:
        return

    col = scene.multicam_cameras
    idx = scene.multicam_active_index
    if not (0 <= idx < len(col)):
        return

    cam_obj = bpy.data.objects.get(col[idx].cam_name)
    if cam_obj is None:
        return

    fstart = scene.multicam_new_cam_start
    fend   = scene.multicam_new_cam_end

    if fstart <= 0:
        fstart = scene.frame_start
        _range_sync_guard[0] = True
        try:
            scene.multicam_new_cam_start = fstart
        finally:
            _range_sync_guard[0] = False
    if fend <= fstart:
        return

    original_frame = scene.frame_current
    try:
        scene.frame_set(fstart)
        cam_obj.keyframe_insert(data_path="location", frame=fstart)
        cam_obj.keyframe_insert(data_path="rotation_euler", frame=fstart)

        scene.frame_set(fend)
        cam_obj.keyframe_insert(data_path="location", frame=fend)
        cam_obj.keyframe_insert(data_path="rotation_euler", frame=fend)
    finally:
        scene.frame_set(original_frame)


def _update_selected_camera_range(self, context):
    _apply_range_to_selected_camera(self, context)


def _restore_state(scene):
    if not _render_state:
        return
    scene.camera      = _render_state["camera"]
    scene.frame_start = _render_state["frame_start"]
    scene.frame_end   = _render_state["frame_end"]
    # Restore markers
    scene.timeline_markers.clear()
    for name, frame, cam in _render_state.get("markers", []):
        m        = scene.timeline_markers.new(name, frame=frame)
        m.camera = cam
    print("[MultiCam] Scene state restored.")


# ──────────────────────────────────────────────────────────────
#  Render event handlers
# ──────────────────────────────────────────────────────────────

@bpy.app.handlers.persistent
def _on_render_post(scene, depsgraph=None):
    """Fires after each frame — update progress counter."""
    _progress["rendered_frames"] += 1
    # Determine which camera is active for this frame
    frame = scene.frame_current
    current = ""
    for name, fmin, fmax in _progress["cameras"]:
        if fmin <= frame <= fmax:
            current = name
            break
    _progress["current_cam"] = current


@bpy.app.handlers.persistent
def _on_render_complete(scene, depsgraph=None):
    """Fires when the full animation render job completes."""
    import datetime
    elapsed      = _time.time() - _render_state.get("start_time", _time.time())
    cameras_snap = list(_progress["cameras"])
    total_frames = _progress["total_frames"]
    resolution   = f"{scene.render.resolution_x}×{scene.render.resolution_y}"
    output_path  = bpy.path.abspath(scene.render.filepath)

    _progress["is_running"] = False
    _redraw_running[0]      = False
    _restore_state(scene)
    should_assemble = scene.multicam_auto_assemble_video
    window = _render_state.get("window")
    _unregister_handlers()
    print("[MultiCam] All cameras rendered. Done.")

    _append_history({
        "timestamp":       datetime.datetime.now().isoformat(timespec="seconds"),
        "status":          "completed",
        "duration_s":      round(elapsed, 1),
        "total_frames":    total_frames,
        "rendered_frames": total_frames,
        "resolution":      resolution,
        "output_path":     output_path,
        "cameras":         [{"name": n, "frames": f"{a}–{b}"} for n, a, b in cameras_snap],
    })

    if should_assemble:
        result = _start_assemble_video(scene, window=window)
        if result != {"FINISHED"}:
            print("[MultiCam] Auto-assemble could not start.")


@bpy.app.handlers.persistent
def _on_render_cancel(scene, depsgraph=None):
    """Fires if the user cancels the render."""
    import datetime
    elapsed       = _time.time() - _render_state.get("start_time", _time.time())
    cameras_snap  = list(_progress["cameras"])
    frames_done   = _progress["rendered_frames"]
    total_frames  = _progress["total_frames"]
    resolution    = f"{scene.render.resolution_x}×{scene.render.resolution_y}"
    output_path   = bpy.path.abspath(scene.render.filepath)

    _progress["is_running"] = False
    _redraw_running[0]      = False
    _restore_state(scene)
    _unregister_handlers()
    print("[MultiCam] Render cancelled.")

    _append_history({
        "timestamp":       datetime.datetime.now().isoformat(timespec="seconds"),
        "status":          "cancelled",
        "duration_s":      round(elapsed, 1),
        "total_frames":    total_frames,
        "rendered_frames": frames_done,
        "resolution":      resolution,
        "output_path":     output_path,
        "cameras":         [{"name": n, "frames": f"{a}–{b}"} for n, a, b in cameras_snap],
    })


def _register_handlers():
    pairs = [
        (bpy.app.handlers.render_post,     _on_render_post),
        (bpy.app.handlers.render_complete, _on_render_complete),
        (bpy.app.handlers.render_cancel,   _on_render_cancel),
    ]
    for lst, fn in pairs:
        if fn not in lst:
            lst.append(fn)


def _unregister_handlers():
    pairs = [
        (bpy.app.handlers.render_post,     _on_render_post),
        (bpy.app.handlers.render_complete, _on_render_complete),
        (bpy.app.handlers.render_cancel,   _on_render_cancel),
    ]
    for lst, fn in pairs:
        if fn in lst:
            lst.remove(fn)


# ──────────────────────────────────────────────────────────────
#  Persistent redraw timer — keeps N-Panel live during render
# ──────────────────────────────────────────────────────────────

def _redraw_tick():
    """Called every 0.4 s while rendering to refresh the panel and update ETA."""
    if not _redraw_running[0]:
        return None  # return None → timer stops

    # ── Update live stats ───────────────────────────────────────
    start = _render_state.get("start_time")
    if start:
        elapsed = _time.time() - start
        done    = _progress["rendered_frames"]
        total   = _progress["total_frames"]
        _render_stats["elapsed_s"] = elapsed
        if done > 0 and elapsed > 0:
            fps = done / elapsed
            _render_stats["fps"] = fps
            remaining = total - done
            _render_stats["eta_s"] = remaining / fps if fps > 0 else None
        else:
            _render_stats["fps"]   = 0.0
            _render_stats["eta_s"] = None

    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()
    except Exception:
        pass
    return 0.4  # reschedule


def _start_redraw_timer():
    _redraw_running[0] = True
    bpy.app.timers.register(_redraw_tick, first_interval=0.4)


def _parallel_poll_tick():
    """Polls subprocess status files every second; stops when all done."""
    if not _parallel_state["is_running"]:
        return None

    all_done = True
    for i, pinfo in enumerate(_parallel_state["processes"]):
        # Read status file written by the subprocess
        try:
            import json as _json
            with open(pinfo["status_path"], "r") as f:
                _parallel_state["status"][i] = _json.load(f)
        except Exception:
            pass

        if pinfo["proc"].poll() is None:
            all_done = False

    # Redraw VIEW_3D panels
    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == "VIEW_3D":
                    area.tag_redraw()
    except Exception:
        pass

    if all_done:
        _parallel_state["is_running"] = False
        print("[MultiCam] All parallel renders complete.")
        return None

    return 1.0  # reschedule every second


# ──────────────────────────────────────────────────────────────
#  PropertyGroup
# ──────────────────────────────────────────────────────────────

class CameraRenderItem(PropertyGroup):
    cam_name: StringProperty(name="Camera Name", default="")
    enabled:  BoolProperty(
        name="Include",
        description="Include this camera in the batch render",
        default=True,
    )


# ──────────────────────────────────────────────────────────────
#  UIList
# ──────────────────────────────────────────────────────────────

class MULTICAM_UL_CameraList(UIList):
    bl_idname = "MULTICAM_UL_camera_list"

    def draw_item(self, context, layout, data, item, icon,
                  active_data, active_propname, index):
        if self.layout_type in {"DEFAULT", "COMPACT"}:
            row = layout.row(align=True)
            row.prop(item, "enabled", text="")
            sub = row.row()
            sub.enabled = item.enabled
            icon_id = _thumb_icon_id(item.cam_name)
            if icon_id:
                sub.label(text=item.cam_name, icon_value=icon_id)
            else:
                sub.label(text=item.cam_name, icon="CAMERA_DATA")
        elif self.layout_type == "GRID":
            layout.alignment = "CENTER"
            layout.prop(item, "enabled", text="")


# ──────────────────────────────────────────────────────────────
#  Operator: Refresh camera list
# ──────────────────────────────────────────────────────────────

class MULTICAM_OT_RefreshCameras(Operator):
    bl_idname      = "multicam.refresh_cameras"
    bl_label       = "Refresh Camera List"
    bl_description = "Scan scene cameras and rebuild the list"

    def execute(self, context):
        scene = context.scene
        col   = scene.multicam_cameras
        prev  = {it.cam_name: it.enabled for it in col}
        col.clear()
        cams  = sorted(
            (o for o in bpy.data.objects if o.type == "CAMERA"),
            key=lambda o: o.name,
        )
        for cam in cams:
            item          = col.add()
            item.cam_name = cam.name
            item.enabled  = prev.get(cam.name, True)
        self.report({"INFO"}, f"{len(cams)} camera(s) found.")
        return {"FINISHED"}


# ──────────────────────────────────────────────────────────────
#  Operator: Start batch render
# ──────────────────────────────────────────────────────────────

class MULTICAM_OT_RenderSequence(Operator):
    """Place markers → single render.render() call → Blender switches
    cameras automatically at each marker frame."""

    bl_idname      = "multicam.render_sequence"
    bl_label       = "Render Selected Cameras Sequence"
    bl_description = (
        "Render each checked camera over its own keyframe range using "
        "timeline markers (single render call — reliable in Blender 5.1)."
    )

    def execute(self, context):
        scene = context.scene
        col   = scene.multicam_cameras

        if not col:
            self.report({"WARNING"},
                        "Camera list is empty — click Refresh first.")
            return {"CANCELLED"}

        # ── Collect valid cameras ───────────────────────────────
        entries  = []   # (cam_obj, fmin, fmax)
        skipped  = []

        for item in col:
            if not item.enabled:
                continue
            cam_obj = bpy.data.objects.get(item.cam_name)
            if cam_obj is None:
                skipped.append(item.cam_name)
                continue
            fmin, fmax = get_keyframe_range(cam_obj)
            if fmin is None:
                print(f"[MultiCam] '{item.cam_name}' has no keyframes — skipped.")
                skipped.append(item.cam_name)
                continue
            entries.append((cam_obj, fmin, fmax))

        if skipped:
            self.report({"WARNING"},
                        f"Skipped (no keyframes): {', '.join(skipped)}")
        if not entries:
            self.report({"ERROR"}, "No valid cameras to render.")
            return {"CANCELLED"}

        # Sort by start frame so markers are placed in order
        entries.sort(key=lambda e: e[1])

        # ── Save current scene state ────────────────────────────
        _save_state(scene)

        # ── Place timeline markers with camera bindings ─────────
        # Blender switches the active render camera when it reaches
        # a marker that has a camera assigned (built-in behavior).
        scene.timeline_markers.clear()
        for cam_obj, fmin, _ in entries:
            marker        = scene.timeline_markers.new(cam_obj.name, frame=fmin)
            marker.camera = cam_obj

        # ── Set combined frame range ────────────────────────────
        global_fmin = entries[0][1]
        global_fmax = entries[-1][2]
        scene.frame_start = global_fmin
        scene.frame_end   = global_fmax

        # ── Set up progress tracking ────────────────────────────
        total = global_fmax - global_fmin + 1
        _progress.update({
            "is_running":      True,
            "total_frames":    total,
            "rendered_frames": 0,
            "cameras":         [(c.name, a, b) for c, a, b in entries],
            "current_cam":     entries[0][0].name,
        })

        _register_handlers()
        _start_redraw_timer()

        # ── Single render call ──────────────────────────────────
        # Context override ensures the operator has a valid window
        # even if called from a script context.
        windows = context.window_manager.windows
        window  = context.window if context.window else (windows[0] if windows else None)

        print(
            f"[MultiCam] Starting batch render — "
            f"{len(entries)} camera(s), frames {global_fmin}–{global_fmax}."
        )

        _render_state["start_time"] = _time.time()
        _render_stats.update({"elapsed_s": 0.0, "eta_s": None, "fps": 0.0})

        if window:
            with context.temp_override(window=window):
                bpy.ops.render.render("INVOKE_DEFAULT", animation=True)
        else:
            bpy.ops.render.render("INVOKE_DEFAULT", animation=True)

        return {"FINISHED"}


# ──────────────────────────────────────────────────────────────
#  Video assembly — state + handlers
# ──────────────────────────────────────────────────────────────

_assemble_state: dict = {}


def _start_assemble_video(scene, window=None, reporter=None):
    import os, glob

    abs_path   = bpy.path.abspath(scene.render.filepath)
    render_dir = abs_path if os.path.isdir(abs_path) else os.path.dirname(abs_path)

    def report(level, message):
        if reporter:
            reporter(level, message)
        print(f"[MultiCam] {message}")

    if not os.path.isdir(render_dir):
        report({"ERROR"}, f"Render directory not found: {render_dir}")
        return {"CANCELLED"}

    frame_files = []
    for ext in ("png", "jpg", "jpeg", "exr", "tif", "tiff", "bmp"):
        found = sorted(glob.glob(os.path.join(render_dir, f"*.{ext}")))
        if found:
            frame_files = found
            break

    if not frame_files:
        report({"ERROR"}, f"No image frames found in: {render_dir}")
        return {"CANCELLED"}

    n = len(frame_files)
    video_dir  = os.path.join(render_dir, "VIDEO")
    os.makedirs(video_dir, exist_ok=True)
    blend_stem = os.path.splitext(bpy.path.basename(bpy.data.filepath))[0] or "assembled"

    tmp = bpy.data.scenes.new("_NZO_assemble_")
    tmp.render.resolution_x = scene.render.resolution_x
    tmp.render.resolution_y = scene.render.resolution_y
    tmp.render.fps          = scene.render.fps
    tmp.render.fps_base     = scene.render.fps_base
    tmp.frame_start         = 1
    tmp.frame_end           = n

    r = tmp.render
    r.image_settings.media_type          = "VIDEO"
    r.image_settings.file_format         = "FFMPEG"
    r.ffmpeg.format                      = scene.multicam_video_container
    r.ffmpeg.codec                       = scene.multicam_video_codec
    r.ffmpeg.constant_rate_factor        = scene.multicam_video_quality
    r.ffmpeg.ffmpeg_preset               = "GOOD"
    r.ffmpeg.gopsize                     = 18
    r.ffmpeg.use_max_b_frames            = False
    r.ffmpeg.audio_codec                 = "NONE"
    r.use_sequencer                      = True
    r.filepath                           = os.path.join(video_dir, blend_stem)

    seq   = tmp.sequence_editor_create()
    strip = seq.strips.new_image(
        name        = "frames",
        filepath    = frame_files[0],
        channel     = 1,
        frame_start = 1,
    )
    for f in frame_files[1:]:
        strip.elements.append(os.path.basename(f))

    _assemble_state.clear()
    _assemble_state.update({
        "window":         window,
        "original_scene": scene,
        "tmp_scene":      tmp,
        "video_dir":      video_dir,
    })

    if _on_assemble_complete not in bpy.app.handlers.render_complete:
        bpy.app.handlers.render_complete.append(_on_assemble_complete)
    if _on_assemble_cancel not in bpy.app.handlers.render_cancel:
        bpy.app.handlers.render_cancel.append(_on_assemble_cancel)

    if window:
        window.scene = tmp
    bpy.app.timers.register(_launch_assemble_render, first_interval=0.15)

    report({"INFO"}, f"Assembling {n} frames -> VIDEO/{blend_stem}")
    return {"FINISHED"}


def _cleanup_assembly():
    window = _assemble_state.get("window")
    orig   = _assemble_state.get("original_scene")
    tmp    = _assemble_state.get("tmp_scene")
    if window and orig and orig.name in bpy.data.scenes:
        window.scene = orig
    if tmp and tmp.name in bpy.data.scenes:
        bpy.data.scenes.remove(tmp)
    for lst, fn in (
        (bpy.app.handlers.render_complete, _on_assemble_complete),
        (bpy.app.handlers.render_cancel,   _on_assemble_cancel),
    ):
        if fn in lst:
            lst.remove(fn)
    vdir = _assemble_state.get("video_dir", "")
    _assemble_state.clear()
    return vdir


@bpy.app.handlers.persistent
def _on_assemble_complete(scene, depsgraph=None):
    if scene.name != "_NZO_assemble_":
        return
    vdir = _cleanup_assembly()
    print(f"[MultiCam] Video assembled → {vdir}")


@bpy.app.handlers.persistent
def _on_assemble_cancel(scene, depsgraph=None):
    if scene.name != "_NZO_assemble_":
        return
    _cleanup_assembly()
    print("[MultiCam] Video assembly cancelled.")


def _launch_assemble_render():
    window = _assemble_state.get("window")
    if window:
        with bpy.context.temp_override(window=window):
            bpy.ops.render.render("INVOKE_DEFAULT", animation=True)
    else:
        bpy.ops.render.render("INVOKE_DEFAULT", animation=True)
    return None


# ──────────────────────────────────────────────────────────────
#  Operator: Assemble rendered frames → Matroska H.264 video
# ──────────────────────────────────────────────────────────────

class MULTICAM_OT_AssembleVideo(Operator):
    """Read all rendered image frames and encode them into a single
    Matroska H.264 video using Blender's built-in FFmpeg support."""

    bl_idname      = "multicam.assemble_video"
    bl_label       = "Assemble to Video"
    bl_description = (
        "Encode rendered frames to Matroska H.264 (perceptually lossless) "
        "in a VIDEO/ subfolder using Blender's built-in FFmpeg"
    )

    def execute(self, context):
        return _start_assemble_video(
            context.scene,
            window=context.window,
            reporter=self.report,
        )
        import os, glob

        scene      = context.scene
        abs_path   = bpy.path.abspath(scene.render.filepath)
        render_dir = abs_path if os.path.isdir(abs_path) else os.path.dirname(abs_path)

        if not os.path.isdir(render_dir):
            self.report({"ERROR"}, f"Render directory not found: {render_dir}")
            return {"CANCELLED"}

        # ── Find rendered frames ──────────────────────────────────
        frame_files = []
        for ext in ("png", "jpg", "jpeg", "exr", "tif", "tiff", "bmp"):
            found = sorted(glob.glob(os.path.join(render_dir, f"*.{ext}")))
            if found:
                frame_files = found
                break

        if not frame_files:
            self.report({"ERROR"}, f"No image frames found in: {render_dir}")
            return {"CANCELLED"}

        n = len(frame_files)

        # ── Output path ───────────────────────────────────────────
        video_dir  = os.path.join(render_dir, "VIDEO")
        os.makedirs(video_dir, exist_ok=True)
        blend_stem = os.path.splitext(bpy.path.basename(bpy.data.filepath))[0] or "assembled"

        # ── Build temporary scene for encoding ────────────────────
        tmp = bpy.data.scenes.new("_NZO_assemble_")
        tmp.render.resolution_x = scene.render.resolution_x
        tmp.render.resolution_y = scene.render.resolution_y
        tmp.render.fps          = scene.render.fps
        tmp.render.fps_base     = scene.render.fps_base
        tmp.frame_start         = 1
        tmp.frame_end           = n

        # ── FFmpeg output settings ────────────────────────────────
        # Blender 5.0+ requires media_type = 'VIDEO' before file_format = 'FFMPEG'
        r = tmp.render
        r.image_settings.media_type          = "VIDEO"
        r.image_settings.file_format         = "FFMPEG"
        r.ffmpeg.format                      = scene.multicam_video_container
        r.ffmpeg.codec                       = scene.multicam_video_codec
        r.ffmpeg.constant_rate_factor        = scene.multicam_video_quality
        r.ffmpeg.ffmpeg_preset               = "GOOD"
        r.ffmpeg.gopsize                     = 18
        r.ffmpeg.use_max_b_frames            = False
        r.ffmpeg.audio_codec                 = "NONE"
        r.use_sequencer                      = True
        r.filepath                           = os.path.join(video_dir, blend_stem)

        # ── Load frames into the temp scene's VSE ────────────────
        seq   = tmp.sequence_editor_create()
        strip = seq.strips.new_image(
            name        = "frames",
            filepath    = frame_files[0],
            channel     = 1,
            frame_start = 1,
        )
        for f in frame_files[1:]:
            strip.elements.append(os.path.basename(f))

        # ── Store refs for the completion handler ─────────────────
        _assemble_state.clear()
        _assemble_state.update({
            "window":         context.window,
            "original_scene": scene,
            "tmp_scene":      tmp,
            "video_dir":      video_dir,
        })

        # ── Register handlers ────────────────────────────────────
        if _on_assemble_complete not in bpy.app.handlers.render_complete:
            bpy.app.handlers.render_complete.append(_on_assemble_complete)
        if _on_assemble_cancel not in bpy.app.handlers.render_cancel:
            bpy.app.handlers.render_cancel.append(_on_assemble_cancel)

        # ── Switch window to temp scene, then launch render ──────
        context.window.scene = tmp
        bpy.app.timers.register(_launch_assemble_render, first_interval=0.15)

        self.report({"INFO"}, f"Assembling {n} frames → VIDEO/{blend_stem}.mkv")
        return {"FINISHED"}


# ──────────────────────────────────────────────────────────────
#  Operator: Parallel render (multi-process / multi-GPU)
# ──────────────────────────────────────────────────────────────

class MULTICAM_OT_RenderParallel(Operator):
    bl_idname      = "multicam.render_parallel"
    bl_label       = "Render Parallel"
    bl_description = (
        "Launch one Blender background process per job slot, each rendering "
        "a subset of cameras simultaneously. Save the .blend first."
    )

    def execute(self, context):
        import subprocess, tempfile, json, math

        scene = context.scene

        if not bpy.data.filepath:
            self.report({"ERROR"},
                        "Save the .blend file before using parallel render.")
            return {"CANCELLED"}

        if _parallel_state["is_running"]:
            self.report({"WARNING"}, "A parallel render is already running.")
            return {"CANCELLED"}

        # ── Collect enabled cameras with valid keyframes ────────
        entries = []
        for item in scene.multicam_cameras:
            if not item.enabled:
                continue
            cam_obj = bpy.data.objects.get(item.cam_name)
            if not cam_obj:
                continue
            fmin, fmax = get_keyframe_range(cam_obj)
            if fmin is None:
                continue
            entries.append({"name": item.cam_name, "fmin": fmin, "fmax": fmax})

        if not entries:
            self.report({"ERROR"}, "No valid cameras to render.")
            return {"CANCELLED"}

        n_jobs    = min(scene.multicam_parallel_jobs, len(entries))
        abs_path  = bpy.path.abspath(scene.render.filepath)
        render_dir = abs_path if os.path.isdir(abs_path) else os.path.dirname(abs_path)
        tmp_dir    = tempfile.gettempdir()
        blend_file = bpy.data.filepath
        blender    = bpy.app.binary_path

        # ── Write the subprocess render script once ─────────────
        script_path = os.path.join(tmp_dir, "nzo_parallel_render.py")
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(_PARALLEL_RENDER_SCRIPT)

        # ── Distribute cameras round-robin across jobs ──────────
        groups: list = [[] for _ in range(n_jobs)]
        for i, entry in enumerate(entries):
            groups[i % n_jobs].append(entry)

        # ── Reset parallel state ────────────────────────────────
        _parallel_state["is_running"] = True
        _parallel_state["processes"]  = []
        _parallel_state["status"]     = []

        # ── Spawn one process per group ─────────────────────────
        for i, group in enumerate(groups):
            if not group:
                continue
            cfg_path    = os.path.join(tmp_dir, f"nzo_par_cfg_{i}.json")
            status_path = os.path.join(tmp_dir, f"nzo_par_status_{i}.json")

            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump({
                    "cameras":    group,
                    "output_dir": render_dir,
                    "status_file": status_path,
                }, f)

            proc = subprocess.Popen(
                [blender, "--background", blend_file,
                 "--python", script_path, "--", cfg_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            _parallel_state["processes"].append({
                "proc":        proc,
                "status_path": status_path,
                "cameras":     [c["name"] for c in group],
                "index":       i,
            })
            _parallel_state["status"].append({
                "cam":   group[0]["name"],
                "frame": 0,
                "done":  False,
            })

        bpy.app.timers.register(_parallel_poll_tick, first_interval=1.0)

        self.report(
            {"INFO"},
            f"Launched {len(_parallel_state['processes'])} parallel process(es) "
            f"for {len(entries)} camera(s).",
        )
        return {"FINISHED"}


# ──────────────────────────────────────────────────────────────
#  Operator: Generate camera thumbnails
# ──────────────────────────────────────────────────────────────

class MULTICAM_OT_GenerateThumbnails(Operator):
    bl_idname      = "multicam.generate_thumbnails"
    bl_label       = "Generate Camera Thumbnails"
    bl_description = (
        "OpenGL-render a small preview for each camera in the list "
        "and display it in the panel"
    )

    def execute(self, context):
        scene = context.scene
        col   = scene.multicam_cameras

        if not col:
            self.report({"WARNING"}, "Camera list is empty — click Refresh first.")
            return {"CANCELLED"}

        # ── Save render state we'll temporarily change ──────────
        orig_cam  = scene.camera
        orig_rx   = scene.render.resolution_x
        orig_ry   = scene.render.resolution_y
        orig_pct  = scene.render.resolution_percentage
        orig_path = scene.render.filepath

        # Tiny resolution for fast OpenGL captures
        scene.render.resolution_x          = 320
        scene.render.resolution_y          = 180
        scene.render.resolution_percentage = 100

        count = 0
        for item in col:
            cam_obj = bpy.data.objects.get(item.cam_name)
            if cam_obj is None:
                continue
            scene.camera        = cam_obj
            scene.render.filepath = _thumb_path(item.cam_name)
            try:
                bpy.ops.render.opengl(write_still=True, view_context=False)
                if _load_thumb(item.cam_name):
                    count += 1
            except Exception as e:
                print(f"[MultiCam] Thumbnail failed for {item.cam_name}: {e}")

        # ── Restore render state ────────────────────────────────
        scene.camera                       = orig_cam
        scene.render.resolution_x          = orig_rx
        scene.render.resolution_y          = orig_ry
        scene.render.resolution_percentage = orig_pct
        scene.render.filepath              = orig_path

        self.report({"INFO"}, f"{count} thumbnail(s) generated.")
        return {"FINISHED"}


# ──────────────────────────────────────────────────────────────
#  Operator: Clear render history
# ──────────────────────────────────────────────────────────────

class MULTICAM_OT_ClearHistory(Operator):
    bl_idname      = "multicam.clear_history"
    bl_label       = "Clear Render History"
    bl_description = "Delete the render history file for this project"

    def execute(self, context):
        import os
        path = _history_path()
        if os.path.isfile(path):
            os.remove(path)
            self.report({"INFO"}, "Render history cleared.")
        else:
            self.report({"INFO"}, "No history file found.")
        return {"FINISHED"}


# ──────────────────────────────────────────────────────────────
#  Operator: Create camera at current viewport view
# ──────────────────────────────────────────────────────────────

class MULTICAM_OT_AddCamera(Operator):
    """Create a new camera aligned to the current viewport view,
    with a fixed-position keyframe at frame_start AND frame_end.
    The camera immediately appears in the list ready to render."""

    bl_idname      = "multicam.add_camera"
    bl_label       = "Add Camera at Current View"
    bl_description = (
        "Create a camera from the current viewport angle and keyframe it "
        "at the defined start/end frames so it appears in the render list"
    )

    def execute(self, context):
        import mathutils

        scene  = context.scene
        fstart = scene.multicam_new_cam_start
        fend   = scene.multicam_new_cam_end

        # ── Validate frame range ────────────────────────────────
        if fend <= fstart:
            self.report({"ERROR"},
                        f"End frame ({fend}) must be greater than start frame ({fstart}).")
            return {"CANCELLED"}

        # ── Remember current state ──────────────────────────────
        original_frame = scene.frame_current

        # ── Find currently selected camera in the list ──────────
        col          = scene.multicam_cameras
        idx          = scene.multicam_active_index
        ref_cam      = None
        if 0 <= idx < len(col):
            ref_cam = bpy.data.objects.get(col[idx].cam_name)

        # ── Create camera: behind selected cam or from viewport ─
        bpy.ops.object.select_all(action='DESELECT')

        if ref_cam is not None:
            # Place the new camera just behind the selected one.
            # Cameras look in their local -Z direction; "behind" them
            # is +Z in local space (away from what they're filming).
            mat      = ref_cam.matrix_world
            # 0.5 units back along the camera's optical axis
            backward = mat.to_3x3() @ mathutils.Vector((0.0, 0.0, 0.5))
            new_loc  = mat.translation + backward
            new_rot  = ref_cam.rotation_euler.copy()
            bpy.ops.object.camera_add(
                align='WORLD',
                location=(new_loc.x, new_loc.y, new_loc.z),
                rotation=(new_rot.x, new_rot.y, new_rot.z),
            )
        else:
            # No camera selected — fall back to current viewport angle
            bpy.ops.object.camera_add(align='VIEW')

        cam_obj  = context.active_object
        cam_name = cam_obj.name

        # ── Insert keyframe at start frame (location + rotation) ─
        scene.frame_set(fstart)
        cam_obj.keyframe_insert(data_path="location",       frame=fstart)
        cam_obj.keyframe_insert(data_path="rotation_euler", frame=fstart)

        # ── Insert keyframe at end frame (same position = static) ─
        scene.frame_set(fend)
        cam_obj.keyframe_insert(data_path="location",       frame=fend)
        cam_obj.keyframe_insert(data_path="rotation_euler", frame=fend)

        # ── Restore timeline position ───────────────────────────
        scene.frame_set(original_frame)

        # ── Update next-camera defaults (next slot right after) ─
        scene.multicam_new_cam_start = fend + 1
        scene.multicam_new_cam_end   = fend + (fend - fstart)

        # ── Refresh the camera list ─────────────────────────────
        bpy.ops.multicam.refresh_cameras()

        self.report({"INFO"},
                    f"Camera '{cam_name}' created — frames {fstart}–{fend}.")
        return {"FINISHED"}


# ──────────────────────────────────────────────────────────────
#  Panel — N-Panel › MultiCam tab
# ──────────────────────────────────────────────────────────────

class MULTICAM_OT_KeySelectedCamera(Operator):
    """Align the selected camera to the current viewport and insert
    static keys at the chosen start/end frames."""

    bl_idname      = "multicam.key_selected_camera"
    bl_label       = "Key Selected Camera"
    bl_description = (
        "Use the current viewport view for the selected camera and "
        "insert location/rotation keys on the chosen frame range"
    )

    def execute(self, context):
        scene  = context.scene
        fstart = scene.multicam_new_cam_start
        fend   = scene.multicam_new_cam_end
        rf_min, rf_max = (None, None)

        col = scene.multicam_cameras
        idx = scene.multicam_active_index
        if not (0 <= idx < len(col)):
            self.report({"ERROR"}, "Select a camera in the list first.")
            return {"CANCELLED"}

        cam_obj = bpy.data.objects.get(col[idx].cam_name)
        if cam_obj is None:
            self.report({"ERROR"}, "Selected camera was not found.")
            return {"CANCELLED"}

        rf_min, rf_max = get_keyframe_range(cam_obj)
        if rf_min is None and fstart <= 0:
            fstart = scene.frame_start
        elif rf_min is not None and fstart < rf_min:
            fstart = rf_min

        if fend <= fstart:
            self.report({"ERROR"},
                        f"End frame ({fend}) must be greater than start frame ({fstart}).")
            return {"CANCELLED"}

        scene.multicam_new_cam_start = fstart

        area = next((a for a in context.screen.areas if a.type == "VIEW_3D"), None)
        if area is None:
            self.report({"ERROR"}, "No 3D View found to capture the current viewport.")
            return {"CANCELLED"}

        region = next((r for r in area.regions if r.type == "WINDOW"), None)
        space = next((s for s in area.spaces if s.type == "VIEW_3D"), None)
        if region is None or space is None:
            self.report({"ERROR"}, "Could not access the current 3D viewport.")
            return {"CANCELLED"}

        original_frame = scene.frame_current
        original_scene_camera = scene.camera
        original_active = context.view_layer.objects.active
        original_selection = list(context.selected_objects)

        try:
            for obj in original_selection:
                obj.select_set(False)
            cam_obj.select_set(True)
            context.view_layer.objects.active = cam_obj
            scene.camera = cam_obj

            with context.temp_override(area=area, region=region, space_data=space):
                bpy.ops.view3d.camera_to_view()

            scene.frame_set(fstart)
            cam_obj.keyframe_insert(data_path="location", frame=fstart)
            cam_obj.keyframe_insert(data_path="rotation_euler", frame=fstart)

            scene.frame_set(fend)
            cam_obj.keyframe_insert(data_path="location", frame=fend)
            cam_obj.keyframe_insert(data_path="rotation_euler", frame=fend)
        finally:
            scene.frame_set(original_frame)
            scene.camera = original_scene_camera
            for obj in context.selected_objects:
                obj.select_set(False)
            for obj in original_selection:
                if obj and obj.name in bpy.data.objects:
                    obj.select_set(True)
            if original_active and original_active.name in bpy.data.objects:
                context.view_layer.objects.active = original_active

        bpy.ops.multicam.refresh_cameras()
        self.report({"INFO"},
                    f"Camera '{cam_obj.name}' keyed â€” frames {fstart}â€“{fend}.")
        return {"FINISHED"}


class MULTICAM_PT_MainPanel(Panel):
    bl_label       = "NZO CamRender3000"
    bl_idname      = "MULTICAM_PT_main_panel"
    bl_space_type  = "VIEW_3D"
    bl_region_type = "UI"
    bl_category    = "NZO CamRender"

    def draw(self, context):
        try:
            self._draw_safe(context)
        except Exception as e:
            self.layout.label(text=f"Error: {e}", icon="ERROR")
            print(f"[MultiCam] Panel draw error: {e}")

    def _draw_safe(self, context):
        layout = self.layout
        scene  = context.scene
        prog   = _progress

        # ── PARALLEL PROGRESS VIEW ──────────────────────────────
        if _parallel_state["is_running"]:
            box = layout.box()
            box.label(text="Parallel Render Running…", icon="RENDER_ANIMATION")
            col = box.column(align=True)
            for i, pinfo in enumerate(_parallel_state["processes"]):
                st   = _parallel_state["status"][i] if i < len(_parallel_state["status"]) else {}
                done = st.get("done", False)
                cam  = st.get("cam", "")
                frm  = st.get("frame", 0)
                names = ", ".join(pinfo["cameras"])
                if done:
                    col.label(text=f"Job {i+1}: ✓ done  ({names})", icon="CHECKMARK")
                else:
                    col.label(
                        text=f"Job {i+1}: {cam}  fr {frm}  |  {names}",
                        icon="RENDER_STILL",
                    )
            col.separator()
            col.label(text="Renders run in background — Blender stays usable.",
                      icon="INFO")
            return

        # ── PROGRESS VIEW (shown while rendering) ───────────────
        if prog["is_running"]:
            box = layout.box()
            col = box.column(align=True)

            done  = prog["rendered_frames"]
            total = prog["total_frames"]
            frac  = min(done / total, 1.0) if total > 0 else 0.0

            # Main progress bar
            bar_text = f"{done} / {total} frames"
            col.progress(factor=frac, text=bar_text)

            # ── ETA + speed row ─────────────────────────────────
            stats = _render_stats
            eta   = stats.get("eta_s")
            fps   = stats.get("fps", 0.0)
            elapsed = stats.get("elapsed_s", 0.0)

            row_stats = col.row(align=True)
            row_stats.label(
                text=f"Elapsed: {_format_duration(elapsed)}",
                icon="TIME",
            )
            if eta is not None:
                row_stats.label(text=f"ETA: {_format_duration(eta)}")
            if fps > 0:
                col.label(text=f"Speed: {fps:.2f} frames/s", icon="SORTTIME")

            # Current camera label
            if prog["current_cam"]:
                col.separator(factor=0.5)
                col.label(
                    text=f"Camera : {prog['current_cam']}",
                    icon="CAMERA_DATA",
                )

            col.separator()

            # Per-camera status
            col.label(text="Cameras in sequence:")
            frames_offset = prog["cameras"][0][1] if prog["cameras"] else 0
            for name, fmin, fmax in prog["cameras"]:
                row  = col.row(align=True)
                cam_frames_done = fmax - frames_offset + 1
                if done >= cam_frames_done:
                    ico = "CHECKMARK"
                elif prog["current_cam"] == name:
                    ico = "RENDER_ANIMATION"
                else:
                    ico = "TIME"
                dur = fmax - fmin
                row.label(text=f"{name}  ({dur + 1} fr)", icon=ico)

            col.separator()
            col.label(text="Press Esc in render window to cancel.",
                      icon="INFO")
            return  # hide edit UI while rendering

        # ── NORMAL UI ───────────────────────────────────────────

        # ── CREATE CAMERA section ───────────────────────────────
        box = layout.box()
        box.label(text="Create Camera", icon="ADD")

        fstart = scene.multicam_new_cam_start
        fend   = scene.multicam_new_cam_end
        dur    = fend - fstart

        # ── Selected camera info (reference) ───────────────────
        col_info = box.column(align=True)
        cam_col  = scene.multicam_cameras
        idx      = scene.multicam_active_index
        ref_cam  = None
        if 0 <= idx < len(cam_col):
            ref_cam = bpy.data.objects.get(cam_col[idx].cam_name)

        if ref_cam is not None:
            rf_min, rf_max = get_keyframe_range(ref_cam)
            if rf_min is not None:
                # Current camera range — greyed out, read-only look
                row_ref = col_info.row(align=True)
                row_ref.enabled = False          # greyed = reference only
                row_ref.label(text=f"{ref_cam.name}", icon="CAMERA_DATA")
                row_ref.label(text=f"{rf_min}  →  {rf_max}")
            else:
                col_info.label(text=f"{ref_cam.name}  (no keyframes)", icon="CAMERA_DATA")
        else:
            col_info.label(text="No camera selected", icon="CAMERA_DATA")

        col_info.separator(factor=0.5)

        # ── New camera range (editable) ─────────────────────────
        row_new = col_info.row(align=True)
        row_new.label(text="Range  ", icon="KEY_HLT")
        sub = row_new.row(align=True)
        sub.prop(scene, "multicam_new_cam_start", text="")
        sub.label(text="→")
        sub.prop(scene, "multicam_new_cam_end",   text="")

        # Duration hint (valid / invalid feedback)
        if dur > 0:
            col_info.label(text=f"{dur} frames", icon="TIME")
        else:
            col_info.label(text="End must be greater than Start", icon="ERROR")

        box.separator(factor=0.3)

        # Create button — disabled when range is invalid
        row = box.row()
        row.scale_y = 1.4
        row.enabled = dur > 0
        row.operator("multicam.add_camera",
                     text="Add Camera at Current View",
                     icon="CAMERA_DATA")

        row = box.row()
        row.scale_y = 1.2
        row.enabled = dur > 0 and ref_cam is not None
        row.operator("multicam.key_selected_camera",
                     text="Key Selected Camera at Current View",
                     icon="KEY_HLT")

        layout.separator()

        # ── CAMERA LIST section ─────────────────────────────────
        layout.operator("multicam.refresh_cameras",
                        text="Refresh Camera List",
                        icon="FILE_REFRESH")
        layout.separator()

        col = layout.column()
        col.label(text="Cameras:")

        cam_col = scene.multicam_cameras
        if len(cam_col) > 0:
            col.template_list(
                "MULTICAM_UL_camera_list",
                "multicam_list",
                scene, "multicam_cameras",
                scene, "multicam_active_index",
                rows=3,
            )

            # ── Selected camera thumbnail preview ───────────────
            idx     = scene.multicam_active_index
            sel_cam = cam_col[idx].cam_name if 0 <= idx < len(cam_col) else None
            if sel_cam:
                icon_id = _thumb_icon_id(sel_cam)
                if icon_id:
                    col.separator(factor=0.5)
                    col.template_icon(icon_value=icon_id, scale=6.0)
        else:
            col.label(text="(click Refresh to scan cameras)")

        layout.operator("multicam.generate_thumbnails",
                        text="Generate Thumbnails",
                        icon="RESTRICT_RENDER_OFF")

        layout.separator()

        # ── RENDER section ──────────────────────────────────────
        box_r = layout.box()
        box_r.label(text="Render", icon="RENDER_ANIMATION")

        row = box_r.row()
        row.scale_y = 1.4
        row.operator("multicam.render_sequence",
                     text="Render Sequential",
                     icon="RENDER_ANIMATION")
        box_r.prop(scene, "multicam_auto_assemble_video",
                   text="Auto Assemble After Render")

        box_r.separator(factor=0.5)

        row_p = box_r.row(align=True)
        row_p.prop(scene, "multicam_parallel_jobs", text="Jobs")
        row_p.operator("multicam.render_parallel",
                       text="Render Parallel",
                       icon="WINDOW")
        box_r.label(
            text="Parallel: each job = 1 Blender process (GPU/CPU).",
            icon="INFO",
        )

        layout.separator()

        # ── ASSEMBLE VIDEO section ──────────────────────────────
        box = layout.box()
        box.label(text="Assemble to Video", icon="FILE_MOVIE")

        col = box.column(align=True)
        col.prop(scene, "multicam_video_container", text="Container")
        col.prop(scene, "multicam_video_codec",     text="Codec")
        col.prop(scene, "multicam_video_quality",   text="Quality")

        row = box.row()
        row.scale_y = 1.4
        row.operator("multicam.assemble_video",
                     text="Assemble Frames to Video",
                     icon="FILE_MOVIE")

        # Output path info
        layout.separator()
        box2 = layout.box()
        box2.label(text="Render output path:")
        try:
            box2.label(text=bpy.path.abspath(scene.render.filepath))
        except Exception:
            box2.label(text=scene.render.filepath)


# ──────────────────────────────────────────────────────────────
#  Sub-panel: Render History
# ──────────────────────────────────────────────────────────────

class MULTICAM_PT_HistoryPanel(Panel):
    bl_label       = "Render History"
    bl_idname      = "MULTICAM_PT_history_panel"
    bl_space_type  = "VIEW_3D"
    bl_region_type = "UI"
    bl_category    = "NZO CamRender"
    bl_parent_id   = "MULTICAM_PT_main_panel"
    bl_options     = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout  = self.layout
        entries = _load_history()

        if not entries:
            layout.label(text="No renders recorded yet.", icon="INFO")
            layout.separator()
            layout.operator("multicam.clear_history",
                            text="Clear History", icon="TRASH")
            return

        for entry in reversed(entries[-5:]):
            box = layout.box()

            # ── Row 1 : timestamp + status + duration ──────────
            row = box.row()
            icon = "CHECKMARK" if entry.get("status") == "completed" else "X"
            ts   = entry.get("timestamp", "")[:16].replace("T", "  ")
            dur  = _format_duration(entry.get("duration_s", 0))
            row.label(text=f"{ts}  •  {dur}", icon=icon)

            # ── Row 2 : frames + resolution ────────────────────
            sub   = box.column(align=True)
            done  = entry.get("rendered_frames", entry.get("total_frames", 0))
            total = entry.get("total_frames", 0)
            res   = entry.get("resolution", "")
            if entry.get("status") == "cancelled":
                sub.label(text=f"{done}/{total} frames  •  {res}")
            else:
                sub.label(text=f"{total} frames  •  {res}")

            # ── Row 3 : camera names ────────────────────────────
            cams = entry.get("cameras", [])
            if cams:
                names = ", ".join(c["name"] for c in cams)
                if len(names) > 42:
                    names = names[:39] + "..."
                sub.label(text=names, icon="CAMERA_DATA")

        layout.separator()
        layout.operator("multicam.clear_history",
                        text="Clear History", icon="TRASH")


# ──────────────────────────────────────────────────────────────
#  Registration
# ──────────────────────────────────────────────────────────────

_CLASSES = (
    CameraRenderItem,
    MULTICAM_UL_CameraList,
    MULTICAM_OT_RefreshCameras,
    MULTICAM_OT_AssembleVideo,
    MULTICAM_OT_ClearHistory,
    MULTICAM_OT_GenerateThumbnails,
    MULTICAM_OT_RenderParallel,
    MULTICAM_OT_AddCamera,
    MULTICAM_OT_KeySelectedCamera,
    MULTICAM_OT_RenderSequence,
    MULTICAM_PT_MainPanel,
    MULTICAM_PT_HistoryPanel,
)


def register():
    global _preview_coll
    if _preview_coll is None:
        _preview_coll = bpy.utils.previews.new()
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.multicam_cameras = CollectionProperty(
        type=CameraRenderItem, name="MultiCam Cameras",
    )
    bpy.types.Scene.multicam_active_index = IntProperty(
        name="Active Camera Index",
        default=0,
        update=_update_cam_range_from_selection,
    )
    bpy.types.Scene.multicam_new_cam_start = IntProperty(
        name="Start Frame",
        description="First frame of the new camera's render range",
        default=0, min=0,
        update=_update_selected_camera_range,
    )
    bpy.types.Scene.multicam_new_cam_end = IntProperty(
        name="End Frame",
        description="Last frame of the new camera's render range",
        default=60, min=0,
        update=_update_selected_camera_range,
    )
    bpy.types.Scene.multicam_video_container = EnumProperty(
        name="Container",
        items=[
            ("MKV",       "Matroska (.mkv)", ""),
            ("MPEG4",     "MP4 (.mp4)",      ""),
            ("AVI",       "AVI (.avi)",      ""),
            ("QUICKTIME", "QuickTime (.mov)",""),
        ],
        default="MKV",
    )
    bpy.types.Scene.multicam_video_codec = EnumProperty(
        name="Codec",
        items=[
            ("H264", "H.264",  ""),
            ("HEVC", "H.265",  ""),
            ("VP9",  "VP9",    ""),
            ("AV1",  "AV1",    ""),
        ],
        default="H264",
    )
    bpy.types.Scene.multicam_video_quality = EnumProperty(
        name="Quality",
        items=[
            ("PERC_LOSSLESS", "Perceptually Lossless", ""),
            ("LOSSLESS",      "Lossless",               ""),
            ("HIGH",          "High",                   ""),
            ("MEDIUM",        "Medium",                 ""),
            ("LOW",           "Low",                    ""),
            ("VERYLOW",       "Very Low",               ""),
        ],
        default="PERC_LOSSLESS",
    )
    bpy.types.Scene.multicam_auto_assemble_video = BoolProperty(
        name="Auto Assemble Frames to Video",
        description="Automatically assemble the rendered frames into a video after the batch render finishes",
        default=False,
    )
    bpy.types.Scene.multicam_parallel_jobs = IntProperty(
        name="Parallel Jobs",
        description="Number of Blender background processes to launch simultaneously",
        default=2, min=1, max=8,
    )


def unregister():
    global _preview_coll
    if _preview_coll is not None:
        bpy.utils.previews.remove(_preview_coll)
        _preview_coll = None
    _unregister_handlers()
    _redraw_running[0] = False
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.multicam_cameras
    del bpy.types.Scene.multicam_active_index
    del bpy.types.Scene.multicam_new_cam_start
    del bpy.types.Scene.multicam_new_cam_end
    del bpy.types.Scene.multicam_video_container
    del bpy.types.Scene.multicam_video_codec
    del bpy.types.Scene.multicam_video_quality
    del bpy.types.Scene.multicam_auto_assemble_video
    del bpy.types.Scene.multicam_parallel_jobs


if __name__ == "__main__":
    register()
