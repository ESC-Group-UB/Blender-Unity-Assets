using UnityEngine;

/// <summary>
/// Measures the positional error between each "real" skeleton bone and its
/// corresponding "ghost" skeleton bone, then feeds those normalized error
/// values into a shader so the ghost mesh can visualize drift in real time.
///
/// Typical use case: a physics-simulated or procedurally-driven ghost rig
/// that should closely follow an animation rig. When a ghost bone drifts too
/// far from its real counterpart, the shader can highlight that region
/// (e.g. with a warning color or opacity change).
///
/// Data flow:
///   BonePair[] → per-frame distance calculation → normalized [0,1] array
///   → Material.SetFloatArray("_BoneError") → GPU shader
/// </summary>
public class GhostBoneShaderDriver : MonoBehaviour
{
    // ── Bone pair configuration ──────────────────────────────────────────────

    /// <summary>
    /// Pairs one bone from the real (animation) skeleton with the corresponding
    /// bone from the ghost (physics / procedural) skeleton.
    /// The index of each BonePair in the bonePairs array must match the index
    /// of that bone inside the Ghost SkinnedMeshRenderer's bones[] array,
    /// because the shader reads _BoneError as a flat float array indexed the same way.
    /// </summary>
    [System.Serializable]
    public class BonePair
    {
        [Tooltip("The bone from the real (animation-driven) skeleton.")]
        public Transform realBone;

        [Tooltip("The bone from the ghost (physics / procedural) skeleton. " +
                 "Index in this array must match its index in the Ghost SkinnedMeshRenderer bones[].")]
        public Transform ghostBone;

        [Tooltip("Per-bone distance threshold in metres. " +
                 "When the ghost bone exceeds this distance from the real bone, normalizedError reaches 1. " +
                 "Set to 0 to fall back to the global globalThreshold.")]
        public float overrideThreshold = 0f;

        // Runtime debug values — visible in the Inspector during Play Mode
        // but hidden from the default serialized view to keep the Inspector tidy.
        [HideInInspector] public float currentError;     // Raw world-space distance (metres) this frame
        [HideInInspector] public float normalizedError;  // currentError / threshold, clamped to [0, 1]
        [HideInInspector] public bool  isTooFar;         // True when currentError exceeds the threshold
    }

    // ── Inspector fields ─────────────────────────────────────────────────────

    [Header("Bone Pairs — fill in Ghost Mesh bones[] order (Bone → Bone.010)")]
    /// <summary>
    /// Array of real↔ghost bone pairs. Size should match the number of bones
    /// in the Ghost SkinnedMeshRenderer (default 11 for an 11-bone tongue chain).
    /// </summary>
    public BonePair[] bonePairs = new BonePair[11];

    [Header("Global Distance Threshold (metres) — used when BonePair.overrideThreshold == 0")]
    /// <summary>
    /// Fallback distance threshold applied to any BonePair whose overrideThreshold is 0.
    /// A value of 0.02 means 2 cm of drift brings normalizedError to 1.0.
    /// </summary>
    public float globalThreshold = 0.02f;

    [Header("Ghost SkinnedMeshRenderer (auto-detected on this GameObject, or assign manually)")]
    /// <summary>
    /// The SkinnedMeshRenderer on the ghost mesh whose material receives the error arrays.
    /// If left null, Start() will call GetComponent to find it automatically.
    /// </summary>
    public SkinnedMeshRenderer ghostRenderer;

    [Header("Debug — per-bone normalized errors readable in the Inspector at runtime")]
    /// <summary>
    /// Mirror of each BonePair's normalizedError, surfaced as a flat array so you
    /// can watch all values at a glance in the Inspector without expanding bonePairs.
    /// Read-only at runtime; written by Update().
    /// </summary>
    public float[] debugNormalizedErrors = new float[11];

    // ── Private state ────────────────────────────────────────────────────────

    /// <summary>
    /// Instance-specific material obtained via ghostRenderer.material (not sharedMaterial).
    /// Using .material creates a unique instance so we don't accidentally modify
    /// a shared material asset and affect other renderers using the same material.
    /// </summary>
    private Material ghostMat;

    // Cache shader property IDs at class-load time to avoid per-frame string hashing.
    // Shader.PropertyToID is a one-time cost; using the int ID in SetFloatArray / SetInt
    // is significantly faster than passing the string name every frame.
    private static readonly int ShaderPropBoneError = Shader.PropertyToID("_BoneError");
    private static readonly int ShaderPropBoneCount = Shader.PropertyToID("_BoneCount");

    // ── Unity lifecycle ──────────────────────────────────────────────────────

