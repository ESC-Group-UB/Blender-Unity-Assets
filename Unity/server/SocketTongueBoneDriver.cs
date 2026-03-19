using System;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using UnityEngine;

/// <summary>
/// Receives per-frame blend shape / morph data from a Python script over TCP,
/// then drives a chain of tongue bones by mapping each normalized float value
/// to a local-space rotation on the chosen axis.
///
/// Communication protocol:
///   - Python connects to this listener and sends newline-delimited JSON messages.
///   - Each message is a TonguePacket: { "frame": int, "vals": [float x 11] }
///   - Values arrive pre-normalized to [-1, 1] by the Python sender.
///   - This script re-applies the same post-processing that Blender uses
///     (tip falloff + asymmetric clamping) so the two environments stay in sync.
/// </summary>
public class SocketTongueBoneDriver : MonoBehaviour
{
    // ── Inspector-exposed configuration ─────────────────────────────────────

    [Header("Socket")]
    /// <summary>TCP port this server listens on. Must match the Python sender's PORT constant.</summary>
    public int port = 50009;

    [Header("Bone chain (size = 11)")]
    /// <summary>
    /// Ordered array of tongue bone Transforms, from root (index 0) to tip (index 10).
    /// Assign all 11 bones in the Inspector; the array length must match COUNT in Python.
    /// </summary>
    public Transform[] bones;

    [Header("Rotation settings")]
    /// <summary>
    /// Multiplier that converts a post-processed value (range ≈ [-maxAbsSlider, maxAbsSlider])
    /// into degrees of rotation. Increase this (e.g. 60–90) for more visible movement.
    /// </summary>
    public float angleScale = 20f;

    /// <summary>Which local axis each bone rotates around when driven by incoming data.</summary>
    public Axis rotationAxis = Axis.Z;

    /// <summary>If true, the sign of each incoming value is flipped before processing.</summary>
    public bool invert = false;

    /// <summary>
    /// Lerp speed used each Update to smoothly blend currentVals toward targetVals.
    /// Higher values = snappier response; lower values = more lag / smoothing.
    /// </summary>
    public float smoothSpeed = 12f;

    [Header("Val processing (match Blender)")]
    /// <summary>
    /// Corresponds to MAX_ABS_SLIDER in the Blender driver script.
    /// Defines the maximum absolute output value after tip-falloff weighting.
    /// Acts as the upper clamp bound; also scales the raw input.
    /// </summary>
    public float maxAbsSlider = 0.45f;

    /// <summary>
    /// Asymmetric lower clamp bound (negative = downward rotation).
    /// Set tighter than maxAbsSlider to prevent the tongue from clipping through
    /// the jaw / lower teeth geometry when driven downward.
    /// </summary>
    public float maxDown = -0.15f;

    /// <summary>
    /// Enables the tip-falloff weight curve (matches tip_falloff(i) in Blender).
    /// When true, bones closer to the tip receive progressively larger rotations
    /// because the weight is i² / (N-1)², producing a quadratic falloff from root to tip.
    /// </summary>
    public bool useTipFalloff = true;

    // ── Private runtime state ────────────────────────────────────────────────

    private TcpListener listener;       // The TCP server that accepts incoming Python connections
    private Thread listenThread;        // Background thread running the accept loop
    private volatile bool isRunning = false; // Shared flag; volatile ensures cross-thread visibility

    /// <summary>
    /// Mutex protecting shared packet data between the network thread and the main Unity thread.
    /// Always lock this before reading or writing latestPacket / hasNewPacket.
    /// </summary>
    private readonly object dataLock = new object();
    private TonguePacket latestPacket = null; // Most recently received packet (may not yet be consumed)
    private bool hasNewPacket = false;         // True when a packet arrived since the last Update

    private Quaternion[] baseLocalRotations; // Captured rest-pose rotations for each bone
    private float[] currentVals;             // Smoothed display values (updated every Update via Lerp)
    private float[] targetVals;              // Processed target values set from the latest packet

    /// <summary>Axis enum used in the Inspector to pick the local rotation axis.</summary>
    public enum Axis { X, Y, Z }

    // ── Value processing helpers (must mirror the Blender driver logic) ──────

    /// <summary>
    /// Quadratic falloff weight for bone at index <paramref name="i"/>.
    /// Replicates the Python/Blender function: tip_falloff(i) = (i / (N-1))²
    ///
    /// Result ranges from 0.0 at the root (i=0) to 1.0 at the tip (i=N-1),
    /// so the root bone barely moves while the tip bone gets the full scaled value.
    /// </summary>
    float TipFalloff(int i)
    {
        float x = (float)i / (bones.Length - 1); // Normalize index to [0, 1]
        return x * x;                              // Square for a quadratic curve
    }

