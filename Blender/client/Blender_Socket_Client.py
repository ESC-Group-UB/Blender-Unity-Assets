import socket, csv, json, time

# ============================================================
# Tongue Data CSV Player
#
# PURPOSE:
#   Reads pre-recorded tongue ultrasound tracking data from a
#   CSV file, normalizes the vertical (v) coordinate of each
#   tongue segment to [-1, 1], and streams the data frame-by-
#   frame to the Blender socket server over TCP.
#
# EXPECTED CSV FORMAT:
#   Row 0 (optional): header row — detected automatically if the
#                     first cell is "frame", "t", or "time".
#   Remaining rows:   frame_number, x0, v0, x1, v1, ..., x10, v10
#                     i.e. pairs of (x, v) for each of COUNT segments.
#                     Only the v (vertical displacement) values are used.
#
# COORDINATE CONVENTION:
#   - v is the raw vertical position of each tongue segment
#     as output by the ultrasound tracking algorithm.
#   - All v values are globally normalized across the entire
#     recording so that the minimum maps to -1 and the maximum
#     maps to +1, giving the Blender rig a consistent range.
#
# BLENDER SERVER:
#   The companion Blender script must be running and listening
#   on HOST:PORT before this script is executed.
# ============================================================


# ─────────────────────────────────────────────
# USER CONFIGURATION
# ─────────────────────────────────────────────

# Absolute path to the CSV file containing the tongue tracking data.
CSV_PATH = r"C:\new.csv"

# TCP address of the Blender socket server (must match the server script).
HOST = "127.0.0.1"
PORT = 50009

# Number of tongue segments tracked per frame.
# The CSV must contain at least COUNT * 2 data columns after the frame column
# (one x and one v value per segment).
COUNT = 11


# ─────────────────────────────────────────────
# NORMALIZATION HELPER
# ─────────────────────────────────────────────

def norm(v, vmin, vmax):
    """
    Linearly map a value v from the range [vmin, vmax] to [-1, 1].

    This is a two-step transform:
      1. t = (v - vmin) / (vmax - vmin)   →  maps to [0, 1]
      2. t * 2.0 - 1.0                    →  maps to [-1, 1]

    Used to convert raw ultrasound pixel/coordinate values into the
    normalized range expected by the Blender rig's custom properties.

    Parameters
    ----------
    v    : float  Raw value to normalize.
    vmin : float  Global minimum v across the entire recording.
    vmax : float  Global maximum v across the entire recording.

    Returns
    -------
    float in [-1, 1]
    """
    t = (v - vmin) / (vmax - vmin)
    return t * 2.0 - 1.0


# ─────────────────────────────────────────────
# STEP 1: READ AND PARSE THE CSV
# ─────────────────────────────────────────────

with open(CSV_PATH, newline="") as f:
    rows = list(csv.reader(f))

# Detect and skip an optional header row.
# The header is identified by the first cell being a common time/frame label.
# If no header is present, start_idx stays 0 and all rows are treated as data.
start_idx = 0
if rows and rows[0] and rows[0][0].lower() in ("frame", "t", "time"):
    start_idx = 1  # Skip the header row

# Parse every data row and simultaneously compute the global v range
# (needed for normalization in a second pass).
parsed = []        # List of (frame_int, [v0, v1, ..., v10])
vmin =  1e18       # Running global minimum  — will be overwritten
vmax = -1e18       # Running global maximum  — will be overwritten

for row in rows[start_idx:]:
    if not row:
        continue  # Skip completely empty rows (e.g. trailing newlines)

    # Column 0: frame number (may be stored as a float like "42.0", so
    # we cast via float first to handle both "42" and "42.0" safely).
    frame = int(float(row[0]))

    # Columns 1 onward: interleaved x, v pairs — x0, v0, x1, v1, ...
    # We need at least COUNT pairs, i.e. 2*COUNT numeric columns.
    nums = list(map(float, row[1:]))
    if len(nums) < 2 * COUNT:
        raise RuntimeError(
            f"Frame {frame} has only {len(nums)} data columns; "
            f"need at least {2 * COUNT} (x,v pairs for {COUNT} segments)."
        )

    # Extract only the v (vertical) component from each (x, v) pair.
    # Pair i occupies indices 2*i (x) and 2*i+1 (v) in the nums list.
    vs = [nums[2 * i + 1] for i in range(COUNT)]

    parsed.append((frame, vs))

    # Expand the global min/max to cover this frame's v values.
    for v in vs:
        vmin = min(vmin, v)
        vmax = max(vmax, v)

# Guard against degenerate data where all v values are identical.
# norm() would produce a division-by-zero in that case.
if vmax - vmin < 1e-9:
    raise RuntimeError(
        "All v values are constant (vmax - vmin < 1e-9). "
        "Cannot normalize — check the CSV for tracking failures."
    )

# Sort by frame number in case the CSV rows are not in chronological order.
parsed.sort(key=lambda x: x[0])

print(f"Loaded {len(parsed)} frames  |  v range: [{vmin:.4f}, {vmax:.4f}]")


# ─────────────────────────────────────────────
# STEP 2: CONNECT TO THE BLENDER SERVER
# ─────────────────────────────────────────────

sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.connect((HOST, PORT))
print(f"Connected to Blender server at {HOST}:{PORT}")


# ─────────────────────────────────────────────
# STEP 3: STREAM FRAMES TO BLENDER
# ─────────────────────────────────────────────

# Playback rate used to pace frame delivery.
# 30 fps means one frame is sent every ~33 ms when the sleep lines are active.
fps = 30.0
frame_time = 1.0 / fps   # seconds per frame ≈ 0.0333 s

# Record the wall-clock start time and the first frame number so we can
# optionally implement drift-corrected timing later.
t0 = time.time()
base_frame = parsed[0][0]

for frame, vs in parsed:
    # Normalize each segment's v value from the raw coordinate space
    # to [-1, 1] using the global min/max computed during parsing.
    vals = [norm(v, vmin, vmax) for v in vs]

    # Build the JSON message expected by the Blender server:
    #   "frame" — tells Blender which timeline frame to set (enables baking)
    #   "vals"  — the COUNT normalized floats for tongue_b0…tongue_b10
    msg = {"frame": frame, "vals": vals}

    # Encode as UTF-8 and append a newline delimiter.
    # The Blender server uses newline-splitting to detect complete messages,
    # so the \n is mandatory — do not omit it.
    sock.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    # ── TIMING OPTIONS ───────────────────────────────────────────────────
    # Option A: Fixed 30 fps playback (uncomment for real-time preview).
    #   time.sleep(frame_time)
    #
    # Option B: Slow playback for debugging / manual scrubbing.
    #   time.sleep(0.1)
    #
    # Current behaviour (both commented out): send all frames as fast as
    # possible. Blender's 50 Hz timer will drain the queue at its own pace.
    # Use this mode when baking to keyframes, where timing doesn't matter.
    # ─────────────────────────────────────────────────────────────────────

sock.close()
print("Done — all frames sent.")