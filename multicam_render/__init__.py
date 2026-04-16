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
from bpy.props import (StringProperty, BoolProperty,
                       CollectionProperty, IntProperty)
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
        # Save all existing timeline markers
        "markers": [
            (m.name, m.frame, m.camera)
            for m in scene.timeline_markers
        ],
    })


def _update_cam_range_from_selection(self, context):
    """Called when the active index in the UIList changes.
    Reads the selected camera's keyframe range and writes it
    into the Create Camera Start / End fields."""
    col = self.multicam_cameras       # self = Scene
    idx = self.multicam_active_index
    if not (0 <= idx < len(col)):
        return
    cam_obj = bpy.data.objects.get(col[idx].cam_name)
    if cam_obj is None:
        return
    fmin, fmax = get_keyframe_range(cam_obj)
    if fmin is not None:
        self.multicam_new_cam_start = fmin
        self.multicam_new_cam_end   = fmax


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
    _progress["is_running"] = False
    _redraw_running[0]      = False
    _restore_state(scene)
    _unregister_handlers()
    print("[MultiCam] All cameras rendered. Done.")


@bpy.app.handlers.persistent
def _on_render_cancel(scene, depsgraph=None):
    """Fires if the user cancels the render."""
    _progress["is_running"] = False
    _redraw_running[0]      = False
    _restore_state(scene)
    _unregister_handlers()
    print("[MultiCam] Render cancelled.")


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
    """Called every 0.4 s while rendering to refresh the panel."""
    if not _redraw_running[0]:
        return None  # return None → timer stops
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

        if window:
            with context.temp_override(window=window):
                bpy.ops.render.render("INVOKE_DEFAULT", animation=True)
        else:
            bpy.ops.render.render("INVOKE_DEFAULT", animation=True)

        return {"FINISHED"}


# ──────────────────────────────────────────────────────────────
#  Video assembly — state + handlers
# ──────────────────────────────────────────────────────────────

_assemble_state: dict = {}   # holds window / scene refs during assembly


def _cleanup_assembly():
    """Restore window scene and delete the temporary assembly scene."""
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
    """Fires when the FFmpeg encoding job finishes."""
    if scene.name != "_NZO_assemble_":
        return
    vdir = _cleanup_assembly()
    print(f"[MultiCam] Video assembled → {vdir}")


@bpy.app.handlers.persistent
def _on_assemble_cancel(scene, depsgraph=None):
    """Fires if the user presses Esc during encoding."""
    if scene.name != "_NZO_assemble_":
        return
    _cleanup_assembly()
    print("[MultiCam] Video assembly cancelled.")


def _launch_assemble_render():
    """Timer callback: called 150 ms after scene switch so Blender
    has time to make the temp scene active before the render starts."""
    window = _assemble_state.get("window")
    if window:
        with bpy.context.temp_override(window=window):
            bpy.ops.render.render("INVOKE_DEFAULT", animation=True)
    return None   # single-shot timer


# ──────────────────────────────────────────────────────────────
#  Operator: Assemble rendered frames → Matroska H.264 video
# ──────────────────────────────────────────────────────────────

