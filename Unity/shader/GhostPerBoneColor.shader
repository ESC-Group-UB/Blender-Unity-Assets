Shader "Custom/GhostPerBoneColor"
{
    // ── Overview ─────────────────────────────────────────────────────────────
    // This shader visualizes per-bone positional error on a ghost (secondary)
    // skinned mesh, driven at runtime by GhostBoneShaderDriver.cs.
    //
    // Each frame, the C# script uploads a float array (_BoneError[11]) where
    // each element holds a normalized error value [0, 1] for one bone:
    //   0.0 = ghost bone perfectly matches the real bone
    //   1.0 = ghost bone has drifted to or beyond its distance threshold
    //
    // In the vertex shader, the four skinning influences (bone indices + weights)
    // stored per-vertex by Unity's skinning system are used to compute a
    // weighted-average error for that vertex.  The result is interpolated across
    // the triangle and used in the fragment shader to lerp between two colors,
    // giving an intuitive green-to-red heat map directly on the mesh surface.
    //
    // Rendering characteristics:
    //   - Transparent / additive-friendly (SrcAlpha OneMinusSrcAlpha blending)
    //   - Double-sided (Cull Off) so the ghost body reads clearly from any angle
    //   - No depth write (ZWrite Off) to avoid occluding geometry behind it

    Properties
    {
        // Color displayed when a bone's error is 0 (ghost matches real skeleton exactly)
        _NormalColor  ("Normal Color",  Color) = (0.2, 1.0, 0.2, 0.25)

        // Color displayed when a bone's error reaches 1 (ghost has drifted beyond threshold)
        _TooFarColor  ("Too Far Color", Color) = (1.0, 0.0, 0.0, 0.25)
    }

    SubShader
    {
        // ── Render queue and type ─────────────────────────────────────────────
        // "Transparent" queue (3000) ensures this ghost mesh is drawn after all
        // opaque geometry, so alpha blending composites correctly over the scene.
        // RenderType = Transparent allows replacement shaders and render features
        // (e.g. SSAO, custom render passes) to treat this object appropriately.
        Tags { "Queue"="Transparent" "RenderType"="Transparent" }
        LOD 100

        Pass
        {
            // Standard alpha blending: output = src.rgb * src.a + dst.rgb * (1 - src.a)
            Blend SrcAlpha OneMinusSrcAlpha

            // Do not write to the depth buffer — ghost mesh should not occlude other objects
            ZWrite Off

            // Render both front and back faces so the ghost volume is clearly visible
            // from inside the mouth or from any camera angle
            Cull Off

            CGPROGRAM
            #pragma vertex   vert
            #pragma fragment frag
            #pragma multi_compile_fog   // Enables Unity's built-in fog support
            #include "UnityCG.cginc"    // Unity helper macros (UnityObjectToClipPos, fog, etc.)

            // ── Uniforms set by GhostBoneShaderDriver.cs every frame ──────────
            //
            // _BoneError[i] holds the normalized positional error for bone i:
            //   0.0 = no drift, 1.0 = at or beyond the per-bone distance threshold.
            // The array is fixed at 11 slots to match the 11-bone tongue chain
            // (Bone through Bone.010).  Unused slots remain 0.
            //
            // _BoneCount tells the shader how many entries in _BoneError are valid,
            // so we can skip out-of-range bone indices safely.
            float _BoneError[11];
            int   _BoneCount;

            // Material color properties (set in the Inspector or via script)
            float4 _NormalColor;
            float4 _TooFarColor;

            // ── Vertex input structure ────────────────────────────────────────
            struct appdata
            {
                float4 vertex      : POSITION;      // Object-space vertex position

                // Unity's skinning system writes the four strongest bone influences
                // for each vertex into these two semantics before the vertex shader runs:
                //   BLENDWEIGHTS — four normalized weights (sum = 1.0)
                //   BLENDINDICES — indices into the skeleton's bone array
                // Together they describe how much each of up to four bones contributes
                // to this vertex's final world-space position.
                float4 boneWeights : BLENDWEIGHTS;
                uint4  boneIndices : BLENDINDICES;
            };

            // ── Vertex-to-fragment interpolation structure ────────────────────
            struct v2f
            {
                float4 pos      : SV_POSITION;  // Clip-space position (required output)
                float  errorVal : TEXCOORD0;    // Weighted bone error [0, 1] for this vertex
                UNITY_FOG_COORDS(1)             // Fog interpolator packed into TEXCOORD1
            };

            // ── Vertex shader ─────────────────────────────────────────────────
            // Transforms the vertex to clip space and computes a single per-vertex
            // error value by blending the errors of up to four influencing bones,
            // weighted by the same skinning weights Unity used for deformation.
            //
            // This means that a vertex on the tip of the tongue (influenced mostly
            // by tip bones) will turn red when only the tip bones drift, while a
            // vertex at the root stays green — giving spatially accurate feedback.
            v2f vert (appdata v)
            {
                v2f o;
                o.pos = UnityObjectToClipPos(v.vertex);

                // Accumulate the weighted bone errors for all four skinning influences.
                // The guard (bi < _BoneCount) prevents reading out-of-bounds array slots
                // on hardware that doesn't clamp array accesses automatically.
                float err = 0.0;

                // Influence 0 — strongest bone (stored in .x components)
                uint  bi0 = v.boneIndices.x;
                float bw0 = v.boneWeights.x;
                if (bi0 < (uint)_BoneCount) err += _BoneError[bi0] * bw0;

                // Influence 1 — second bone
                uint  bi1 = v.boneIndices.y;
                float bw1 = v.boneWeights.y;
                if (bi1 < (uint)_BoneCount) err += _BoneError[bi1] * bw1;

                // Influence 2 — third bone
                uint  bi2 = v.boneIndices.z;
                float bw2 = v.boneWeights.z;
                if (bi2 < (uint)_BoneCount) err += _BoneError[bi2] * bw2;

                // Influence 3 — weakest / fourth bone (stored in .w components)
                uint  bi3 = v.boneIndices.w;
                float bw3 = v.boneWeights.w;
                if (bi3 < (uint)_BoneCount) err += _BoneError[bi3] * bw3;

                // saturate clamps the final weighted sum to [0, 1].
                // Theoretically the sum should already be in range (since weights sum to 1
                // and errors are pre-clamped), but floating-point drift can push it slightly
                // outside — saturate is cheap insurance.
                o.errorVal = saturate(err);

                // Transfer fog density to the v2f struct for application in the fragment shader
                UNITY_TRANSFER_FOG(o, o.pos);
                return o;
            }

            // ── Fragment shader ───────────────────────────────────────────────
            // Produces the final pixel color by linearly interpolating between
            // _NormalColor (green, low error) and _TooFarColor (red, high error)
            // based on the per-vertex error value interpolated across the triangle.
            //
            // The alpha channel is also interpolated, so regions near the threshold
            // can become more opaque if the two colors have different alpha values.
            fixed4 frag (v2f i) : SV_Target
            {
                // lerp(a, b, t): returns a when t=0 (_NormalColor), b when t=1 (_TooFarColor)
                fixed4 col = lerp(_NormalColor, _TooFarColor, i.errorVal);

                // Apply Unity's scene fog on top of the blended color
                UNITY_APPLY_FOG(i.fogCoord, col);

                return col;
            }

            ENDCG
        }
    }

    // If this shader is not supported (e.g. very old hardware), fall back to
    // Unity's built-in Transparent/Diffuse so the mesh still renders rather than disappearing.
    FallBack "Transparent/Diffuse"
}