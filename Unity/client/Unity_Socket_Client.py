import socket, csv, json, time

# Path to the CSV file containing facial animation frame data
CSV_PATH = r"C:\new.csv"

# Unity TCP server connection settings
HOST = "127.0.0.1"   # Localhost - Unity is running on the same machine
PORT = 50009          # Must match the port Unity's TCP listener is bound to
COUNT = 11            # Number of blend shape / morph target values per frame
MAX_RETRIES = 5       # How many times to retry connecting before giving up


def norm(v, vmin, vmax):
    """
    Normalize a value from its original range [vmin, vmax] into [-1.0, 1.0].
    
    This is a two-step process:
      1. Map v into [0, 1] using standard min-max normalization.
      2. Remap [0, 1] to [-1, 1] by applying: t * 2.0 - 1.0
    
    Args:
        v:    The raw value to normalize.
        vmin: The minimum observed value across all frames (used as lower bound).
        vmax: The maximum observed value across all frames (used as upper bound).
    
    Returns:
        A float in the range [-1.0, 1.0].
    """
    t = (v - vmin) / (vmax - vmin)  # Step 1: normalize to [0, 1]
    return t * 2.0 - 1.0             # Step 2: remap to [-1, 1]


# ─── Load and Parse the CSV File ────────────────────────────────────────────

with open(CSV_PATH, newline="") as f:
    rows = list(csv.reader(f))

# Skip the header row if the first cell looks like a column label
# (e.g. "frame", "t", or "time" — case-insensitive)
start_idx = 0
if rows and rows[0] and rows[0][0].lower() in ("frame", "t", "time"):
    start_idx = 1

parsed = []          # Will hold (frame_number, [v0, v1, ..., v10]) tuples
vmin =  1e18         # Running minimum across all values (start very high)
vmax = -1e18         # Running maximum across all values (start very low)

for row in rows[start_idx:]:
    if not row:
        continue  # Skip any completely empty rows

    # Column 0 is the frame index (may be a float like "120.0", so cast via float first)
    frame = int(float(row[0]))

    # Remaining columns are alternating pairs: [x0, y0, x1, y1, ..., xN, yN]
    # We only want every second value starting at index 1 (the "y" component)
    nums = list(map(float, row[1:]))
    if len(nums) < 2 * COUNT:
        raise RuntimeError(
            f"Frame {frame}: only {len(nums)} data columns found, "
            f"but need at least {2 * COUNT} (2 per blend shape × {COUNT} shapes)"
        )

    # Extract the "y" value for each of the COUNT blend shapes
    vs = [nums[2 * i + 1] for i in range(COUNT)]

    parsed.append((frame, vs))

    # Track the global min/max so we can normalize consistently across all frames
    for v in vs:
        vmin = min(vmin, v)
        vmax = max(vmax, v)

# Guard against degenerate data where all values are identical (would cause divide-by-zero)
if vmax - vmin < 1e-9:
    raise RuntimeError(
        "All v values are essentially constant — cannot normalize. "
        "Check that the CSV contains meaningful animation data."
    )

# Sort frames in ascending order in case the CSV rows are out of sequence
parsed.sort(key=lambda x: x[0])

print(f"Loaded {len(parsed)} frames, vmin={vmin:.3f}, vmax={vmax:.3f}")


# ─── Connect to Unity via TCP (with retry logic) ─────────────────────────────

# Unity may take a moment to start its TCP listener after entering Play Mode,
# so we retry the connection several times before giving up.
sock = None
for attempt in range(MAX_RETRIES):
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((HOST, PORT))
        print(f"Connected to Unity (attempt {attempt + 1})")
        break  # Connection succeeded — exit the retry loop
    except ConnectionRefusedError:
        print(f"Connection refused, retrying in 2s... ({attempt + 1}/{MAX_RETRIES})")
        sock.close()
        time.sleep(2)  # Wait before the next attempt
else:
    # The for-loop completed without hitting 'break', meaning all retries failed
    raise RuntimeError(
        "Could not connect to Unity after retries. "
        "Make sure the Unity scene is in Play Mode and the TCP listener is active."
    )


# ─── Stream Frame Data to Unity ──────────────────────────────────────────────

fps = 30.0                   # Target playback rate matching the original capture framerate
frame_time = 1.0 / fps       # Seconds to wait between sending frames (~0.0333s at 30 fps)

try:
    for i, (frame, vs) in enumerate(parsed):
        # Normalize each blend shape value to [-1, 1] and round to 4 decimal places
        # to keep the JSON payload compact without losing meaningful precision
        vals = [round(norm(v, vmin, vmax), 4) for v in vs]

        # Build a simple JSON message with the frame index and normalized values
        msg = {"frame": frame, "vals": vals}

        # Encode as UTF-8 and append a newline so Unity can delimit messages
        # when reading from the TCP stream (newline-delimited JSON / NDJSON protocol)
        sock.sendall((json.dumps(msg) + "\n").encode("utf-8"))

        # Throttle sending to match the target framerate so Unity can keep up
        time.sleep(frame_time)

        # Print a progress update every 30 frames (once per second at 30 fps)
        if i % 30 == 0:
            print(f"Sent frame {frame}, vals[0]={vals[0]:.3f}")

    # After sending all frames, pause briefly before closing the socket.
    # This gives the OS TCP send buffer time to flush any remaining data
    # to Unity before the connection is torn down — without this, the last
    # few frames could be lost if the socket closes too quickly.
    print("All frames sent, waiting for buffer flush...")
    time.sleep(1.0)

finally:
    # Always close the socket, even if an exception was raised mid-stream
    sock.close()
    print("Done")