import bpy
import socket
import threading
import queue
import json

# ============================================================
# Tongue Socket Server (Blender)
#
# PURPOSE:
#   Receives real-time tongue shape data over a TCP socket and
#   applies it to a Blender armature via custom properties,
#   which drive bones/mesh through Blender's driver system.
#
# PROTOCOL:
#   Each message is a JSON object terminated by a newline (\n):
#     {"frame": int or null, "vals": [float x COUNT]}
#   - "frame": optional timeline frame to set (used for baking)
#   - "vals":  COUNT float values in [-1, 1] representing tongue
#              segment displacements
#
# CUSTOM PROPERTIES WRITTEN:
#   arm["tongue_b0"] ... arm["tongue_b{COUNT-1}"]
#   These are expected to be wired to bone drivers in the rig.
#
# BAKING:
#   Set bpy.context.scene["tongue_socket_bake"] = True to
#   automatically insert keyframes as data arrives.
# ============================================================


# ─────────────────────────────────────────────
# USER CONFIGURATION — edit these before running
# ─────────────────────────────────────────────

# Name of the armature object in the Blender scene that owns
# the tongue_b0...tongue_bN custom properties / drivers.
ARM_NAME = "Armature.001"

# TCP address the server will bind to.
# Use 127.0.0.1 (loopback) so only local processes can connect.
HOST = "127.0.0.1"
PORT = 50009

# Number of tongue control segments (sliders).
# Must match the number of "tongue_b*" custom props on the rig
# AND the length of the "vals" array sent by the client.
COUNT = 11

# Maximum absolute value written to any tongue custom property.
# The tip-falloff function (below) scales segments toward the tip
# further down, so tip segments will naturally stay below this cap.
MAX_ABS_SLIDER = 0.45


# ─────────────────────────────────────────────
# TIP FALLOFF
# ─────────────────────────────────────────────

def tip_falloff(i):
    """
    Returns a weight in [0, 1] that scales the influence of segment i.

    Segment 0 (tongue root) gets weight 0.0 — it is not moved at all.
    Segment COUNT-1 (tongue tip) gets weight 1.0 — full amplitude.

    The quadratic curve (x^2) makes influence grow slowly at the root
    and accelerate toward the tip, which matches how a real tongue deforms:
    the base is anchored and the tip has the most freedom.

    x = normalized position along the tongue (0 = root, 1 = tip)
    """
    x = i / (COUNT - 1)
    return x * x   # quadratic falloff: 0 → 0, 0.5 → 0.25, 1 → 1


def clamp(x, lo, hi):
    """Return x clamped to the closed interval [lo, hi]."""
    return lo if x < lo else hi if x > hi else x


# ─────────────────────────────────────────────
# MODULE-LEVEL STATE
# ─────────────────────────────────────────────

# Background thread that runs the blocking TCP accept/recv loop.
_server_thread = None

# Setting this event signals the background thread to exit cleanly.
_stop_event = threading.Event()

# Thread-safe queue: the socket thread pushes parsed JSON dicts here;
# the Blender main-thread timer pops and applies them.
_msg_queue = queue.Queue()


# ─────────────────────────────────────────────
# BLENDER HELPERS
# ─────────────────────────────────────────────

def get_arm():
    """
    Look up the armature object by ARM_NAME and validate it.
    Returns the bpy.types.Object, or None if not found / wrong type.
    Prints a descriptive error so the user knows what to fix.
    """
    arm = bpy.data.objects.get(ARM_NAME)
    if not arm:
        print(f"❌ Armature '{ARM_NAME}' not found. Check ARM_NAME.", flush=True)
        return None
    if arm.type != "ARMATURE":
        print(f"❌ Object '{ARM_NAME}' is not an ARMATURE.", flush=True)
        return None
    return arm


def ensure_action(arm):
    """
    Guarantee the armature has animation data and an Action assigned.
    Required before keyframe_insert() can write keys.

    If no Action exists, one is created and named after the armature.
    Returns the Action so callers can introspect it if needed.
    """
    if arm.animation_data is None:
        arm.animation_data_create()
    if arm.animation_data.action is None:
        arm.animation_data.action = bpy.data.actions.new(
            name=f"{arm.name}_TongueSocket"
        )
    return arm.animation_data.action


# ─────────────────────────────────────────────
# CORE: APPLY VALUES TO THE RIG
# ─────────────────────────────────────────────

