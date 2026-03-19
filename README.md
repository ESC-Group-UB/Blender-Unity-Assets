Folder Structure
Assets/models

FBX/TongueBond.fbx
FBX version of the tongue model. Recommended for stable use in Unity.

GLTF-GLB/TongueBond.glb
GLB version of the tongue model. Includes mesh, textures, armature, and shape keys in a single file.

Blender
client

Blender_Socket_Client.py
Sends data from Blender to an external application (e.g., Unity).

server

Blender_socket_server.py
Receives data in Blender and can be used to drive model movement or testing.

Unity
CheckThreshold

GhostBoneShaderDriver.cs
Unity script that monitors the difference between real bones and ghost bones, and changes shader color when the deviation exceeds a threshold (e.g., turning red when too far).

client

Unity_Socket_Client.py
Receives data in Unity from Blender or another source.

server

SocketTongueBoneDriver.cs
Unity C# script that applies incoming data to control tongue bones or animations.