    void Start()
    {
        // Auto-detect the SkinnedMeshRenderer if the user didn't assign one in the Inspector
        if (ghostRenderer == null)
            ghostRenderer = GetComponent<SkinnedMeshRenderer>();

        if (ghostRenderer == null)
        {
            Debug.LogError("[GhostBoneShaderDriver] No SkinnedMeshRenderer found on this GameObject. " +
                           "Assign one manually or move this component onto the ghost mesh object.");
            enabled = false; // Disable Update() to avoid null-reference spam
            return;
        }

        // Obtain a per-instance material so writes to _BoneError don't affect
        // any other objects that share the same material asset in the project.
        ghostMat = ghostRenderer.material;

        // Resize the debug array to exactly match the number of configured bone pairs
        // (in case the default size of 11 doesn't match what was set in the Inspector)
        debugNormalizedErrors = new float[bonePairs.Length];
    }

    void Update()
    {
        // Early-out guards — nothing to do if setup is incomplete
        if (bonePairs == null || bonePairs.Length == 0) return;
        if (ghostMat == null) return;

        // Allocate the error array that will be uploaded to the shader each frame.
        // We use a local array rather than a class field to keep the data immutable
        // between calculation and upload within the same frame.
        float[] errors = new float[bonePairs.Length];

        for (int i = 0; i < bonePairs.Length; i++)
        {
            var pair = bonePairs[i];

            // Skip incomplete pairs without logging warnings every frame
            if (pair == null || pair.realBone == null || pair.ghostBone == null)
            {
                errors[i] = 0f; // Treat missing bones as zero error
                continue;
            }

            // ── Step 1: Compute raw world-space positional error ──────────────
            // Distance is measured in world space so it's unaffected by scale differences
            // between the real and ghost rigs.
            pair.currentError = Vector3.Distance(
                pair.realBone.position,
                pair.ghostBone.position
            );

            // ── Step 2: Select the effective threshold for this bone ──────────
            // Use the per-bone override if it has been set (> 0); otherwise fall
            // back to the shared globalThreshold.
            float threshold = (pair.overrideThreshold > 0f)
                ? pair.overrideThreshold
                : globalThreshold;

            // ── Step 3: Normalize the error to [0, 1] ─────────────────────────
            // 0 = bones perfectly aligned, 1 = drift has reached the threshold.
            // Clamp01 prevents values above 1 so the shader never receives out-of-range input.
            pair.normalizedError = Mathf.Clamp01(pair.currentError / threshold);

            // Convenience boolean for game logic that needs a simple "too far" flag
            pair.isTooFar = pair.currentError > threshold;

            // Write into the upload array and the Inspector-visible debug array
            errors[i]                = pair.normalizedError;
            debugNormalizedErrors[i] = pair.normalizedError;
        }

        // ── Step 4: Upload the error data to the GPU ─────────────────────────
        // The shader reads _BoneError as a float array indexed by bone number,
        // and _BoneCount tells it how many entries are valid.
        ghostMat.SetFloatArray(ShaderPropBoneError, errors);
        ghostMat.SetInt(ShaderPropBoneCount, bonePairs.Length);
    }

    // ── Scene-view Gizmos ────────────────────────────────────────────────────

    /// <summary>
    /// Draws debug visualizations in the Scene view (not visible in the Game view):
    ///   - A line between each real bone and its ghost counterpart.
    ///   - A small wire sphere at the ghost bone's position.
    ///   - A text label showing the current error in millimetres.
    /// Line and sphere color lerps from green (no error) to red (at or beyond threshold).
    /// </summary>
    void OnDrawGizmos()
    {
        if (bonePairs == null) return;

        for (int i = 0; i < bonePairs.Length; i++)
        {
            var pair = bonePairs[i];
            if (pair == null || pair.realBone == null || pair.ghostBone == null) continue;

            // Lerp color from green → red based on how close we are to the threshold
            Gizmos.color = Color.Lerp(Color.green, Color.red, pair.normalizedError);

            // Draw a line connecting the real bone to the ghost bone so you can
            // see the direction and magnitude of drift at a glance
            Gizmos.DrawLine(pair.realBone.position, pair.ghostBone.position);

            // Small sphere at the ghost bone position — radius 4 mm
            Gizmos.DrawWireSphere(pair.ghostBone.position, 0.004f);

            // Display the raw error in millimetres next to each ghost bone.
            // Millimetres are more readable than metres for the small distances involved.
            // This block is Editor-only and compiled out in builds.
#if UNITY_EDITOR
            UnityEditor.Handles.Label(
                pair.ghostBone.position + Vector3.up * 0.01f,
                $"B{i}: {pair.currentError * 1000f:F1}mm"  // Convert metres → mm for display
            );
#endif
        }
    }
}