class MULTICAM_OT_AssembleVideo(Operator):
    """Read all rendered image frames from the output folder and
    encode them into a single Matroska H.264 video file saved in a
    VIDEO/ subfolder next to the frames."""

    bl_idname      = "multicam.assemble_video"
    bl_label       = "Assemble to Video"
    bl_description = (
        "Encode rendered frames to Matroska H.264 (perceptually lossless) "
        "in a VIDEO/ subfolder of the current render output path"
    )

    def execute(self, context):
        import os, glob

        scene    = context.scene
        abs_path = bpy.path.abspath(scene.render.filepath)

        # Resolve render directory (filepath can be a dir or a prefix)
        render_dir = abs_path if os.path.isdir(abs_path) else os.path.dirname(abs_path)

        if not os.path.isdir(render_dir):
            self.report({"ERROR"}, f"Render directory not found: {render_dir}")
            return {"CANCELLED"}

        # ── Find rendered frames (first matching extension wins) ─
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

        # ── Create VIDEO/ output subdirectory ────────────────────
        video_dir = os.path.join(render_dir, "VIDEO")
        os.makedirs(video_dir, exist_ok=True)

        # ── Build a temporary Blender scene for encoding ─────────
        # We never touch the user's current scene or its VSE.
        tmp = bpy.data.scenes.new("_NZO_assemble_")
        tmp.render.resolution_x = scene.render.resolution_x
        tmp.render.resolution_y = scene.render.resolution_y
        tmp.render.fps          = scene.render.fps
        tmp.render.fps_base     = scene.render.fps_base
        tmp.frame_start = 1
        tmp.frame_end   = n

        # ── FFmpeg output — exact settings from the screenshot ───
        r = tmp.render
        r.image_settings.file_format     = "FFMPEG"
        r.ffmpeg.format                  = "MKV"          # Matroska
        r.ffmpeg.codec                   = "H264"
        r.ffmpeg.color_depth             = "8"
        r.ffmpeg.constant_rate_factor    = "PERCEPTUALLY_LOSSLESS"
        r.ffmpeg.ffmpeg_preset           = "GOOD"
        r.ffmpeg.gopsize                 = 18             # Keyframe interval
        r.ffmpeg.use_max_b_frames        = False
        r.ffmpeg.audio_codec             = "NONE"
        r.use_sequencer                  = True           # render VSE, not 3D
        blend_stem = os.path.splitext(bpy.path.basename(bpy.data.filepath))[0] or "assembled"
        r.filepath                       = os.path.join(video_dir, blend_stem)

        # ── Load frames into the temp scene's VSE ────────────────
        seq   = tmp.sequence_editor_create()
        strip = seq.sequences.new_image(
            name       = "frames",
            filepath   = frame_files[0],
            channel    = 1,
            frame_start= 1,
        )
        for f in frame_files[1:]:
            strip.elements.append(os.path.basename(f))

        # ── Store refs for the completion handler ─────────────────
        _assemble_state.clear()
        _assemble_state.update({
            "window":          context.window,
            "original_scene":  scene,
            "tmp_scene":       tmp,
            "video_dir":       video_dir,
        })

        # ── Register handlers ────────────────────────────────────
        if _on_assemble_complete not in bpy.app.handlers.render_complete:
            bpy.app.handlers.render_complete.append(_on_assemble_complete)
        if _on_assemble_cancel not in bpy.app.handlers.render_cancel:
            bpy.app.handlers.render_cancel.append(_on_assemble_cancel)

        # ── Switch active window scene then launch encoding ──────
        # A short timer delay lets Blender register the scene change
        # before the render operator is invoked.
        context.window.scene = tmp
        bpy.app.timers.register(_launch_assemble_render, first_interval=0.15)

        self.report({"INFO"}, f"Assembling {n} frames → {video_dir}/{blend_stem}.mkv")
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

            # Current camera label
            if prog["current_cam"]:
                col.label(
                    text=f"Camera : {prog['current_cam']}",
                    icon="CAMERA_DATA",
                )

            col.separator()

            # Per-camera status
            col.label(text="Cameras in sequence:")
            for name, fmin, fmax in prog["cameras"]:
                row  = col.row(align=True)
                # Determine icon based on progress
                if done >= (fmax - prog["cameras"][0][1] + 1):
                    ico = "CHECKMARK"
                elif prog["current_cam"] == name:
                    ico = "RENDER_ANIMATION"
                else:
                    ico = "TIME"
                row.label(text=f"{name}  ({fmin}–{fmax})", icon=ico)

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
        row_new.label(text="New cam  ", icon="ADD")
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
        else:
            col.label(text="(click Refresh to scan cameras)")

        layout.separator()
        row = layout.row()
        row.scale_y = 1.6
        row.operator("multicam.render_sequence",
                     text="Render Selected Cameras",
                     icon="RENDER_ANIMATION")

        layout.separator()

        # ── ASSEMBLE VIDEO section ──────────────────────────────
        box = layout.box()
        box.label(text="Assemble to Video", icon="FILE_MOVIE")

        col = box.column(align=True)
        col.label(text="Container : Matroska  |  Codec : H.264")
        col.label(text="Quality : Perceptually Lossless  |  8-bit")

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
#  Registration
# ──────────────────────────────────────────────────────────────

_CLASSES = (
    CameraRenderItem,
    MULTICAM_UL_CameraList,
    MULTICAM_OT_RefreshCameras,
    MULTICAM_OT_AssembleVideo,
    MULTICAM_OT_AddCamera,
    MULTICAM_OT_RenderSequence,
    MULTICAM_PT_MainPanel,
)


def register():
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
    )
    bpy.types.Scene.multicam_new_cam_end = IntProperty(
        name="End Frame",
        description="Last frame of the new camera's render range",
        default=60, min=0,
    )


def unregister():
    _unregister_handlers()
    _redraw_running[0] = False
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.multicam_cameras
    del bpy.types.Scene.multicam_active_index
    del bpy.types.Scene.multicam_new_cam_start
    del bpy.types.Scene.multicam_new_cam_end


if __name__ == "__main__":
    register()