    /// <summary>
    /// Applies tip-falloff weighting, scales by maxAbsSlider, then asymmetrically clamps
    /// the result — exactly mirroring the Blender process_val(raw, i) function.
    ///
    /// Processing steps:
    ///   1. Compute the falloff weight w for this bone index.
    ///   2. Scale: v = raw * w * maxAbsSlider
    ///   3. Clamp: lower bound = maxDown * w  (tighter downward limit to avoid mesh clipping)
    ///             upper bound = maxAbsSlider * w
    /// </summary>
    float ProcessVal(float raw, int i)
    {
        float w = useTipFalloff ? TipFalloff(i) : 1f; // Weight: quadratic falloff or uniform
        float v = raw * w * maxAbsSlider;              // Apply scale

        // Asymmetric clamp: allow less downward travel than upward
        // to prevent the tongue tip from intersecting the lower jaw
        float lo = maxDown * w;
        float hi = maxAbsSlider * w;
        return Mathf.Clamp(v, lo, hi);
    }

    // ── Unity lifecycle ──────────────────────────────────────────────────────

    void Start()
    {
        if (bones == null || bones.Length == 0)
        {
            Debug.LogWarning("No bones assigned. Assign tongue bones in the Inspector.");
            return;
        }

        // Allocate per-bone arrays
        baseLocalRotations = new Quaternion[bones.Length];
        currentVals        = new float[bones.Length];
        targetVals         = new float[bones.Length];

        // Snapshot each bone's rest-pose rotation so we can apply deltas on top of it
        for (int i = 0; i < bones.Length; i++)
            if (bones[i] != null)
                baseLocalRotations[i] = bones[i].localRotation;

        // Start the TCP listener on a background thread so it never blocks the main thread
        isRunning    = true;
        listenThread = new Thread(ListenLoop) { IsBackground = true };
        listenThread.Start();

        Debug.Log($"Unity TCP server started, listening on port {port}");
    }

    void Update()
    {
        // ── Step 1: Safely consume the latest packet from the network thread ──
        TonguePacket packetToUse = null;

        lock (dataLock)
        {
            if (hasNewPacket && latestPacket != null)
            {
                packetToUse  = latestPacket;   // Grab the reference
                hasNewPacket = false;           // Mark as consumed so we don't process it twice
            }
        }

        // ── Step 2: If a new packet arrived, update the rotation targets ──────
        if (packetToUse != null)
            UpdateTargets(packetToUse);

        // ── Step 3: Every frame, lerp current values toward targets and apply ─
        ApplyBoneRotations();
    }

    /// <summary>
    /// Converts raw packet values into processed rotation targets.
    /// Called from Update on the main thread whenever a new packet is available.
    /// </summary>
    void UpdateTargets(TonguePacket packet)
    {
        if (packet.vals == null)
        {
            Debug.LogWarning("Received packet with null vals array — skipping.");
            return;
        }

        // Guard against mismatched array sizes (e.g. Python sending fewer than 11 values)
        int n = Mathf.Min(packet.vals.Length, bones.Length);

        for (int i = 0; i < n; i++)
        {
            // Clamp to [-1, 1] in case of any floating-point overshoot from Python
            float raw = Mathf.Clamp(packet.vals[i], -1f, 1f);

            // Optionally flip the sign (e.g. to correct for mirrored rig orientation)
            if (invert) raw = -raw;

            // Apply the same post-processing as Blender so the two systems stay in sync
            targetVals[i] = ProcessVal(raw, i);
        }

        Debug.Log($"Frame {packet.frame} | " +
                  $"vals[0]={targetVals[0]:F3}  " +
                  $"vals[5]={targetVals[5]:F3}  " +
                  $"vals[10]={targetVals[10]:F3}");
    }

    /// <summary>
    /// Smoothly interpolates currentVals toward targetVals each frame,
    /// then writes the resulting rotation to each bone's localRotation.
    ///
    /// The delta rotation is built on top of the bone's captured rest-pose
    /// so that any existing offsets (twist, correction rotations, etc.) are preserved.
    /// </summary>
    void ApplyBoneRotations()
    {
        for (int i = 0; i < bones.Length; i++)
        {
            if (bones[i] == null) continue;

            // Lerp toward the target — smoothSpeed controls how quickly bones track the data
            currentVals[i] = Mathf.Lerp(currentVals[i], targetVals[i], Time.deltaTime * smoothSpeed);

            // ProcessVal output is in [-maxAbsSlider, maxAbsSlider].
            // Multiplying by angleScale maps this small range to degrees of rotation.
            // Tip: increase angleScale (e.g. 60–90) for more visible tongue movement.
            float angle = currentVals[i] * angleScale;

            // Build a local-space delta rotation around the chosen axis
            Quaternion delta = rotationAxis switch
            {
                Axis.X => Quaternion.Euler(angle, 0f, 0f),
                Axis.Y => Quaternion.Euler(0f, angle, 0f),
                _      => Quaternion.Euler(0f, 0f, angle),  // Default: Z axis
            };

            // Apply the delta on top of the rest-pose rotation (not world space)
            bones[i].localRotation = baseLocalRotations[i] * delta;
        }
    }