def apply_vals(frame, vals, bake=False):
    """
    Write a set of tongue slider values to the armature's custom properties
    and force Blender to propagate the changes through drivers and constraints.

    Parameters
    ----------
    frame : int or None
        Timeline frame number. When not None, bpy.context.scene.frame_set()
        is called so any frame-dependent drivers evaluate at the right time.
        Also used as the keyframe index when bake=True.
    vals : list[float]
        Raw values in [-1, 1], one per segment. They are scaled by the
        tip_falloff weight and clamped to ±MAX_ABS_SLIDER before writing.
    bake : bool
        If True, insert a keyframe for every custom property after writing.
        Requires the armature to have a valid Action (ensure_action handles this).
    """
    arm = get_arm()
    if arm is None:
        return  # Error already printed by get_arm()

    scene = bpy.context.scene

    # If we are baking, make sure the armature has an Action to write into.
    if bake:
        ensure_action(arm)

    # Advance the timeline so frame-dependent drivers are correct.
    # Even in live (non-bake) mode this is harmless and aids scrubbing.
    if frame is not None:
        scene.frame_set(int(frame))

    # Write each segment's custom property.
    for i in range(COUNT):
        raw = float(vals[i])

        # Scale by the tip-falloff weight so root segments barely move
        # and tip segments receive near-full amplitude.
        w = tip_falloff(i)

        # Multiply raw signal by the weight and the global amplitude cap,
        # then clamp to ensure we never exceed ±MAX_ABS_SLIDER regardless
        # of floating-point edge cases.
        v = clamp(raw * w * MAX_ABS_SLIDER, -MAX_ABS_SLIDER, MAX_ABS_SLIDER)

        # Write to the armature custom property.
        # Drivers on bones/shape keys read this value every depsgraph update.
        arm[f"tongue_b{i}"] = v

        # Optionally bake to a keyframe at the specified frame.
        if bake and frame is not None:
            arm.keyframe_insert(
                data_path=f'["tongue_b{i}"]',
                frame=int(frame)
            )

    # ── CRITICAL: tell Blender something changed ──────────────────────────
    # Without these two calls, Blender's dependency graph may not re-evaluate
    # drivers, bone constraints, or shape keys that read the custom properties,
    # so the rig would appear frozen even though the data changed.
    #
    # update_tag(refresh={'OBJECT'}) marks the armature as dirty.
    # view_layer.update() triggers a full depsgraph evaluation pass.
    arm.update_tag(refresh={'OBJECT'})
    bpy.context.view_layer.update()


# ─────────────────────────────────────────────
# BACKGROUND THREAD: TCP SERVER LOOP
# ─────────────────────────────────────────────

def _socket_server_loop():
    """
    Runs in a daemon background thread.

    Lifecycle
    ---------
    1. Bind a TCP socket to HOST:PORT.
    2. Wait for a single client connection (we only support one at a time).
    3. Receive raw bytes, accumulate them in a buffer, and split on newlines
       to extract complete JSON messages.
    4. Parse each message and push it onto _msg_queue for the main thread.
    5. On client disconnect, reset and wait for the next client.
    6. Exit cleanly when _stop_event is set (via stop_tongue_server()).

    Design notes
    ------------
    - A 0.5-second timeout on both accept() and recv() lets the loop check
      _stop_event regularly without busy-waiting.
    - The newline-delimited protocol means messages can be split across
      multiple recv() calls; the buffer handles reassembly correctly.
    - Only one client is accepted at a time (listen backlog = 1).
      The previous connection must drop before a new one is accepted.
    """
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # Allow rapid restart without waiting for the OS to release the port.
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    try:
        srv.bind((HOST, PORT))
    except Exception as e:
        print(f"❌ Failed to bind {HOST}:{PORT} -> {e}", flush=True)
        return

    srv.listen(1)
    srv.settimeout(0.5)   # Non-blocking accept so we can check _stop_event
    print(f"✅ Socket server listening on {HOST}:{PORT}", flush=True)

    conn = None     # Active client socket (None when waiting for a client)
    buf  = b""      # Byte accumulation buffer for partial messages

    try:
        while not _stop_event.is_set():

            # ── Phase 1: Accept a new client if we have none ──────────────
            if conn is None:
                try:
                    conn, addr = srv.accept()
                    conn.settimeout(0.5)  # Non-blocking recv so we can stop
                    print(f"✅ Client connected: {addr}", flush=True)
                except socket.timeout:
                    continue  # No client yet — loop back and check stop event

            # ── Phase 2: Receive data from the connected client ───────────
            try:
                data = conn.recv(4096)

                if not data:
                    # An empty recv means the client closed the connection.
                    print("⚠️ Client disconnected.", flush=True)
                    conn.close()
                    conn = None
                    buf  = b""
                    continue

                # Append new bytes to the buffer, then extract whole lines.
                buf += data
                while b"\n" in buf:
                    # Split on the first newline to get one complete message.
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue  # Skip blank lines (e.g. trailing \r\n)

                    try:
                        msg = json.loads(line.decode("utf-8"))
                        _msg_queue.put(msg)  # Hand off to main thread
                    except Exception as e:
                        print("❌ Bad JSON:", e, flush=True)

            except socket.timeout:
                # No data arrived within 0.5 s — normal, keep looping.
                continue
            except Exception as e:
                print("❌ Socket error:", e, flush=True)
                try:
                    conn.close()
                except Exception:
                    pass
                conn = None
                buf  = b""

    finally:
        # Always clean up, even if an unexpected exception occurred.
        if conn:
            try:
                conn.close()
            except Exception:
                pass
        try:
            srv.close()
        except Exception:
            pass
        print("🛑 Server stopped.", flush=True)


