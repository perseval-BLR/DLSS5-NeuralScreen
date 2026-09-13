# Optical Flow API headers

Unmodified NVIDIA Optical Flow SDK 5.0 interface headers, copyright NVIDIA
2020-2023. Each header contains its MIT license and attribution.

Public source mirror, pinned to commit
`54e68293b4898a530bc07e4d7df71efbc5d30f9b`:

- https://github.com/mbucchia/Optical-Flow-SDK/blob/54e68293b4898a530bc07e4d7df71efbc5d30f9b/NvOFInterface/nvOpticalFlowCommon.h
- https://github.com/mbucchia/Optical-Flow-SDK/blob/54e68293b4898a530bc07e4d7df71efbc5d30f9b/NvOFInterface/nvOpticalFlowD3D12.h

The header license does not imply a license to redistribute SDK binaries.
No Optical Flow runtime is bundled: the worker loads `nvofapi64.dll` from
Windows System32, where the NVIDIA display driver installs it.

API reference:
https://docs.nvidia.com/video-technologies/optical-flow-sdk/nvofa-programming-guide/index.html