    // ── Network layer ────────────────────────────────────────────────────────

    /// <summary>
    /// Runs on a dedicated background thread.
    /// Starts the TcpListener and blocks on AcceptTcpClient(), spawning a new
    /// handler thread for each incoming Python connection.
    /// Multiple simultaneous connections are supported, though in practice only
    /// one Python sender connects at a time.
    /// </summary>
    void ListenLoop()
    {
        try
        {
            listener = new TcpListener(IPAddress.Any, port);
            listener.Start();

            while (isRunning)
            {
                // AcceptTcpClient blocks until a client connects; this is fine on a background thread
                TcpClient client = listener.AcceptTcpClient();
                Debug.Log("Python client connected.");

                // Handle each client on its own thread so the accept loop stays responsive
                new Thread(() => HandleClient(client)) { IsBackground = true }.Start();
            }
        }
        catch (SocketException e)
        {
            // Suppress the expected exception thrown when listener.Stop() is called during shutdown
            if (isRunning) Debug.LogError("SocketException in ListenLoop: " + e.Message);
        }
        catch (Exception e)
        {
            Debug.LogError("Unexpected exception in ListenLoop: " + e.Message);
        }
    }

    /// <summary>
    /// Handles all communication with a single connected Python client.
    /// Reads raw bytes from the stream into a StringBuilder, then splits on '\n'
    /// to extract complete JSON messages (newline-delimited JSON / NDJSON protocol).
    ///
    /// Each complete line is deserialized into a TonguePacket and stored in
    /// latestPacket under the dataLock so Update() can consume it safely.
    /// </summary>
    void HandleClient(TcpClient client)
    {
        try
        {
            using (client)
            using (NetworkStream stream = client.GetStream())
            {
                byte[]        buffer = new byte[4096]; // Read buffer — 4 KB is plenty for JSON packets
                StringBuilder sb     = new StringBuilder(); // Accumulates partial data between reads

                while (isRunning && client.Connected)
                {
                    // Read whatever bytes are available; blocks until at least one byte arrives
                    int bytesRead = stream.Read(buffer, 0, buffer.Length);
                    if (bytesRead <= 0) break; // 0 bytes means the remote side closed the connection

                    // Append the newly received bytes to our working buffer
                    sb.Append(Encoding.UTF8.GetString(buffer, 0, bytesRead));

                    // Extract all complete lines from the buffer (there may be more than one
                    // if packets arrived together, or none if the current chunk is a partial packet)
                    while (true)
                    {
                        string s   = sb.ToString();
                        int    idx = s.IndexOf('\n'); // Look for the newline message delimiter
                        if (idx < 0) break;           // No complete message yet — wait for more data

                        string line = s.Substring(0, idx).Trim();
                        sb.Remove(0, idx + 1); // Consume this line from the buffer
                        if (string.IsNullOrEmpty(line)) continue; // Skip blank lines

                        try
                        {
                            TonguePacket packet = JsonUtility.FromJson<TonguePacket>(line);
                            if (packet != null)
                            {
                                // Hand the packet to the main thread via the shared lock
                                lock (dataLock)
                                {
                                    latestPacket = packet;
                                    hasNewPacket = true;
                                }
                            }
                        }
                        catch (Exception e)
                        {
                            // Log malformed JSON but keep reading — don't crash the handler
                            Debug.LogWarning("JSON parse failed: " + e.Message);
                        }
                    }
                }
            }
        }
        catch (Exception e)
        {
            // Common causes: Python script ended, network reset, or application quitting
            Debug.LogWarning("Client handler ended: " + e.Message);
        }

        Debug.Log("Python client disconnected.");
    }

    // ── Cleanup ──────────────────────────────────────────────────────────────

    void OnApplicationQuit()
    {
        // Signal the background threads to stop their loops
        isRunning = false;

        // Stop the listener so AcceptTcpClient() unblocks and the listen thread can exit
        try { listener?.Stop(); }     catch { }

        // Abort the listen thread as a fallback (Abort is a last resort in older Unity versions)
        try { listenThread?.Abort(); } catch { }
    }
}