# ─────────────────────────────────────────────
# MAIN-THREAD TIMER: CONSUME THE QUEUE
# ─────────────────────────────────────────────

def _timer_poll_queue():
    """
    Registered as a Blender application timer; called on the main thread.

    Why a timer instead of applying values directly in the socket thread?
    ──────────────────────────────────────────────────────────────────────
    Blender's Python API (bpy.*) is NOT thread-safe. Calling bpy functions
    from a background thread causes crashes or silent corruption. The correct
    pattern is:
      - Background thread  →  collects data into a thread-safe queue
      - Main-thread timer  →  drains the queue and calls bpy functions safely

    Throughput throttle
    -------------------
    We process at most 100 messages per timer tick to prevent a burst of
    incoming data from freezing Blender's UI for a visible amount of time.
    Any excess messages remain in the queue and are processed next tick.

    Return value
    ------------
    Returning a float (seconds) reschedules the timer automatically.
    0.02 s → ~50 Hz poll rate, which is smooth for real-time animation.
    """
    processed = 0

    # Read the bake flag from the scene each tick so the user can toggle it
    # live without restarting the server.
    bake = bool(bpy.context.scene.get("tongue_socket_bake", False))

    while not _msg_queue.empty() and processed < 100:
        msg = _msg_queue.get()

        frame = msg.get("frame", None)
        vals  = msg.get("vals",  None)

        # Silently skip malformed messages (no crash, no spam).
        if vals is None or len(vals) < COUNT:
            continue

        # Apply the first COUNT values; ignore any extras.
        apply_vals(frame, vals[:COUNT], bake=bake)
        processed += 1

    # One final depsgraph update per tick to catch any remaining dirty state.
    bpy.context.view_layer.update()

    return 0.02  # Reschedule at ~50 Hz


# ─────────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────────

def start_tongue_server():
    """
    Start the TCP socket server and the Blender timer that feeds data to the rig.

    Call this once from the Blender Python Console after running this script:
        start_tongue_server()

    Guards
    ------
    - Verifies the target armature exists before binding the socket.
    - Does nothing if the server is already running (safe to call twice).
    """
    global _server_thread

    # Validate the armature up front so the user gets a clear error message
    # rather than a confusing runtime failure deep inside the server loop.
    arm = get_arm()
    if arm is None:
        print("❌ Fix ARM_NAME then run again.", flush=True)
        return

    if _server_thread and _server_thread.is_alive():
        print("⚠️ Server already running.", flush=True)
        return

    # Clear the stop flag in case the server was previously stopped.
    _stop_event.clear()

    # Start the socket listener in a daemon thread so it is automatically
    # killed when Blender exits (no manual cleanup required on quit).
    _server_thread = threading.Thread(target=_socket_server_loop, daemon=True)
    _server_thread.start()

    # Register the main-thread timer (persistent=True keeps it alive across
    # scene changes and file loads for the duration of the Blender session).
    bpy.app.timers.register(_timer_poll_queue, persistent=True)
    print("✅ Timer registered.", flush=True)


def stop_tongue_server():
    """
    Signal the background socket thread to shut down gracefully.

    The thread checks _stop_event on every 0.5-second timeout, so it will
    exit within about half a second of this call.

    Note: This does NOT unregister the Blender timer. If you want to stop
    the timer as well, call:
        bpy.app.timers.unregister(_timer_poll_queue)
    """
    _stop_event.set()
    print("🛑 Stop requested.", flush=True)


# ============================================================
# QUICK-START GUIDE
# ============================================================
# 1. Set ARM_NAME to the exact name of your armature object.
# 2. Run this script (Text Editor → Run Script, or drag into Console).
# 3. The server starts automatically (start_tongue_server() is called below).
#
# Manual control from the Python Console:
#   start_tongue_server()   # start (or restart after stop)
#   stop_tongue_server()    # stop gracefully
#
# Enable keyframe baking (insert keys as data arrives):
#   bpy.context.scene["tongue_socket_bake"] = True
#
# Client message format (one JSON object per line, UTF-8):
#   {"frame": 42, "vals": [0.0, 0.1, ..., 0.9]}   # 11 floats
#   {"frame": null, "vals": [...]}                  # live, no frame advance
# ============================================================

start_tongue_server()