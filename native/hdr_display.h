#pragma once
#include <windows.h>
#include <cstring>
#include <vector>

struct HdrDisplayInfo
{
    bool enabled = false;
    float white = 1.0f; // scRGB units (80 nits), not nits
};

static bool HdrEnabled()
{
    static const bool enabled = [] {
        char value[16] = {};
        GetEnvironmentVariableA("NS_HDR", value, sizeof(value));
        return strcmp(value, "0") != 0;
    }();
    return enabled;
}

static HdrDisplayInfo QueryHdrDisplay(HMONITOR monitor)
{
    HdrDisplayInfo result;
    MONITORINFOEXW mi = {};
    mi.cbSize = sizeof(mi);
    if (!GetMonitorInfoW(monitor, &mi)) return result;
    // Topology can change between the size query and QueryDisplayConfig.
    for (int attempt = 0; attempt < 3; ++attempt)
    {
        UINT32 np = 0, nm = 0;
        if (GetDisplayConfigBufferSizes(QDC_ONLY_ACTIVE_PATHS, &np, &nm) != ERROR_SUCCESS)
            return result;
        std::vector<DISPLAYCONFIG_PATH_INFO> paths(np);
        std::vector<DISPLAYCONFIG_MODE_INFO> modes(nm);
        LONG rc = QueryDisplayConfig(QDC_ONLY_ACTIVE_PATHS, &np, paths.data(), &nm, modes.data(), nullptr);
        if (rc == ERROR_INSUFFICIENT_BUFFER) continue;
        if (rc != ERROR_SUCCESS) return result;
        for (UINT32 i = 0; i < np; ++i)
        {
            const auto &p = paths[i];
            DISPLAYCONFIG_SOURCE_DEVICE_NAME source = {};
            source.header = {DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME, sizeof(source),
                             p.sourceInfo.adapterId, p.sourceInfo.id};
            if (DisplayConfigGetDeviceInfo(&source.header) != ERROR_SUCCESS ||
                wcscmp(source.viewGdiDeviceName, mi.szDevice) != 0) continue;
            DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO color = {};
            color.header = {DISPLAYCONFIG_DEVICE_INFO_GET_ADVANCED_COLOR_INFO, sizeof(color),
                            p.targetInfo.adapterId, p.targetInfo.id};
            if (DisplayConfigGetDeviceInfo(&color.header) == ERROR_SUCCESS)
                result.enabled = color.advancedColorEnabled && !color.wideColorEnforced;
            DISPLAYCONFIG_SDR_WHITE_LEVEL white = {};
            white.header = {DISPLAYCONFIG_DEVICE_INFO_GET_SDR_WHITE_LEVEL, sizeof(white),
                            p.targetInfo.adapterId, p.targetInfo.id};
            if (DisplayConfigGetDeviceInfo(&white.header) == ERROR_SUCCESS && white.SDRWhiteLevel > 0)
                result.white = white.SDRWhiteLevel / 1000.0f;
            return result;
        }
        return result;
    }
    return result;
}
