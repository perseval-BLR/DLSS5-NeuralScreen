// dlss5-feed-host64 - the 64-bit half of DLSS5-Feeder for 32-bit games.
//
// A 32-bit game cannot load NGX or the DLSS 5 add-on (both x64-only). This little
// process can: it puts ReShade x64 (dxgi.dll) and renodx-dlss5.addon64 next to
// itself, opens a hidden 1x1 window with a minimal D3D12 swapchain -- so from the
// DLSS 5 add-on's point of view it IS a D3D12 game -- and runs the NGX DLAA
// evaluate on frames the game delivers through cross-process shared textures
// (created game-side on D3D11; see the phase-0 spike) and shared fences.
//
//   dlss5-feed-host64.exe --test   stand-alone: synthetic pattern, no game needed
//                                  (phase-1 proof: "feature 18 created" in ReShade.log)
//   dlss5-feed-host64.exe <pid>    serve the game with that PID over the pipe
//
// Logs to dlss5-feed-host.log next to the exe; the DLSS 5 add-on's own state
// appears in the host's ReShade.log.

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <d3d11.h>
#include <d3d11_4.h>
#include <dwmapi.h>
// Windows Graphics Capture - the per-window input (WGCW). C++/WinRT needs
// C++17, which is why build-host.bat carries /std:c++17.
#include <winrt/base.h>
#include <winrt/Windows.Foundation.h>
#include <winrt/Windows.Graphics.h>
#include <winrt/Windows.Graphics.Capture.h>
#include <winrt/Windows.Graphics.DirectX.h>
#include <winrt/Windows.Graphics.DirectX.Direct3D11.h>
#include <windows.graphics.capture.interop.h>
#include <windows.graphics.directx.direct3d11.interop.h>
#include <d3d12.h>
#include <dxgi1_4.h>
#include <dxgi1_6.h>   // IDXGIOutput6: the captured display's colour space (HDR)
#include <d3dcompiler.h>
#include "spout_bridge.h"
#include <cstdio>
#include <cstdarg>
#include <cstdint>
#include <cstring>
#include <algorithm>
#include <fcntl.h>
#include <io.h>
#include <string>
#include <vector>
#include "hdr_display.h"
#include "hdr_shaders.h"

#include <nvsdk_ngx.h>
#include <nvsdk_ngx_helpers.h>

#include "../src/feed_ipc.h"

// ---------------------------------------------------------------------------
// Logging
// ---------------------------------------------------------------------------

static char g_log_path[MAX_PATH];
static bool g_show_window = false;   // visible host window = the user's door to the DLSS 5 panel
static bool g_renodx_lazy = false;   // DLSS 5 add-on is v45+ (per-present rescan, lazy adoption)
static bool g_video_mode = false;    // stdin/stdout are a binary frame protocol in this mode

static void Log(const char *fmt, ...);

// Detect the DLSS 5 add-on generation next to this exe: v45+ ('EnableHooks' marker in
// the binary) rescans every present and adopts missed features lazily, so the warm-up
// re-create is unnecessary -- and its EnableHooks key should be '2' (NGX-only) for this
// feeder, written into OUR ReShade.ini before ReShade loads and the add-on reads it.
static void DetectRenodxAddon()
{
    char dir[MAX_PATH], path[MAX_PATH], ini[MAX_PATH];
    GetModuleFileNameA(nullptr, dir, MAX_PATH);
    if (char *s = strrchr(dir, '\\')) *(s + 1) = '\0';
    sprintf_s(path, "%srenodx-dlss5.addon64", dir);
    sprintf_s(ini, "%sReShade.ini", dir);

    HANDLE f = CreateFileA(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr, OPEN_EXISTING, 0, nullptr);
    if (f == INVALID_HANDLE_VALUE) { Log("[host] renodx-dlss5.addon64 not found next to the host"); return; }
    const DWORD size = GetFileSize(f, nullptr);
    DWORD got = 0;
    char *buf = (size > 0 && size < 8u * 1024 * 1024) ? static_cast<char *>(malloc(size)) : nullptr;
    if (buf != nullptr && ReadFile(f, buf, size, &got, nullptr) && got == size)
        for (DWORD i = 0; i + 11 < size; ++i)
            if (memcmp(buf + i, "EnableHooks", 11) == 0) { g_renodx_lazy = true; break; }
    free(buf);
    CloseHandle(f);

    char ver[48] = "?";
    DWORD dummy = 0;
    const DWORD vsize = GetFileVersionInfoSizeA(path, &dummy);
    if (vsize > 0)
    {
        void *vdata = malloc(vsize);
        VS_FIXEDFILEINFO *ffi = nullptr;
        UINT flen = 0;
        if (vdata != nullptr && GetFileVersionInfoA(path, 0, vsize, vdata) &&
            VerQueryValueA(vdata, "\\", reinterpret_cast<void **>(&ffi), &flen) && ffi != nullptr)
            sprintf_s(ver, "%u.%u.%u.%u", HIWORD(ffi->dwFileVersionMS), LOWORD(ffi->dwFileVersionMS),
                      HIWORD(ffi->dwFileVersionLS), LOWORD(ffi->dwFileVersionLS));
        free(vdata);
    }
    Log("[host] DLSS 5 add-on: v%s -- %s engine", ver,
        g_renodx_lazy ? "v45+ (lazy adoption; warm-up skipped)" : "classic (warm-up stays on)");

    if (g_renodx_lazy)
    {
        char v[16] = {};
        GetPrivateProfileStringA("RenoDX.DLSS5", "EnableHooks", "", v, sizeof(v), ini);
        if (v[0] == '\0')
        {
            WritePrivateProfileStringA("RenoDX.DLSS5", "EnableHooks", "2", ini);
            Log("[host] EnableHooks was unset; wrote EnableHooks=2 into the host's ReShade.ini");
        }
        else
            Log("[host] EnableHooks=%s (user-set; leaving it alone)", v);
    }
}

static void Log(const char *fmt, ...)
{
    char line[2048];
    va_list ap;
    va_start(ap, fmt);
    _vsnprintf_s(line, sizeof(line), _TRUNCATE, fmt, ap);
    va_end(ap);
    SYSTEMTIME st;
    GetLocalTime(&st);
    FILE *console = g_video_mode ? stderr : stdout;
    fprintf(console, "%02u:%02u:%02u.%03u  %s\n", st.wHour, st.wMinute, st.wSecond, st.wMilliseconds, line);
    fflush(console);
    FILE *f = nullptr;
    if (!g_video_mode && fopen_s(&f, g_log_path, "a") == 0 && f != nullptr)
    {
        fprintf(f, "%02u:%02u:%02u.%03u  %s\n", st.wHour, st.wMinute, st.wSecond, st.wMilliseconds, line);
        fclose(f);
    }
}

static const char *NgxResultName(NVSDK_NGX_Result r)
{
    switch (static_cast<unsigned>(r))
    {
    case 0x1:        return "Success";
    case 0xBAD00001: return "FeatureNotSupported";
    // What the feature library answers when the call did not leave a module
    // whose path contains "nvngx.dll" - the commonest way for a broken
    // install to fail, and it used to print as "?".
    case 0xBAD00002: return "PlatformError";
    case 0xBAD00003: return "FeatureAlreadyExists";
    case 0xBAD00004: return "FeatureNotFound";
    case 0xBAD00005: return "InvalidParameter";
    case 0xBAD00007: return "NotInitialized";
    case 0xBAD00008: return "UnsupportedInputFormat";
    case 0xBAD0000A: return "MissingInput";
    case 0xBAD0000B: return "UnableToInitializeFeature";
    case 0xBAD0000D: return "OutOfGPUMemory";
    case 0xBAD0000E: return "UnsupportedFormat";
    default:         return "?";
    }
}

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

struct Host
{
    HWND                       hwnd;
    IDXGISwapChain1           *swap;
    ID3D12Device              *dev;
    ID3D12CommandQueue        *queue;      // NGX work
    ID3D12CommandQueue        *pump_queue; // owns the dummy swapchain
    ID3D12GraphicsCommandList *list;
    static const int           kFrames = 3;
    ID3D12CommandAllocator    *alloc[kFrames];
    UINT64                     alloc_fence[kFrames];
    int                        frame_slot;
    ID3D12Fence               *fence;      // internal (allocator ring)
    HANDLE                     fence_event;
    UINT64                     fence_value;

    // cross-process
    ID3D12Fence *fence_in;   // game signals, host waits
    ID3D12Fence *fence_out;  // host signals, game waits

    bool                 ngx_inited;
    NVSDK_NGX_Parameter *params;
    NVSDK_NGX_Handle    *feature;

    ID3D12Resource *tex[FEED_SLOTS];
    UINT            width, height;
    DXGI_FORMAT     color_fmt, output_fmt;
};

static Host h;

using PFN_NR_InitExt = NVSDK_NGX_Result (NVSDK_CONV *)(unsigned long long, const wchar_t *, ID3D12Device *, NVSDK_NGX_Version, const NVSDK_NGX_Parameter *);
using PFN_NR_Create = NVSDK_NGX_Result (NVSDK_CONV *)(ID3D12GraphicsCommandList *, NVSDK_NGX_Feature, const NVSDK_NGX_Parameter *, NVSDK_NGX_Handle **);
using PFN_NR_Evaluate = NVSDK_NGX_Result (NVSDK_CONV *)(ID3D12GraphicsCommandList *, const NVSDK_NGX_Handle *, const NVSDK_NGX_Parameter *, PFN_NVSDK_NGX_ProgressCallback);
using PFN_NR_Release = NVSDK_NGX_Result (NVSDK_CONV *)(NVSDK_NGX_Handle *);
static HMODULE g_nr_module;
static PFN_NR_InitExt g_nr_init_ext;
static PFN_NR_Create g_nr_create;
static PFN_NR_Evaluate g_nr_evaluate;
static PFN_NR_Release g_nr_release;
static uint32_t g_create_result;
static uint32_t g_eval_count;


// ---------------------------------------------------------------------------
// GPU architecture spoof for feature 18 (NS_ARCH_SPOOF=0 to disable)
//
// nvngx_dlssnr.dll refuses to create the feature on anything older than
// Blackwell: inside it there is a check with the message
// "DLSSNR: Unsupported GPU architecture 0x%x, minimum required 0x%x".
// Yet the compiled kernels in it cover Turing (sm_75), Ampere (sm_86), Ada
// (sm_89) and Blackwell (sm_120) -- across all fifteen fatbins, with no gaps.
// So the refusal is policy, not missing code.
//
// The library learns the architecture through nvapi: it loads nvapi64.dll,
// takes its single export nvapi_QueryInterface and asks for the id
// NvAPI_GPU_GetArchInfo. Replacing nvapi64.dll with our own copy next to the
// worker is useless: the D3D12 driver loads the real one from System32 first
// and the name is already taken (checked against the process module list).
// So we patch in the memory of our own process instead.
//
// The patch is installed with the bytes preserved: before calling the real
// function the prologue is restored and put back afterwards. That way no
// trampoline is needed (it would require decoding instruction lengths) and
// any GPU handle is served by the real code rather than by our guesses.
//
// Nothing in the NVIDIA files is modified: the edit lives only in memory.
// Enabled by default (NS_ARCH_SPOOF=0 to disable); on Blackwell it does
// nothing at all.
// ---------------------------------------------------------------------------
static constexpr unsigned NVAPI_ID_INITIALIZE = 0x0150E828u;
static constexpr unsigned NVAPI_ID_ENUM_GPUS  = 0xE5AC921Fu;
static constexpr unsigned NVAPI_ID_GET_ARCH   = 0xD8265D24u;

static constexpr unsigned NV_ARCH_TURING    = 0x160u;
static constexpr unsigned NV_ARCH_AMPERE    = 0x170u;
static constexpr unsigned NV_ARCH_HOPPER    = 0x180u;
static constexpr unsigned NV_ARCH_ADA       = 0x190u;
// The spoof target. This is NOT the NVAPI name table (Turing 0x160, Ampere
// 0x170, Ada 0x190, Blackwell GB1XX 0x1A0 / GB2XX 0x1B0 - see gpuinfo.py) -
// it is the value the feature DLL is willing to accept. The leaked runtimes
// (dcc0dc24 and the 310.8.SF family) refuse feature 18 below 0x1B0, which
// is why v1.4.1+ (spoof 0x1A0, from commit 7e186fb) regressed RTX 30/40
// while v1.3.0 (spoof 0x1B0) worked on every generation. Keep it 0x1B0.
static constexpr unsigned NV_ARCH_BLACKWELL = 0x1B0u;

struct NvArchInfo
{
    unsigned version, architecture, implementation, revision;
};

using PFN_NvQueryInterface = void *(__cdecl *)(unsigned);
using PFN_NvGetArchInfo = int (__cdecl *)(void *, NvArchInfo *);

// The answers are cached at startup, before the patch is installed: the GPU
// architecture does not change while the process runs, and the cache removes
// the very cause of the race - the hook no longer needs to lift the prologue
// to call the real function.
static const int         kArchMaxGpus = 64;
static void             *g_arch_handles[kArchMaxGpus];
static NvArchInfo        g_arch_cache[kArchMaxGpus];
static int               g_arch_count;
static bool              g_arch_patched;
static unsigned          g_arch_real_group;   // what it actually was

static int __cdecl ArchInfoHook(void *gpu, NvArchInfo *info);

static bool WriteCode(void *at, const void *src, size_t bytes)
{
    DWORD old = 0;
    if (!VirtualProtect(at, bytes, PAGE_EXECUTE_READWRITE, &old)) return false;
    memcpy(at, src, bytes);
    DWORD tmp = 0;
    VirtualProtect(at, bytes, old, &tmp);
    FlushInstructionCache(GetCurrentProcess(), at, bytes);
    return true;
}

// mov rax, imm64 ; jmp rax
static void *g_arch_target;

static bool ArchApplyPatch()
{
    BYTE code[12] = { 0x48, 0xB8 };
    void *dst = reinterpret_cast<void *>(&ArchInfoHook);
    memcpy(code + 2, &dst, sizeof(dst));
    code[10] = 0xFF; code[11] = 0xE0;
    if (!WriteCode(g_arch_target, code, sizeof(code)))
        return false;
    g_arch_patched = true;
    return true;
}

static int __cdecl ArchInfoHook(void *gpu, NvArchInfo *info)
{
    if (info == nullptr) return -1;              // NVAPI_ERROR
    const unsigned want = info->version;
    // Find the handle in the cache; an unknown handle gets the primary
    // card's answer - it is almost always the same GPU reached through
    // another API path (NvAPI_GPU_GetHandleFromDXGI etc.), and an honest
    // error here would kill CreateFeature for no reason.
    int idx = -1;
    for (int i = 0; i < g_arch_count; ++i)
        if (g_arch_handles[i] == gpu) { idx = i; break; }
    if (idx < 0) idx = 0;
    // Keep the structure version the caller asked for: V1 and V2 differ
    // only in that field, the size is the same.
    *info = g_arch_cache[idx];
    info->version = want;
    // Spoof ONLY the primary card (idx 0): on multi-GPU machines the
    // secondary NVIDIA card (and any non-NVIDIA iGPU reached through other
    // APIs) must keep its real architecture, or NGX may try to create the
    // feature on the wrong adapter.
    const unsigned group = info->architecture & 0xFFFFFFF0u;
    if (idx == 0 && (group == NV_ARCH_TURING || group == NV_ARCH_AMPERE
        || group == NV_ARCH_ADA))
    {
        // Lie consistently: implementation/revision were taken from a
        // live RTX 5070 Ti - the check may look at more than the group.
        info->architecture   = NV_ARCH_BLACKWELL;
        info->implementation = 0x3u;
        info->revision       = 0xA1u;
    }
    return 0;                                     // NVAPI_OK
}

static bool ArchSpoofRequested()
{
    // On by default: on Blackwell the hook disables itself, and on older
    // cards it is the only way to get feature 18. NS_ARCH_SPOOF=0 turns it
    // off explicitly.
    char buf[8] = {};
    const DWORD got = GetEnvironmentVariableA("NS_ARCH_SPOOF", buf, sizeof(buf));
    if (got > 0 && got < sizeof(buf) && buf[0] == '0') return false;
    return true;
}

// Returns: 0 - not needed, 1 - installed, -1 - failed.
static int SetupArchSpoof()
{
    if (!ArchSpoofRequested()) return 0;
    HMODULE nvapi = LoadLibraryW(L"nvapi64.dll");
    if (nvapi == nullptr)
    { Log("[arch] nvapi64.dll did not load, err=%lu", GetLastError()); return -1; }
    auto qi = reinterpret_cast<PFN_NvQueryInterface>(
        GetProcAddress(nvapi, "nvapi_QueryInterface"));
    if (qi == nullptr) { Log("[arch] no nvapi_QueryInterface"); return -1; }

    auto init = reinterpret_cast<int (__cdecl *)()>(qi(NVAPI_ID_INITIALIZE));
    auto enum_gpus = reinterpret_cast<int (__cdecl *)(void **, unsigned *)>(
        qi(NVAPI_ID_ENUM_GPUS));
    auto get_arch = reinterpret_cast<PFN_NvGetArchInfo>(qi(NVAPI_ID_GET_ARCH));
    if (init == nullptr || enum_gpus == nullptr || get_arch == nullptr)
    { Log("[arch] the nvapi functions did not resolve by id"); return -1; }
    if (init() != 0) { Log("[arch] NvAPI_Initialize failed"); return -1; }

    void *handles[kArchMaxGpus] = {};
    unsigned count = 0;
    if (enum_gpus(handles, &count) != 0 || count == 0)
    { Log("[arch] could not get the GPU list"); return -1; }
    if (count > static_cast<unsigned>(kArchMaxGpus)) count = kArchMaxGpus;

    // Take the answers for ALL cards before the patch: afterwards the real
    // function cannot be called, and the hook must answer for any handle.
    g_arch_count = 0;
    for (unsigned i = 0; i < count; ++i)
    {
        NvArchInfo one = {};
        one.version = static_cast<unsigned>(sizeof(NvArchInfo)) | (2u << 16);
        if (get_arch(handles[i], &one) != 0)
        {
            one.version = static_cast<unsigned>(sizeof(NvArchInfo)) | (1u << 16);
            if (get_arch(handles[i], &one) != 0) continue;
        }
        g_arch_handles[g_arch_count] = handles[i];
        g_arch_cache[g_arch_count] = one;
        ++g_arch_count;
    }
    if (g_arch_count == 0)
    { Log("[arch] GetArchInfo answered for no card at all"); return -1; }

    const NvArchInfo info = g_arch_cache[0];
    g_arch_real_group = info.architecture & 0xFFFFFFF0u;
    // NS_ARCH_FORCE=1 installs the patch even where it is not needed. It is
    // there for testing: on Blackwell the hook is otherwise never executed and
    // there would be no way to confirm it answers at all. On such a card no
    // spoofing happens - the hook returns the cached real values.
    char force[8] = {};
    const DWORD force_got = GetEnvironmentVariableA("NS_ARCH_FORCE", force,
                                                    sizeof(force));
    const bool forced = force_got > 0 && force_got < sizeof(force)
                        && force[0] == '1';
    if (g_arch_real_group >= NV_ARCH_BLACKWELL && !forced)
    {
        Log("[arch] architecture 0x%X is supported anyway - no spoof needed",
            info.architecture);
        return 0;
    }

    g_arch_target = reinterpret_cast<void *>(get_arch);
    if (!ArchApplyPatch())
    { Log("[arch] could not install the patch, err=%lu", GetLastError()); return -1; }
    Log("[arch] patch installed: %d cards cached, architecture 0x%X%s",
        g_arch_count, info.architecture,
        forced ? " (NS_ARCH_FORCE=1, no spoofing)"
               : " (spoofed to the DLL's accepted value)");
    return 1;
}

// Point the four NGX pointers at our forwarder instead of at the feature
// library itself.
//
// nvngx_dlssnr.dll decides whether to serve a call by the path of the module
// the call returns to: it must contain the substring "nvngx.dll". The process
// name is not looked at - measured, see native/ns_forwarder.cpp. That is the
// only reason this executable is named nvngx.dll today, and routing the calls
// through a module that carries the substring in its own file name removes it.
//
// On failure this returns false and the caller falls back to the direct path,
// which works only while the executable itself is named nvngx.dll. The reason
// always goes to the log - a missing forwarder must not look like "Neural
// Rendering is not supported on this card".
static bool LoadNrForwarder(const wchar_t *dll_name)
{
    // NS_FORWARDER=<path> points at a different module. It exists for the
    // test that proves the rule: the same binary copied to a name without the
    // substring must be refused. Without a way to aim the worker at that copy
    // the rule could only be asserted in a comment.
    wchar_t path[MAX_PATH] = {};
    if (GetEnvironmentVariableW(L"NS_FORWARDER", path, MAX_PATH) == 0)
    {
        GetModuleFileNameW(nullptr, path, MAX_PATH);
        if (wchar_t *s = wcsrchr(path, L'\\')) *(s + 1) = L'\0';
        wcsncat_s(path, L"nvngx.dll_ns-forwarder.dll", _TRUNCATE);
    }

    const HMODULE fwd = LoadLibraryW(path);
    if (fwd == nullptr)
    { Log("[pure] forwarder %ls did not load, err=%lu", path, GetLastError()); return false; }

    const auto load = reinterpret_cast<int (*)(const wchar_t *)>(GetProcAddress(fwd, "NsFwdLoad"));
    const auto where = reinterpret_cast<void (*)(wchar_t *, unsigned int)>(GetProcAddress(fwd, "NsFwdPath"));
    const auto init_ext = reinterpret_cast<PFN_NR_InitExt>(GetProcAddress(fwd, "NsFwdInitExt"));
    const auto create = reinterpret_cast<PFN_NR_Create>(GetProcAddress(fwd, "NsFwdCreate"));
    const auto evaluate = reinterpret_cast<PFN_NR_Evaluate>(GetProcAddress(fwd, "NsFwdEvaluate"));
    const auto release = reinterpret_cast<PFN_NR_Release>(GetProcAddress(fwd, "NsFwdRelease"));
    if (load == nullptr || init_ext == nullptr || create == nullptr
        || evaluate == nullptr || release == nullptr)
    { Log("[pure] the forwarder is missing exports - is it an old build?"); return false; }

    const int lr = load(dll_name);
    if (lr != 0)
    { Log("[pure] the forwarder could not load %ls (%d)", dll_name, lr); return false; }

    g_nr_module = fwd;
    g_nr_init_ext = init_ext;
    g_nr_create = create;
    g_nr_evaluate = evaluate;
    g_nr_release = release;

    wchar_t actual[MAX_PATH] = {};
    if (where != nullptr) where(actual, MAX_PATH);
    Log("[pure] NGX calls go through %ls", actual[0] != L'\0' ? actual : path);
    return true;
}

static bool InitDirectNr(const wchar_t *data_path)
{
    // Before the NVIDIA library is loaded: it asks for the architecture when
    // creating the feature, but we patch the prologue in advance, while none
    // of its threads exist in the process yet.
    SetupArchSpoof();
    // NS_NGX_CORE=<path to the driver's nvngx.dll>: load NGX Core before the
    // feature DLL.
    //
    // Historical: this experiment asked whether preloading the real NGX core
    // would let a differently named process through, on the suspicion that the
    // feature library only wanted a module called nvngx.dll to be PRESENT. It
    // never worked, and now we know why - the library wants the module that
    // CALLS it, not one that happens to be loaded. See LoadNrForwarder above;
    // the flag is kept because the preload costs nothing and the core is worth
    // having loaded first.
    wchar_t core[MAX_PATH] = {};
    if (GetEnvironmentVariableW(L"NS_NGX_CORE", core, MAX_PATH) > 0)
    {
        const HMODULE m = LoadLibraryW(core);
        Log("[pure] NGX core preload -> %s (err=%lu)",
            m != nullptr ? "loaded" : "FAILED", m != nullptr ? 0UL : GetLastError());
    }
    // NS_NGX_VIA_CORE=1: create and evaluate feature 18 through NGX Core
    // instead of calling the feature DLL directly.
    //
    // This was the other way out of the naming constraint, back when the
    // constraint looked like it was on the process. It is not needed for that
    // any more - the forwarder answers it - and creating feature 18 through
    // the core fails anyway (FAIL_UnableToInitializeFeature, every time). Kept
    // as a switch because the comparison is occasionally useful.
    char via[8] = {};
    const DWORD via_got = GetEnvironmentVariableA("NS_NGX_VIA_CORE", via, sizeof(via));
    if (via_got > 0 && via_got < sizeof(via) && via[0] == '1')
    {
        // The SDK declares the parameter block non-const where our pointers
        // take it const; the call itself is identical.
        g_nr_create = reinterpret_cast<PFN_NR_Create>(&NVSDK_NGX_D3D12_CreateFeature);
        g_nr_evaluate = reinterpret_cast<PFN_NR_Evaluate>(&NVSDK_NGX_D3D12_EvaluateFeature);
        g_nr_release = reinterpret_cast<PFN_NR_Release>(&NVSDK_NGX_D3D12_ReleaseFeature);
        Log("[pure] NS_NGX_VIA_CORE=1: feature 18 goes through NGX Core");
        return true;
    }
    // NS_NR_DLL=<path>: load a different runtime build without rebuilding
    // the worker (the swappable-runtime pattern from RHI's
    // dlss_manifest.json). The path is relative to the worker's directory
    // or absolute; the default is the bundled nvngx_dlssnr.dll.
    wchar_t dll_path[MAX_PATH] = {};
    const wchar_t *dll_name = L"nvngx_dlssnr.dll";
    if (GetEnvironmentVariableW(L"NS_NR_DLL", dll_path, MAX_PATH) > 0)
    {
        dll_name = dll_path;
        Log("[pure] NS_NR_DLL=%ls", dll_name);
    }
    // NS_NO_FORWARDER=1 keeps the old shape, where the calls leave this
    // executable - which the feature library serves only while the executable
    // is named nvngx.dll. Kept for comparison, and as a way out on a machine
    // that will not load the forwarder for some reason of its own.
    char nofwd[8] = {};
    const DWORD nofwd_got = GetEnvironmentVariableA("NS_NO_FORWARDER", nofwd, sizeof(nofwd));
    const bool asked_direct = nofwd_got > 0 && nofwd_got < sizeof(nofwd) && nofwd[0] == '1';
    if (asked_direct) Log("[pure] NS_NO_FORWARDER=1: calling the feature library from the worker");
    const bool direct = asked_direct || !LoadNrForwarder(dll_name);
    if (direct)
    {
        g_nr_module = LoadLibraryW(dll_name);
        if (!g_nr_module) { Log("[pure] LoadLibrary(%ls) failed %lu", dll_name, GetLastError()); return false; }
        g_nr_init_ext = reinterpret_cast<PFN_NR_InitExt>(GetProcAddress(g_nr_module, "NVSDK_NGX_D3D12_Init_Ext"));
        g_nr_create = reinterpret_cast<PFN_NR_Create>(GetProcAddress(g_nr_module, "NVSDK_NGX_D3D12_CreateFeature"));
        g_nr_evaluate = reinterpret_cast<PFN_NR_Evaluate>(GetProcAddress(g_nr_module, "NVSDK_NGX_D3D12_EvaluateFeature"));
        g_nr_release = reinterpret_cast<PFN_NR_Release>(GetProcAddress(g_nr_module, "NVSDK_NGX_D3D12_ReleaseFeature"));
        if (!g_nr_init_ext || !g_nr_create || !g_nr_evaluate || !g_nr_release)
        { Log("[pure] missing direct exports in nvngx_dlssnr.dll: Init_Ext=%s Create=%s Evaluate=%s Release=%s (GetLastError=%lu)",
             g_nr_init_ext ? "ok" : "MISSING", g_nr_create ? "ok" : "MISSING",
             g_nr_evaluate ? "ok" : "MISSING", g_nr_release ? "ok" : "MISSING",
             GetLastError()); return false; }
    }
    const auto r = g_nr_init_ext(0x1000000ULL, data_path, h.dev, NVSDK_NGX_Version_API, h.params);
    Log("[pure] DLSSNR Init_Ext (%s) -> 0x%08X (%s)",
        direct ? "from the worker" : "through the forwarder", r, NgxResultName(r));
    return NVSDK_NGX_SUCCEED(r);
}

// ---------------------------------------------------------------------------
// Command submission (allocator ring), same shape as the add-on
// ---------------------------------------------------------------------------

static void LogDeviceRemoved(const char *where);   // defined below BeginCommands
static bool WaitFenceValue(ID3D12Fence *f, UINT64 v, DWORD ms);  // defined below

static bool BeginCommands()
{
    const int slot = h.frame_slot;
    const UINT64 retire = h.alloc_fence[slot];
    if (retire != 0 && h.fence->GetCompletedValue() < retire)
    {
        // Through the same helper as every other wait: this one carried a
        // second copy of the shared-event bug, and it also left a
        // registration behind on a timeout for the next wait to trip over.
        if (!WaitFenceValue(h.fence, retire, 2000))
        { Log("[host] GPU did not retire allocator slot %d", slot); return false; }
    }
    if (FAILED(h.alloc[slot]->Reset()))
    {
        // The allocator Reset() is where a removed device surfaces first
        // (issue #1: Win10 TDR, "BeginCommands err=0"). Log the reason and
        // the DRED breadcrumbs - without them the log says only "code 6".
        LogDeviceRemoved("allocator reset");
        return false;
    }
    return SUCCEEDED(h.list->Reset(h.alloc[slot], nullptr));
}

// Log the device-removed reason plus DRED breadcrumbs, once per removal.
static void LogDeviceRemoved(const char *where)
{
    if (h.dev == nullptr) return;
    const HRESULT reason = h.dev->GetDeviceRemovedReason();
    Log("[host] device removed at %s: reason 0x%08X", where, (unsigned)reason);
    ID3D12DeviceRemovedExtendedData1 *dred = nullptr;
    if (FAILED(h.dev->QueryInterface(__uuidof(ID3D12DeviceRemovedExtendedData1),
                                     reinterpret_cast<void **>(&dred))))
        return;
    D3D12_DRED_AUTO_BREADCRUMBS_OUTPUT crumbs = {};
    if (SUCCEEDED(dred->GetAutoBreadcrumbsOutput(&crumbs)) && crumbs.pHeadAutoBreadcrumbNode)
    {
        const D3D12_AUTO_BREADCRUMB_NODE *n = crumbs.pHeadAutoBreadcrumbNode;
        Log("[host] DRED: %u breadcrumbs, last value %u, command list %p",
            n->BreadcrumbCount,
            n->pLastBreadcrumbValue ? *n->pLastBreadcrumbValue : 0u,
            (void *)n->pCommandList);
    }
    D3D12_DRED_PAGE_FAULT_OUTPUT pf = {};
    if (SUCCEEDED(dred->GetPageFaultAllocationOutput(&pf)) && pf.PageFaultVA != 0)
        Log("[host] DRED: page fault at VA 0x%llX", (unsigned long long)pf.PageFaultVA);
    dred->Release();
}

static UINT64 EndCommands()
{
    h.list->Close();
    ID3D12CommandList *lists[] = { h.list };
    h.queue->ExecuteCommandLists(1, lists);
    const UINT64 v = ++h.fence_value;
    // Signal can fail with DXGI_ERROR_DEVICE_REMOVED - the fence value
    // would then never complete and every wait would time out. Surface
    // the failure instead of pretending the commands were submitted
    // (code review finding).
    const HRESULT sig = h.queue->Signal(h.fence, v);
    if (FAILED(sig)) { Log("[host] queue Signal failed 0x%08X", sig); return 0; }
    h.alloc_fence[h.frame_slot] = v;
    h.frame_slot = (h.frame_slot + 1) % Host::kFrames;
    return v;
}

static bool WaitFenceValue(ID3D12Fence *f, UINT64 v, DWORD ms)
{
    // 0 means EndCommands could not submit (queue Signal failed) - there
    // is nothing to wait for, and waiting on 0 would be a false success
    // (the fence is already at 0 or beyond).
    if (v == 0) return false;
    // UINT64_MAX is the device-removed marker: GetCompletedValue returns
    // it when the device is gone, and "completed >= v" would then be a
    // false success (code review finding). Check it first.
    if (f->GetCompletedValue() == UINT64_MAX) return false;
    if (f->GetCompletedValue() >= v) return true;
    // One auto-reset event serves every wait in this process - several
    // fences among them - and SetEventOnCompletion is NOT cancelled when a
    // wait returns. So the event can arrive already signalled by a
    // registration made for an older value, or be signalled by one while
    // this wait is asleep. Returning false on such a wake was the bug: the
    // next wait inherited the next stale signal and the pipeline stayed
    // exactly one completion out of step, for good. In the wild that read
    // as 2766 consecutive "[cap] swizzle fence timeout" lines, every one of
    // them 30 ms apart rather than the 10 s the timeout asks for, ending
    // only when a monitor switch tore the pipeline down and built it again
    // (issue #33: "NR worked on the second try").
    //
    // So: drop any stale signal, re-check, and treat an early wake as what
    // it is - not this wait's completion. Keep waiting until the value is
    // really reached or the deadline passes.
    ResetEvent(h.fence_event);
    if (f->GetCompletedValue() >= v) return true;
    if (FAILED(f->SetEventOnCompletion(v, h.fence_event))) return false;
    const ULONGLONG deadline = GetTickCount64() + ms;
    for (;;)
    {
        const ULONGLONG now_ms = GetTickCount64();
        const DWORD left = now_ms >= deadline ? 0 : (DWORD)(deadline - now_ms);
        if (WaitForSingleObject(h.fence_event, left) != WAIT_OBJECT_0) return false;
        const UINT64 now = f->GetCompletedValue();
        if (now == UINT64_MAX) return false;
        if (now >= v) return true;
    }
}

static void CloseListGuarded()
{
    __try { h.list->Close(); } __except (EXCEPTION_EXECUTE_HANDLER) {}
}

static void AbortCommands()   // never execute a list NGX crashed in
{
    if (h.list == nullptr) return;
    CloseListGuarded();
    h.list->Release();
    h.list = nullptr;
    if (SUCCEEDED(h.dev->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, h.alloc[h.frame_slot], nullptr,
                                           __uuidof(ID3D12GraphicsCommandList), reinterpret_cast<void **>(&h.list))))
        h.list->Close();
}

static NVSDK_NGX_Result SafeCreateDLSS(NVSDK_NGX_DLSS_Create_Params *cp, DWORD *code)
{
    *code = 0;
    __try { return NGX_D3D12_CREATE_DLSS_EXT(h.list, 1, 1, &h.feature, h.params, cp); }
    __except (EXCEPTION_EXECUTE_HANDLER) { *code = GetExceptionCode(); return static_cast<NVSDK_NGX_Result>(0x7FFFFFFF); }
}

static NVSDK_NGX_Result SafeEvaluateDLSS(NVSDK_NGX_D3D12_DLSS_Eval_Params *ep, DWORD *code)
{
    *code = 0;
    __try { return NGX_D3D12_EVALUATE_DLSS_EXT(h.list, h.feature, h.params, ep); }
    __except (EXCEPTION_EXECUTE_HANDLER) { *code = GetExceptionCode(); return static_cast<NVSDK_NGX_Result>(0x7FFFFFFF); }
}

static void SafeReleaseFeature(NVSDK_NGX_Handle *f)
{
    if (f == nullptr) return;
    // Give the handle back to whoever issued it. The feature is created
    // through nvngx_dlssnr.dll (g_nr_create), and this released it through
    // the NGX CORE instead - a different implementation, which knows nothing
    // about that handle. Nothing was freed: every recreate left the model
    // and its scratch behind, ~420 MB a time, and dragging a slider (one
    // RNSZ per step) filled a 16 GB card in half a minute (issue #48,
    // measured: +12.6 GB over 30 rebuilds).
    //
    // g_nr_release is set on both paths - to the forwarder on the direct
    // one, to the core's own function under NS_NGX_VIA_CORE - so this is
    // simply "the matching release", not a special case.
    __try
    {
        if (g_nr_release != nullptr) g_nr_release(f);
        else NVSDK_NGX_D3D12_ReleaseFeature(f);
    }
    __except (EXCEPTION_EXECUTE_HANDLER) { Log("[host] ReleaseFeature raised 0x%08X (ignored)", GetExceptionCode()); }
}

// ---------------------------------------------------------------------------
// The disguise: hidden window + minimal D3D12 swapchain so ReShade x64 loads
// and the DLSS 5 add-on arms itself, exactly as in a real D3D12 game.
// ---------------------------------------------------------------------------

static LRESULT CALLBACK WndProc(HWND w, UINT m, WPARAM wp, LPARAM lp)
{
    if (m == WM_CLOSE) { ShowWindow(w, SW_HIDE); return 0; }   // closing only hides; the feed lives on
    return DefWindowProcW(w, m, wp, lp);
}

// --- banner: "32-bit DLSS 5 Feeder" rendered once with GDI, copied into every frame ---

static ID3D12Resource             *g_banner;
static IDXGISwapChain3            *g_swap3;
static ID3D12CommandAllocator     *g_pump_alloc;
static ID3D12GraphicsCommandList  *g_pump_list;
static ID3D12Fence                *g_pump_fence;
static UINT64                      g_pump_val;
static HANDLE                      g_pump_ev;

static bool BeginCommands();
static UINT64 EndCommands();
static bool WaitFenceValue(ID3D12Fence *f, UINT64 v, DWORD ms);

static void InitBanner()
{
    const int W = 960, H = 540;

    // 1. Render the text with GDI into a 32-bit DIB.
    BITMAPINFO bi = {};
    bi.bmiHeader.biSize        = sizeof(bi.bmiHeader);
    bi.bmiHeader.biWidth       = W;
    bi.bmiHeader.biHeight      = -H;   // top-down
    bi.bmiHeader.biPlanes      = 1;
    bi.bmiHeader.biBitCount    = 32;
    bi.bmiHeader.biCompression = BI_RGB;
    void *bits = nullptr;
    HDC dc = CreateCompatibleDC(nullptr);
    HBITMAP bmp = CreateDIBSection(dc, &bi, DIB_RGB_COLORS, &bits, nullptr, 0);
    if (dc == nullptr || bmp == nullptr || bits == nullptr) return;
    HGDIOBJ old_bmp = SelectObject(dc, bmp);

    RECT full = { 0, 0, W, H };
    HBRUSH bg = CreateSolidBrush(RGB(18, 18, 22));
    FillRect(dc, &full, bg);
    DeleteObject(bg);
    SetBkMode(dc, TRANSPARENT);

    HFONT fnt_big   = CreateFontW(64, 0, 0, 0, FW_BOLD, FALSE, FALSE, FALSE, DEFAULT_CHARSET, 0, 0,
                                  CLEARTYPE_QUALITY, DEFAULT_PITCH, L"Segoe UI");
    HFONT fnt_small = CreateFontW(26, 0, 0, 0, FW_NORMAL, FALSE, FALSE, FALSE, DEFAULT_CHARSET, 0, 0,
                                  CLEARTYPE_QUALITY, DEFAULT_PITCH, L"Segoe UI");
    HGDIOBJ old_font = SelectObject(dc, fnt_big);
    SetTextColor(dc, RGB(118, 185, 0));
    RECT r1 = { 0, 150, W, 240 };
    DrawTextW(dc, L"32-bit DLSS 5 Feeder", -1, &r1, DT_CENTER | DT_SINGLELINE | DT_VCENTER);
    SelectObject(dc, fnt_small);
    SetTextColor(dc, RGB(200, 200, 205));
    RECT r2 = { 0, 260, W, 300 };
    DrawTextW(dc, L"DLSS 5 neural rendering runs here for your 32-bit game.", -1, &r2,
              DT_CENTER | DT_SINGLELINE | DT_VCENTER);
    RECT r3 = { 0, 305, W, 345 };
    DrawTextW(dc, L"Press  Home  in this window to tune it  \x2022  closing only hides the window", -1, &r3,
              DT_CENTER | DT_SINGLELINE | DT_VCENTER);
    SelectObject(dc, old_font);
    DeleteObject(fnt_big);
    DeleteObject(fnt_small);
    GdiFlush();

    // 2. Upload it (BGRA -> RGBA) and keep it as a copy source.
    D3D12_HEAP_PROPERTIES up = {};
    up.Type = D3D12_HEAP_TYPE_UPLOAD;
    const UINT pitch = (W * 4 + 255) & ~255u;
    D3D12_RESOURCE_DESC bd = {};
    bd.Dimension        = D3D12_RESOURCE_DIMENSION_BUFFER;
    bd.Width            = static_cast<UINT64>(pitch) * H;
    bd.Height           = 1;
    bd.DepthOrArraySize = 1;
    bd.MipLevels        = 1;
    bd.SampleDesc.Count = 1;
    bd.Layout           = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
    ID3D12Resource *staging = nullptr;
    D3D12_HEAP_PROPERTIES def = {};
    def.Type = D3D12_HEAP_TYPE_DEFAULT;
    D3D12_RESOURCE_DESC td = {};
    td.Dimension        = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
    td.Width            = W;
    td.Height           = H;
    td.DepthOrArraySize = 1;
    td.MipLevels        = 1;
    td.Format           = DXGI_FORMAT_R8G8B8A8_UNORM;
    td.SampleDesc.Count = 1;
    td.Layout           = D3D12_TEXTURE_LAYOUT_UNKNOWN;
    if (FAILED(h.dev->CreateCommittedResource(&up, D3D12_HEAP_FLAG_NONE, &bd, D3D12_RESOURCE_STATE_GENERIC_READ,
                                              nullptr, __uuidof(ID3D12Resource), reinterpret_cast<void **>(&staging))) ||
        FAILED(h.dev->CreateCommittedResource(&def, D3D12_HEAP_FLAG_NONE, &td, D3D12_RESOURCE_STATE_COPY_DEST,
                                              nullptr, __uuidof(ID3D12Resource), reinterpret_cast<void **>(&g_banner))))
    { SelectObject(dc, old_bmp); DeleteObject(bmp); DeleteDC(dc); return; }

    BYTE *dst = nullptr;
    staging->Map(0, nullptr, reinterpret_cast<void **>(&dst));
    const BYTE *srcp = static_cast<const BYTE *>(bits);
    for (int y = 0; y < H; ++y)
        for (int x = 0; x < W; ++x)
        {
            const BYTE *p = srcp + (static_cast<size_t>(y) * W + x) * 4;   // GDI: BGRA
            BYTE *q = dst + static_cast<size_t>(y) * pitch + static_cast<size_t>(x) * 4;
            q[0] = p[2]; q[1] = p[1]; q[2] = p[0]; q[3] = 0xFF;
        }
    staging->Unmap(0, nullptr);
    SelectObject(dc, old_bmp);
    DeleteObject(bmp);
    DeleteDC(dc);

    if (BeginCommands())
    {
        D3D12_TEXTURE_COPY_LOCATION src = {}, dcl = {};
        src.pResource = staging;
        src.Type      = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
        src.PlacedFootprint.Footprint.Format   = DXGI_FORMAT_R8G8B8A8_UNORM;
        src.PlacedFootprint.Footprint.Width    = W;
        src.PlacedFootprint.Footprint.Height   = H;
        src.PlacedFootprint.Footprint.Depth    = 1;
        src.PlacedFootprint.Footprint.RowPitch = pitch;
        dcl.pResource = g_banner;
        dcl.Type      = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
        h.list->CopyTextureRegion(&dcl, 0, 0, 0, &src, nullptr);
        D3D12_RESOURCE_BARRIER b = {};
        b.Type                   = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
        b.Transition.pResource   = g_banner;
        b.Transition.StateBefore = D3D12_RESOURCE_STATE_COPY_DEST;
        b.Transition.StateAfter  = D3D12_RESOURCE_STATE_COPY_SOURCE;
        b.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
        h.list->ResourceBarrier(1, &b);
        const UINT64 v = EndCommands();
        WaitFenceValue(h.fence, v, 2000);
    }
    staging->Release();

    // 3. A tiny allocator/list/fence pair on the pump queue for the per-frame copy.
    h.dev->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, __uuidof(ID3D12CommandAllocator),
                                  reinterpret_cast<void **>(&g_pump_alloc));
    if (g_pump_alloc != nullptr)
        h.dev->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, g_pump_alloc, nullptr,
                                 __uuidof(ID3D12GraphicsCommandList), reinterpret_cast<void **>(&g_pump_list));
    if (g_pump_list != nullptr) g_pump_list->Close();
    h.dev->CreateFence(0, D3D12_FENCE_FLAG_NONE, __uuidof(ID3D12Fence), reinterpret_cast<void **>(&g_pump_fence));
    g_pump_ev = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    h.swap->QueryInterface(__uuidof(IDXGISwapChain3), reinterpret_cast<void **>(&g_swap3));
    Log("[host] banner ready");
}

typedef HRESULT (WINAPI *PFN_D3D12CreateDevice_)(IUnknown *, D3D_FEATURE_LEVEL, REFIID, void **);
typedef HRESULT (WINAPI *PFN_CreateDXGIFactory1_)(REFIID, void **);

static void PumpPresent()
{
    MSG msg;
    while (PeekMessageW(&msg, nullptr, 0, 0, PM_REMOVE)) { TranslateMessage(&msg); DispatchMessageW(&msg); }
    if (h.swap == nullptr) return;

    // Paint the banner into the backbuffer (ReShade's overlay composites on top at Present).
    // h.pump_queue is not created in this build (a dead path from InitBanner)
    // - the check is mandatory, otherwise a latent NULL crash.
    if (g_banner != nullptr && g_pump_list != nullptr && g_swap3 != nullptr
        && h.pump_queue != nullptr)
    {
        ID3D12Resource *bb = nullptr;
        if (SUCCEEDED(g_swap3->GetBuffer(g_swap3->GetCurrentBackBufferIndex(), __uuidof(ID3D12Resource),
                                         reinterpret_cast<void **>(&bb))) && bb != nullptr)
        {
            if (SUCCEEDED(g_pump_alloc->Reset()) && SUCCEEDED(g_pump_list->Reset(g_pump_alloc, nullptr)))
            {
                D3D12_RESOURCE_BARRIER b = {};
                b.Type                   = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
                b.Transition.pResource   = bb;
                b.Transition.StateBefore = D3D12_RESOURCE_STATE_PRESENT;
                b.Transition.StateAfter  = D3D12_RESOURCE_STATE_COPY_DEST;
                b.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
                g_pump_list->ResourceBarrier(1, &b);
                g_pump_list->CopyResource(bb, g_banner);
                b.Transition.StateBefore = D3D12_RESOURCE_STATE_COPY_DEST;
                b.Transition.StateAfter  = D3D12_RESOURCE_STATE_PRESENT;
                g_pump_list->ResourceBarrier(1, &b);
                g_pump_list->Close();
                ID3D12CommandList *lists[] = { g_pump_list };
                h.pump_queue->ExecuteCommandLists(1, lists);
                h.pump_queue->Signal(g_pump_fence, ++g_pump_val);
                if (g_pump_fence->GetCompletedValue() < g_pump_val && g_pump_ev != nullptr)
                {
                    g_pump_fence->SetEventOnCompletion(g_pump_val, g_pump_ev);
                    WaitForSingleObject(g_pump_ev, 100);
                }
            }
            bb->Release();
        }
    }
    h.swap->Present(0, 0);
}

// NS_GPU: which DXGI adapter the worker runs on, by the index EnumAdapters1
// uses - the same number the "[host] adapter N: ..." lines print, so the log
// of one run tells the user what to set.
//
// Unset is the default and stays the default. Two different defaults are
// deliberate: NGX picks the first NVIDIA adapter (on a hybrid laptop the
// integrated GPU is usually adapter 0 and NGX would refuse it), while the
// capture stays on adapter 0, which is where the display normally hangs.
//
// When it IS set, it applies to BOTH: the network and the capture have to
// live on one adapter, since the captured frame reaches D3D12 through an
// NT-shared texture and a shared handle does not cross adapters. Picking a
// card that drives no display therefore fails at DuplicateOutput, with the
// reason in the log, rather than producing a black picture.
//
// Returns -1 when unset or unusable.
static int SelectedAdapterIndex()
{
    char buf[16] = {};
    const DWORD got = GetEnvironmentVariableA("NS_GPU", buf, sizeof(buf));
    if (got == 0 || got >= sizeof(buf)) return -1;
    char *end = nullptr;
    const long value = strtol(buf, &end, 10);
    if (end == buf || *end != '\0' || value < 0 || value > 63)
    {
        Log("[host] NS_GPU=%s is not an adapter index - ignored", buf);
        return -1;
    }
    return static_cast<int>(value);
}


//: The adapter the network ACTUALLY runs on, as a DXGI index. NS_GPU is a
//: wish: an index that is not a usable NVIDIA adapter falls back to the
//: first one that is. The capture and the duplication have to land on the
//: same card - the frame crosses to D3D12 through a shared handle, which
//: does not cross adapters - so they ask this rather than NS_GPU, which is
//: what they used to do (issue #34: a hybrid laptop ran the network on the
//: 4090 and the capture on the iGPU, and showed nothing).
static int g_adapter_index = -1;
//: Kept for QueryVideoMemoryInfo - what this process has on the card right
//: now. Logged on every feature create, so a leak shows up in a user's log
//: as a number that climbs instead of "it crashed after a while" (#48).
static IDXGIAdapter3 *g_adapter3 = nullptr;

static void LogVideoMemory(const char *where)
{
    if (g_adapter3 == nullptr) return;
    DXGI_QUERY_VIDEO_MEMORY_INFO info = {};
    if (SUCCEEDED(g_adapter3->QueryVideoMemoryInfo(
            0, DXGI_MEMORY_SEGMENT_GROUP_LOCAL, &info)))
        Log("[host] video memory after %s: %llu MB of %llu MB budget", where,
            (unsigned long long)(info.CurrentUsage >> 20),
            (unsigned long long)(info.Budget >> 20));
}

static int ActiveAdapterIndex()
{
    return g_adapter_index >= 0 ? g_adapter_index : SelectedAdapterIndex();
}


static bool InitDisguise()
{
    // Pure D3D12 setup. No window, swapchain, ReShade, RenoDX, or DLSS carrier.
    HMODULE dxgi = LoadLibraryW(L"dxgi.dll");
    HMODULE d3d12 = LoadLibraryW(L"d3d12.dll");
    auto create_device  = d3d12 ? reinterpret_cast<PFN_D3D12CreateDevice_>(GetProcAddress(d3d12, "D3D12CreateDevice")) : nullptr;
    auto create_factory = dxgi ? reinterpret_cast<PFN_CreateDXGIFactory1_>(GetProcAddress(dxgi, "CreateDXGIFactory1")) : nullptr;
    if (create_device == nullptr || create_factory == nullptr) { Log("[host] dxgi/d3d12 exports missing"); return false; }

    IDXGIFactory2 *factory = nullptr;
    HRESULT hr = create_factory(__uuidof(IDXGIFactory2), reinterpret_cast<void **>(&factory));
    if (FAILED(hr)) { Log("[host] CreateDXGIFactory1 failed 0x%08X", hr); return false; }

    // Hybrid laptops/desktops commonly expose the integrated adapter first. NGX initialization
    // then fails even when a supported RTX card is present, so explicitly select NVIDIA.
    // The whole list is logged either way: the index printed here is what
    // NS_GPU takes, so one run tells the user what to choose.
    const int want = SelectedAdapterIndex();
    IDXGIAdapter1 *nvidia = nullptr;
    IDXGIAdapter1 *chosen = nullptr;
    int nvidia_idx = -1;
    int chosen_idx = -1;
    for (UINT i = 0; ; ++i)
    {
        IDXGIAdapter1 *candidate = nullptr;
        if (factory->EnumAdapters1(i, &candidate) == DXGI_ERROR_NOT_FOUND) break;
        DXGI_ADAPTER_DESC1 desc = {};
        candidate->GetDesc1(&desc);
        // VRAM and the LUID go in the line too. One user has a single
        // RTX 5080 that DXGI reports as adapters 0 AND 2, and only one of
        // the two can run the neural pass (issue #33) - from the outside
        // the two entries are identical, and these are the fields that
        // might tell them apart. Nothing reads them yet; they are here so
        // the next log settles it instead of another round of guessing.
        Log("[host] adapter %u: %ls vendor=0x%04X vram=%lluMB luid=%08X:%08X",
            i, desc.Description, desc.VendorId,
            (unsigned long long)(desc.DedicatedVideoMemory >> 20),
            (unsigned)desc.AdapterLuid.HighPart, (unsigned)desc.AdapterLuid.LowPart);
        const bool usable = desc.VendorId == 0x10DE &&
                            !(desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE);
        if (want >= 0 && static_cast<int>(i) == want && usable)
        { chosen = candidate; chosen_idx = static_cast<int>(i); continue; }
        if (nvidia == nullptr && usable)
        { nvidia = candidate; nvidia_idx = static_cast<int>(i); continue; }
        candidate->Release();
    }
    if (want >= 0 && chosen == nullptr)
        Log("[host] NS_GPU=%d is not a usable NVIDIA adapter - using the first one",
            want);
    if (chosen != nullptr)
    {
        if (nvidia != nullptr) nvidia->Release();
        nvidia = chosen;
        nvidia_idx = chosen_idx;
        Log("[host] adapter %d selected by NS_GPU", want);
    }
    if (nvidia == nullptr) { factory->Release(); Log("[host] no NVIDIA adapter found"); return false; }
    // From here on this is THE adapter: the capture and the duplication read
    // it instead of NS_GPU, so a wish that could not be granted cannot split
    // the pipeline across two cards (issue #34).
    g_adapter_index = nvidia_idx;
    Log("[host] adapter %d runs the network and the capture", nvidia_idx);
    if (FAILED(nvidia->QueryInterface(__uuidof(IDXGIAdapter3),
                                      reinterpret_cast<void **>(&g_adapter3))))
        g_adapter3 = nullptr;

    hr = create_device(nvidia, D3D_FEATURE_LEVEL_11_0, __uuidof(ID3D12Device),
                       reinterpret_cast<void **>(&h.dev));
    nvidia->Release();
    if (FAILED(hr)) { Log("[host] D3D12CreateDevice failed 0x%08X", hr); return false; }

    // DRED breadcrumbs: when the device is removed (TDR on Win10, issue #1)
    // the reason code and the faulting command list are the only way to tell
    // WHERE it died. Without them the log says only "code 6" and the user
    // cannot help. Enable the settings interface before any work is
    // submitted; the device itself is queried for the breadcrumbs at the
    // failure site (BeginCommands).
    {
        ID3D12DeviceRemovedExtendedDataSettings1 *dred1 = nullptr;
        const HRESULT hr1 = h.dev->QueryInterface(__uuidof(ID3D12DeviceRemovedExtendedDataSettings1),
                                                  reinterpret_cast<void **>(&dred1));
        if (SUCCEEDED(hr1))
        {
            dred1->SetAutoBreadcrumbsEnablement(D3D12_DRED_ENABLEMENT_FORCED_ON);
            dred1->SetPageFaultEnablement(D3D12_DRED_ENABLEMENT_FORCED_ON);
            dred1->Release();
            Log("[host] DRED breadcrumbs enabled (settings1)");
        }
        else
        {
            // Older SDKs / OS builds: the v1 settings interface.
            ID3D12DeviceRemovedExtendedDataSettings *dred = nullptr;
            const HRESULT hr0 = h.dev->QueryInterface(__uuidof(ID3D12DeviceRemovedExtendedDataSettings),
                                                      reinterpret_cast<void **>(&dred));
            if (SUCCEEDED(hr0))
            {
                dred->SetAutoBreadcrumbsEnablement(D3D12_DRED_ENABLEMENT_FORCED_ON);
                dred->SetPageFaultEnablement(D3D12_DRED_ENABLEMENT_FORCED_ON);
                dred->Release();
                Log("[host] DRED breadcrumbs enabled (settings)");
            }
            else
            {
                // Pre-1903 Windows 10: the settings interface does not exist.
                Log("[host] DRED settings unavailable (settings1=0x%08X, settings=0x%08X)",
                    (unsigned)hr1, (unsigned)hr0);
            }
        }
    }

    factory->Release();
    D3D12_COMMAND_QUEUE_DESC qd = {};
    h.dev->CreateCommandQueue(&qd, __uuidof(ID3D12CommandQueue), reinterpret_cast<void **>(&h.queue));
    if (h.queue == nullptr) { Log("[host] queue creation failed"); return false; }
    Log("[pure] standalone D3D12 device ready; no swapchain or carrier modules");

    // Ring + internal fence for our own submissions.
    for (int i = 0; i < Host::kFrames; ++i)
        h.dev->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, __uuidof(ID3D12CommandAllocator),
                                      reinterpret_cast<void **>(&h.alloc[i]));
    if (h.alloc[0] != nullptr)
        h.dev->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, h.alloc[0], nullptr,
                                 __uuidof(ID3D12GraphicsCommandList), reinterpret_cast<void **>(&h.list));
    if (h.list != nullptr) h.list->Close();
    h.dev->CreateFence(0, D3D12_FENCE_FLAG_NONE, __uuidof(ID3D12Fence), reinterpret_cast<void **>(&h.fence));
    h.fence_event = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    if (h.list == nullptr || h.fence == nullptr) { Log("[host] list/fence creation failed"); return false; }

    return true;
}

static bool InitNgx()
{
    wchar_t data_path[MAX_PATH] = {};
    GetModuleFileNameW(nullptr, data_path, MAX_PATH);
    if (wchar_t *s = wcsrchr(data_path, L'\\')) *(s + 1) = L'\0';

    NVSDK_NGX_Result r = NVSDK_NGX_D3D12_Init(0x1000000ULL, data_path, h.dev, nullptr, NVSDK_NGX_Version_API);
    Log("[host] NVSDK_NGX_D3D12_Init -> 0x%08X (%s)", r, NgxResultName(r));
    if (NVSDK_NGX_FAILED(r))
    {
        r = NVSDK_NGX_D3D12_Init_with_ProjectID("a0f57b54-1daf-4934-90ae-c4035c19df04", NVSDK_NGX_ENGINE_TYPE_CUSTOM,
                                                "1.0", data_path, h.dev, nullptr, NVSDK_NGX_Version_API);
        Log("[host] Init_with_ProjectID -> 0x%08X (%s)", r, NgxResultName(r));
    }
    if (NVSDK_NGX_FAILED(r)) return false;
    h.ngx_inited = true;

    r = NVSDK_NGX_D3D12_AllocateParameters(&h.params);
    if (NVSDK_NGX_FAILED(r) || h.params == nullptr) { Log("[host] AllocateParameters failed 0x%08X", r); return false; }
    return InitDirectNr(data_path);
}

// The NR model is chosen by a hint when the feature is created. It was 0
// (default) and never questioned; in the related Ray Reconstruction the
// numbers hold different transformer models with different costs. The value
// comes from NS_NR_PRESET so it can be swept without a rebuild.
static UINT NrPresetHint()
{
    static int cached = -1;
    if (cached < 0)
    {
        char buf[16] = {};
        const DWORD got = GetEnvironmentVariableA("NS_NR_PRESET", buf, sizeof(buf));
        cached = (got > 0 && got < sizeof(buf)) ? atoi(buf) : 0;
        if (cached < 0) cached = 0;
    }
    return static_cast<UINT>(cached);
}

static bool CreateFeature(UINT w, UINT h_, int flags, NVSDK_NGX_Result *out_r, UINT full_w = 0, UINT full_h = 0)
{
    (void)flags;
    // New mode: the client sends full-res frames (full_w x full_h) and the feature
    // itself downsamples to the work resolution (w x h_), runs NR, and upsamples back.
    const bool upscale = (full_w > 0 && full_h > 0 && (full_w != w || full_h != h_));
    h.params->Reset();
    h.params->Set("CreationNodeMask", 1u);
    h.params->Set("VisibilityNodeMask", 1u);
    h.params->Set("DLSSNR.Width", w); h.params->Set("DLSSNR.Height", h_);
    h.params->Set("DLSSNR.InputWidth", upscale ? full_w : w);
    h.params->Set("DLSSNR.InputHeight", upscale ? full_h : h_);
    h.params->Set("DLSSNR.OutputWidth", upscale ? full_w : w);
    h.params->Set("DLSSNR.OutputHeight", upscale ? full_h : h_);
    h.params->Set("DLSSNR.Output.Width", upscale ? full_w : w);
    h.params->Set("DLSSNR.Output.Height", upscale ? full_h : h_);
    h.params->Set("DLSSNR.Upscaling", upscale ? 1u : 0u);
    h.params->Set("DLSSNR.Scale", upscale ? static_cast<float>(w) / static_cast<float>(full_w) : 1.0f);
    h.params->Set("DLSSNR.ScalingRatio", upscale ? static_cast<float>(w) / static_cast<float>(full_w) : 1.0f);
    h.params->Set("DLSSNR.Hint.Render.Preset", NrPresetHint());
    h.params->Set("DLSS.Feature.Create.Flags", 0u);

    if (!BeginCommands()) return false;
    DWORD ccode = 0;
    NVSDK_NGX_Result rf = static_cast<NVSDK_NGX_Result>(0x7FFFFFFF);
    __try { rf = g_nr_create(h.list, NVSDK_NGX_Feature_Reserved18, h.params, &h.feature); }
    __except (EXCEPTION_EXECUTE_HANDLER) { ccode = GetExceptionCode(); }
    g_create_result = static_cast<uint32_t>(rf);
    if (out_r != nullptr) *out_r = rf;
    if (ccode != 0)
    {
        AbortCommands();
        // NGX may have partially written *OutHandle before the fault; never trust it.
        h.feature = nullptr;
        Log("[host] CreateFeature raised 0x%08X (caught; nothing submitted)", ccode);
        return false;
    }
    const UINT64 v = EndCommands();
    if (!WaitFenceValue(h.fence, v, 30000)) { Log("[pure] feature create did not complete"); return false; }
    if (NVSDK_NGX_FAILED(rf) || h.feature == nullptr)
    { Log("[pure] direct feature 18 create failed 0x%08X (%s)", rf, NgxResultName(rf)); h.feature = nullptr; return false; }
    Log("[pure] direct feature 18 ready: %ux%u%s preset=%u result=0x%08X", w, h_,
        upscale ? " (upscaling full->work->full)" : "", NrPresetHint(), rf);
    LogVideoMemory("feature create");
    return true;
}

// A crashed CreateFeature can leave NGX's own internal state broken (seen in BioShock
// Remastered: the add-on faulted once during a resolution/HDR change, and every following
// create failed too, with the SEH catching a different exception each time -- NGX was
// never going to recover on its own). Reset NGX itself as a last resort so the feed can
// come back without the user having to restart the game.
static bool ReinitNgx()
{
    Log("[host] NGX looks corrupted after repeated failures; reinitializing");
    if (h.params != nullptr) { NVSDK_NGX_D3D12_DestroyParameters(h.params); h.params = nullptr; }
    if (h.ngx_inited) { NVSDK_NGX_D3D12_Shutdown1(h.dev); h.ngx_inited = false; }
    h.feature = nullptr;
    return InitNgx();
}

static bool Evaluate(ID3D12Resource *color, ID3D12Resource *output, ID3D12Resource *depth, ID3D12Resource *mv,
                     UINT w, UINT h_, int reset, float mvsx, float mvsy)
{
    if (!BeginCommands()) return false;

    NVSDK_NGX_D3D12_DLSS_Eval_Params ep = {};
    ep.Feature.pInColor  = color;
    ep.Feature.pInOutput = output;
    ep.pInDepth          = depth;
    ep.pInMotionVectors  = mv;
    ep.InRenderSubrectDimensions.Width  = w;
    ep.InRenderSubrectDimensions.Height = h_;
    ep.InReset           = reset;
    ep.InMVScaleX        = mvsx;
    ep.InMVScaleY        = mvsy;
    ep.InPreExposure     = 1.0f;
    ep.InExposureScale   = 1.0f;

    DWORD ecode = 0;
    NVSDK_NGX_Result re = SafeEvaluateDLSS(&ep, &ecode);
    if (ecode != 0) { AbortCommands(); Log("[host] evaluate raised 0x%08X (caught; nothing submitted)", ecode); return false; }
    if (EndCommands() == 0) return false;   // queue Signal failed (device removed)
    if (NVSDK_NGX_FAILED(re)) { Log("[host] evaluate failed 0x%08X (%s)", re, NgxResultName(re)); return false; }
    return true;
}

// ---------------------------------------------------------------------------
// --test: prove the whole stack with no game attached
// ---------------------------------------------------------------------------

static ID3D12Resource *MakeTex(UINT w, UINT h_, DXGI_FORMAT fmt, bool uav)
{
    D3D12_HEAP_PROPERTIES hp = {};
    hp.Type = D3D12_HEAP_TYPE_DEFAULT;
    D3D12_RESOURCE_DESC rd = {};
    rd.Dimension        = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
    rd.Width            = w;
    rd.Height           = h_;
    rd.DepthOrArraySize = 1;
    rd.MipLevels        = 1;
    rd.Format           = fmt;
    rd.SampleDesc.Count = 1;
    rd.Layout           = D3D12_TEXTURE_LAYOUT_UNKNOWN;
    rd.Flags            = uav ? D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS : D3D12_RESOURCE_FLAG_NONE;
    ID3D12Resource *t = nullptr;
    h.dev->CreateCommittedResource(&hp, D3D12_HEAP_FLAG_NONE, &rd, D3D12_RESOURCE_STATE_COMMON, nullptr,
                                   __uuidof(ID3D12Resource), reinterpret_cast<void **>(&t));
    return t;
}

static int RunTest()
{
    const UINT W = 640, H = 360;
    Log("[host] --test: %ux%u synthetic DLAA", W, H);

    ID3D12Resource *color  = MakeTex(W, H, DXGI_FORMAT_R8G8B8A8_UNORM, false);
    ID3D12Resource *output = MakeTex(W, H, DXGI_FORMAT_R8G8B8A8_UNORM, true);
    ID3D12Resource *depth  = MakeTex(W, H, DXGI_FORMAT_R32_FLOAT, false);
    ID3D12Resource *mv     = MakeTex(W, H, DXGI_FORMAT_R16G16_FLOAT, false);
    if (!color || !output || !depth || !mv) { Log("[host] test texture creation failed"); return 1; }

    // Give the DLSS 5 add-on its hook-arming time, with the swapchain pumping.
    for (int i = 0; i < 120; ++i) { PumpPresent(); Sleep(8); }

    int flags = NVSDK_NGX_DLSS_Feature_Flags_MVLowRes | NVSDK_NGX_DLSS_Feature_Flags_AutoExposure |
                NVSDK_NGX_DLSS_Feature_Flags_DepthInverted;
    NVSDK_NGX_Result rf = NVSDK_NGX_Result_Fail;
    if (!CreateFeature(W, H, flags, &rf)) return 1;

    int good = 0;
    for (int i = 0; i < 300; ++i)
    {
        PumpPresent();
        if (Evaluate(color, output, depth, mv, W, H, i == 0 ? 1 : 0, 1.0f, 1.0f)) ++good;
        else break;
        if (i == 180)   // the warm-up re-create, same medicine as in-game
        {
            Log("[host] warm-up: re-creating the feature once");
            NVSDK_NGX_Handle *old = h.feature;
            h.feature = nullptr;
            if (!CreateFeature(W, H, flags, &rf)) { h.feature = old; Log("[host] keeping the previous feature"); }
            else SafeReleaseFeature(old);
        }
    }
    Log("[host] --test finished: %d/300 evaluates succeeded", good);
    Log("[host] check the host's ReShade.log for 'feature 18 created' / 'evaluation succeeded'");
    return good >= 250 ? 0 : 1;
}

// ---------------------------------------------------------------------------
// --video: portable video-frame protocol for the Gradio front end
// ---------------------------------------------------------------------------

static constexpr uint32_t VIDEO_MAGIC     = 0x32563544u; // "D5V2" -- legacy 56-byte header
static constexpr uint32_t VIDEO_MAGIC_EXT = 0x33563544u; // "D5V3" -- 64-byte header with full_w/full_h
static constexpr uint32_t FRAME_MAGIC = 0x314D5246u; // "FRM1"
static constexpr uint32_t OUT_MAGIC   = 0x3154554Fu; // "OUT1"
static constexpr uint32_t RESIZE_MAGIC    = 0x5A534E52u; // "RNSZ" -- reconfigure on the fly (work size + params)
static constexpr uint32_t RESIZE_ACK_MAGIC = 0x4B434152u; // "RACK" -- worker -> client reply to RNSZ
static constexpr uint32_t SHM_MAGIC     = 0x494D4853u; // "SHMI" -- client -> worker: frame payload lives in shared memory
static constexpr uint32_t SHM_ACK_MAGIC = 0x4B434153u; // "SACK" -- worker -> client reply to SHMI
static constexpr uint32_t WINDOW_MAGIC     = 0x4F444E57u; // "WNDO" -- client -> worker: present results yourself
static constexpr uint32_t WINDOW_ACK_MAGIC = 0x4B434157u; // "WACK" -- worker -> client reply to WNDO
static constexpr uint32_t MOTION_MAGIC     = 0x53544F4Du; // "MOTS" -- client -> worker: motion arrives at this reduced size
static constexpr uint32_t MOTION_ACK_MAGIC = 0x4B43414Du; // "MACK" -- worker -> client reply to MOTS
static constexpr uint32_t DDA_MAGIC        = 0x31414444u; // "DDA1" -- client -> worker: worker takes over capture
static constexpr uint32_t DDA_ACK_MAGIC    = 0x4B434144u; // "DACK" -- worker -> client reply to DDA1
static constexpr uint32_t WGC_MAGIC        = 0x57434757u; // "WGCW" -- client -> worker: capture ONE window, not the desktop
static constexpr uint32_t WGC_ACK_MAGIC    = 0x4B414757u; // "WGAK" -- worker -> client reply to WGCW
static constexpr uint32_t GRAY_MAGIC       = 0x59415247u; // "GRAY" -- client -> worker: gray goes back into this mapping
static constexpr uint32_t GRAY_ACK_MAGIC   = 0x4B434147u; // "GAK"  -- worker -> client reply to GRAY
// OUTS: the reverse channel for PIXELS. A recorded frame weighs 33 MB at 4K,
// and through the pipe that is ~7 ms per frame (measured: recv 17.4 -> 31.6 ms
// when recording is switched on). Through shared memory those bytes never
// travel down the pipe.
static constexpr uint32_t OUTS_MAGIC       = 0x5354554Fu; // "OUTS" -- client -> worker: pixels go into this mapping
static constexpr uint32_t OUTS_ACK_MAGIC   = 0x324B414Fu; // "OAK2" -- worker -> client reply to OUTS
// VideoResultHeader.bytes: the pixels are in the OUTS section, not in the pipe.
static constexpr uint32_t OUT_BYTES_IN_SHM = 0xFFFFFFFFu;
// WNDO flags
static constexpr uint32_t WINDOW_FLAG_CAPTURABLE = 0x1u; // debug: do NOT hide the window from screen capture
static constexpr uint32_t WINDOW_FLAG_DISABLE    = 0x2u; // tear the window down, go back to sending pixels
// VideoFrameHeader.reserved bit 0: payload is in the mapping, not in the pipe.
static constexpr uint32_t FRAME_FLAG_SHM = 0x1u;
// bit 1: even while presenting into the overlay, send the pixels back for
// this one frame (the client needs them for a screenshot).
static constexpr uint32_t FRAME_FLAG_WANT_PIXELS = 0x2u;
// bit 2: the motion payload of this frame is at the reduced size agreed by
// MOTS; the worker upscales it to the work resolution on the GPU.
static constexpr uint32_t FRAME_FLAG_MOTION_SMALL = 0x4u;
// bit 3: DDA capture is active — this frame carries NO colour payload.
// The worker takes the colour from Desktop Duplication itself and reads
// only the motion block from the pipe.
static constexpr uint32_t FRAME_FLAG_NO_COLOR = 0x8u;
// bit 4: NR OFF — skip the NGX evaluate and present the raw captured
// colour instead. Keeps the overlay alive (picture + HUD) while the
// neural pass is disabled; the next non-bypass frame resumes NGX.
static constexpr uint32_t FRAME_FLAG_BYPASS = 0x10u;
// bit 5: show the frame with a before/after wipe - raw capture on the left,
// the NGX result on the right. The wipe position lives in the high 16 bits of
// reserved (0..65535 -> 0..1 of the frame width): there is no dedicated field
// in the header, and resizing it for a single number would break the protocol
// on both sides.
static constexpr uint32_t FRAME_FLAG_SPLIT = 0x20u;
// bit 6: skip static frames - the capture has no new frame (DDA
// WAIT_TIMEOUT, WGC empty pool), so the network is NOT re-run on the stale
// texture. An empty OUT1 is the "nothing changed" answer; WANT_PIXELS wins
// over this bit (a screenshot or a recording wants the picture either way).
static constexpr uint32_t FRAME_FLAG_SKIP_STATIC = 0x40u;

static UINT SplitXFromFlags(uint32_t reserved, UINT width)
{
    const uint32_t frac = (reserved >> 16) & 0xFFFFu;
    return static_cast<UINT>((static_cast<uint64_t>(width) * frac) / 0xFFFFu);
}
static constexpr size_t   VIDEO_HEADER_LEGACY_SIZE = 56; // magic..skin_structure (no full_w/full_h)

#pragma pack(push, 1)
struct VideoHeader
{
    uint32_t magic, width, height, warmup, frame_count, profile, preset, style, auto_mask, ui_correction;
    float intensity, local_tone, local_structure, skin_structure;
    uint32_t full_w, full_h;   // full-res input/output frame size (new mode); 0 = legacy 1:1
};
struct VideoFrameHeader
{
    uint32_t magic, index, reset, reserved;
    int64_t pts;
};
struct VideoResultHeader
{
    uint32_t magic, index, ok, bytes, ngx_result;
    int64_t pts;
};
// RNSZ: client -> worker, between frames. Same field layout as the D5V3
// header (magic..skin_structure + full_w/full_h) so the client can reuse
// its header builder. The worker tears down the feature/textures, creates
// new ones at the new work size and replies with a fixed-size RACK.
struct VideoResizeCmd
{
    uint32_t magic, width, height, warmup;
    // The slot VideoHeader keeps frame_count in. A resize has no frame count,
    // so it carries flags instead - see RESIZE_FLAG_*. The layout stays
    // identical to the header, which is the whole point of reusing it.
    uint32_t flags;
    uint32_t profile, preset, style, auto_mask, ui_correction;
    float intensity, local_tone, local_structure, skin_structure;
    uint32_t full_w, full_h;
};
// Run the network at the work size and scale the result back up, instead of
// handing it the whole screen. Travels with the resize because in the menu it
// is one control: the resolution the network sees, with "full screen" at the
// top of the slider.
static constexpr uint32_t RESIZE_FLAG_NR_SMALL = 0x1u;
struct VideoResizeAck
{
    uint32_t magic, ok, ngx_result, reserved;
    int64_t pts;
};
// SHMI: client -> worker, once per worker lifetime (right after the stream
// header). Hands over the name of a named section that holds the input frame.
// Layout inside the mapping is fixed and does NOT depend on work_scale:
//     [0 .. color_bytes)                  RGBA8, full-res (capacity, not the
//                                         per-frame size)
//     [color_bytes .. +motion_bytes)      motion float16, work-res
// so RNSZ never needs to renegotiate. The first 24 bytes share the layout of
// VideoFrameHeader, which is how ReadVideoMessage can peek the magic first.
struct VideoShmCmd
{
    uint32_t magic, color_bytes, motion_bytes, flags;
    int64_t pts;
    char name[64];
};
struct VideoShmAck
{
    uint32_t magic, ok, reserved0, reserved1;
    int64_t pts;
};
// WNDO: client -> worker. "Open a borderless click-through overlay of this size
// and show the NGX result in it yourself." Same 24-byte shape as
// VideoFrameHeader, so no extra read is needed. While the window is up, OUT1
// carries bytes=0: the pixels never come back to the client at all.
struct VideoWindowCmd
{
    uint32_t magic, width, height, flags;
    int64_t pts;
};
struct VideoWindowAck
{
    uint32_t magic, ok, reserved0, reserved1;
    int64_t pts;
};
// MOTS: client -> worker. "From now on the motion field arrives at this size;
// stretch it to the work resolution yourself." Sending 0x0 turns it off and
// motion goes back to arriving at the work resolution. Same 24-byte shape as
// VideoFrameHeader.
struct VideoMotionCmd
{
    uint32_t magic, width, height, flags;
    int64_t pts;
};
struct VideoMotionAck
{
    uint32_t magic, ok, reserved0, reserved1;
    int64_t pts;
};
// DDA1: client -> worker. "Take over capture from the desktop yourself."
// width/height = required capture size (usually the full output size);
// flags: bit0 WANT pixels back (screenshot), bit1 = present overlay stays on.
// The worker opens Desktop Duplication (D3D11) and feeds the NGX pipeline
// straight from GPU textures; the client no longer sends FRM1 frames while
// active. Sending DDA1 with width=0 stops capture and reverts to pipe frames.
struct VideoDdaCmd
{
    uint32_t magic, width, height, flags;
    int64_t pts;
};
struct VideoDdaAck
{
    uint32_t magic, ok, reserved0, reserved1;
    int64_t pts;
};
// WGCW: client -> worker. "Capture THIS WINDOW instead of the whole desktop."
// Windows Graphics Capture of a single window is unaffected by whatever is
// drawn on top of that window (measured: a fullscreen overlay over the target
// contributes 0% of the captured pixels), so there is no self-capture loop and
// our overlay no longer has to hide from screen capture - which is what lets
// OBS see it and the NVIDIA App record at all.
// hwnd = the target window, 0 stops the capture. width/height are advisory;
// the ack reports the size the capture actually produces, which on a scaled
// display is the window's PHYSICAL size, not its logical one.
struct VideoWgcCmd
{
    uint32_t magic, width, height, flags;
    int64_t pts;
    uint64_t hwnd;
};
struct VideoWgcAck
{
    uint32_t magic, ok, width, height;
    int64_t pts;
};
// GRAY: client -> worker. "Downsample the captured colour to luminance
// (width x height, typically 320x180 = flow size) and write it into this
// named mapping — the client needs it for the optical-flow guides. The
// DDA capture must be active; the block is computed after the swizzle."
// Layout: width*height bytes, R8 (single channel), client-owned mapping.
struct VideoGrayCmd
{
    uint32_t magic, width, height, flags;
    int64_t pts;
    char name[64];
};
struct VideoOutCmd
{
    uint32_t magic, width, height, flags;
    int64_t pts;
    char name[64];
};
struct VideoOutAck
{
    uint32_t magic, ok, reserved0, reserved1;
    int64_t pts;
};
struct VideoGrayAck
{
    uint32_t magic, ok, reserved0, reserved1;
    int64_t pts;
};
#pragma pack(pop)

// The wire protocol, pinned. Every size below is what main.py's struct format
// packs (struct.calcsize with '<' - no padding), and tests/test_protocol_sizes
// checks the same numbers from the Python side. If a field is added inside the
// packed region, or a struct drifts out of it, this stops being a mysterious
// "protocol desync" at runtime and becomes a compile error here.
static_assert(sizeof(VideoHeader) == 64, "VideoHeader != HEADER_FMT");
static_assert(sizeof(VideoFrameHeader) == 24, "VideoFrameHeader != FRAME_FMT");
static_assert(sizeof(VideoResultHeader) == 28, "VideoResultHeader != OUT_FMT");
static_assert(sizeof(VideoResizeCmd) == 64, "VideoResizeCmd != RESIZE_FMT");
static_assert(sizeof(VideoResizeAck) == 24, "VideoResizeAck != RACK_FMT");
static_assert(sizeof(VideoShmCmd) == 88, "VideoShmCmd != SHM_FMT");
static_assert(sizeof(VideoShmAck) == 24, "VideoShmAck != SHM_ACK_FMT");
static_assert(sizeof(VideoWindowCmd) == 24, "VideoWindowCmd != WINDOW_FMT");
static_assert(sizeof(VideoWindowAck) == 24, "VideoWindowAck != WINDOW_ACK_FMT");
static_assert(sizeof(VideoMotionCmd) == 24, "VideoMotionCmd != MOTION_FMT");
static_assert(sizeof(VideoMotionAck) == 24, "VideoMotionAck != MOTION_ACK_FMT");
static_assert(sizeof(VideoDdaCmd) == 24, "VideoDdaCmd != DDA_FMT");
static_assert(sizeof(VideoDdaAck) == 24, "VideoDdaAck != DDA_ACK_FMT");
static_assert(sizeof(VideoWgcCmd) == 32, "VideoWgcCmd != WGC_FMT");
static_assert(sizeof(VideoWgcAck) == 24, "VideoWgcAck != WGC_ACK_FMT");
static_assert(sizeof(VideoGrayCmd) == 88, "VideoGrayCmd != GRAY_FMT");
static_assert(sizeof(VideoGrayAck) == 24, "VideoGrayAck != GRAY_ACK_FMT");
static_assert(sizeof(VideoOutCmd) == 88, "VideoOutCmd != OUTS_FMT");
static_assert(sizeof(VideoOutAck) == 24, "VideoOutAck != OUTS_ACK_FMT");
// The offset of pts is what a mis-packed struct gets wrong first: the four
// leading uint32s are followed by an 8-byte field that natural alignment
// would push to 24.
static_assert(offsetof(VideoFrameHeader, pts) == 16, "VideoFrameHeader is not packed");
static_assert(offsetof(VideoResultHeader, pts) == 20, "VideoResultHeader is not packed");
static_assert(offsetof(VideoWgcCmd, hwnd) == 24, "VideoWgcCmd is not packed");

struct VideoTex
{
    ID3D12Resource *tex = nullptr;
    ID3D12Resource *upload = nullptr;
    D3D12_PLACED_SUBRESOURCE_FOOTPRINT fp = {};
    UINT rows = 0;
    UINT64 row_size = 0;
};

struct VideoState
{
    UINT w = 0, hgt = 0;          // work resolution (NGX feature resolution)
    UINT full_w = 0, full_h = 0;  // full-res frame size (0 = legacy 1:1 mode)
    bool upscale = false;         // full size given AND different from work: feature does full->work->full
    VideoTex color, depth, mv, mask;
    ID3D12Resource *output = nullptr;
    ID3D12Resource *readback = nullptr;
    D3D12_PLACED_SUBRESOURCE_FOOTPRINT out_fp = {};
    UINT out_rows = 0;
    UINT64 out_row_size = 0;
    bool inputs_ready = false;

    // NS_NR_SMALL=1: run Neural Rendering on a smaller frame than the screen.
    //
    // The network is same-resolution - it enhances, it does not upscale - so
    // its cost tracks the pixels it is handed: 1.50 ms fixed + 1.51 ms per
    // megapixel on a 5070 Ti (measured). Handing it the full screen, which is
    // what "upscaling" mode really does, is why work_scale never bought
    // anything.
    //
    // Here colour is scaled down into nr_in, the network runs there, and
    // nr_out is scaled back up into output. Everything downstream of output -
    // the present, the wipe, the recording, the bypass - keeps seeing full-res
    // textures and needs no changes at all.
    bool nr_small = false;
    UINT nr_w = 0, nr_h = 0;
    ID3D12Resource *nr_in = nullptr;    // NON_PIXEL_SHADER_RESOURCE at rest
    ID3D12Resource *nr_out = nullptr;   // UNORDERED_ACCESS at rest
    // Matched residual composite: instead of stretching nr_out up to full
    // size, compose native + (nr_out_up - nr_in_up) * strength. The native
    // frame stays the 1:1 anchor, so text and edges keep full sharpness
    // while the network runs at the cheap work resolution.
    bool residual = false;
    float residual_strength = 1.0f;
};

static bool g_hdr_capture = false;
static HMONITOR g_capture_monitor = nullptr;
static HdrDisplayInfo g_capture_display;
static float g_hdr_frame_white = 1.0f;
static UINT g_hdr_split = UINT_MAX;
static bool PresentHdr(VideoState &v, bool bypass);
static bool EnsurePresentFormat(bool hdr);
static void CloseHdrResources();

static VideoHeader g_video_options = {};
static uint32_t g_last_eval_result = 0;
// Static-frame skipping (FRAME_FLAG_SKIP_STATIC): how many frames were skipped
// since the last change, and whether the "idle" line was already written for
// this stretch (one line per idle stretch, not per frame).
//
// Skipping must have no visual price, so a frame whose OUTPUT would change is
// never skipped even on a frozen screen: a bypass toggle (NR on/off), a wipe
// move, a fresh feature (RNSZ), a re-opened present window. The last output
// state is kept to spot those transitions.
static uint32_t g_skip_static_count = 0;
static bool     g_skip_static_logged = false;
static bool     g_last_out_bypass = false;
static bool     g_last_out_split_on = false;
static uint32_t g_last_out_split_x = 0;
static bool     g_force_next_frame = false;   // render one frame even if unchanged
static bool g_live_force = false;   // --live: treat the stream as unbounded even with frame_count > 0

// Shared input frame (SHMI). Read-only view of the client's named section:
// the frame is uploaded to the GPU straight from here, so the payload never
// travels through the pipe and is never copied into a std::vector.
static HANDLE g_shm_handle = nullptr;
static const BYTE *g_shm_base = nullptr;
static size_t g_shm_bytes = 0;         // mapped length
static size_t g_shm_motion_off = 0;    // offset of the motion block (= colour capacity)

static void CloseSharedInput()
{
    if (g_shm_base != nullptr) { UnmapViewOfFile(g_shm_base); g_shm_base = nullptr; }
    if (g_shm_handle != nullptr) { CloseHandle(g_shm_handle); g_shm_handle = nullptr; }
    g_shm_bytes = 0;
    g_shm_motion_off = 0;
}

// ---------------------------------------------------------------------------
// Presentation window: the worker shows the NGX result itself, so the frame
// never goes back to the client. Kills the GPU->CPU readback, the return pipe
// and the client-side blit in one move.
// ---------------------------------------------------------------------------
#ifndef WDA_EXCLUDEFROMCAPTURE
#define WDA_EXCLUDEFROMCAPTURE 0x00000011
#endif

// Defined below, next to UploadVideoFrame.
static D3D12_RESOURCE_BARRIER Transition(ID3D12Resource *r, D3D12_RESOURCE_STATES before,
                                         D3D12_RESOURCE_STATES after);

static HWND                       g_present_hwnd;
// Where the overlay window belongs on the virtual desktop: the origin of the
// chosen monitor (the client puts it in NS_WINDOW_POS as "x,y"). Read on
// every OpenPresent - a monitor switch restarts the worker anyway.
static int                        g_present_x = 0;
static int                        g_present_y = 0;
static bool                       g_present_shown = false;      // visible right now (minimised target hides it)
static bool                       g_present_revealed = false;   // the first Present already happened
static RECT                       g_present_follow = {};   // where the target window was last seen
// Defined here rather than with the capture code below: the present window
// has to know whether one window is being captured, and which one, and this
// file is compiled top to bottom.
static bool                       g_wgc_active = false;
static HWND                       g_wgc_hwnd = nullptr;


static IDXGISwapChain3           *g_present_swap;
// What the swap chain has already been told its colours mean. Asking DXGI
// every frame is both a waste and a way to fail on the SDR path, which has
// never made the call at all - see EnsurePresentFormat.
static DXGI_COLOR_SPACE_TYPE      g_present_space = DXGI_COLOR_SPACE_RGB_FULL_G22_NONE_P709;
static bool                       g_present_space_set = false;
static HANDLE                     g_present_thread;
static DWORD                      g_present_tid;
static UINT                       g_present_w, g_present_h;
static bool                       g_present_capturable;  // debug flag from WNDO

// Whether the picture window hides itself from screen capture.
//
// With Desktop Duplication as the input it MUST hide: otherwise the pipeline
// captures its own output and feeds on itself. With one window as the input
// there is no loop - the capture of a window is unaffected by anything drawn
// on top of it - so the flag comes off and an outside recorder can finally
// see the overlay. Called wherever the mode changes; a flag baked in at
// window creation would go stale on the next switch.
static void ApplyPresentAffinity()
{
    if (g_present_hwnd == nullptr) return;
    const bool hide = !(g_wgc_active || g_present_capturable);
    const DWORD want = hide ? WDA_EXCLUDEFROMCAPTURE : 0u /* WDA_NONE */;
    if (!SetWindowDisplayAffinity(g_present_hwnd, want))
        Log("[present] SetWindowDisplayAffinity(%lu) failed, err=%lu",
            want, GetLastError());
    else
        Log("[present] %s screen capture", hide ? "hidden from" : "VISIBLE to");
}

static volatile LONG              g_present_state;       // 0 = pending, 1 = window up, -1 = failed

static LRESULT CALLBACK PresentWndProc(HWND w, UINT m, WPARAM wp, LPARAM lp)
{
    switch (m)
    {
    // Clicks and hover fall through to whatever sits underneath. This replaces
    // WS_EX_TRANSPARENT: that style only makes a window click-through when it is
    // also WS_EX_LAYERED, and a layered window cannot host a flip-model
    // swapchain (CreateSwapChainForHwnd fails with DXGI_ERROR_INVALID_CALL).
    case WM_NCHITTEST:     return HTTRANSPARENT;
    case WM_MOUSEACTIVATE: return MA_NOACTIVATE;
    case WM_ERASEBKGND:    return 1;
    case WM_CLOSE:         ShowWindow(w, SW_HIDE); return 0;
    default: break;
    }
    return DefWindowProcW(w, m, wp, lp);
}

// The window lives on its own thread with its own message loop: the main thread
// spends its life blocked in ReadExact on stdin and would never pump messages,
// which Windows reports as a hung window after a few seconds.
// NS_WINDOW_POS: where to put the overlay window, "x,y" in virtual-desktop
// pixels. Without it the window was created at (0,0) - the primary monitor -
// while the chosen monitor can sit at a nonzero origin: the picture landed on
// the wrong screen (#28, #33).
static void ReadPresentOrigin()
{
    g_present_x = 0;
    g_present_y = 0;
    char buf[32] = {};
    const DWORD got = GetEnvironmentVariableA("NS_WINDOW_POS", buf, sizeof(buf));
    if (got == 0 || got >= sizeof(buf)) return;
    int x = 0, y = 0;
    if (sscanf_s(buf, "%d,%d", &x, &y) == 2) { g_present_x = x; g_present_y = y; }
}

static DWORD WINAPI PresentWindowThread(LPVOID)
{
    WNDCLASSEXW wc = {};
    wc.cbSize        = sizeof(wc);
    wc.lpfnWndProc   = PresentWndProc;
    wc.hInstance     = GetModuleHandleW(nullptr);
    wc.lpszClassName = L"NeuralScreenPresent";
    RegisterClassExW(&wc);   // duplicate registration is harmless: it just fails

    // WS_EX_TRANSPARENT on top of HTTRANSPARENT: the hit test alone was not
    // enough for the system - the click still went to the overlay (measured by
    // injecting a click, _work/test_present_clickthrough.py).
    g_present_hwnd = CreateWindowExW(
        WS_EX_TOPMOST | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TRANSPARENT,
        wc.lpszClassName, L"NeuralScreen", WS_POPUP,
        g_present_x, g_present_y, static_cast<int>(g_present_w), static_cast<int>(g_present_h),
        nullptr, nullptr, wc.hInstance, nullptr);
    if (g_present_hwnd == nullptr)
    {
        Log("[present] CreateWindowEx failed, err=%lu", GetLastError());
        InterlockedExchange(&g_present_state, -1);
        return 0;
    }
    // Hidden from screen capture unless a single window is the input (or the
    // debug flag says otherwise) - see ApplyPresentAffinity.
    ApplyPresentAffinity();

    // The window stays HIDDEN until the first successful Present: showing a
    // blank topmost window during the NGX warm-up flashes black over the
    // desktop on every start and on every one-window mode switch (user:
    // screen flashes black). Revealed by RevealOnFirstPresent below.
    SetWindowPos(g_present_hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                 SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE | SWP_HIDEWINDOW);
    InterlockedExchange(&g_present_state, 1);

    MSG msg;
    while (GetMessageW(&msg, nullptr, 0, 0) > 0) { TranslateMessage(&msg); DispatchMessageW(&msg); }
    return 0;
}

static void ClosePresent()
{
    if (g_present_swap != nullptr) { g_present_swap->Release(); g_present_swap = nullptr; }
    if (g_present_tid != 0) PostThreadMessageW(g_present_tid, WM_QUIT, 0, 0);
    if (g_present_hwnd != nullptr) { PostMessageW(g_present_hwnd, WM_CLOSE, 0, 0); }
    if (g_present_thread != nullptr)
    {
        WaitForSingleObject(g_present_thread, 2000);
        CloseHandle(g_present_thread);
        g_present_thread = nullptr;
    }
    if (g_present_hwnd != nullptr) { DestroyWindow(g_present_hwnd); g_present_hwnd = nullptr; }
    g_present_tid = 0;
    g_present_w = g_present_h = 0;
    g_present_state = 0;
    // A fresh OpenPresent starts a fresh window: the reveal-on-first-Present
    // logic must run again (user: blank flash on mode switches).
    g_present_shown = false;
    g_present_revealed = false;
    // A fresh swap chain knows nothing about its colour space either.
    g_present_space_set = false;
    g_present_space = DXGI_COLOR_SPACE_RGB_FULL_G22_NONE_P709;
}

static bool OpenPresent(UINT width, UINT height, uint32_t flags)
{
    ClosePresent();
    if (width < 64 || height < 64 || width > 7680 || height > 4320)
    {
        Log("[present] refused: implausible window size %ux%u", width, height);
        return false;
    }
    g_present_w = width;
    g_present_h = height;
    ReadPresentOrigin();
    g_present_capturable = (flags & WINDOW_FLAG_CAPTURABLE) != 0;
    g_present_state = 0;
    g_present_thread = CreateThread(nullptr, 0, PresentWindowThread, nullptr, 0, &g_present_tid);
    if (g_present_thread == nullptr) { Log("[present] CreateThread failed"); return false; }
    for (int i = 0; i < 500 && g_present_state == 0; ++i) Sleep(4);   // up to 2 s for the window
    if (g_present_state != 1) { Log("[present] window did not come up"); ClosePresent(); return false; }

    HMODULE dxgi = LoadLibraryW(L"dxgi.dll");
    auto create_factory = dxgi ? reinterpret_cast<PFN_CreateDXGIFactory1_>(
                                     GetProcAddress(dxgi, "CreateDXGIFactory1")) : nullptr;
    if (create_factory == nullptr) { Log("[present] CreateDXGIFactory1 missing"); ClosePresent(); return false; }
    IDXGIFactory2 *factory = nullptr;
    HRESULT hr = create_factory(__uuidof(IDXGIFactory2), reinterpret_cast<void **>(&factory));
    if (FAILED(hr)) { Log("[present] CreateDXGIFactory1 failed 0x%08X", hr); ClosePresent(); return false; }

    DXGI_SWAP_CHAIN_DESC1 sd = {};
    sd.Width       = width;
    sd.Height      = height;
    sd.Format      = DXGI_FORMAT_R8G8B8A8_UNORM;   // must match VideoState::output for CopyResource
    sd.SampleDesc.Count = 1;
    sd.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
    sd.BufferCount = 2;
    sd.SwapEffect  = DXGI_SWAP_EFFECT_FLIP_DISCARD;
    sd.AlphaMode   = DXGI_ALPHA_MODE_IGNORE;
    IDXGISwapChain1 *sc1 = nullptr;
    // The swapchain must be created on the very queue that will write the back
    // buffer, which is the same queue NGX submits on.
    hr = factory->CreateSwapChainForHwnd(h.queue, g_present_hwnd, &sd, nullptr, nullptr, &sc1);
    if (FAILED(hr) || sc1 == nullptr)
    {
        Log("[present] CreateSwapChainForHwnd failed 0x%08X", hr);
        factory->Release(); ClosePresent(); return false;
    }
    factory->MakeWindowAssociation(g_present_hwnd, DXGI_MWA_NO_ALT_ENTER | DXGI_MWA_NO_WINDOW_CHANGES);
    factory->Release();
    hr = sc1->QueryInterface(__uuidof(IDXGISwapChain3), reinterpret_cast<void **>(&g_present_swap));
    sc1->Release();
    if (FAILED(hr) || g_present_swap == nullptr)
    {
        Log("[present] IDXGISwapChain3 unavailable 0x%08X", hr);
        ClosePresent(); return false;
    }
    // Click-through. The order follows the working recipe from display.py:
    // style -> SetLayeredWindowAttributes -> SWP_FRAMECHANGED. Neither
    // HTTRANSPARENT nor WS_EX_TRANSPARENT alone lets the click through
    // (measured by injection, _work/test_present_clickthrough.py) -
    // WS_EX_LAYERED is required. The styles are set AFTER the swapchain is
    // created: CreateSwapChainForHwnd refuses a layered window, while an
    // already created chain keeps working.
    const LONG ex = GetWindowLongW(g_present_hwnd, GWL_EXSTYLE);
    SetWindowLongW(g_present_hwnd, GWL_EXSTYLE, ex | WS_EX_LAYERED | WS_EX_TRANSPARENT);
    if (!SetLayeredWindowAttributes(g_present_hwnd, 0, 255, LWA_ALPHA))
        Log("[present] SetLayeredWindowAttributes failed, err=%lu", GetLastError());
    SetWindowPos(g_present_hwnd, nullptr, 0, 0, 0, 0,
                 SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED);

    Log("[present] overlay %ux%u ready (flip-discard, layered click-through%s)",
        width, height, g_present_capturable ? ", CAPTURABLE" : ", hidden from capture");
    return true;
}

// True when the result can go straight to the overlay instead of the client.
static bool PresentModeActive(const VideoState &v)
{
    if (g_present_swap == nullptr) return false;
    const UINT ow = v.upscale ? v.full_w : v.w;
    const UINT oh = v.upscale ? v.full_h : v.hgt;
    if (ow != g_present_w || oh != g_present_h)
    {
        static bool warned = false;
        if (!warned)
        {
            warned = true;
            Log("[present] output %ux%u does not match overlay %ux%u -- falling back to "
                "sending pixels to the client", ow, oh, g_present_w, g_present_h);
        }
        // A size change makes the overlay stale, not the pipeline: the client
        // rebuilds on its own when the window moves/resizes, and a one-time
        // warning is enough. The overlay must stay hidden rather than show a
        // stale-sized picture (audit C++ M1).
        g_present_shown = false;
        return false;
    }
    return true;
}

// One-window mode: keep the overlay exactly on the window being processed.
//
// The position is followed here, in the worker, because the worker is what
// knows which window it captures - and it is two cheap calls plus a
// SetWindowPos only when the window actually moved. The SIZE is deliberately
// left alone (SWP_NOSIZE): the swap chain, the textures and the client's
// shared memory are all built for one frame size, so a resize means rebuilding
// the pipeline, which only the client can do. Until it does, the picture keeps
// the old size in the corner of the window.
static void FollowCapturedWindow()
{
    if (!g_wgc_active || g_present_hwnd == nullptr || g_wgc_hwnd == nullptr) return;
    if (!IsWindow(g_wgc_hwnd)) return;      // the client notices and switches back
    if (IsIconic(g_wgc_hwnd))
    {
        // Minimised: the capture falls silent, so the overlay gets out of the
        // way instead of hanging a frozen frame over whatever is underneath.
        if (g_present_shown)
        {
            ShowWindow(g_present_hwnd, SW_HIDE);
            g_present_shown = false;
            Log("[wgc] the window is minimised - the overlay is hidden");
        }
        return;
    }
    RECT r = {};
    // DWMWA_EXTENDED_FRAME_BOUNDS, not GetWindowRect: the latter includes the
    // invisible resize border, so the overlay would sit several pixels off
    // and the picture would not line up with the window under it. This is
    // also the rectangle Windows Graphics Capture hands over.
    if (FAILED(DwmGetWindowAttribute(g_wgc_hwnd, DWMWA_EXTENDED_FRAME_BOUNDS,
                                     &r, sizeof(r))) &&
        !GetWindowRect(g_wgc_hwnd, &r))
        return;
    if (!g_present_shown && g_present_revealed)
    {
        ShowWindow(g_present_hwnd, SW_SHOWNOACTIVATE);
        g_present_shown = true;
        Log("[wgc] the window is back - the overlay is shown");
    }
    if (r.left != g_present_follow.left || r.top != g_present_follow.top ||
        r.right != g_present_follow.right || r.bottom != g_present_follow.bottom)
    {
        g_present_follow = r;
        SetWindowPos(g_present_hwnd, HWND_TOPMOST, r.left, r.top, 0, 0,
                     SWP_NOSIZE | SWP_NOACTIVATE);
    }
}

// Come back on top now and then. A game that goes fullscreen raises its own
// window above every topmost window, ours included, and the picture would
// stay underneath for as long as the game runs. Rare on purpose: the client
// re-asserts the HUD above us much more often, and doing this every frame
// would leave the HUD blinking under the picture.
// The check is "is the topmost window ours?" - in the steady state the
// answer is yes (both our windows are topmost), so this is zero
// SetWindowPos calls and no DWM flicker. A borderless game (Cyberpunk)
// keeps itself on top and would hide the picture forever without this.
static void ReassertPresentTopmost()
{
    if (g_present_hwnd == nullptr) return;
    static uint32_t tick = 0;
    if ((++tick % 300) != 0) return;
    HWND top = GetTopWindow(0);
    if (top == nullptr || top == g_present_hwnd) return;
    wchar_t cls[64];
    if (GetClassNameW(top, cls, 64) > 0 && wcscmp(cls, L"pygame") == 0)
        return;  // the HUD is on top - leave it there
    RECT r;
    if (GetWindowRect(top, &r) && r.right == r.left && r.bottom == r.top)
        return;  // zero-sized (IME, helpers) cannot cover the picture
    SetWindowPos(g_present_hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                 SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE);
}

// Show the present window on its first successful Present.
//
// The window is created hidden (PresentWindowThread) so a blank topmost
// window never flashes over the desktop during the NGX warm-up. The first
// frame that actually presents is the moment the window becomes visible -
// it appears with a picture already on it (user: screen flashes black on
// startup and on one-window mode switches).
static void RevealOnFirstPresent()
{
    if (g_present_hwnd == nullptr || g_present_revealed) return;
    ShowWindow(g_present_hwnd, SW_SHOWNOACTIVATE);
    g_present_shown = true;
    g_present_revealed = true;
    Log("[present] window revealed on the first Present");
}

static bool PresentFrame(VideoState &v)
{
    if (g_hdr_capture) return PresentHdr(v, false);
    if (!EnsurePresentFormat(false)) return false;
    ID3D12Resource *bb = nullptr;
    if (FAILED(g_present_swap->GetBuffer(g_present_swap->GetCurrentBackBufferIndex(),
                                         __uuidof(ID3D12Resource),
                                         reinterpret_cast<void **>(&bb))) || bb == nullptr)
    { Log("[present] GetBuffer failed"); return false; }

    bool ok = false;
    if (BeginCommands())
    {
        D3D12_RESOURCE_BARRIER pre[] = {
            Transition(bb, D3D12_RESOURCE_STATE_PRESENT, D3D12_RESOURCE_STATE_COPY_DEST),
            Transition(v.output, D3D12_RESOURCE_STATE_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_COPY_SOURCE),
        };
        h.list->ResourceBarrier(_countof(pre), pre);
        h.list->CopyResource(bb, v.output);
        SpoutBridgeCopy(h.list, v.output, v.upscale ? v.full_w : v.w,
                        v.upscale ? v.full_h : v.hgt);
        D3D12_RESOURCE_BARRIER post[] = {
            Transition(bb, D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_PRESENT),
            Transition(v.output, D3D12_RESOURCE_STATE_COPY_SOURCE, D3D12_RESOURCE_STATE_UNORDERED_ACCESS),
        };
        h.list->ResourceBarrier(_countof(post), post);
        const UINT64 fv = EndCommands();
        if (WaitFenceValue(h.fence, fv, 2000))
        {
            ok = SUCCEEDED(g_present_swap->Present(0, 0));
            SpoutBridgeSend();
        }
        else
            Log("[present] fence wait timed out");
    }
    bb->Release();
    if (ok) RevealOnFirstPresent();
    return ok;
}

// NR OFF (FRAME_FLAG_BYPASS): show the raw capture (v.color) instead of the NGX result.
// v.color is full-res in upscale mode and work-res in 1:1 - it matches the
// window that PresentModeActive has already checked against the output size.
static bool PresentBypass(VideoState &v)
{
    if (g_hdr_capture) return PresentHdr(v, true);
    if (!EnsurePresentFormat(false)) return false;
    ID3D12Resource *bb = nullptr;
    if (FAILED(g_present_swap->GetBuffer(g_present_swap->GetCurrentBackBufferIndex(),
                                         __uuidof(ID3D12Resource),
                                         reinterpret_cast<void **>(&bb))) || bb == nullptr)
    { Log("[present] bypass GetBuffer failed"); return false; }

    bool ok = false;
    if (BeginCommands())
    {
        D3D12_RESOURCE_BARRIER pre[] = {
            Transition(bb, D3D12_RESOURCE_STATE_PRESENT, D3D12_RESOURCE_STATE_COPY_DEST),
            Transition(v.color.tex, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE,
                       D3D12_RESOURCE_STATE_COPY_SOURCE),
        };
        h.list->ResourceBarrier(_countof(pre), pre);
        h.list->CopyResource(bb, v.color.tex);
        SpoutBridgeCopy(h.list, v.color.tex, v.upscale ? v.full_w : v.w,
                        v.upscale ? v.full_h : v.hgt);
        D3D12_RESOURCE_BARRIER post[] = {
            Transition(bb, D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_PRESENT),
            Transition(v.color.tex, D3D12_RESOURCE_STATE_COPY_SOURCE,
                       D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE),
        };
        h.list->ResourceBarrier(_countof(post), post);
        const UINT64 fv = EndCommands();
        if (WaitFenceValue(h.fence, fv, 2000))
        {
            ok = SUCCEEDED(g_present_swap->Present(0, 0));
            SpoutBridgeSend();
        }
        else
            Log("[present] bypass fence wait timed out");
    }
    bb->Release();
    if (ok) RevealOnFirstPresent();
    return ok;
}

static bool ReadExact(FILE *f, void *p, size_t n)
{
    BYTE *dst = static_cast<BYTE *>(p);
    while (n != 0)
    {
        const size_t got = fread(dst, 1, n, f);
        if (got == 0) return false;
        dst += got; n -= got;
    }
    return true;
}

static bool WriteExact(FILE *f, const void *p, size_t n)
{
    const BYTE *src = static_cast<const BYTE *>(p);
    while (n != 0)
    {
        const size_t put = fwrite(src, 1, n, f);
        if (put == 0) return false;
        src += put; n -= put;
    }
    fflush(f);
    return true;
}

static bool CreateVideoTex(VideoTex &v, UINT w, UINT hgt, DXGI_FORMAT fmt, UINT packed_pitch,
                           D3D12_RESOURCE_FLAGS res_flags = D3D12_RESOURCE_FLAG_NONE)
{
    D3D12_HEAP_PROPERTIES def = {};
    def.Type = D3D12_HEAP_TYPE_DEFAULT;
    D3D12_RESOURCE_DESC td = {};
    td.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
    td.Width = w; td.Height = hgt; td.DepthOrArraySize = 1; td.MipLevels = 1;
    td.Format = fmt; td.SampleDesc.Count = 1; td.Layout = D3D12_TEXTURE_LAYOUT_UNKNOWN;
    td.Flags = res_flags;
    if (FAILED(h.dev->CreateCommittedResource(&def, D3D12_HEAP_FLAG_NONE, &td, D3D12_RESOURCE_STATE_COPY_DEST,
        nullptr, __uuidof(ID3D12Resource), reinterpret_cast<void **>(&v.tex)))) return false;

    UINT64 total = 0;
    h.dev->GetCopyableFootprints(&td, 0, 1, 0, &v.fp, &v.rows, &v.row_size, &total);
    if (v.rows != hgt || v.row_size < packed_pitch) return false;
    D3D12_HEAP_PROPERTIES up = {};
    up.Type = D3D12_HEAP_TYPE_UPLOAD;
    D3D12_RESOURCE_DESC bd = {};
    bd.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
    bd.Width = total; bd.Height = 1; bd.DepthOrArraySize = 1; bd.MipLevels = 1;
    bd.SampleDesc.Count = 1; bd.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
    return SUCCEEDED(h.dev->CreateCommittedResource(&up, D3D12_HEAP_FLAG_NONE, &bd,
        D3D12_RESOURCE_STATE_GENERIC_READ, nullptr, __uuidof(ID3D12Resource),
        reinterpret_cast<void **>(&v.upload)));
}

// Defined below with the rest of the scaling pipeline; needed here because the
// working textures cannot be created without it.
static bool EnsureScalePipeline();

// NS_NR_SMALL=1: run the network at the work resolution instead of the screen
// resolution. Off by default while the picture is being compared side by side.
static bool NrSmallRequested()
{
    static int cached = -1;
    if (cached < 0)
    {
        char buf[8] = {};
        const DWORD got = GetEnvironmentVariableA("NS_NR_SMALL", buf, sizeof(buf));
        cached = (got > 0 && got < sizeof(buf) && buf[0] == '1') ? 1 : 0;
    }
    return cached == 1;
}

// NS_NR_RESIDUAL=0: force the plain upscale path (tests only). By default
// the matched residual composite is ALWAYS on with nr_small - the user's
// single resolution slider must never produce a soft picture (the whole
// point of the Cost-Scaler principle: cheap network + 1:1 native anchor).
static bool ResidualRequested()
{
    char buf[8] = {};
    const DWORD got = GetEnvironmentVariableA("NS_NR_RESIDUAL", buf, sizeof(buf));
    return !(got > 0 && got < sizeof(buf) && buf[0] == '0');
}

// NS_NR_RESIDUAL_STRENGTH=0..1: how much of the neural delta is applied.
// 0.0 is a real value (the native frame untouched) - do not treat it as unset.
static float ResidualStrengthRequested()
{
    char buf[16] = {};
    const DWORD got = GetEnvironmentVariableA("NS_NR_RESIDUAL_STRENGTH", buf, sizeof(buf));
    if (got > 0 && got < sizeof(buf))
    {
        const float f = static_cast<float>(atof(buf));
        if (f >= 0.0f && f <= 2.0f) return f;
    }
    return 1.0f;
}

// want_small < 0 means "whatever NS_NR_SMALL says" - used for the very first
// creation, before the client has had a chance to ask for anything.
static bool CreateVideoResources(VideoState &v, UINT w, UINT hgt, UINT full_w = 0, UINT full_h = 0,
                                 int want_small = -1)
{
    v.w = w; v.hgt = hgt;
    v.full_w = full_w; v.full_h = full_h;
    v.upscale = (full_w > 0 && full_h > 0 && (full_w != w || full_h != hgt));
    // Only worth doing when the work resolution is actually smaller: at
    // work == full there is nothing to scale and the two extra passes would
    // be pure loss.
    const bool asked = (want_small < 0) ? NrSmallRequested() : (want_small != 0);
    v.nr_small = v.upscale && asked;
    v.nr_w = v.nr_small ? w : 0;
    v.nr_h = v.nr_small ? hgt : 0;
    // Residual compose rides on nr_small: at work == full there is nothing
    // to compose (native would equal nr_in). Env-overridable for now; the
    // menu wiring lands with the release.
    v.residual = v.nr_small && ResidualRequested();
    v.residual_strength = v.residual ? ResidualStrengthRequested() : 1.0f;
    const UINT cw = v.upscale ? full_w : w;   // color texture: full-res in upscale mode
    const UINT ch = v.upscale ? full_h : hgt;
    if (!CreateVideoTex(v.color, cw, ch, DXGI_FORMAT_R8G8B8A8_UNORM, cw * 4) ||
        // ALLOW_UNORDERED_ACCESS: the motion field upscale shader writes into it
        !CreateVideoTex(v.mv, w, hgt, DXGI_FORMAT_R16G16_FLOAT, w * 4,
                        D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS))
        return false;

    D3D12_HEAP_PROPERTIES def = {};
    def.Type = D3D12_HEAP_TYPE_DEFAULT;
    D3D12_RESOURCE_DESC td = {};
    td.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
    td.Width = cw; td.Height = ch; td.DepthOrArraySize = 1; td.MipLevels = 1;
    td.Format = DXGI_FORMAT_R8G8B8A8_UNORM; td.SampleDesc.Count = 1;
    td.Layout = D3D12_TEXTURE_LAYOUT_UNKNOWN;
    td.Flags = D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS;
    if (FAILED(h.dev->CreateCommittedResource(&def, D3D12_HEAP_FLAG_NONE, &td,
        D3D12_RESOURCE_STATE_UNORDERED_ACCESS, nullptr, __uuidof(ID3D12Resource),
        reinterpret_cast<void **>(&v.output)))) return false;

    if (v.nr_small)
    {
        // The scaling pipeline is normally brought up by MOTS; here it is
        // needed whether or not the client ever negotiated a motion size.
        if (!EnsureScalePipeline()) { v.nr_small = false; }
        else
        {
            D3D12_RESOURCE_DESC nd = td;
            nd.Width = v.nr_w; nd.Height = v.nr_h;
            const bool ok =
                SUCCEEDED(h.dev->CreateCommittedResource(
                    &def, D3D12_HEAP_FLAG_NONE, &nd,
                    D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE, nullptr,
                    __uuidof(ID3D12Resource), reinterpret_cast<void **>(&v.nr_in))) &&
                SUCCEEDED(h.dev->CreateCommittedResource(
                    &def, D3D12_HEAP_FLAG_NONE, &nd,
                    D3D12_RESOURCE_STATE_UNORDERED_ACCESS, nullptr,
                    __uuidof(ID3D12Resource), reinterpret_cast<void **>(&v.nr_out)));
            if (!ok)
            {
                Log("[nr] %ux%u working textures failed - staying at full resolution",
                    v.nr_w, v.nr_h);
                if (v.nr_in != nullptr) { v.nr_in->Release(); v.nr_in = nullptr; }
                if (v.nr_out != nullptr) { v.nr_out->Release(); v.nr_out = nullptr; }
                v.nr_small = false;
            }
            else
                Log("[nr] network runs at %ux%u, scaled back to %ux%u",
                    v.nr_w, v.nr_h, cw, ch);
        }
    }

    UINT64 total = 0;
    h.dev->GetCopyableFootprints(&td, 0, 1, 0, &v.out_fp, &v.out_rows, &v.out_row_size, &total);
    D3D12_HEAP_PROPERTIES rb = {};
    rb.Type = D3D12_HEAP_TYPE_READBACK;
    D3D12_RESOURCE_DESC bd = {};
    bd.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
    bd.Width = total; bd.Height = 1; bd.DepthOrArraySize = 1; bd.MipLevels = 1;
    bd.SampleDesc.Count = 1; bd.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
    return SUCCEEDED(h.dev->CreateCommittedResource(&rb, D3D12_HEAP_FLAG_NONE, &bd,
        D3D12_RESOURCE_STATE_COPY_DEST, nullptr, __uuidof(ID3D12Resource),
        reinterpret_cast<void **>(&v.readback)));
}

static bool FillUpload(VideoTex &v, const BYTE *src, UINT src_pitch, UINT hgt)
{
    BYTE *dst = nullptr;
    if (FAILED(v.upload->Map(0, nullptr, reinterpret_cast<void **>(&dst)))) return false;
    for (UINT y = 0; y < hgt; ++y)
        memcpy(dst + static_cast<size_t>(y) * v.fp.Footprint.RowPitch,
               src + static_cast<size_t>(y) * src_pitch, src_pitch);
    v.upload->Unmap(0, nullptr);
    return true;
}

static void CopyUpload(ID3D12GraphicsCommandList *list, VideoTex &v)
{
    D3D12_TEXTURE_COPY_LOCATION src = {}, dst = {};
    src.pResource = v.upload;
    src.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
    src.PlacedFootprint = v.fp;
    dst.pResource = v.tex;
    dst.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
    list->CopyTextureRegion(&dst, 0, 0, 0, &src, nullptr);
}

static D3D12_RESOURCE_BARRIER Transition(ID3D12Resource *r, D3D12_RESOURCE_STATES before,
                                         D3D12_RESOURCE_STATES after)
{
    D3D12_RESOURCE_BARRIER b = {};
    b.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
    b.Transition.pResource = r;
    b.Transition.StateBefore = before;
    b.Transition.StateAfter = after;
    b.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
    return b;
}

// ---------------------------------------------------------------------------
// The scaling compute shader. For now it is used to upscale the motion field
// (the client sends it at the optical-flow resolution, ~320x180, instead of
// the work resolution) - that saved the CPU ~8 ms per frame: 6 million values,
// a resize plus a conversion to float16.
//
// Bilinear sampling with edge clamping - the same interpolation
// cv2.resize(INTER_LINEAR) did: pixel centres are taken as (i + 0.5) / size.
// ---------------------------------------------------------------------------
// Bilinear is done by hand instead of through the hardware sampler: sampler
// weights are quantised to 1/256 of a texel, and over a sevenfold upscale
// that showed up as up to 21 levels of difference in the final NGX frame
// against the cv2 path (measured). Here the weights are float32 and the
// mapping is exactly cv2.resize(INTER_LINEAR):
//     src = (dst + 0.5) * srcSize / dstSize - 0.5, edges clamped.
static const char kScaleHlsl[] =
    "Texture2D<float2>   gSrc : register(t0);\n"
    "RWTexture2D<float2> gDst : register(u0);\n"
    "cbuffer Sizes : register(b0) { uint gDstW; uint gDstH; uint gSrcW; uint gSrcH; };\n"
    "[numthreads(8, 8, 1)]\n"
    "void CSMain(uint3 id : SV_DispatchThreadID)\n"
    "{\n"
    "    if (id.x >= gDstW || id.y >= gDstH) return;\n"
    "    float2 src = (float2(id.xy) + 0.5f) * float2(gSrcW, gSrcH)\n"
    "                 / float2(gDstW, gDstH) - 0.5f;\n"
    "    float2 f  = frac(src);\n"
    "    int2   p0 = int2(floor(src));\n"
    "    int2   hi = int2(gSrcW - 1, gSrcH - 1);\n"
    "    int2   a  = clamp(p0,              int2(0, 0), hi);\n"
    "    int2   b  = clamp(p0 + int2(1, 1), int2(0, 0), hi);\n"
    "    float2 v00 = gSrc[int2(a.x, a.y)];\n"
    "    float2 v10 = gSrc[int2(b.x, a.y)];\n"
    "    float2 v01 = gSrc[int2(a.x, b.y)];\n"
    "    float2 v11 = gSrc[int2(b.x, b.y)];\n"
    "    gDst[id.xy] = lerp(lerp(v00, v10, f.x), lerp(v01, v11, f.x), f.y);\n"
    "}\n";

// The same scaler for four-channel colour (RGBA8). Kept as a separate shader
// rather than one generic float4 pass over both: the motion field is
// R16G16_FLOAT, and a typed UAV has to match the resource format exactly.
//
// Used to run Neural Rendering on a smaller frame than the screen. The network
// is same-resolution - it enhances, it does not upscale - so its cost tracks
// the pixel count it is handed: measured 1.50 ms fixed + 1.51 ms per megapixel
// on a 5070 Ti. Feeding it the work resolution and scaling the result back is
// therefore worth about half the frame time at 4K.
static const char kScaleHlsl4[] =
    "Texture2D<float4>   gSrc : register(t0);\n"
    "RWTexture2D<float4> gDst : register(u0);\n"
    "cbuffer Sizes : register(b0) { uint gDstW; uint gDstH; uint gSrcW; uint gSrcH; };\n"
    "[numthreads(8, 8, 1)]\n"
    "void CSMain(uint3 id : SV_DispatchThreadID)\n"
    "{\n"
    "    if (id.x >= gDstW || id.y >= gDstH) return;\n"
    "    float2 src = (float2(id.xy) + 0.5f) * float2(gSrcW, gSrcH)\n"
    "                 / float2(gDstW, gDstH) - 0.5f;\n"
    "    float2 f  = frac(src);\n"
    "    int2   p0 = int2(floor(src));\n"
    "    int2   hi = int2(gSrcW - 1, gSrcH - 1);\n"
    "    int2   a  = clamp(p0,              int2(0, 0), hi);\n"
    "    int2   b  = clamp(p0 + int2(1, 1), int2(0, 0), hi);\n"
    "    float4 v00 = gSrc[int2(a.x, a.y)];\n"
    "    float4 v10 = gSrc[int2(b.x, a.y)];\n"
    "    float4 v01 = gSrc[int2(a.x, b.y)];\n"
    "    float4 v11 = gSrc[int2(b.x, b.y)];\n"
    "    gDst[id.xy] = lerp(lerp(v00, v10, f.x), lerp(v01, v11, f.x), f.y);\n"
    "}\n";

// Matched residual composite (the DLSSNR-Cost-Scaler principle): run the
// network at the work resolution, then compose the neural delta onto the
// pristine 1:1 native frame instead of stretching the low-res result.
//   edit   = nr_out(up) - nr_in(up)   -- what the network changed
//   result = native + edit * strength  -- the native frame stays the anchor,
//                                        so text/edges keep full sharpness
// The arithmetic is in DISPLAY space (the textures are UNORM and the network
// works on those values, so the delta matches its input). Not linear light:
// deliberate, and the place to look first if shadows ever misbehave.
// while the cheap low-res network does the relighting.
static const char kResidualHlsl[] =
    "Texture2D<float4>   gNative : register(t0);\n"
    "Texture2D<float4>   gNrIn   : register(t1);\n"
    "Texture2D<float4>   gNrOut  : register(t2);\n"
    "RWTexture2D<float4> gDst    : register(u0);\n"
    "cbuffer RC : register(b0) { uint gW; uint gH; float gStrength; };\n"
    "SamplerState gSamp : register(s0);\n"
    "[numthreads(8, 8, 1)]\n"
    "void CSMain(uint3 id : SV_DispatchThreadID)\n"
    "{\n"
    "    if (id.x >= gW || id.y >= gH) return;\n"
    "    float2 uv = (float2(id.xy) + 0.5f) / float2(gW, gH);\n"
    "    float3 native = gNative[id.xy].rgb;\n"
    "    float3 nrIn  = gNrIn.SampleLevel(gSamp, uv, 0).rgb;\n"
    "    float3 nrOut = gNrOut.SampleLevel(gSamp, uv, 0).rgb;\n"
    "    float3 result = max(native + (nrOut - nrIn) * gStrength, 0.0);\n"
    "    gDst[id.xy] = float4(result, 1.0);\n"
    "}\n";

typedef HRESULT(WINAPI *PFN_D3DCompile_)(LPCVOID, SIZE_T, LPCSTR, const void *, void *,
                                         LPCSTR, LPCSTR, UINT, UINT, ID3DBlob **, ID3DBlob **);
typedef HRESULT(WINAPI *PFN_D3D12SerializeRootSignature_)(const D3D12_ROOT_SIGNATURE_DESC *,
                                                          D3D_ROOT_SIGNATURE_VERSION,
                                                          ID3DBlob **, ID3DBlob **);

static ID3D12RootSignature  *g_scale_rs;
static ID3D12PipelineState  *g_scale_pso;
static ID3D12DescriptorHeap *g_scale_heap;
static ID3D12Resource       *g_scale_src_bound;   // which resources already have descriptors
static ID3D12Resource       *g_scale_dst_bound;
static ID3D12PipelineState  *g_scale4_pso;        // the RGBA8 variant, for colour
static ID3D12DescriptorHeap *g_scale4_heap;
static ID3D12Resource       *g_scale4_src_bound;   // slot 0: the downscale pair
static ID3D12Resource       *g_scale4_dst_bound;
static ID3D12Resource       *g_scale4_src_bound2;  // slot 1: the upscale pair
static ID3D12Resource       *g_scale4_dst_bound2;
// Matched residual composite pass: native + (nr_out - nr_in) * strength.
// One descriptor set of three SRVs + one UAV, rebuilt on resource change.
static ID3D12RootSignature  *g_residual_rs;   // kept for the dispatch (PSO owns its own ref)
static ID3D12PipelineState  *g_residual_pso;
static ID3D12DescriptorHeap *g_residual_heap;
static ID3D12Resource       *g_res_native_bound;
static ID3D12Resource       *g_res_in_bound;
static ID3D12Resource       *g_res_out_bound;
static ID3D12Resource       *g_res_dst_bound;
static VideoTex              g_motion_small;      // motion field at flow resolution
static UINT                  g_motion_w, g_motion_h;  // 0 = motion arrives at work resolution

static void ReleaseVideoTex(VideoTex &t)
{
    if (t.tex != nullptr) { t.tex->Release(); t.tex = nullptr; }
    if (t.upload != nullptr) { t.upload->Release(); t.upload = nullptr; }
}

static void CloseMotionScaler()
{
    ReleaseVideoTex(g_motion_small);
    g_motion_w = g_motion_h = 0;
    g_scale_src_bound = nullptr;
    g_scale_dst_bound = nullptr;
}

// Shader and root signature compilation - once per process lifetime.
static bool EnsureScalePipeline()
{
    if (g_scale_pso != nullptr) return true;

    HMODULE compiler = LoadLibraryW(L"d3dcompiler_47.dll");
    auto compile = compiler ? reinterpret_cast<PFN_D3DCompile_>(
                                  GetProcAddress(compiler, "D3DCompile")) : nullptr;
    HMODULE d3d12 = GetModuleHandleW(L"d3d12.dll");
    auto serialize = d3d12 ? reinterpret_cast<PFN_D3D12SerializeRootSignature_>(
                                 GetProcAddress(d3d12, "D3D12SerializeRootSignature")) : nullptr;
    if (compile == nullptr || serialize == nullptr)
    { Log("[scale] D3DCompile/D3D12SerializeRootSignature unavailable"); return false; }

    ID3DBlob *code = nullptr, *errors = nullptr;
    HRESULT hr = compile(kScaleHlsl, sizeof(kScaleHlsl) - 1, "scale.hlsl", nullptr, nullptr,
                         "CSMain", "cs_5_0", 0, 0, &code, &errors);
    if (FAILED(hr) || code == nullptr)
    {
        Log("[scale] shader compile failed 0x%08X: %s", hr,
            errors ? static_cast<const char *>(errors->GetBufferPointer()) : "(no log)");
        if (errors) errors->Release();
        return false;
    }
    if (errors) errors->Release();

    D3D12_DESCRIPTOR_RANGE ranges[2] = {};
    ranges[0].RangeType = D3D12_DESCRIPTOR_RANGE_TYPE_SRV;
    ranges[0].NumDescriptors = 1;
    ranges[0].BaseShaderRegister = 0;
    ranges[0].OffsetInDescriptorsFromTableStart = 0;
    ranges[1].RangeType = D3D12_DESCRIPTOR_RANGE_TYPE_UAV;
    ranges[1].NumDescriptors = 1;
    ranges[1].BaseShaderRegister = 0;
    ranges[1].OffsetInDescriptorsFromTableStart = 1;

    D3D12_ROOT_PARAMETER params[2] = {};
    params[0].ParameterType = D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS;
    params[0].Constants.ShaderRegister = 0;
    params[0].Constants.Num32BitValues = 4;
    params[0].ShaderVisibility = D3D12_SHADER_VISIBILITY_ALL;
    params[1].ParameterType = D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE;
    params[1].DescriptorTable.NumDescriptorRanges = 2;
    params[1].DescriptorTable.pDescriptorRanges = ranges;
    params[1].ShaderVisibility = D3D12_SHADER_VISIBILITY_ALL;

    D3D12_STATIC_SAMPLER_DESC samp = {};
    samp.Filter = D3D12_FILTER_MIN_MAG_MIP_LINEAR;
    samp.AddressU = samp.AddressV = samp.AddressW = D3D12_TEXTURE_ADDRESS_MODE_CLAMP;
    samp.MaxLOD = D3D12_FLOAT32_MAX;
    samp.ShaderRegister = 0;
    samp.ShaderVisibility = D3D12_SHADER_VISIBILITY_ALL;

    D3D12_ROOT_SIGNATURE_DESC rsd = {};
    rsd.NumParameters = _countof(params);
    rsd.pParameters = params;
    rsd.NumStaticSamplers = 1;
    rsd.pStaticSamplers = &samp;

    ID3DBlob *rs_blob = nullptr;
    hr = serialize(&rsd, D3D_ROOT_SIGNATURE_VERSION_1, &rs_blob, &errors);
    if (FAILED(hr) || rs_blob == nullptr)
    {
        Log("[scale] root signature serialize failed 0x%08X: %s", hr,
            errors ? static_cast<const char *>(errors->GetBufferPointer()) : "(no log)");
        if (errors) errors->Release();
        code->Release();
        return false;
    }
    if (errors) errors->Release();
    hr = h.dev->CreateRootSignature(0, rs_blob->GetBufferPointer(), rs_blob->GetBufferSize(),
                                    __uuidof(ID3D12RootSignature),
                                    reinterpret_cast<void **>(&g_scale_rs));
    rs_blob->Release();
    if (FAILED(hr)) { Log("[scale] CreateRootSignature failed 0x%08X", hr); code->Release(); return false; }

    D3D12_COMPUTE_PIPELINE_STATE_DESC pd = {};
    pd.pRootSignature = g_scale_rs;
    pd.CS.pShaderBytecode = code->GetBufferPointer();
    pd.CS.BytecodeLength = code->GetBufferSize();
    hr = h.dev->CreateComputePipelineState(&pd, __uuidof(ID3D12PipelineState),
                                           reinterpret_cast<void **>(&g_scale_pso));
    code->Release();
    if (FAILED(hr)) { Log("[scale] CreateComputePipelineState failed 0x%08X", hr); return false; }

    D3D12_DESCRIPTOR_HEAP_DESC hd = {};
    hd.Type = D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV;
    hd.NumDescriptors = 2;
    hd.Flags = D3D12_DESCRIPTOR_HEAP_FLAG_SHADER_VISIBLE;
    hr = h.dev->CreateDescriptorHeap(&hd, __uuidof(ID3D12DescriptorHeap),
                                     reinterpret_cast<void **>(&g_scale_heap));
    if (FAILED(hr)) { Log("[scale] CreateDescriptorHeap failed 0x%08X", hr); return false; }

    // The colour variant. Same root signature - the layout does not depend on
    // the texture format - so only the shader and a heap of its own are new.
    ID3DBlob *code4 = nullptr;
    hr = compile(kScaleHlsl4, sizeof(kScaleHlsl4) - 1, "scale4.hlsl", nullptr, nullptr,
                 "CSMain", "cs_5_0", 0, 0, &code4, &errors);
    if (FAILED(hr) || code4 == nullptr)
    {
        Log("[scale] colour shader compile failed 0x%08X: %s", hr,
            errors ? static_cast<const char *>(errors->GetBufferPointer()) : "(no log)");
        if (errors) errors->Release();
        return false;
    }
    if (errors) errors->Release();
    pd.CS.pShaderBytecode = code4->GetBufferPointer();
    pd.CS.BytecodeLength = code4->GetBufferSize();
    hr = h.dev->CreateComputePipelineState(&pd, __uuidof(ID3D12PipelineState),
                                           reinterpret_cast<void **>(&g_scale4_pso));
    code4->Release();
    if (FAILED(hr)) { Log("[scale] colour pipeline failed 0x%08X", hr); return false; }
    hd.NumDescriptors = 4;   // two SRV/UAV pairs: one to scale down, one up
    hr = h.dev->CreateDescriptorHeap(&hd, __uuidof(ID3D12DescriptorHeap),
                                     reinterpret_cast<void **>(&g_scale4_heap));
    if (FAILED(hr)) { Log("[scale] colour descriptor heap failed 0x%08X", hr); return false; }

    Log("[scale] compute pipeline ready (bilinear, clamp; motion and colour)");

    // --- Matched residual composite pipeline -------------------------------
    // Three SRVs (native, nr_in, nr_out) + one UAV (dst), a 32-bit-constants
    // cbuffer (w, h, strength) and the same linear-clamp sampler. A separate
    // root signature because the layout differs from the scale pair.
    ID3DBlob *res_code = nullptr;
    hr = compile(kResidualHlsl, sizeof(kResidualHlsl) - 1, "residual.hlsl", nullptr, nullptr,
                 "CSMain", "cs_5_0", 0, 0, &res_code, &errors);
    if (FAILED(hr) || res_code == nullptr)
    {
        Log("[residual] shader compile failed 0x%08X: %s", hr,
            errors ? static_cast<const char *>(errors->GetBufferPointer()) : "(no log)");
        if (errors) errors->Release();
        return false;
    }
    if (errors) errors->Release();

    D3D12_DESCRIPTOR_RANGE rres[2] = {};
    rres[0].RangeType = D3D12_DESCRIPTOR_RANGE_TYPE_SRV;
    rres[0].NumDescriptors = 3;
    rres[0].BaseShaderRegister = 0;
    rres[0].OffsetInDescriptorsFromTableStart = 0;
    rres[1].RangeType = D3D12_DESCRIPTOR_RANGE_TYPE_UAV;
    rres[1].NumDescriptors = 1;
    rres[1].BaseShaderRegister = 0;
    rres[1].OffsetInDescriptorsFromTableStart = 3;

    D3D12_ROOT_PARAMETER rparams[2] = {};
    rparams[0].ParameterType = D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS;
    rparams[0].Constants.ShaderRegister = 0;
    rparams[0].Constants.Num32BitValues = 3;   // gW, gH, gStrength
    rparams[0].ShaderVisibility = D3D12_SHADER_VISIBILITY_ALL;
    rparams[1].ParameterType = D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE;
    rparams[1].DescriptorTable.NumDescriptorRanges = 2;
    rparams[1].DescriptorTable.pDescriptorRanges = rres;
    rparams[1].ShaderVisibility = D3D12_SHADER_VISIBILITY_ALL;

    D3D12_ROOT_SIGNATURE_DESC rrsd = {};
    rrsd.NumParameters = _countof(rparams);
    rrsd.pParameters = rparams;
    rrsd.NumStaticSamplers = 1;
    rrsd.pStaticSamplers = &samp;

    ID3DBlob *rs_res = nullptr;
    hr = serialize(&rrsd, D3D_ROOT_SIGNATURE_VERSION_1, &rs_res, &errors);
    if (FAILED(hr) || rs_res == nullptr)
    {
        Log("[residual] root signature serialize failed 0x%08X", hr);
        if (errors) errors->Release();
        res_code->Release();
        return false;
    }
    if (errors) errors->Release();
    hr = h.dev->CreateRootSignature(0, rs_res->GetBufferPointer(), rs_res->GetBufferSize(),
                                    __uuidof(ID3D12RootSignature),
                                    reinterpret_cast<void **>(&g_residual_rs));
    rs_res->Release();
    if (FAILED(hr)) { Log("[residual] CreateRootSignature failed 0x%08X", hr); res_code->Release(); return false; }

    D3D12_COMPUTE_PIPELINE_STATE_DESC rpd = {};
    rpd.pRootSignature = g_residual_rs;
    rpd.CS.pShaderBytecode = res_code->GetBufferPointer();
    rpd.CS.BytecodeLength = res_code->GetBufferSize();
    hr = h.dev->CreateComputePipelineState(&rpd, __uuidof(ID3D12PipelineState),
                                           reinterpret_cast<void **>(&g_residual_pso));
    res_code->Release();
    if (FAILED(hr)) { Log("[residual] CreateComputePipelineState failed 0x%08X", hr); return false; }

    D3D12_DESCRIPTOR_HEAP_DESC rhd = {};
    rhd.Type = D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV;
    rhd.NumDescriptors = 4;
    rhd.Flags = D3D12_DESCRIPTOR_HEAP_FLAG_SHADER_VISIBLE;
    hr = h.dev->CreateDescriptorHeap(&rhd, __uuidof(ID3D12DescriptorHeap),
                                     reinterpret_cast<void **>(&g_residual_heap));
    if (FAILED(hr)) { Log("[residual] descriptor heap failed 0x%08X", hr); return false; }

    Log("[residual] compute pipeline ready (native + (nr_out - nr_in) * strength)");
    return true;
}

// The descriptors are recreated only when the resources change (RNSZ, MOTS).
static void BindScaleDescriptors(ID3D12Resource *src, ID3D12Resource *dst)
{
    if (src == g_scale_src_bound && dst == g_scale_dst_bound) return;
    const UINT stride = h.dev->GetDescriptorHandleIncrementSize(
        D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV);
    D3D12_CPU_DESCRIPTOR_HANDLE cpu = g_scale_heap->GetCPUDescriptorHandleForHeapStart();

    D3D12_SHADER_RESOURCE_VIEW_DESC sd = {};
    sd.Format = DXGI_FORMAT_R16G16_FLOAT;
    sd.ViewDimension = D3D12_SRV_DIMENSION_TEXTURE2D;
    sd.Shader4ComponentMapping = D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING;
    sd.Texture2D.MipLevels = 1;
    h.dev->CreateShaderResourceView(src, &sd, cpu);

    D3D12_UNORDERED_ACCESS_VIEW_DESC ud = {};
    ud.Format = DXGI_FORMAT_R16G16_FLOAT;
    ud.ViewDimension = D3D12_UAV_DIMENSION_TEXTURE2D;
    cpu.ptr += stride;
    h.dev->CreateUnorderedAccessView(dst, nullptr, &ud, cpu);

    g_scale_src_bound = src;
    g_scale_dst_bound = dst;
}

// Same idea for the colour pairs, on their own heap.
//
// Two slots, not one, and this is not an optimisation: a shader-visible
// descriptor heap is read by the GPU when the dispatch runs, not when it is
// recorded. The downscale and the upscale are recorded into the same command
// list, so writing both pairs into the same two descriptors means the first
// dispatch executes against the second pair's descriptors. That showed up as
// a completely blank frame - the network was fed a texture nothing had ever
// written to.
static void BindScale4Descriptors(ID3D12Resource *src, ID3D12Resource *dst, UINT slot)
{
    ID3D12Resource **bound_src = (slot == 0) ? &g_scale4_src_bound : &g_scale4_src_bound2;
    ID3D12Resource **bound_dst = (slot == 0) ? &g_scale4_dst_bound : &g_scale4_dst_bound2;
    if (src == *bound_src && dst == *bound_dst) return;
    const UINT stride = h.dev->GetDescriptorHandleIncrementSize(
        D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV);
    D3D12_CPU_DESCRIPTOR_HANDLE cpu = g_scale4_heap->GetCPUDescriptorHandleForHeapStart();
    cpu.ptr += static_cast<SIZE_T>(slot) * 2 * stride;

    D3D12_SHADER_RESOURCE_VIEW_DESC sd = {};
    sd.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    sd.ViewDimension = D3D12_SRV_DIMENSION_TEXTURE2D;
    sd.Shader4ComponentMapping = D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING;
    sd.Texture2D.MipLevels = 1;
    h.dev->CreateShaderResourceView(src, &sd, cpu);

    D3D12_UNORDERED_ACCESS_VIEW_DESC ud = {};
    ud.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    ud.ViewDimension = D3D12_UAV_DIMENSION_TEXTURE2D;
    cpu.ptr += stride;
    h.dev->CreateUnorderedAccessView(dst, nullptr, &ud, cpu);

    *bound_src = src;
    *bound_dst = dst;
}

// Bilinear-scale one RGBA8 texture into another, inside an already open
// command list. Barriers are the caller's: only it knows what these resources
// were doing before and after, and guessing here would mean transitioning
// twice on every frame.
//
// src must be in NON_PIXEL_SHADER_RESOURCE, dst in UNORDERED_ACCESS. slot
// picks which descriptor pair to use - two dispatches in one list need two.
static void ScaleColorInto(ID3D12Resource *src, UINT sw, UINT sh,
                           ID3D12Resource *dst, UINT dw, UINT dh, UINT slot)
{
    BindScale4Descriptors(src, dst, slot);
    const UINT stride = h.dev->GetDescriptorHandleIncrementSize(
        D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV);
    ID3D12DescriptorHeap *heaps[] = { g_scale4_heap };
    h.list->SetDescriptorHeaps(1, heaps);
    h.list->SetComputeRootSignature(g_scale_rs);
    h.list->SetPipelineState(g_scale4_pso);
    const UINT sizes[4] = { dw, dh, sw, sh };
    h.list->SetComputeRoot32BitConstants(0, 4, sizes, 0);
    D3D12_GPU_DESCRIPTOR_HANDLE gpu = g_scale4_heap->GetGPUDescriptorHandleForHeapStart();
    gpu.ptr += static_cast<UINT64>(slot) * 2 * stride;
    h.list->SetComputeRootDescriptorTable(1, gpu);
    h.list->Dispatch((dw + 7) / 8, (dh + 7) / 8, 1);
}

// The residual composite needs three SRVs + one UAV, descriptor at slot 0.
// Recreated only when the resources change (RNSZ, MOTS, per-frame pointers
// are stable across frames in the steady state).
static void BindResidualDescriptors(ID3D12Resource *native, ID3D12Resource *nr_in,
                                    ID3D12Resource *nr_out, ID3D12Resource *dst)
{
    if (native == g_res_native_bound && nr_in == g_res_in_bound &&
        nr_out == g_res_out_bound && dst == g_res_dst_bound)
        return;
    const UINT stride = h.dev->GetDescriptorHandleIncrementSize(
        D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV);
    D3D12_CPU_DESCRIPTOR_HANDLE cpu = g_residual_heap->GetCPUDescriptorHandleForHeapStart();

    D3D12_SHADER_RESOURCE_VIEW_DESC sd = {};
    sd.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    sd.ViewDimension = D3D12_SRV_DIMENSION_TEXTURE2D;
    sd.Shader4ComponentMapping = D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING;
    sd.Texture2D.MipLevels = 1;
    for (int i = 0; i < 3; ++i)
    {
        h.dev->CreateShaderResourceView(i == 0 ? native : (i == 1 ? nr_in : nr_out),
                                        &sd, cpu);
        cpu.ptr += stride;
    }

    D3D12_UNORDERED_ACCESS_VIEW_DESC ud = {};
    ud.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    ud.ViewDimension = D3D12_UAV_DIMENSION_TEXTURE2D;
    h.dev->CreateUnorderedAccessView(dst, nullptr, &ud, cpu);

    g_res_native_bound = native;
    g_res_in_bound = nr_in;
    g_res_out_bound = nr_out;
    g_res_dst_bound = dst;
}

// Compose native + (nr_out - nr_in) * strength at full resolution.
// native must be in NON_PIXEL_SHADER_RESOURCE, dst and (optionally) nr_out
// in UNORDERED_ACCESS - the caller owns the barriers, as with ScaleColorInto.
static void ResidualCompose(ID3D12Resource *native, ID3D12Resource *nr_in,
                            ID3D12Resource *nr_out, UINT w, UINT h_,
                            ID3D12Resource *dst, float strength)
{
    BindResidualDescriptors(native, nr_in, nr_out, dst);
    ID3D12DescriptorHeap *heaps[] = { g_residual_heap };
    h.list->SetDescriptorHeaps(1, heaps);
    h.list->SetComputeRootSignature(g_residual_rs);
    h.list->SetPipelineState(g_residual_pso);
    UINT32 rc[3] = { w, h_, 0 };
    float str = strength;
    memcpy(&rc[2], &str, sizeof(float));
    h.list->SetComputeRoot32BitConstants(0, 3, rc, 0);
    D3D12_GPU_DESCRIPTOR_HANDLE gpu = g_residual_heap->GetGPUDescriptorHandleForHeapStart();
    h.list->SetComputeRootDescriptorTable(1, gpu);
    h.list->Dispatch((w + 7) / 8, (h_ + 7) / 8, 1);
}

// Open the "motion arrives downscaled" path. 0x0 closes it.
static bool OpenMotionScaler(UINT w, UINT hgt)
{
    CloseMotionScaler();
    if (w == 0 || hgt == 0) return true;   // turning it off counts as success
    if (w < 8 || hgt < 8 || w > 7680 || hgt > 4320)
    { Log("[scale] MOTS rejected: implausible motion size %ux%u", w, hgt); return false; }
    if (!EnsureScalePipeline()) return false;
    if (!CreateVideoTex(g_motion_small, w, hgt, DXGI_FORMAT_R16G16_FLOAT, w * 4))
    { Log("[scale] motion texture %ux%u creation failed", w, hgt); return false; }
    g_motion_w = w;
    g_motion_h = hgt;
    Log("[scale] motion arrives at %ux%u, stretched on the GPU", w, hgt);
    return true;
}

// Load the downscaled motion field and upscale it into v.mv.tex.
// Called inside an already open command list.
static bool ScaleMotionInto(VideoState &v, const BYTE *mv, bool mv_was_ready)
{
    if (!FillUpload(g_motion_small, mv, g_motion_w * 4, g_motion_h)) return false;
    D3D12_RESOURCE_BARRIER to_dst = Transition(
        g_motion_small.tex, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE,
        D3D12_RESOURCE_STATE_COPY_DEST);
    if (g_scale_src_bound == g_motion_small.tex)  // not the first frame: the texture was an SRV
        h.list->ResourceBarrier(1, &to_dst);
    CopyUpload(h.list, g_motion_small);

    D3D12_RESOURCE_BARRIER pre[] = {
        Transition(g_motion_small.tex, D3D12_RESOURCE_STATE_COPY_DEST,
                   D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE),
        Transition(v.mv.tex,
                   mv_was_ready ? D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE
                                : D3D12_RESOURCE_STATE_COPY_DEST,
                   D3D12_RESOURCE_STATE_UNORDERED_ACCESS),
    };
    h.list->ResourceBarrier(_countof(pre), pre);

    BindScaleDescriptors(g_motion_small.tex, v.mv.tex);
    ID3D12DescriptorHeap *heaps[] = { g_scale_heap };
    h.list->SetDescriptorHeaps(1, heaps);
    h.list->SetComputeRootSignature(g_scale_rs);
    h.list->SetPipelineState(g_scale_pso);
    const UINT sizes[4] = { v.w, v.hgt, g_motion_w, g_motion_h };
    h.list->SetComputeRoot32BitConstants(0, 4, sizes, 0);
    h.list->SetComputeRootDescriptorTable(
        1, g_scale_heap->GetGPUDescriptorHandleForHeapStart());
    h.list->Dispatch((v.w + 7) / 8, (v.hgt + 7) / 8, 1);

    D3D12_RESOURCE_BARRIER post = Transition(v.mv.tex, D3D12_RESOURCE_STATE_UNORDERED_ACCESS,
                                             D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
    h.list->ResourceBarrier(1, &post);
    return true;
}

// ---------------------------------------------------------------------------
// Desktop Duplication capture (DDA1). When active, the colour frame is taken
// by this worker straight from the desktop (D3D11 DDA -> NT shared texture ->
// D3D12), swizzled BGRA->RGBA into v.color.tex, and the client keeps sending
// only motion. All guarded by g_dda_active, the pipe path stays untouched.
// ---------------------------------------------------------------------------
static bool                    g_dda_hdr_mode = false;
static bool                    g_dda_active = false;   // DDA1 with w>0 has been acked
static UINT                    g_dda_w = 0, g_dda_h = 0;
static ID3D11Device           *g_dda_d11 = nullptr;
static ID3D11DeviceContext    *g_dda_ctx = nullptr;
static IDXGIOutputDuplication *g_dda_dup = nullptr;
static ID3D11Texture2D        *g_dda_shared = nullptr;
static HANDLE                  g_dda_nt = nullptr;
static ID3D12Resource         *g_dda_d12 = nullptr;
static ID3D12Resource         *g_dda_dst = nullptr;       // swizzled RGBA into color
static ID3D12Fence            *g_dda_signal = nullptr;   // shared, D3D11 side signals
static ID3D11Fence            *g_dda_signal11 = nullptr;
static HANDLE                  g_dda_fence_ev = nullptr;  // event wait for the D3D11 copy
static UINT64                  g_dda_fence_value = 1;
static bool                    g_dda_ready = false;      // current frame is in v.color
// The WGCW command, filled by the dispatcher and read by its handler.
static VideoWgcCmd             g_wgc_cmd = {};
// Gray downsample (GRAY): write luminance (flow size) into a client mapping.
static HANDLE                  g_gray_file = nullptr;   // client's mapping handle
static BYTE                   *g_gray_map = nullptr;    // mapped view
static size_t                  g_gray_bytes = 0;
static UINT                    g_gray_w = 0, g_gray_h = 0;
static UINT                    g_gray_pitch = 0; // readback row pitch (aligned)
static ID3D12Resource         *g_gray_readback = nullptr; // R8 buffer for gray
static ID3D12Resource         *g_gray_uav = nullptr;      // R8 UAV texture (area kernel writes)
static bool                    g_gray_mapped = false;

static void CloseGray()
{
    g_gray_mapped = false;
    if (g_gray_map) { UnmapViewOfFile(g_gray_map); g_gray_map = nullptr; }
    if (g_gray_file) { CloseHandle(g_gray_file); g_gray_file = nullptr; }
    // The D3D12 resources created in OpenGray leak on every channel reopen
    // otherwise (audit C++ L4). OpenGray always re-creates them, so a plain
    // release is safe.
    if (g_gray_uav) { g_gray_uav->Release(); g_gray_uav = nullptr; }
    if (g_gray_readback) { g_gray_readback->Release(); g_gray_readback = nullptr; }
    g_gray_bytes = 0;
    g_gray_w = g_gray_h = 0;
    g_gray_pitch = 0;
}

static void CloseDda()
{
    g_dda_active = false;
    g_dda_ready = false;
    g_hdr_capture = false;
    CloseHdrResources();
    if (g_dda_d12) { g_dda_d12->Release(); g_dda_d12 = nullptr; }
    if (g_dda_nt)  { CloseHandle(g_dda_nt); g_dda_nt = nullptr; }
    if (g_dda_shared) { g_dda_shared->Release(); g_dda_shared = nullptr; }
    if (g_dda_dup) { g_dda_dup->Release(); g_dda_dup = nullptr; }
    if (g_dda_ctx) { g_dda_ctx->Release(); g_dda_ctx = nullptr; }
    if (g_dda_d11) { g_dda_d11->Release(); g_dda_d11 = nullptr; }
    if (g_dda_signal) { g_dda_signal->Release(); g_dda_signal = nullptr; }
    if (g_dda_signal11) { g_dda_signal11->Release(); g_dda_signal11 = nullptr; }
    if (g_dda_fence_ev) { CloseHandle(g_dda_fence_ev); g_dda_fence_ev = nullptr; }
    // The swizzle destination is created together with the shared texture
    // (inside the same "first frame" block); it must die with the chain,
    // otherwise a resolution change would keep the old-sized dst and the
    // swizzle would copy into a stale resource.
    if (g_dda_dst) { g_dda_dst->Release(); g_dda_dst = nullptr; }
}

// Compute pipeline to swizzle BGRA->RGBA (the DDA frame and v.color are both
// RGBA; DDA gives B8G8R8A8). Shares the worker's D3D12 device.
static ID3D12RootSignature  *g_dda_rs = nullptr;
static ID3D12PipelineState  *g_dda_pso = nullptr;
static ID3D12DescriptorHeap *g_dda_heap = nullptr;
static bool EnsureDdaSwizzle()
{
    if (g_dda_pso) return true;
    ID3DBlob *code = nullptr, *err = nullptr;
    HRESULT hr = D3DCompile(kHdrCaptureHlsl, sizeof(kHdrCaptureHlsl) - 1, "dda-swizzle.hlsl",
                            nullptr, nullptr, "CSMain", "cs_5_0", 0, 0, &code, &err);
    if (FAILED(hr)) { Log("[dda] swizzle compile failed: %s", err ? (char *)err->GetBufferPointer() : "?"); return false; }
    D3D12_ROOT_PARAMETER prm[3] = {};
    D3D12_DESCRIPTOR_RANGE r0 = { D3D12_DESCRIPTOR_RANGE_TYPE_SRV, 1, 0 };
    D3D12_DESCRIPTOR_RANGE r1 = { D3D12_DESCRIPTOR_RANGE_TYPE_UAV, 1, 0 };
    prm[0].ParameterType = D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE;
    prm[0].DescriptorTable.NumDescriptorRanges = 1; prm[0].DescriptorTable.pDescriptorRanges = &r0;
    prm[0].ShaderVisibility = D3D12_SHADER_VISIBILITY_ALL;
    prm[1].ParameterType = D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE;
    prm[1].DescriptorTable.NumDescriptorRanges = 1; prm[1].DescriptorTable.pDescriptorRanges = &r1;
    prm[1].ShaderVisibility = D3D12_SHADER_VISIBILITY_ALL;
    D3D12_ROOT_SIGNATURE_DESC rsd = {};
    prm[2].ParameterType = D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS;
    prm[2].Constants.Num32BitValues = 2;
    prm[2].Constants.ShaderRegister = 0;
    rsd.NumParameters = 3; rsd.pParameters = prm;
    ID3DBlob *sig = nullptr;
    if (FAILED(D3D12SerializeRootSignature(&rsd, D3D_ROOT_SIGNATURE_VERSION_1, &sig, &err)))
    { Log("[dda] RS serialize failed"); return false; }
    if (FAILED(h.dev->CreateRootSignature(0, sig->GetBufferPointer(), sig->GetBufferSize(),
                                          __uuidof(ID3D12RootSignature),
                                          reinterpret_cast<void **>(&g_dda_rs))))
    { Log("[dda] RS create failed"); return false; }
    sig->Release();
    D3D12_COMPUTE_PIPELINE_STATE_DESC pd = {};
    pd.pRootSignature = g_dda_rs;
    pd.CS.pShaderBytecode = code->GetBufferPointer(); pd.CS.BytecodeLength = code->GetBufferSize();
    if (FAILED(h.dev->CreateComputePipelineState(&pd, __uuidof(ID3D12PipelineState),
                                                 reinterpret_cast<void **>(&g_dda_pso))))
    { Log("[dda] PSO create failed"); return false; }
    code->Release();
    D3D12_DESCRIPTOR_HEAP_DESC hd = {};
    hd.Type = D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV;
    hd.NumDescriptors = 2; hd.Flags = D3D12_DESCRIPTOR_HEAP_FLAG_SHADER_VISIBLE;
    if (FAILED(h.dev->CreateDescriptorHeap(&hd, __uuidof(ID3D12DescriptorHeap),
                                           reinterpret_cast<void **>(&g_dda_heap))))
    { Log("[dda] heap create failed"); return false; }
    Log("[dda] swizzle pipeline ready");
    return true;
}

static void BindDdaDescriptors(ID3D12Resource *src)
{
    const UINT stride = h.dev->GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV);
    D3D12_CPU_DESCRIPTOR_HANDLE cpu = g_dda_heap->GetCPUDescriptorHandleForHeapStart();
    D3D12_SHADER_RESOURCE_VIEW_DESC sd = {};
    sd.Format = src->GetDesc().Format; sd.ViewDimension = D3D12_SRV_DIMENSION_TEXTURE2D;
    sd.Shader4ComponentMapping = D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING;
    sd.Texture2D.MipLevels = 1;
    h.dev->CreateShaderResourceView(src, &sd, cpu);
    D3D12_UNORDERED_ACCESS_VIEW_DESC ud = {};
    ud.Format = DXGI_FORMAT_R8G8B8A8_UNORM; ud.ViewDimension = D3D12_UAV_DIMENSION_TEXTURE2D;
    cpu.ptr += stride;
    h.dev->CreateUnorderedAccessView(g_dda_dst, nullptr, &ud, cpu);
}

// ---------------------------------------------------------------------------
// Gray downsample (GRAY): an honest block average RGBA -> R8 luminance.
// The block is exactly ceil(src/dst) (12x12 at 3840x2160 -> 320x180) - what
// cv2.resize(INTER_AREA) does. Bilinear sampling will NOT do: aliasing on text
// breaks the optical flow (verified).
// ---------------------------------------------------------------------------
static ID3D12RootSignature  *g_gray_rs = nullptr;
static ID3D12PipelineState  *g_gray_pso = nullptr;
static const char kGrayHlsl[] =
    "Texture2D<float4>   gSrc : register(t0);\n"
    "RWTexture2D<float>  gDst : register(u0);\n"
    "cbuffer Sizes : register(b0) { uint gSrcW; uint gSrcH; uint gDstW; uint gDstH; };\n"
    "[numthreads(8, 8, 1)]\n"
    "void CSMain(uint3 id : SV_DispatchThreadID)\n"
    "{\n"
    "    if (id.x >= gDstW || id.y >= gDstH) return;\n"
    "    uint bx = (gSrcW + gDstW - 1) / gDstW;\n"
    "    uint by = (gSrcH + gDstH - 1) / gDstH;\n"
    "    uint x0 = id.x * bx, y0 = id.y * by;\n"
    "    uint x1 = min(x0 + bx, gSrcW), y1 = min(y0 + by, gSrcH);\n"
    "    float sum = 0.0f;\n"
    "    for (uint y = y0; y < y1; ++y)\n"
    "        for (uint x = x0; x < x1; ++x)\n"
    "        {\n"
    "            float4 c = gSrc.Load(int3(x, y, 0));\n"
    "            sum += dot(c.rgb, float3(0.299f, 0.587f, 0.114f));\n"
    "        }\n"
    "    gDst[id.xy] = sum / (float)((x1 - x0) * (y1 - y0));\n"
    "}\n";

static bool EnsureGrayPipeline()
{
    if (g_gray_pso) return true;
    ID3DBlob *code = nullptr, *err = nullptr;
    HRESULT hr = D3DCompile(kGrayHlsl, sizeof(kGrayHlsl) - 1, "gray-area.hlsl",
                            nullptr, nullptr, "CSMain", "cs_5_0", 0, 0, &code, &err);
    if (FAILED(hr)) { Log("[gray] compile failed: %s", err ? (char *)err->GetBufferPointer() : "?"); return false; }
    D3D12_DESCRIPTOR_RANGE r0 = { D3D12_DESCRIPTOR_RANGE_TYPE_SRV, 1, 0 };
    D3D12_DESCRIPTOR_RANGE r1 = { D3D12_DESCRIPTOR_RANGE_TYPE_UAV, 1, 0 };
    D3D12_ROOT_PARAMETER prm[3] = {};
    prm[0].ParameterType = D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS;
    prm[0].Constants.Num32BitValues = 4; prm[0].Constants.ShaderRegister = 0;
    prm[0].ShaderVisibility = D3D12_SHADER_VISIBILITY_ALL;
    prm[1].ParameterType = D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE;
    prm[1].DescriptorTable.NumDescriptorRanges = 1; prm[1].DescriptorTable.pDescriptorRanges = &r0;
    prm[1].ShaderVisibility = D3D12_SHADER_VISIBILITY_ALL;
    prm[2].ParameterType = D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE;
    prm[2].DescriptorTable.NumDescriptorRanges = 1; prm[2].DescriptorTable.pDescriptorRanges = &r1;
    prm[2].ShaderVisibility = D3D12_SHADER_VISIBILITY_ALL;
    D3D12_ROOT_SIGNATURE_DESC rsd = {};
    rsd.NumParameters = 3; rsd.pParameters = prm;
    ID3DBlob *sig = nullptr;
    if (FAILED(D3D12SerializeRootSignature(&rsd, D3D_ROOT_SIGNATURE_VERSION_1, &sig, &err)))
    { Log("[gray] RS serialize failed"); return false; }
    if (FAILED(h.dev->CreateRootSignature(0, sig->GetBufferPointer(), sig->GetBufferSize(),
                                          __uuidof(ID3D12RootSignature),
                                          reinterpret_cast<void **>(&g_gray_rs))))
    { Log("[gray] RS create failed"); return false; }
    sig->Release();
    D3D12_COMPUTE_PIPELINE_STATE_DESC pd = {};
    pd.pRootSignature = g_gray_rs;
    pd.CS.pShaderBytecode = code->GetBufferPointer(); pd.CS.BytecodeLength = code->GetBufferSize();
    if (FAILED(h.dev->CreateComputePipelineState(&pd, __uuidof(ID3D12PipelineState),
                                                 reinterpret_cast<void **>(&g_gray_pso))))
    { Log("[gray] PSO create failed"); return false; }
    code->Release();
    Log("[gray] AREA pipeline ready");
    return true;
}

// Open the reverse client mapping (GRAY). w/h is the luminance size (320x180).
// --- OUTS: the section for the returned pixels ---------------------------
// Layout: [0..8) uint64 seqlock (odd while writing, even when done),
//         [8..8+size) the RGBA8 frame.
static HANDLE g_out_file;
static BYTE  *g_out_map;
static size_t g_out_bytes;
static uint64_t g_out_seq;

static void CloseOut()
{
    if (g_out_map != nullptr) { UnmapViewOfFile(g_out_map); g_out_map = nullptr; }
    if (g_out_file != nullptr) { CloseHandle(g_out_file); g_out_file = nullptr; }
    g_out_bytes = 0;
}

static bool OpenOut(const VideoOutCmd &oc)
{
    CloseOut();
    if (oc.width == 0 || oc.height == 0) { Log("[outs] off"); return true; }
    char name[64] = {};
    memcpy(name, oc.name, sizeof(name) - 1);
    const size_t need = static_cast<size_t>(oc.width) * oc.height * 4 + 8; // + seqlock
    g_out_file = OpenFileMappingA(FILE_MAP_WRITE, FALSE, name);
    if (g_out_file == nullptr)
    { Log("[outs] OpenFileMapping('%s') failed %lu", name, GetLastError()); return false; }
    // The client created the section and we must not write past its end.
    // The size cannot be asked with GetFileSizeEx: a section object is not a
    // file, and a pagefile-backed one answers 0 - that rejected every client
    // outright (the audit #3 check, caught by test_out_shm). So map the whole
    // section and ask the size of the mapped region instead.
    g_out_map = static_cast<BYTE *>(MapViewOfFile(g_out_file, FILE_MAP_WRITE, 0, 0, 0));
    if (g_out_map == nullptr)
    {
        Log("[outs] MapViewOfFile('%s') failed %lu", name, GetLastError());
        CloseHandle(g_out_file); g_out_file = nullptr;
        return false;
    }
    MEMORY_BASIC_INFORMATION mbi = {};
    const size_t have = (VirtualQuery(g_out_map, &mbi, sizeof(mbi)) == sizeof(mbi))
                            ? static_cast<size_t>(mbi.RegionSize) : 0u;
    if (have < need)
    {
        Log("[outs] section '%s' is %zu bytes, need %zu - ignoring", name, have, need);
        CloseOut();
        return false;
    }
    g_out_bytes = need;
    Log("[outs] pixels will go into '%s', %zu bytes", name, need);
    return true;
}

// Deliver the pixels: into the section when it is agreed and the frame fits
// EXACTLY (a mismatched size would leave stale bytes in the tail - the client
// copies the whole slot), otherwise the old way - inline through the pipe.
// The seqlock in the first 8 bytes lets the client detect a torn frame:
// odd while we write, even when done.
static bool DeliverPixels(const std::vector<BYTE> &output, uint32_t index,
                          int64_t pts)
{
    if (g_out_map != nullptr && !output.empty()
        && output.size() + 8 == g_out_bytes)
    {
        uint64_t seq = ++g_out_seq;
        if ((seq & 1) == 0) ++seq;          // make it odd: writing
        memcpy(g_out_map, &seq, sizeof(seq));
        memcpy(g_out_map + 8, output.data(), output.size());
        ++seq;                              // even: done
        memcpy(g_out_map, &seq, sizeof(seq));
        VideoResultHeader out = { OUT_MAGIC, index, 1u, OUT_BYTES_IN_SHM,
                                  g_last_eval_result, pts };
        return WriteExact(stdout, &out, sizeof(out));
    }
    VideoResultHeader out = { OUT_MAGIC, index, 1u,
                              static_cast<uint32_t>(output.size()),
                              g_last_eval_result, pts };
    return WriteExact(stdout, &out, sizeof(out))
        && WriteExact(stdout, output.data(), output.size());
}

static bool OpenGray(const VideoGrayCmd &gc)
{
    CloseGray();
    char name[64] = {};
    memcpy(name, gc.name, sizeof(name) - 1);
    if (gc.width == 0 || gc.height == 0) { Log("[gray] off"); return true; }
    const size_t need = static_cast<size_t>(gc.width) * gc.height;
    g_gray_file = OpenFileMappingA(FILE_MAP_WRITE, FALSE, name);
    if (g_gray_file == nullptr) { Log("[gray] OpenFileMapping('%s') failed %lu", name, GetLastError()); return false; }
    g_gray_map = static_cast<BYTE *>(MapViewOfFile(g_gray_file, FILE_MAP_WRITE, 0, 0, need));
    if (g_gray_map == nullptr) { Log("[gray] MapViewOfFile(%zu) failed %lu", need, GetLastError()); CloseHandle(g_gray_file); g_gray_file = nullptr; return false; }
    g_gray_bytes = need; g_gray_w = gc.width; g_gray_h = gc.height;
    if (!EnsureGrayPipeline()) return false;
    // R8 UAV texture
    D3D12_HEAP_PROPERTIES def = { D3D12_HEAP_TYPE_DEFAULT };
    D3D12_RESOURCE_DESC td = {};
    td.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
    td.Width = gc.width; td.Height = gc.height; td.DepthOrArraySize = 1; td.MipLevels = 1;
    td.Format = DXGI_FORMAT_R8_UNORM; td.SampleDesc.Count = 1;
    td.Layout = D3D12_TEXTURE_LAYOUT_UNKNOWN;
    td.Flags = D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS;
    if (FAILED(h.dev->CreateCommittedResource(&def, D3D12_HEAP_FLAG_NONE, &td,
                                              D3D12_RESOURCE_STATE_UNORDERED_ACCESS, nullptr,
                                              __uuidof(ID3D12Resource),
                                              reinterpret_cast<void **>(&g_gray_uav))))
    { Log("[gray] UAV tex failed"); return false; }
    // The readback row pitch comes from the driver, not from the width:
    // D3D12 readback rows are aligned (320 is not a multiple of 256, so a
    // packed pitch would be UB - the copy would write past the row and the
    // client would read garbage on the last rows). Ask the driver for the
    // real footprint and size the buffer for pitch*h (code review finding).
    D3D12_PLACED_SUBRESOURCE_FOOTPRINT fp = {};
    UINT rows = 0;
    UINT64 row_size = 0, total = 0;
    h.dev->GetCopyableFootprints(&td, 0, 1, 0, &fp, &rows, &row_size, &total);
    if (rows != gc.height || row_size < gc.width)
    { Log("[gray] unexpected footprint %ux%u row %llu", rows, gc.height, (unsigned long long)row_size); return false; }
    g_gray_pitch = static_cast<UINT>(fp.Footprint.RowPitch);
    // a readback buffer of exactly pitch*h bytes
    D3D12_HEAP_PROPERTIES rb = { D3D12_HEAP_TYPE_READBACK };
    D3D12_RESOURCE_DESC bd = {};
    bd.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
    bd.Width = static_cast<UINT64>(g_gray_pitch) * gc.height; bd.Height = 1; bd.DepthOrArraySize = 1; bd.MipLevels = 1;
    bd.SampleDesc.Count = 1; bd.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
    if (FAILED(h.dev->CreateCommittedResource(&rb, D3D12_HEAP_FLAG_NONE, &bd,
                                              D3D12_RESOURCE_STATE_COPY_DEST, nullptr,
                                              __uuidof(ID3D12Resource),
                                              reinterpret_cast<void **>(&g_gray_readback))))
    { Log("[gray] readback failed"); return false; }
    g_gray_mapped = true;
    Log("[gray] mapping '%s' %ux%u (%zu B, pitch %u) active", gc.name, gc.width, gc.height, need, g_gray_pitch);
    return true;
}

// Run the AREA average g_dda_dst -> gray and write it into the client mapping.
// Called from DdaGrab after the swizzle (it cannot be in the same Begin/End
// block - a separate fence is needed), hence its own Begin/End here.
static bool AreaToGray()
{
    if (!g_gray_mapped || !g_dda_dst) return false;
    if (!BeginCommands()) return false;
    // Is g_dda_dst in COPY_SOURCE after the copy into v.color? No - after the
    // swizzle it goes back to UNORDERED_ACCESS (see DdaGrab). We read it as an SRV.
    D3D12_RESOURCE_BARRIER to_srv = Transition(g_dda_dst, D3D12_RESOURCE_STATE_UNORDERED_ACCESS,
                                               D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
    h.list->ResourceBarrier(1, &to_srv);
    // descriptors: 0 = SRV g_dda_dst, 1 = UAV g_gray_uav
    const UINT stride = h.dev->GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV);
    D3D12_CPU_DESCRIPTOR_HANDLE cpu = g_dda_heap->GetCPUDescriptorHandleForHeapStart();
    D3D12_SHADER_RESOURCE_VIEW_DESC sd = {};
    sd.Format = DXGI_FORMAT_R8G8B8A8_UNORM; sd.ViewDimension = D3D12_SRV_DIMENSION_TEXTURE2D;
    sd.Shader4ComponentMapping = D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING;
    sd.Texture2D.MipLevels = 1;
    h.dev->CreateShaderResourceView(g_dda_dst, &sd, cpu);
    D3D12_UNORDERED_ACCESS_VIEW_DESC ud = {};
    ud.Format = DXGI_FORMAT_R8_UNORM; ud.ViewDimension = D3D12_UAV_DIMENSION_TEXTURE2D;
    D3D12_CPU_DESCRIPTOR_HANDLE cpu2 = cpu; cpu2.ptr += stride;
    h.dev->CreateUnorderedAccessView(g_gray_uav, nullptr, &ud, cpu2);
    ID3D12DescriptorHeap *heaps[] = { g_dda_heap };
    h.list->SetDescriptorHeaps(1, heaps);
    h.list->SetComputeRootSignature(g_gray_rs);
    h.list->SetPipelineState(g_gray_pso);
    const UINT sizes[4] = { g_dda_w, g_dda_h, g_gray_w, g_gray_h };
    h.list->SetComputeRoot32BitConstants(0, 4, sizes, 0);
    D3D12_GPU_DESCRIPTOR_HANDLE g0 = g_dda_heap->GetGPUDescriptorHandleForHeapStart();
    D3D12_GPU_DESCRIPTOR_HANDLE g1 = g0; g1.ptr += stride;
    h.list->SetComputeRootDescriptorTable(1, g0);
    h.list->SetComputeRootDescriptorTable(2, g1);
    h.list->Dispatch((g_gray_w + 7) / 8, (g_gray_h + 7) / 8, 1);
    // gray UAV -> COPY_SOURCE, copy into the readback buffer
    D3D12_RESOURCE_BARRIER to_copy = Transition(g_gray_uav, D3D12_RESOURCE_STATE_UNORDERED_ACCESS,
                                                D3D12_RESOURCE_STATE_COPY_SOURCE);
    h.list->ResourceBarrier(1, &to_copy);
    D3D12_TEXTURE_COPY_LOCATION src = {}, dst = {};
    src.pResource = g_gray_uav; src.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX; src.SubresourceIndex = 0;
    dst.pResource = g_gray_readback; dst.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
    dst.PlacedFootprint.Footprint.Width = g_gray_w;
    dst.PlacedFootprint.Footprint.Height = g_gray_h;
    dst.PlacedFootprint.Footprint.Depth = 1;
    dst.PlacedFootprint.Footprint.RowPitch = g_gray_pitch;
    dst.PlacedFootprint.Footprint.Format = DXGI_FORMAT_R8_UNORM;
    h.list->CopyTextureRegion(&dst, 0, 0, 0, &src, nullptr);
    // Put g_dda_dst back into COPY_SOURCE? No - it stays
    // NON_PIXEL_SHADER_RESOURCE for the next swizzle? In DdaGrab it is put
    // back into UNORDERED_ACCESS after the copy. Here we took it into
    // NON_PIXEL from UNORDERED_ACCESS - so we put it back.
    D3D12_RESOURCE_BARRIER back_uav = Transition(g_dda_dst, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE,
                                                 D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
    D3D12_RESOURCE_BARRIER back_uav2 = Transition(g_gray_uav, D3D12_RESOURCE_STATE_COPY_SOURCE,
                                                  D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
    D3D12_RESOURCE_BARRIER backs[2] = { back_uav, back_uav2 };
    h.list->ResourceBarrier(2, backs);
    const UINT64 fence = EndCommands();
    if (!WaitFenceValue(h.fence, fence, 10000)) { Log("[gray] fence timeout"); return false; }
    // map the readback -> memcpy into the client mapping. The readback
    // rows are pitch-aligned; the client mapping is packed w*h, so the
    // copy is row by row (code review finding).
    BYTE *mapped = nullptr;
    D3D12_RANGE rr = { 0, static_cast<SIZE_T>(g_gray_pitch) * g_gray_h };
    if (FAILED(g_gray_readback->Map(0, &rr, reinterpret_cast<void **>(&mapped)))) return false;
    for (UINT y = 0; y < g_gray_h; ++y)
        memcpy(g_gray_map + static_cast<size_t>(y) * g_gray_w,
               mapped + static_cast<size_t>(y) * g_gray_pitch, g_gray_w);
    g_gray_readback->Unmap(0, nullptr);
    return true;
}

// ---------------------------------------------------------------------------
// Adaptive exposure (PaperWhite principle, Ghady983/RenoDX-DLSS-5-Artifact-Fix)
// ---------------------------------------------------------------------------
// A static exposure leaves dark scenes underexposed: the network sees a
// near-black frame and produces artifacts/flicker on textures. The desktop
// is exactly that case - windows of very different brightness (dark IDE +
// bright site). The fix: sample the AREA luminance we already compute for
// the guides (320x180 R8), map the average through a smoothstep between
// Dark/Lit thresholds, and feed the result into DLSS.Exposure.Scale with
// temporal smoothing so the value cannot flicker.
//
// NS_PW=0 disables (tests); default on, like the residual composite.
// NS_PW_DARK / NS_PW_LIT: luminance thresholds (0..1) for the smoothstep.
// NS_PW_MIN / NS_PW_MAX: exposure range the value is mapped into.
// NS_PW_TAU: EMA time constant in seconds (0 = instant).
static bool PwEnabled()
{
    char buf[8] = {};
    const DWORD got = GetEnvironmentVariableA("NS_PW", buf, sizeof(buf));
    return !(got > 0 && got < sizeof(buf) && buf[0] == '0');
}

static float PwEnvFloat(const char *name, float def)
{
    char buf[32] = {};
    const DWORD got = GetEnvironmentVariableA(name, buf, sizeof(buf));
    if (got > 0 && got < sizeof(buf))
    {
        const float f = static_cast<float>(atof(buf));
        if (f >= 0.0f) return f;
    }
    return def;
}

static float g_pw_exposure = 1.0f;   // current smoothed value
static double g_pw_last = 0.0;       // last update time (GetTickCount64 ms)
static bool   g_pw_logged = false;

// Called once per captured frame, after AreaToGray filled g_gray_map.
static void UpdateAdaptiveExposure()
{
    if (!PwEnabled() || !g_gray_mapped || g_gray_w == 0 || g_gray_h == 0)
    {
        g_pw_exposure = 1.0f;
        return;
    }
    // The tuning values are read ONCE: they come from the process
    // environment, which cannot change while we run, and this function is
    // called for every captured frame - six GetEnvironmentVariable calls per
    // frame bought nothing.
    static float dark = 0.0f, lit = 0.0f, mn = 0.0f, mx = 0.0f, tau = 0.0f;
    static bool  env_read = false;
    if (!env_read)
    {
        dark = PwEnvFloat("NS_PW_DARK", 0.10f);
        lit  = PwEnvFloat("NS_PW_LIT", 0.40f);
        mn   = PwEnvFloat("NS_PW_MIN", 1.00f);
        mx   = PwEnvFloat("NS_PW_MAX", 1.10f);
        tau  = PwEnvFloat("NS_PW_TAU", 0.50f);
        // A zero-wide window would divide by zero below and hand NGX a NaN
        // exposure. Fall back to the default spread around the given dark
        // point rather than refusing to work.
        if (lit <= dark) lit = dark + 0.30f;
        env_read = true;
    }
    if (!g_pw_logged)
    {
        Log("[pw] adaptive exposure on (dark=%.2f lit=%.2f min=%.2f max=%.2f tau=%.2f)",
            dark, lit, mn, mx, tau);
        g_pw_logged = true;
    }

    // Average luminance of the AREA frame (0..1). The bytes are summed as
    // integers and scaled once: a division per pixel was 57 600 of them per
    // frame for a number that is the same either way.
    uint64_t sum = 0;
    const size_t n = static_cast<size_t>(g_gray_w) * g_gray_h;
    for (size_t i = 0; i < n; ++i) sum += g_gray_map[i];
    const float avg = static_cast<float>(
        static_cast<double>(sum) / (255.0 * static_cast<double>(n)));

    // smoothstep(dark, lit, avg): 0 in dark scenes, 1 in lit ones. The
    // exposure goes UP in dark scenes (the network sees a brighter frame
    // and stops producing artifacts in the shadows - the Ghady983
    // principle) and stays at 1.0 in lit ones.
    float t = (avg - dark) / (lit - dark);
    t = (t < 0.0f) ? 0.0f : (t > 1.0f) ? 1.0f : t;
    const float target = mx - (mx - mn) * (t * t * (3.0f - 2.0f * t));

    // Temporal smoothing: EMA with a time constant.
    const double now = static_cast<double>(GetTickCount64());
    if (tau <= 0.0f || g_pw_last == 0.0)
    {
        g_pw_exposure = target;
    }
    else
    {
        const float dt = static_cast<float>((now - g_pw_last) / 1000.0);
        const float a = 1.0f - expf(-dt / tau);
        g_pw_exposure += (target - g_pw_exposure) * a;
    }
    g_pw_last = now;
}

// Open capture. w/h = capture size; the worker keeps its own pipe for motion.
// The D3D11 device the capture runs on. Desktop Duplication and Windows
// Graphics Capture both hand their frames to the same bridge into D3D12, so
// they share one device; CloseDda releases it, and only one source is ever
// open at a time.
static bool EnsureCaptureDevice()
{
    if (g_dda_d11 != nullptr) return true;
    IDXGIFactory1 *factory = nullptr;
    if (FAILED(CreateDXGIFactory1(__uuidof(IDXGIFactory1), (void **)&factory)))
    { Log("[cap] DXGI factory failed"); return false; }
    // The adapter the network landed on - NOT the raw NS_GPU. The capture has
    // to be on the same card as the network, because the frame crosses to
    // D3D12 through a shared handle and a shared handle does not cross
    // adapters. Reading NS_GPU here meant that an index the network had
    // REJECTED (a hybrid laptop's iGPU at index 0, which is also the shipped
    // default) still got the capture: the network on the 4090, the capture
    // on the integrated chip, and nothing on screen (issue #34).
    const int want = ActiveAdapterIndex();
    IDXGIAdapter1 *adapter = nullptr;
    if (FAILED(factory->EnumAdapters1(want >= 0 ? (UINT)want : 0, &adapter)))
    { Log("[cap] no adapter %d", want >= 0 ? want : 0); factory->Release(); return false; }
    if (want >= 0) Log("[cap] adapter %d, the one the network runs on", want);
    D3D_FEATURE_LEVEL fl = D3D_FEATURE_LEVEL_11_0;
    const HRESULT hr = D3D11CreateDevice(adapter, D3D_DRIVER_TYPE_UNKNOWN, nullptr,
                                         D3D11_CREATE_DEVICE_BGRA_SUPPORT, &fl, 1,
                                         D3D11_SDK_VERSION, &g_dda_d11, nullptr,
                                         &g_dda_ctx);
    adapter->Release(); factory->Release();
    if (FAILED(hr)) { Log("[cap] D3D11 device failed 0x%08X", hr); return false; }
    return true;
}

static void CloseWgc();

// NS_OUTPUT: which OUTPUT of the chosen adapter to duplicate, by DXGI device
// name ("\\\\.\\DISPLAY2"). Without it the worker duplicated output 0 - on a
// multi-monitor machine that is the primary one, while the client was built
// for the chosen monitor and showed an empty picture on it (#28, #33). The
// name is the identity: it survives a reorder, an unplug or a dock change.
// A name that is not on this adapter falls back to output 0 with a line in
// the log - the capture is never taken down over it.
static IDXGIOutput *EnumCaptureOutput(IDXGIAdapter1 *adapter)
{
    wchar_t want[64] = {};
    const DWORD got = GetEnvironmentVariableW(L"NS_OUTPUT", want, 64);
    UINT index = 0;
    if (got > 0 && got < _countof(want))
    {
        bool found = false;
        for (UINT i = 0; ; ++i)
        {
            IDXGIOutput *candidate = nullptr;
            // Any failure ends the search, not just NOT_FOUND: on another
            // error EnumOutputs leaves the pointer null and the GetDesc
            // below would dereference it (audit, cpp-worker).
            if (FAILED(adapter->EnumOutputs(i, &candidate)) || candidate == nullptr) break;
            DXGI_OUTPUT_DESC desc = {};
            candidate->GetDesc(&desc);
            const bool match = wcscmp(desc.DeviceName, want) == 0;
            candidate->Release();
            if (match) { index = i; found = true; break; }
        }
        if (found)
            Log("[dda] output %u selected by NS_OUTPUT (%ls)", index, want);
        else
            Log("[dda] NS_OUTPUT=%ls is not an output of this adapter - using output 0", want);
    }
    IDXGIOutput *output = nullptr;
    if (FAILED(adapter->EnumOutputs(index, &output))) return nullptr;
    return output;
}

static bool OpenDda(UINT w, UINT hgt)
{
    CloseWgc();              // one source at a time; this also frees the bridge
    CloseDda();
    if (w == 0 || hgt == 0) { Log("[dda] capture off"); return true; }
    if (!EnsureDdaSwizzle()) return false;
    if (!EnsureCaptureDevice()) return false;
    IDXGIFactory1 *factory = nullptr;
    HRESULT hr = CreateDXGIFactory1(__uuidof(IDXGIFactory1), (void **)&factory);
    if (FAILED(hr)) { Log("[dda] factory failed 0x%08X", hr); return false; }
    // Same adapter as the capture device, or DuplicateOutput would be asked
    // to duplicate an output that belongs to another card.
    const int want_dda = ActiveAdapterIndex();
    IDXGIAdapter1 *adapter = nullptr;
    if (FAILED(factory->EnumAdapters1(want_dda >= 0 ? (UINT)want_dda : 0, &adapter)))
    { Log("[dda] no adapter %d", want_dda >= 0 ? want_dda : 0); factory->Release(); return false; }
    IDXGIOutput *output = EnumCaptureOutput(adapter);
    if (output == nullptr) { Log("[dda] no output"); adapter->Release(); factory->Release(); return false; }
    // Is the captured display in HDR? The network is trained on SDR and the
    // result on an HDR desktop reads as "everything is too bright, and the
    // sliders do nothing" (issue #27 territory; a user asked for this notice
    // in issue #33). Asked of the OUTPUT being captured, not of the registry:
    // the registry answer is per monitor and says nothing about which one is
    // on screen here.
    {
        IDXGIOutput6 *out6 = nullptr;
        if (SUCCEEDED(output->QueryInterface(__uuidof(IDXGIOutput6), (void **)&out6)))
        {
            DXGI_OUTPUT_DESC1 d1 = {};
            if (SUCCEEDED(out6->GetDesc1(&d1)))
            {
                // Every PQ (ST.2084) colour space counts, not just the one
                // a display most commonly reports: full and studio range,
                // RGB and YCbCr. Matching a single value would miss an HDR
                // display that reports one of the others - a false NEGATIVE,
                // which is the harmless direction but still wrong.
                //
                // The raw number is logged either way, so a case this list
                // does not cover can be settled from a user's log instead of
                // by guesswork.
                const bool hdr =
                    d1.ColorSpace == DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020 ||
                    d1.ColorSpace == DXGI_COLOR_SPACE_RGB_STUDIO_G2084_NONE_P2020 ||
                    d1.ColorSpace == DXGI_COLOR_SPACE_YCBCR_STUDIO_G2084_LEFT_P2020 ||
                    d1.ColorSpace == DXGI_COLOR_SPACE_YCBCR_STUDIO_G2084_TOPLEFT_P2020;
                Log("[dda] output colour space %d%s", (int)d1.ColorSpace,
                    hdr ? " - HDR IS ON for the captured display" : "");
            }
            out6->Release();
        }
    }
    IDXGIOutput1 *output1 = nullptr;
    if (FAILED(output->QueryInterface(__uuidof(IDXGIOutput1), (void **)&output1)))
    { Log("[dda] no Output1"); output->Release(); adapter->Release(); factory->Release(); return false; }
    DXGI_OUTPUT_DESC output_desc = {};
    output->GetDesc(&output_desc);
    g_capture_monitor = output_desc.Monitor;
    g_capture_display = QueryHdrDisplay(g_capture_monitor);
    g_dda_hdr_mode = HdrEnabled() && g_capture_display.enabled;
    IDXGIOutput5 *output5 = nullptr;
    hr = E_FAIL;
    if (g_dda_hdr_mode && SUCCEEDED(output->QueryInterface(__uuidof(IDXGIOutput5), (void **)&output5)))
    {
        const DXGI_FORMAT formats[] = {DXGI_FORMAT_R16G16B16A16_FLOAT, DXGI_FORMAT_B8G8R8A8_UNORM};
        hr = output5->DuplicateOutput1(g_dda_d11, 0, _countof(formats), formats, &g_dda_dup);
        output5->Release();
        if (FAILED(hr))
            Log("[hdr] FP16 duplication refused 0x%08X - capturing in SDR instead", hr);
    }
    else if (g_dda_hdr_mode)
        Log("[hdr] this Windows has no IDXGIOutput5 - capturing in SDR instead");
    if (FAILED(hr))
    {
        // SDR, and never silently: the first staged frame logs "capture=SDR"
        // and the menu then says HDR is not being preserved (warn_hdr). The
        // alternative - refusing to duplicate at all - turns a washed-out
        // picture into no picture, which is the worse of the two on hardware
        // nobody here can see.
        //
        // g_dda_hdr_mode stays as it is on purpose: it records what this
        // capture was OPENED for, and DdaGrab compares it against what the
        // desktop is doing now to notice a mode change. Lowering it here
        // would make that comparison disagree every second and reopen the
        // duplication forever.
        hr = output1->DuplicateOutput(g_dda_d11, &g_dda_dup);
    }
    output1->Release(); output->Release(); adapter->Release(); factory->Release();
    if (FAILED(hr)) { Log("[dda] DuplicateOutput failed 0x%08X", hr); return false; }
    // How the display is rotated, in the duplication's own words. Nothing
    // acts on it yet: a rotated desktop is duplicated UNROTATED, so on
    // "Landscape (flipped)" the picture we hand back is upside down, and on
    // a portrait mode the width and the height are swapped as well (issue
    // #47). Reading it is the half that can be shipped without a rotated
    // display to test on - the next log says what Windows actually reports
    // for that mode, which is what a fix has to key off.
    {
        DXGI_OUTDUPL_DESC dd = {};
        g_dda_dup->GetDesc(&dd);
        static const char *kRot[] = { "unspecified", "none", "90", "180", "270" };
        const unsigned r = (unsigned)dd.Rotation;
        Log("[dda] desktop rotation: %s%s", r < 5 ? kRot[r] : "?",
            (r == DXGI_MODE_ROTATION_ROTATE90 || r == DXGI_MODE_ROTATION_ROTATE180
             || r == DXGI_MODE_ROTATION_ROTATE270)
                ? " - NOT handled yet, the picture will not match the screen" : "");
    }
    g_dda_w = w; g_dda_h = hgt; g_dda_active = true;
    Log("[dda] capture %ux%u active", w, hgt);
    return true;
}

// The phase profiler is declared below (before RunVideo), but the probe is
// needed here - so the phase list and the prototypes are hoisted up.
enum { PH_ACQ, PH_DDA, PH_UPLOAD, PH_EVAL, PH_PRESENT, PH_FRAME, PH_COUNT };
static double PhaseNow();
static void PhaseAdd(int idx, double t0);
static bool PhaseEnabled();

enum class StageResult { Ok, SizeChanged, Failed };

// Everything between "a captured D3D11 texture" and "the bytes are in the
// shared texture and D3D12 may read them". Desktop Duplication and Windows
// Graphics Capture produce the same kind of texture, so this half of the path
// exists once. The caller owns the source frame and releases it afterwards -
// duplication may only call ReleaseFrame() once the fence below has fired.
static StageResult StageCapturedFrame(ID3D11Texture2D *frame, UINT *out_w, UINT *out_h)
{
    D3D11_TEXTURE2D_DESC fd = {};
    frame->GetDesc(&fd);
    if (out_w != nullptr) *out_w = fd.Width;
    if (out_h != nullptr) *out_h = fd.Height;
    if (fd.Format != DXGI_FORMAT_B8G8R8A8_UNORM &&
        fd.Format != DXGI_FORMAT_R16G16B16A16_FLOAT)
    { Log("[hdr] unsupported capture format %u", fd.Format); return StageResult::Failed; }
    if (g_dda_shared == nullptr)
    {
        g_hdr_capture = fd.Format == DXGI_FORMAT_R16G16B16A16_FLOAT;
        Log("[hdr] capture=%s; neural processing=SDR proxy; export=SDR",
            g_hdr_capture ? "FP16 scRGB" : "SDR");
        D3D11_TEXTURE2D_DESC sd = {};
        sd.Width = fd.Width; sd.Height = fd.Height; sd.MipLevels = 1; sd.ArraySize = 1;
        sd.Format = fd.Format; sd.SampleDesc.Count = 1; sd.Usage = D3D11_USAGE_DEFAULT;
        sd.BindFlags = D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_RENDER_TARGET;
        sd.MiscFlags = D3D11_RESOURCE_MISC_SHARED | D3D11_RESOURCE_MISC_SHARED_NTHANDLE;
        if (FAILED(g_dda_d11->CreateTexture2D(&sd, nullptr, &g_dda_shared)))
        { Log("[cap] shared tex failed"); return StageResult::Failed; }
        IDXGIResource1 *r1 = nullptr;
        g_dda_shared->QueryInterface(__uuidof(IDXGIResource1), (void **)&r1);
        if (!r1 || FAILED(r1->CreateSharedHandle(nullptr, DXGI_SHARED_RESOURCE_READ, nullptr, &g_dda_nt)))
        { Log("[cap] NT handle failed"); if (r1) r1->Release(); goto fail_capture; }
        r1->Release();
        if (FAILED(h.dev->OpenSharedHandle(g_dda_nt, __uuidof(ID3D12Resource),
                                           (void **)&g_dda_d12)))
        { Log("[cap] OpenSharedHandle failed"); goto fail_capture; }
        // cross-device signal fence
        g_dda_fence_value = 1;
        if (FAILED(h.dev->CreateFence(0, D3D12_FENCE_FLAG_SHARED, __uuidof(ID3D12Fence),
                                      reinterpret_cast<void **>(&g_dda_signal))))
        { Log("[cap] signal fence failed"); goto fail_capture; }
        HANDLE nt_f = nullptr;
        h.dev->CreateSharedHandle(g_dda_signal, nullptr, GENERIC_ALL, nullptr, &nt_f);
        ID3D11Device5 *d5 = nullptr;
        if (g_dda_d11->QueryInterface(__uuidof(ID3D11Device5), (void **)&d5) == S_OK)
        {
            d5->OpenSharedFence(nt_f, __uuidof(ID3D11Fence), (void **)&g_dda_signal11);
            d5->Release();
        }
        if (nt_f) CloseHandle(nt_f);
        if (!g_dda_signal11)
        { Log("[cap] no D3D11 fence - capture invalid"); goto fail_capture; }
        D3D12_HEAP_PROPERTIES def = { D3D12_HEAP_TYPE_DEFAULT };
        D3D12_RESOURCE_DESC td = {};
        td.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
        td.Width = fd.Width; td.Height = fd.Height; td.DepthOrArraySize = 1; td.MipLevels = 1;
        td.Format = DXGI_FORMAT_R8G8B8A8_UNORM; td.SampleDesc.Count = 1;
        td.Layout = D3D12_TEXTURE_LAYOUT_UNKNOWN;
        td.Flags = D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS;
        if (FAILED(h.dev->CreateCommittedResource(&def, D3D12_HEAP_FLAG_NONE, &td,
                                                  D3D12_RESOURCE_STATE_UNORDERED_ACCESS, nullptr,
                                                  __uuidof(ID3D12Resource),
                                                  reinterpret_cast<void **>(&g_dda_dst))))
        { Log("[cap] dst UAV failed"); goto fail_capture; }
        Log("[cap] shared texture %ux%u ready", (UINT)fd.Width, (UINT)fd.Height);
    }
    // Any failure inside the "first frame" block leaves a PARTIAL bridge:
    // g_dda_shared alive with a NULL fence, and the next frame would copy
    // into the texture and hit g_dda_signal->GetCompletedValue() on null.
    // CloseDda() releases the whole bridge so the next attempt restarts
    // from scratch (audit #4, MEDIUM).
    // g_dda_shared is created for the size of the FIRST frame. When the source
    // changes size (monitor resolution, or the captured window resized), a
    // CopyResource with mismatched sizes gives device removed, and a cropped
    // copy would leave stale pixels in the tail that NGX would then evaluate -
    // garbage in the lower/right part of the frame (M2 of audit #3). So the
    // caller is told to rebuild the whole chain instead.
    {
        D3D11_TEXTURE2D_DESC sd = {};
        g_dda_shared->GetDesc(&sd);
        if (sd.Width != fd.Width || sd.Height != fd.Height || sd.Format != fd.Format)
            return StageResult::SizeChanged;
        D3D11_BOX box = { 0, 0, 0, sd.Width, sd.Height, 1 };
        g_dda_ctx->CopySubresourceRegion(g_dda_shared, 0, 0, 0, 0, frame, 0, &box);
    }
    ID3D11DeviceContext4 *ctx4 = nullptr;
    if (g_dda_ctx->QueryInterface(__uuidof(ID3D11DeviceContext4), (void **)&ctx4) == S_OK)
    {
        ctx4->Signal(g_dda_signal11, g_dda_fence_value);
        ctx4->Release();
    }
    g_dda_ctx->Flush();

    // Wait for the D3D11 copy with an event, NOT by polling with Sleep(1).
    // At the default Windows timer resolution Sleep(1) sleeps up to 15.6 ms,
    // and that gave 13 ms for the dda phase where the work takes 1-2 ms
    // (measured by the phase profiler: acq=0.0, dda=13.0).
    // SetEventOnCompletion wakes the thread precisely.
    const UINT64 want = g_dda_fence_value;
    const DWORD wait_ms = 5000;
    if (g_dda_fence_ev == nullptr)
        g_dda_fence_ev = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    if (g_dda_signal->GetCompletedValue() < want)
    {
        if (g_dda_fence_ev != nullptr &&
            SUCCEEDED(g_dda_signal->SetEventOnCompletion(want, g_dda_fence_ev)))
            WaitForSingleObject(g_dda_fence_ev, wait_ms);
        else
        {
            // The event is unavailable - fall back to the old polling.
            const DWORD start = GetTickCount();
            while (g_dda_signal->GetCompletedValue() < want &&
                   GetTickCount() - start < wait_ms) Sleep(1);
        }
    }
    if (g_dda_signal->GetCompletedValue() < want)
    { Log("[cap] signal fence timeout"); return StageResult::Failed; }
    ++g_dda_fence_value;
    return StageResult::Ok;
fail_capture:
    // A partial bridge is worse than none: the texture without the fence
    // would be copied into and then dereferenced as NULL on the next frame.
    // Tear down ONLY the bridge - the source (dup/wgc, ctx, d11) stays up,
    // so the next StageCapturedFrame rebuilds the channel from scratch.
    if (g_dda_d12) { g_dda_d12->Release(); g_dda_d12 = nullptr; }
    if (g_dda_nt)  { CloseHandle(g_dda_nt); g_dda_nt = nullptr; }
    if (g_dda_signal) { g_dda_signal->Release(); g_dda_signal = nullptr; }
    if (g_dda_signal11) { g_dda_signal11->Release(); g_dda_signal11 = nullptr; }
    if (g_dda_dst) { g_dda_dst->Release(); g_dda_dst = nullptr; }
    if (g_dda_shared) { g_dda_shared->Release(); g_dda_shared = nullptr; }
    return StageResult::Failed;
}

// The D3D12 half: swizzle BGRA->RGBA out of the shared texture into
// g_dda_dst, copy that into v.color.tex, and hand the client the luminance
// frame the optical-flow guides need.
static bool SwizzleCaptureIntoColor(VideoState &v)
{
    static ULONGLONG last_display_query = 0;
    if (GetTickCount64() - last_display_query > 1000 || last_display_query == 0)
    {
        g_capture_display = QueryHdrDisplay(g_capture_monitor);
        last_display_query = GetTickCount64();
    }
    g_hdr_frame_white = g_capture_display.white;
    if (!BeginCommands()) return false;
    D3D12_RESOURCE_BARRIER to_srv = Transition(g_dda_d12, D3D12_RESOURCE_STATE_COMMON,
                                               D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
    h.list->ResourceBarrier(1, &to_srv);
    BindDdaDescriptors(g_dda_d12);
    ID3D12DescriptorHeap *heaps[] = { g_dda_heap };
    h.list->SetDescriptorHeaps(1, heaps);
    h.list->SetComputeRootSignature(g_dda_rs);
    h.list->SetPipelineState(g_dda_pso);
    D3D12_GPU_DESCRIPTOR_HANDLE g0 = g_dda_heap->GetGPUDescriptorHandleForHeapStart();
    D3D12_GPU_DESCRIPTOR_HANDLE g1 = g0;
    g1.ptr += h.dev->GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV);
    h.list->SetComputeRootDescriptorTable(0, g0);
    h.list->SetComputeRootDescriptorTable(1, g1);
    struct { UINT is_float; float white; } hdr = {g_hdr_capture ? 1u : 0u, g_hdr_frame_white};
    h.list->SetComputeRoot32BitConstants(2, 2, &hdr, 0);
    h.list->Dispatch((g_dda_w + 7) / 8, (g_dda_h + 7) / 8, 1);
    // copy swizzled dst into v.color.tex
    D3D12_RESOURCE_BARRIER pre_color = Transition(v.color.tex, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE,
                                                  D3D12_RESOURCE_STATE_COPY_DEST);
    D3D12_RESOURCE_BARRIER to_copy = Transition(g_dda_dst, D3D12_RESOURCE_STATE_UNORDERED_ACCESS,
                                                D3D12_RESOURCE_STATE_COPY_SOURCE);
    D3D12_RESOURCE_BARRIER pre_c[2] = { to_copy, pre_color };
    h.list->ResourceBarrier(2, pre_c);
    D3D12_TEXTURE_COPY_LOCATION src = {}, dst = {};
    src.pResource = g_dda_dst; src.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX; src.SubresourceIndex = 0;
    dst.pResource = v.color.tex; dst.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX; dst.SubresourceIndex = 0;
    // The copy is strictly of the minimum size: g_dda_dst is fixed by the
    // first captured frame and v.color.tex by the client work/full size; when
    // the sizes diverge a whole-resource copy (a nullptr box) gives device
    // removed. A cropped frame for one tick beats a crash.
    {
        const D3D12_RESOURCE_DESC sd = g_dda_dst->GetDesc();
        const D3D12_RESOURCE_DESC dd = v.color.tex->GetDesc();
        const UINT cw = (UINT)((sd.Width < dd.Width) ? sd.Width : dd.Width);
        const UINT ch = (UINT)((sd.Height < dd.Height) ? sd.Height : dd.Height);
        if (cw != sd.Width || ch != sd.Height)
            Log("[cap] size mismatch %llux%llu vs %llux%llu - clipped",
                (unsigned long long)sd.Width, (unsigned long long)sd.Height,
                (unsigned long long)dd.Width, (unsigned long long)dd.Height);
        if (cw == 0 || ch == 0)
        { Log("[cap] zero copy size - skip"); return false; }
        D3D12_BOX box = { 0, 0, 0, cw, ch, 1 };
        h.list->CopyTextureRegion(&dst, 0, 0, 0, &src, &box);
    }
    D3D12_RESOURCE_BARRIER to_uav = Transition(g_dda_dst, D3D12_RESOURCE_STATE_COPY_SOURCE,
                                               D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
    D3D12_RESOURCE_BARRIER to_nps = Transition(v.color.tex, D3D12_RESOURCE_STATE_COPY_DEST,
                                               D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
    D3D12_RESOURCE_BARRIER to_common = Transition(g_dda_d12, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE,
                                                  D3D12_RESOURCE_STATE_COMMON);
    D3D12_RESOURCE_BARRIER post_c[3] = { to_uav, to_nps, to_common };
    h.list->ResourceBarrier(3, post_c);
    const UINT64 fence = EndCommands();
    if (!WaitFenceValue(h.fence, fence, 10000)) { Log("[cap] swizzle fence timeout"); return false; }
    // Hand the client the luminance frame (320x180) for the optical flow
    if (!AreaToGray()) { /* best effort: guides go without a fresh frame */ }
    UpdateAdaptiveExposure();
    g_dda_ready = true;
    return true;
}

#include "hdr_present.inl"

// Grab the latest desktop frame into v.color.tex (RGBA, GPU-resident).
static bool DdaGrab(VideoState &v)
{
    if (!g_dda_active) return false;
    static ULONGLONG last_mode_query = 0;
    if (GetTickCount64() - last_mode_query > 1000)
    {
        g_capture_display = QueryHdrDisplay(g_capture_monitor);
        last_mode_query = GetTickCount64();
        if (g_dda_hdr_mode != (HdrEnabled() && g_capture_display.enabled))
        {
            Log("[hdr] desktop display mode changed; recreating capture");
            OpenDda(g_dda_w, g_dda_h);
            return false;
        }
    }
    IDXGIResource *res = nullptr;
    DXGI_OUTDUPL_FRAME_INFO fi = {};
    const double t_acq = PhaseNow();
    HRESULT hr = g_dda_dup->AcquireNextFrame(100, &fi, &res);
    PhaseAdd(PH_ACQ, t_acq);
    if (hr == DXGI_ERROR_WAIT_TIMEOUT) return false;      // desktop unchanged
    if (FAILED(hr))
    {
        Log("[dda] acquire failed 0x%08X - recreating", hr);
        OpenDda(g_dda_w, g_dda_h);
        return false;
    }
    ID3D11Texture2D *frame = nullptr;
    if (FAILED(res->QueryInterface(__uuidof(ID3D11Texture2D), (void **)&frame)))
    {
        g_dda_dup->ReleaseFrame();
        res->Release();
        return false;
    }
    UINT new_w = 0, new_h = 0;
    const StageResult st = StageCapturedFrame(frame, &new_w, &new_h);
    frame->Release();
    res->Release();
    // Desktop Duplication requires ReleaseFrame() only AFTER every read of the
    // frame has finished. StageCapturedFrame waits on the fence, so the copy
    // is done; releasing earlier let the compositor overwrite the surface
    // mid-copy (torn frames on motion).
    g_dda_dup->ReleaseFrame();
    if (st == StageResult::SizeChanged)
    {
        Log("[dda] monitor resolution changed -> %ux%u - recreating", new_w, new_h);
        OpenDda(new_w, new_h);
        return false;
    }
    if (st != StageResult::Ok) return false;
    return SwizzleCaptureIntoColor(v);
}

// ---------------------------------------------------------------------------
// Windows Graphics Capture of ONE window (WGCW). The frames arrive as the
// same ID3D11Texture2D duplication produces, so only the source differs.
// ---------------------------------------------------------------------------
namespace ns_wgc = winrt::Windows::Graphics::Capture;
namespace ns_wgdx = winrt::Windows::Graphics::DirectX;

// The surface -> ID3D11Texture2D bridge has no C++/WinRT projection; this
// hand-rolled declaration is the documented way to reach it.
struct __declspec(uuid("A9B3D012-3DF2-4EE3-B8D1-8695F457D3C1"))
INsDxgiInterfaceAccess : ::IUnknown
{
    virtual HRESULT __stdcall GetInterface(GUID const &id, void **object) = 0;
};

// The WinRT objects live on the heap and are freed by CloseWgc. As globals
// with destructors they would be torn down during DLL unload, after WinRT
// itself is gone.
struct WgcSession
{
    ns_wgc::GraphicsCaptureItem item{nullptr};
    ns_wgc::Direct3D11CaptureFramePool pool{nullptr};
    ns_wgc::GraphicsCaptureSession session{nullptr};
    ns_wgdx::Direct3D11::IDirect3DDevice device{nullptr};
    bool hdr = false;
};

static WgcSession *g_wgc = nullptr;   // g_wgc_active / g_wgc_hwnd live up with the present window

static void CloseWgc()
{
    g_wgc_active = false;
    g_present_follow = RECT{};
    // Back to the desktop as the input: hide again or the pipeline would
    // capture its own output.
    ApplyPresentAffinity();
    if (!g_present_shown && g_present_hwnd != nullptr && g_present_revealed)
    {
        // It was hidden because the target was minimised; the next mode must
        // not inherit an invisible overlay. Only after the first Present:
        // before that the window has no picture yet and must stay hidden
        // until RevealOnFirstPresent (user: blank flash on mode switches).
        ShowWindow(g_present_hwnd, SW_SHOWNOACTIVATE);
        g_present_shown = true;
    }
    if (g_wgc != nullptr)
    {
        try
        {
            if (g_wgc->session != nullptr) g_wgc->session.Close();
            if (g_wgc->pool != nullptr) g_wgc->pool.Close();
        }
        catch (winrt::hresult_error const &) { /* going away anyway */ }
        delete g_wgc;
        g_wgc = nullptr;
        Log("[wgc] window capture closed");
    }
    CloseDda();          // the bridge and the D3D11 device are shared
}

// Any capture source at all - the pipe path is the alternative.
static bool CaptureActive()
{
    return g_dda_active || g_wgc_active;
}

static bool OpenWgc(HWND hwnd)
{
    CloseWgc();
    if (hwnd == nullptr) { Log("[wgc] window capture off"); return true; }
    if (!IsWindow(hwnd)) { Log("[wgc] %p is not a window", (void *)hwnd); return false; }
    if (!ns_wgc::GraphicsCaptureSession::IsSupported())
    { Log("[wgc] Windows Graphics Capture is not supported here"); return false; }
    if (!EnsureDdaSwizzle()) return false;
    if (!EnsureCaptureDevice()) return false;
    // WinRT needs an apartment on this thread. The worker initialises none of
    // its own; a second call on an already-initialised MTA throws and is fine.
    try { winrt::init_apartment(winrt::apartment_type::multi_threaded); }
    catch (winrt::hresult_error const &) {}

    WgcSession *s = new WgcSession();
    try
    {
        IDXGIDevice *dxgi = nullptr;
        if (FAILED(g_dda_d11->QueryInterface(__uuidof(IDXGIDevice), (void **)&dxgi)))
        { Log("[wgc] no IDXGIDevice"); delete s; return false; }
        winrt::com_ptr<::IInspectable> insp;
        const HRESULT hr = CreateDirect3D11DeviceFromDXGIDevice(dxgi, insp.put());
        dxgi->Release();
        if (FAILED(hr))
        { Log("[wgc] device wrap failed 0x%08X", hr); delete s; return false; }
        s->device = insp.as<ns_wgdx::Direct3D11::IDirect3DDevice>();
        auto interop = winrt::get_activation_factory<ns_wgc::GraphicsCaptureItem>()
                           .as<::IGraphicsCaptureItemInterop>();
        const HRESULT ir = interop->CreateForWindow(
            hwnd, winrt::guid_of<ns_wgc::GraphicsCaptureItem>(),
            winrt::put_abi(s->item));
        if (FAILED(ir) || s->item == nullptr)
        { Log("[wgc] CreateForWindow failed 0x%08X", ir); delete s; return false; }
        const auto size = s->item.Size();
        if (size.Width <= 0 || size.Height <= 0)
        { Log("[wgc] the window has no size (minimised?)"); delete s; return false; }
        g_capture_monitor = MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST);
        g_capture_display = QueryHdrDisplay(g_capture_monitor);
        s->hdr = HdrEnabled() && g_capture_display.enabled;
        s->pool = ns_wgc::Direct3D11CaptureFramePool::CreateFreeThreaded(
            s->device, s->hdr ? ns_wgdx::DirectXPixelFormat::R16G16B16A16Float
                                    : ns_wgdx::DirectXPixelFormat::B8G8R8A8UIntNormalized, 2, size);
        s->session = s->pool.CreateCaptureSession(s->item);
        try { s->session.IsCursorCaptureEnabled(false); }
        catch (winrt::hresult_error const &) { Log("[wgc] cursor capture stays on"); }
        // The yellow "this window is being captured" outline. Measured: the
        // setter takes effect for an unpackaged process on Windows 11 26200
        // even though GraphicsCaptureAccess(Borderless) answers
        // UserPromptRequired. Where it does not, the overlay covers the
        // window anyway.
        try { s->session.IsBorderRequired(false); }
        catch (winrt::hresult_error const &) { Log("[wgc] capture border stays on"); }
        s->session.StartCapture();
        g_wgc = s;
        g_wgc_hwnd = hwnd;
        g_wgc_active = true;
        // No self-capture loop in this mode, so stop hiding: this is what
        // makes the overlay visible to OBS and lets the NVIDIA App record.
        ApplyPresentAffinity();
        g_dda_w = (UINT)size.Width;
        g_dda_h = (UINT)size.Height;
        Log("[wgc] capturing window %p, %dx%d", (void *)hwnd, size.Width, size.Height);
        return true;
    }
    catch (winrt::hresult_error const &e)
    {
        Log("[wgc] open threw 0x%08X", (unsigned)e.code());
        delete s;
        return false;
    }
}

// Grab the latest frame of the captured WINDOW into v.color.tex.
static bool WgcGrab(VideoState &v)
{
    if (!g_wgc_active || g_wgc == nullptr) return false;
    const HMONITOR monitor = MonitorFromWindow(g_wgc_hwnd, MONITOR_DEFAULTTONEAREST);
    static ULONGLONG last_mode_query = 0;
    if (monitor != g_capture_monitor || GetTickCount64() - last_mode_query > 1000)
    {
        g_capture_monitor = monitor;
        g_capture_display = QueryHdrDisplay(monitor);
        last_mode_query = GetTickCount64();
        if (g_wgc->hdr != (HdrEnabled() && g_capture_display.enabled))
        {
            const HWND hwnd = g_wgc_hwnd;
            Log("[hdr] window display mode changed; recreating capture");
            OpenWgc(hwnd);
            return false;
        }
    }
    try
    {
        const double t_acq = PhaseNow();
        auto frame = g_wgc->pool.TryGetNextFrame();
        PhaseAdd(PH_ACQ, t_acq);
        // Nothing new: the window has not redrawn. Same meaning as
        // DXGI_ERROR_WAIT_TIMEOUT on the duplication path - the caller keeps
        // the previous frame.
        if (frame == nullptr) return false;
        auto access = frame.Surface().as<INsDxgiInterfaceAccess>();
        ID3D11Texture2D *tex = nullptr;
        if (FAILED(access->GetInterface(__uuidof(ID3D11Texture2D), (void **)&tex)) ||
            tex == nullptr)
        { frame.Close(); return false; }
        UINT new_w = 0, new_h = 0;
        const StageResult st = StageCapturedFrame(tex, &new_w, &new_h);
        tex->Release();
        frame.Close();
        if (st == StageResult::SizeChanged)
        {
            Log("[wgc] the window resized -> %ux%u - recreating", new_w, new_h);
            OpenWgc(g_wgc_hwnd);
            return false;
        }
        if (st != StageResult::Ok) return false;
        return SwizzleCaptureIntoColor(v);
    }
    catch (winrt::hresult_error const &e)
    {
        Log("[wgc] grab threw 0x%08X - capture closed", (unsigned)e.code());
        CloseWgc();
        return false;
    }
}

static bool UploadVideoFrame(VideoState &v, const BYTE *color, const BYTE *mv, bool motion_small)
{
    const UINT cw = v.upscale ? v.full_w : v.w;
    const UINT ch = v.upscale ? v.full_h : v.hgt;
    if (!FillUpload(v.color, color, cw * 4, ch)) return false;
    if (!motion_small && !FillUpload(v.mv, mv, v.w * 4, v.hgt)) return false;
    if (!BeginCommands()) return false;
    if (v.inputs_ready)
    {
        // The colour always goes as a copy; in downscaled-field mode it is
        // ScaleMotionInto, not this code, that moves MV into COPY_DEST - it
        // has its own state chain there.
        D3D12_RESOURCE_BARRIER pre[] = {
            Transition(v.color.tex, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE, D3D12_RESOURCE_STATE_COPY_DEST),
            Transition(v.mv.tex, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE, D3D12_RESOURCE_STATE_COPY_DEST),
        };
        h.list->ResourceBarrier(motion_small ? 1 : _countof(pre), pre);
    }
    CopyUpload(h.list, v.color);
    if (motion_small)
    {
        if (!ScaleMotionInto(v, mv, v.inputs_ready)) return false;
        D3D12_RESOURCE_BARRIER post = Transition(v.color.tex, D3D12_RESOURCE_STATE_COPY_DEST,
                                                 D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
        h.list->ResourceBarrier(1, &post);
    }
    else
    {
        CopyUpload(h.list, v.mv);
        D3D12_RESOURCE_BARRIER post[] = {
            Transition(v.color.tex, D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE),
            Transition(v.mv.tex, D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE),
        };
        h.list->ResourceBarrier(_countof(post), post);
    }
    const UINT64 fence = EndCommands();
    v.inputs_ready = true;
    return WaitFenceValue(h.fence, fence, 30000);
}

#include "gpu_motion_experiment.inl"

// DDA mode: the colour is already in v.color.tex (DdaGrab), we upload only motion.
static bool UploadMotionOnly(VideoState &v, const BYTE *mv, bool motion_small)
{
    if (!motion_small && !FillUpload(v.mv, mv, v.w * 4, v.hgt)) return false;
    if (!BeginCommands()) return false;
    if (v.inputs_ready)
    {
        D3D12_RESOURCE_BARRIER pre = Transition(v.mv.tex, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE,
                                                D3D12_RESOURCE_STATE_COPY_DEST);
        h.list->ResourceBarrier(1, &pre);
    }
    if (motion_small)
    {
        if (!ScaleMotionInto(v, mv, v.inputs_ready)) return false;
    }
    else
    {
        CopyUpload(h.list, v.mv);
        D3D12_RESOURCE_BARRIER post = Transition(v.mv.tex, D3D12_RESOURCE_STATE_COPY_DEST,
                                                 D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
        h.list->ResourceBarrier(1, &post);
    }
    const UINT64 fence = EndCommands();
    v.inputs_ready = true;
    return WaitFenceValue(h.fence, fence, 30000);
}

// ---------------------------------------------------------------------------
// How much of the Evaluate time the GPU actually spends computing
//
// PH_EVAL measures submit plus the CPU wait on the fence: it includes both
// queueing the work and waking the thread. Timestamps on the queue answer how
// much of that the GPU really computes.
//
// Measured (RTX 5070 Ti, 2560x1600 desktop): CPU 8.9 ms, GPU 8.0 ms - 0.9 ms
// of overhead, the rest is real work. And the GPU time does not depend on the
// input resolution: 7.9-8.6 ms over 0.37-3.32 MPix, nine times the pixels for
// the same time. The model computes at its own internal resolution, which is
// why work_scale never cost anything.
// ---------------------------------------------------------------------------
static ID3D12QueryHeap *g_ts_heap;
static ID3D12Resource  *g_ts_readback;
static UINT64           g_ts_freq;
static int              g_ts_state;   // 0 - not tried, 1 - ready, -1 - failed
static double           g_ts_sum;
static double           g_ts_max;
static unsigned         g_ts_n;

static bool EnsureTimestamps()
{
    if (g_ts_state != 0) return g_ts_state == 1;
    g_ts_state = -1;
    if (h.dev == nullptr || h.queue == nullptr) return false;
    D3D12_QUERY_HEAP_DESC qd = {};
    qd.Type = D3D12_QUERY_HEAP_TYPE_TIMESTAMP;
    qd.Count = 2;
    if (FAILED(h.dev->CreateQueryHeap(&qd, __uuidof(ID3D12QueryHeap),
                                      reinterpret_cast<void **>(&g_ts_heap))))
    { Log("[phase] CreateQueryHeap failed - nothing to measure eval GPU time with"); return false; }
    D3D12_HEAP_PROPERTIES rb = {};
    rb.Type = D3D12_HEAP_TYPE_READBACK;
    D3D12_RESOURCE_DESC bd = {};
    bd.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
    bd.Width = sizeof(UINT64) * 2; bd.Height = 1; bd.DepthOrArraySize = 1;
    bd.MipLevels = 1; bd.SampleDesc.Count = 1;
    bd.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
    if (FAILED(h.dev->CreateCommittedResource(&rb, D3D12_HEAP_FLAG_NONE, &bd,
        D3D12_RESOURCE_STATE_COPY_DEST, nullptr, __uuidof(ID3D12Resource),
        reinterpret_cast<void **>(&g_ts_readback)))) return false;
    if (FAILED(h.queue->GetTimestampFrequency(&g_ts_freq)) || g_ts_freq == 0) return false;
    g_ts_state = 1;
    Log("[phase] GPU timestamps around Evaluate are on (frequency %llu Hz)", g_ts_freq);
    return true;
}

static void ReadEvalGpuTime()
{
    if (g_ts_state != 1) return;
    D3D12_RANGE r = { 0, sizeof(UINT64) * 2 };
    void *mapped = nullptr;
    if (FAILED(g_ts_readback->Map(0, &r, &mapped)) || mapped == nullptr) return;
    const UINT64 *ts = static_cast<const UINT64 *>(mapped);
    if (ts[1] > ts[0])
    {
        const double ms = static_cast<double>(ts[1] - ts[0]) * 1000.0
                          / static_cast<double>(g_ts_freq);
        g_ts_sum += ms;
        if (ms > g_ts_max) g_ts_max = ms;
        ++g_ts_n;
    }
    D3D12_RANGE none = { 0, 0 };
    g_ts_readback->Unmap(0, &none);
}

static bool EvaluateVideo(VideoState &v, int reset)
{
    if (!BeginCommands()) return false;
    const UINT cw = v.upscale ? v.full_w : v.w;
    const UINT ch = v.upscale ? v.full_h : v.hgt;
    // What the network is actually handed. In nr_small mode that is the work
    // resolution, and colour has to be scaled down into nr_in first.
    const UINT nw = v.nr_small ? v.nr_w : cw;
    const UINT nh = v.nr_small ? v.nr_h : ch;
    if (v.nr_small)
    {
        D3D12_RESOURCE_BARRIER to_uav = Transition(
            v.nr_in, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE,
            D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
        h.list->ResourceBarrier(1, &to_uav);
        ScaleColorInto(v.color.tex, cw, ch, v.nr_in, nw, nh, 0);
        D3D12_RESOURCE_BARRIER to_srv = Transition(
            v.nr_in, D3D12_RESOURCE_STATE_UNORDERED_ACCESS,
            D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
        h.list->ResourceBarrier(1, &to_srv);
    }
    // The timestamps go around the network alone: the scaling passes are our
    // own cost, and folding them into "eval on GPU" would make the number
    // incomparable with every measurement taken so far.
    const bool ts = PhaseEnabled() && EnsureTimestamps();
    if (ts) h.list->EndQuery(g_ts_heap, D3D12_QUERY_TYPE_TIMESTAMP, 0);
    ID3D12Resource *nr_color = v.nr_small ? v.nr_in : v.color.tex;
    ID3D12Resource *nr_result = v.nr_small ? v.nr_out : v.output;
    h.params->Reset();
    h.params->Set("DLSSNR.Color", nr_color); h.params->Set("DLSSNR.Output", nr_result);
    h.params->Set("DLSSNR.MVec", v.mv.tex);
    h.params->Set("DLSSNR.ColorSubrectBaseX", 0u); h.params->Set("DLSSNR.ColorSubrectBaseY", 0u);
    h.params->Set("DLSSNR.ColorSubrectWidth", nw); h.params->Set("DLSSNR.ColorSubrectHeight", nh);
    h.params->Set("DLSSNR.MVecSubrectBaseX", 0u); h.params->Set("DLSSNR.MVecSubrectBaseY", 0u);
    h.params->Set("DLSSNR.MVecSubrectWidth", v.w); h.params->Set("DLSSNR.MVecSubrectHeight", v.hgt);
    h.params->Set("DLSSNR.OutputSubrectBaseX", 0u); h.params->Set("DLSSNR.OutputSubrectBaseY", 0u);
    h.params->Set("DLSSNR.OutputSubrectWidth", nw); h.params->Set("DLSSNR.OutputSubrectHeight", nh);
    h.params->Set("DLSSNR.MVecScaleX", 1.0f); h.params->Set("DLSSNR.MVecScaleY", 1.0f);
    h.params->Set("DLSSNR.Enabled", 1u); h.params->Set("DLSSNR.Reset", reset);
    h.params->Set("DLSSNR.Intensity", g_video_options.intensity);
    h.params->Set("DLSSNR.LocalToneStrength", g_video_options.local_tone);
    h.params->Set("DLSSNR.LocalStructureStrength", g_video_options.local_structure);
    h.params->Set("DLSSNR.SkinStructureStrength", g_video_options.skin_structure);
    h.params->Set("DLSSNR.UseAutoMask", g_video_options.auto_mask);
    h.params->Set("DLSSNR.Style", g_video_options.style);
    h.params->Set("DLSSNR.UICorrection", g_video_options.ui_correction);
    h.params->Set("DLSS.Pre.Exposure", 1.0f);
    h.params->Set("DLSS.Exposure.Scale", g_pw_exposure);
    DWORD code = 0;
    NVSDK_NGX_Result result = static_cast<NVSDK_NGX_Result>(0x7FFFFFFF);
    __try { result = g_nr_evaluate(h.list, h.feature, h.params, nullptr); }
    __except (EXCEPTION_EXECUTE_HANDLER) { code = GetExceptionCode(); }
    g_last_eval_result = static_cast<uint32_t>(result);
    if (code != 0) { AbortCommands(); Log("[pure] direct evaluate exception 0x%08X", code); return false; }
    if (ts) h.list->EndQuery(g_ts_heap, D3D12_QUERY_TYPE_TIMESTAMP, 1);
    if (v.nr_small)
    {
        // The result is work-sized; stretch it into the full-res output the
        // rest of the pipeline expects. In residual mode the work-res nr_out
        // and nr_in are sampled at full-res UVs inside ResidualCompose and
        // the pristine native colour stays the anchor - the network does the
        // relighting, the native frame keeps the sharpness. output stays
        // UNORDERED_ACCESS, which is exactly what the compute pass writes to.
        D3D12_RESOURCE_BARRIER to_srv = Transition(
            v.nr_out, D3D12_RESOURCE_STATE_UNORDERED_ACCESS,
            D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
        h.list->ResourceBarrier(1, &to_srv);
        if (v.residual)
            ResidualCompose(v.color.tex, v.nr_in, v.nr_out, cw, ch, v.output,
                            v.residual_strength);
        else
            ScaleColorInto(v.nr_out, nw, nh, v.output, cw, ch, 1);
        D3D12_RESOURCE_BARRIER back = Transition(
            v.nr_out, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE,
            D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
        h.list->ResourceBarrier(1, &back);
    }
    if (ts)
        h.list->ResolveQueryData(g_ts_heap, D3D12_QUERY_TYPE_TIMESTAMP, 0, 2,
                                 g_ts_readback, 0);
    const UINT64 fence = EndCommands();
    if (NVSDK_NGX_FAILED(result)) { Log("[pure] direct evaluate failed 0x%08X (%s)", result, NgxResultName(result)); return false; }
    ++g_eval_count;
    if (!WaitFenceValue(h.fence, fence, 60000)) return false;
    if (ts) ReadEvalGpuTime();
    return true;
}

// The wipe divider: a narrow strip over the output texture.
//
// Drawn through ClearUnorderedAccessViewFloat with a rectangle - a solid strip
// needs no shader, and v.output already sits in UNORDERED_ACCESS, so we get by
// without barriers. It needs two descriptors for the same UAV: one visible to
// shaders and an ordinary CPU one, hence the two tiny heaps.
static ID3D12DescriptorHeap *g_split_heap_gpu;
static ID3D12DescriptorHeap *g_split_heap_cpu;
static ID3D12Resource       *g_split_uav_for;   // which resource the UAV was made for

static bool EnsureSplitUav(ID3D12Resource *res)
{
    if (res == nullptr) return false;
    if (g_split_uav_for == res && g_split_heap_gpu != nullptr) return true;
    if (g_split_heap_gpu != nullptr) { g_split_heap_gpu->Release(); g_split_heap_gpu = nullptr; }
    if (g_split_heap_cpu != nullptr) { g_split_heap_cpu->Release(); g_split_heap_cpu = nullptr; }
    g_split_uav_for = nullptr;

    D3D12_DESCRIPTOR_HEAP_DESC hd = {};
    hd.Type = D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV;
    hd.NumDescriptors = 1;
    hd.Flags = D3D12_DESCRIPTOR_HEAP_FLAG_SHADER_VISIBLE;
    if (FAILED(h.dev->CreateDescriptorHeap(&hd, __uuidof(ID3D12DescriptorHeap),
            reinterpret_cast<void **>(&g_split_heap_gpu)))) return false;
    hd.Flags = D3D12_DESCRIPTOR_HEAP_FLAG_NONE;
    if (FAILED(h.dev->CreateDescriptorHeap(&hd, __uuidof(ID3D12DescriptorHeap),
            reinterpret_cast<void **>(&g_split_heap_cpu)))) return false;

    D3D12_UNORDERED_ACCESS_VIEW_DESC ud = {};
    ud.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    ud.ViewDimension = D3D12_UAV_DIMENSION_TEXTURE2D;
    h.dev->CreateUnorderedAccessView(res, nullptr, &ud,
        g_split_heap_gpu->GetCPUDescriptorHandleForHeapStart());
    h.dev->CreateUnorderedAccessView(res, nullptr, &ud,
        g_split_heap_cpu->GetCPUDescriptorHandleForHeapStart());
    g_split_uav_for = res;
    return true;
}

// The before/after wipe: the left part of the frame is replaced by the raw capture.
//
// Both textures are full size and of the same format (NGX shrinks its input
// internally), so this is a single region copy, entirely on the GPU. It runs
// BEFORE the present and before the pixels are handed over, so the wipe ends
// up in the recording and the screenshot by itself.
static bool SplitCompose(VideoState &v, UINT split_x)
{
    const UINT cw = v.upscale ? v.full_w : v.w;
    const UINT ch = v.upscale ? v.full_h : v.hgt;
    if (split_x == 0 || cw == 0 || ch == 0) return true;
    if (split_x > cw) split_x = cw;
    if (!BeginCommands()) return false;
    D3D12_RESOURCE_BARRIER pre[] = {
        Transition(v.color.tex, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE,
                   D3D12_RESOURCE_STATE_COPY_SOURCE),
        Transition(v.output, D3D12_RESOURCE_STATE_UNORDERED_ACCESS,
                   D3D12_RESOURCE_STATE_COPY_DEST),
    };
    h.list->ResourceBarrier(_countof(pre), pre);
    D3D12_TEXTURE_COPY_LOCATION src = {}, dst = {};
    src.pResource = v.color.tex;
    src.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX; src.SubresourceIndex = 0;
    dst.pResource = v.output;
    dst.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX; dst.SubresourceIndex = 0;
    D3D12_BOX box = { 0, 0, 0, split_x, ch, 1 };
    h.list->CopyTextureRegion(&dst, 0, 0, 0, &src, &box);
    D3D12_RESOURCE_BARRIER post[] = {
        Transition(v.color.tex, D3D12_RESOURCE_STATE_COPY_SOURCE,
                   D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE),
        Transition(v.output, D3D12_RESOURCE_STATE_COPY_DEST,
                   D3D12_RESOURCE_STATE_UNORDERED_ACCESS),
    };
    h.list->ResourceBarrier(_countof(post), post);

    // The divider only when the wipe really splits the frame: at the edges the
    // strip would hug the border for no reason at all.
    if (split_x < cw && EnsureSplitUav(v.output))
    {
        const UINT lw = (ch >= 1400u) ? 3u : 2u;
        LONG left = static_cast<LONG>(split_x) - static_cast<LONG>(lw / 2);
        if (left < 0) left = 0;
        LONG right = left + static_cast<LONG>(lw);
        if (right > static_cast<LONG>(cw)) { right = static_cast<LONG>(cw); left = right - static_cast<LONG>(lw); }
        // #D97757 - the same clay accent as in the menu. The texture is plain
        // UNORM (not _SRGB), so the bytes go in as they are, without a gamma
        // conversion.
        const FLOAT accent[4] = { 217.0f / 255.0f, 119.0f / 255.0f, 87.0f / 255.0f, 1.0f };
        const D3D12_RECT rect = { left, 0, right, static_cast<LONG>(ch) };
        ID3D12DescriptorHeap *heaps[] = { g_split_heap_gpu };
        h.list->SetDescriptorHeaps(1, heaps);
        h.list->ClearUnorderedAccessViewFloat(
            g_split_heap_gpu->GetGPUDescriptorHandleForHeapStart(),
            g_split_heap_cpu->GetCPUDescriptorHandleForHeapStart(),
            v.output, accent, 1, &rect);
    }
    const UINT64 fence = EndCommands();
    return WaitFenceValue(h.fence, fence, 30000);
}

static bool DownloadVideoFrame(VideoState &v, std::vector<BYTE> &packed,
                               ID3D12Resource *src = nullptr,
                               D3D12_RESOURCE_STATES src_before = D3D12_RESOURCE_STATE_UNORDERED_ACCESS,
                               D3D12_RESOURCE_STATES src_after = D3D12_RESOURCE_STATE_UNORDERED_ACCESS)
{
    if (src == nullptr) src = v.output;
    if (!BeginCommands()) return false;
    D3D12_RESOURCE_BARRIER a = Transition(src, src_before,
                                           D3D12_RESOURCE_STATE_COPY_SOURCE);
    h.list->ResourceBarrier(1, &a);
    D3D12_TEXTURE_COPY_LOCATION s = {}, d = {};
    s.pResource = src; s.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
    d.pResource = v.readback; d.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
    d.PlacedFootprint = v.out_fp;
    h.list->CopyTextureRegion(&d, 0, 0, 0, &s, nullptr);
    D3D12_RESOURCE_BARRIER b = Transition(src, D3D12_RESOURCE_STATE_COPY_SOURCE,
                                           src_after);
    h.list->ResourceBarrier(1, &b);
    const UINT64 fence = EndCommands();
    if (!WaitFenceValue(h.fence, fence, 60000)) return false;

    BYTE *mapped = nullptr;
    // The readback buffer is RowPitch*(rows-1) + row_size bytes: D3D12 pads
    // every row of a copyable footprint to 256 bytes EXCEPT the last one.
    // Asking Map for RowPitch*rows reaches past the end of the resource and
    // Map simply fails - which killed the worker (exit 9, nothing logged) for
    // every frame width that is not a multiple of 64, those being the only
    // ones whose pitch needs no padding. Screen resolutions all are, so this
    // only surfaced when one-window mode made arbitrary widths normal.
    const UINT rb_rows = v.out_rows != 0 ? v.out_rows : v.hgt;
    const SIZE_T rb_bytes =
        static_cast<SIZE_T>(v.out_fp.Footprint.RowPitch) * (rb_rows - 1) +
        static_cast<SIZE_T>(v.out_row_size);
    D3D12_RANGE read_range = { 0, rb_bytes };
    if (FAILED(v.readback->Map(0, &read_range, reinterpret_cast<void **>(&mapped)))) return false;
    const UINT ow = v.upscale ? v.full_w : v.w;
    const UINT oh = v.upscale ? v.full_h : v.hgt;
    packed.resize(static_cast<size_t>(ow) * oh * 4);
    for (UINT y = 0; y < oh; ++y)
        memcpy(packed.data() + static_cast<size_t>(y) * ow * 4,
               mapped + static_cast<size_t>(y) * v.out_fp.Footprint.RowPitch,
               static_cast<size_t>(ow) * 4);
    D3D12_RANGE written = { 0, 0 };
    v.readback->Unmap(0, &written);
    return true;
}

static bool ReShadeHasFeature18()
{
    char path[MAX_PATH] = {};
    GetModuleFileNameA(nullptr, path, MAX_PATH);
    if (char *s = strrchr(path, '\\')) strcpy_s(s + 1, MAX_PATH - (s + 1 - path), "ReShade.log");
    HANDLE file = CreateFileA(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                              nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (file == INVALID_HANDLE_VALUE) return false;
    const DWORD n = GetFileSize(file, nullptr);
    std::vector<char> data(n > 0 ? static_cast<size_t>(n) + 1 : 1, 0);
    DWORD got = 0;
    if (n > 0) ReadFile(file, data.data(), n, &got, nullptr);
    CloseHandle(file);
    return strstr(data.data(), "feature 18 created") != nullptr &&
           strstr(data.data(), "inline feature 18 evaluation succeeded") != nullptr;
}

// Reads the next 24-byte message header. Returns:
//   1 = frame ready (color_ptr/mv_ptr point at the pixels, either at the
//       std::vectors filled from the pipe or straight into the shared mapping),
//   2 = resize command read into rc,
//   3 = shared-memory handover read into sc,
//   4 = presentation-window command read into wc,
//   5 = motion-size command read into mc,
//   6 = desktop-capture command read into dc,
//   7 = gray-downsample command read into gc (88 bytes: 24 hdr + 64 name),
//   0 = EOF/error.
static int ReadVideoMessage(VideoState &v, VideoFrameHeader &fh, std::vector<BYTE> &color,
                            std::vector<BYTE> &mv, VideoResizeCmd &rc, VideoShmCmd &sc,
                            VideoWindowCmd &wc, VideoMotionCmd &mc, VideoDdaCmd &dc,
                            VideoGrayCmd &gc, VideoOutCmd &oc,
                            const BYTE **color_ptr, const BYTE **mv_ptr)
{
    if (!ReadExact(stdin, &fh, sizeof(fh))) return 0;
    if (fh.magic == FRAME_MAGIC)
    {
        bool no_color = (fh.reserved & FRAME_FLAG_NO_COLOR) != 0;
        if (no_color && !CaptureActive())
        {
            // The client thinks a capture is active but it died (recreate
            // failed). We try to reopen once; if that fails or there is
            // nothing to reopen (a protocol desync) we exit: the client will
            // restart the worker and set the capture up again. Otherwise we
            // would read colour from the pipe that is not there - a
            // "truncated frame".
            if (g_wgc_hwnd != nullptr)
            {
                if (!OpenWgc(g_wgc_hwnd) || !g_wgc_active) return 0;
            }
            else if (g_dda_w == 0 || g_dda_h == 0)
            { Log("[cap] NO_COLOR with no capture to reopen - protocol desync"); return 0; }
            else if (!OpenDda(g_dda_w, g_dda_h) || !g_dda_active) return 0;
        }
        const size_t cw = v.upscale ? v.full_w : v.w;
        const size_t ch = v.upscale ? v.full_h : v.hgt;
        const size_t color_bytes = cw * ch * 4;
        // The downscaled motion field (MOTS) takes as much as it takes, not
        // the work resolution - otherwise the stream parsing falls apart.
        const bool small_mv = (fh.reserved & FRAME_FLAG_MOTION_SMALL) != 0 && g_motion_w != 0;
        const size_t mv_bytes = small_mv
            ? static_cast<size_t>(g_motion_w) * g_motion_h * 4
            : static_cast<size_t>(v.w) * v.hgt * 4;
        if (no_color)
        {
            // DDA mode: the worker grabs the colour itself; the pipe carries only motion.
            *color_ptr = nullptr;
            mv.resize(mv_bytes);
            *mv_ptr = mv.data();
            return ReadExact(stdin, mv.data(), mv.size()) ? 1 : 0;
        }
        if ((fh.reserved & FRAME_FLAG_SHM) != 0)
        {
            // Pixels are already in the mapping; the pipe carried only this header.
            if (g_shm_base == nullptr || g_shm_motion_off < color_bytes ||
                g_shm_bytes < g_shm_motion_off + mv_bytes)
            {
                Log("[video] SHM frame %u but mapping is missing or too small "
                    "(mapped=%zu, need colour=%zu at 0, motion=%zu at %zu)",
                    fh.index, g_shm_bytes, color_bytes, mv_bytes, g_shm_motion_off);
                return 0;
            }
            *color_ptr = g_shm_base;
            *mv_ptr = g_shm_base + g_shm_motion_off;
            return 1;
        }
        color.resize(color_bytes); mv.resize(mv_bytes);
        *color_ptr = color.data(); *mv_ptr = mv.data();
        return ReadExact(stdin, color.data(), color.size()) && ReadExact(stdin, mv.data(), mv.size()) ? 1 : 0;
    }
    if (fh.magic == SHM_MAGIC)
    {
        BYTE *p = reinterpret_cast<BYTE *>(&sc);
        memcpy(p, &fh, sizeof(fh));
        if (!ReadExact(stdin, p + sizeof(fh), sizeof(sc) - sizeof(fh))) return 0;
        return 3;
    }
    if (fh.magic == WINDOW_MAGIC)
    {
        // Same 24 bytes as the frame header — everything is already in fh.
        memcpy(&wc, &fh, sizeof(wc));
        return 4;
    }
    if (fh.magic == MOTION_MAGIC)
    {
        memcpy(&mc, &fh, sizeof(mc));
        return 5;
    }
    if (fh.magic == DDA_MAGIC)
    {
        memcpy(&dc, &fh, sizeof(dc));
        return 6;
    }
    if (fh.magic == WGC_MAGIC)
    {
        // 32 bytes: the 24-byte header is in fh, the HWND follows. It lands in
        // a file-scope command rather than another out-parameter - this
        // dispatcher already carries six of them.
        BYTE *p = reinterpret_cast<BYTE *>(&g_wgc_cmd);
        memcpy(p, &fh, sizeof(fh));
        if (!ReadExact(stdin, p + sizeof(fh), sizeof(g_wgc_cmd) - sizeof(fh))) return 0;
        return 9;
    }
    if (fh.magic == GRAY_MAGIC)
    {
        // The first 24 bytes are already in fh (it matches VideoFrameHeader);
        // read the remaining 64 bytes of the name - 88 in total, like SHMI.
        BYTE *p = reinterpret_cast<BYTE *>(&gc);
        memcpy(p, &fh, sizeof(fh));
        if (!ReadExact(stdin, p + sizeof(fh), sizeof(gc) - sizeof(fh))) return 0;
        return 7;
    }
    if (fh.magic == OUTS_MAGIC)
    {
        // Same layout as GRAY: the 24-byte header is already read, the name follows.
        BYTE *p = reinterpret_cast<BYTE *>(&oc);
        memcpy(p, &fh, sizeof(fh));
        if (!ReadExact(stdin, p + sizeof(fh), sizeof(oc) - sizeof(fh))) return 0;
        return 8;
    }
    if (fh.magic == RESIZE_MAGIC)
    {
        // The first 24 bytes of the 64-byte command already sit in fh; read the rest.
        BYTE *p = reinterpret_cast<BYTE *>(&rc);
        memcpy(p, &fh, sizeof(fh));
        if (!ReadExact(stdin, p + sizeof(fh), sizeof(rc) - sizeof(fh))) return 0;
        return 2;
    }
    return 0;
}

static void ReleaseVideoTextures(VideoState &v)
{
    // The shader descriptors referenced these resources - after they are
    // released the descriptors must be reissued (see BindScaleDescriptors).
    if (v.mv.tex == g_scale_dst_bound) g_scale_dst_bound = nullptr;
    if (v.color.tex != nullptr) { v.color.tex->Release(); v.color.tex = nullptr; }
    if (v.color.upload != nullptr) { v.color.upload->Release(); v.color.upload = nullptr; }
    if (v.mv.tex != nullptr) { v.mv.tex->Release(); v.mv.tex = nullptr; }
    if (v.mv.upload != nullptr) { v.mv.upload->Release(); v.mv.upload = nullptr; }
    if (v.output != nullptr) { v.output->Release(); v.output = nullptr; }
    if (v.readback != nullptr) { v.readback->Release(); v.readback = nullptr; }
    // The descriptors point at these resources; after a release they must be
    // reissued or the next frame samples freed memory.
    g_scale4_src_bound = g_scale4_dst_bound = nullptr;
    g_scale4_src_bound2 = g_scale4_dst_bound2 = nullptr;
    // Same for the matched residual pass: its descriptor set references the
    // nr_in/nr_out/native/output textures. Without the reset, an RNSZ whose
    // new allocations land at the just-freed addresses would make
    // BindResidualDescriptors skip the rebuild (pointer equality) and
    // dispatch against freed GPU memory (audit C++ H1).
    g_res_native_bound = g_res_in_bound = nullptr;
    g_res_out_bound = g_res_dst_bound = nullptr;
    if (v.nr_in != nullptr) { v.nr_in->Release(); v.nr_in = nullptr; }
    if (v.nr_out != nullptr) { v.nr_out->Release(); v.nr_out = nullptr; }
    v.nr_small = false;
    v.inputs_ready = false;
    // The capture flag says "the current frame is already in v.color" -
    // the texture was just released, so the flag is a lie. Without the
    // reset, an RNSZ in capture mode (WGCW/DDA1) evaluates on a freed
    // texture when the window has not redrawn: the frame comes out black
    // until the next real capture (user: the window goes black after a
    // profile change and only a click into it restores the picture).
    g_dda_ready = false;
}

// Forward declaration: defined below, called from RunVideo on shutdown.
static void CleanupVideoNgx();

// ---------------------------------------------------------------------------
// Frame phase profiling. The goal is to see where the time goes and, in
// particular, to test the vblank hypothesis - at 60 Hz the budget is 16.7 ms,
// and if Present paces the pipeline in multiples of it, that shows in a
// histogram rather than in an average. So Present also gets a bucket
// distribution.
// ---------------------------------------------------------------------------
static const char *kPhaseNames[PH_COUNT] =
    { "acq(wait desktop)", "dda(total)", "upload", "eval", "present", "frame" };
static double g_ph_sum[PH_COUNT];
static double g_ph_max[PH_COUNT];
static unsigned g_ph_n[PH_COUNT];
// Present buckets, ms: <5, 5-12, 12-20 (~1 vblank), 20-28, 28-40 (~2 vblank), 40+
static const double kPresentBins[] = { 5.0, 12.0, 20.0, 28.0, 40.0 };
static unsigned g_ph_bins[6];
static LARGE_INTEGER g_qpf;
static UINT64 g_ph_tick;

// The profiler is turned on by the NS_PHASE=1 environment variable. Off by
// default: otherwise it litters the log with a line every 2 seconds all session.
static int g_phase_on = -1;

static bool PhaseEnabled()
{
    if (g_phase_on < 0)
    {
        char buf[8] = {};
        const DWORD got = GetEnvironmentVariableA("NS_PHASE", buf, sizeof(buf));
        g_phase_on = (got > 0 && buf[0] == '1') ? 1 : 0;
        if (g_phase_on) Log("[phase] phase profiler enabled (NS_PHASE=1)");
    }
    return g_phase_on == 1;
}

static double PhaseNow()
{
    if (g_qpf.QuadPart == 0) QueryPerformanceFrequency(&g_qpf);
    LARGE_INTEGER t;
    QueryPerformanceCounter(&t);
    return static_cast<double>(t.QuadPart) * 1000.0 / static_cast<double>(g_qpf.QuadPart);
}

static void PhaseAdd(int idx, double t0)
{
    if (!PhaseEnabled()) return;
    const double ms = PhaseNow() - t0;
    g_ph_sum[idx] += ms;
    if (ms > g_ph_max[idx]) g_ph_max[idx] = ms;
    ++g_ph_n[idx];
    if (idx == PH_PRESENT)
    {
        int b = 0;
        while (b < 5 && ms >= kPresentBins[b]) ++b;
        ++g_ph_bins[b];
    }
}

static void PhaseReport(bool bypass)
{
    if (!PhaseEnabled()) return;
    const UINT64 now = GetTickCount64();
    if (g_ph_tick == 0) { g_ph_tick = now; return; }
    if (now - g_ph_tick < 2000) return;
    g_ph_tick = now;

    char line[640];
    int off = _snprintf_s(line, sizeof(line), _TRUNCATE, "[phase] %s", bypass ? "bypass" : "NR");
    for (int i = 0; i < PH_COUNT && off > 0; ++i)
    {
        if (g_ph_n[i] == 0) continue;
        off += _snprintf_s(line + off, sizeof(line) - off, _TRUNCATE, " | %s %.1f/%.1f",
                           kPhaseNames[i], g_ph_sum[i] / g_ph_n[i], g_ph_max[i]);
        g_ph_sum[i] = 0.0; g_ph_max[i] = 0.0; g_ph_n[i] = 0;
    }
    if (g_ts_n > 0)
    {
        _snprintf_s(line + off, sizeof(line) - off, _TRUNCATE,
                    " | eval on GPU %.1f/%.1f", g_ts_sum / g_ts_n, g_ts_max);
        g_ts_sum = 0.0; g_ts_max = 0.0; g_ts_n = 0;
    }
    Log("%s (mean/max, ms)", line);
    Log("[phase] present by bucket, ms: <5=%u 5-12=%u 12-20=%u 20-28=%u 28-40=%u 40+=%u",
        g_ph_bins[0], g_ph_bins[1], g_ph_bins[2], g_ph_bins[3], g_ph_bins[4], g_ph_bins[5]);
    for (int b = 0; b < 6; ++b) g_ph_bins[b] = 0;
}

static int RunVideo()
{
    _setmode(_fileno(stdin), _O_BINARY);
    _setmode(_fileno(stdout), _O_BINARY);
    VideoHeader vh = {};
    // Accept both the legacy 56-byte header (magic 0x32563544, no full_w/full_h)
    // and the extended 64-byte header (magic 0x33563544, with full_w/full_h).
    BYTE raw[sizeof(VideoHeader)] = {};
    if (!ReadExact(stdin, raw, VIDEO_HEADER_LEGACY_SIZE)) { Log("[video] no stream header"); return 2; }
    memcpy(&vh, raw, VIDEO_HEADER_LEGACY_SIZE);
    if (vh.magic == VIDEO_MAGIC_EXT)
    {
        if (!ReadExact(stdin, raw + VIDEO_HEADER_LEGACY_SIZE, sizeof(VideoHeader) - VIDEO_HEADER_LEGACY_SIZE))
        { Log("[video] truncated extended header"); return 2; }
        memcpy(&vh, raw, sizeof(VideoHeader));
    }
    if ((vh.magic != VIDEO_MAGIC && vh.magic != VIDEO_MAGIC_EXT) || vh.width < 64 || vh.height < 64 ||
        vh.width > 7680 || vh.height > 4320)
    { Log("[video] invalid stream header (magic=0x%08X)", vh.magic); return 2; }
    const bool upscale = (vh.magic == VIDEO_MAGIC_EXT) && vh.full_w > 0 && vh.full_h > 0 &&
                         (vh.full_w != vh.width || vh.full_h != vh.height);
    if (upscale && (vh.full_w < vh.width || vh.full_h < vh.height || vh.full_w > 7680 || vh.full_h > 4320))
    { Log("[video] invalid full-res size %ux%u", vh.full_w, vh.full_h); return 2; }
    g_video_options = vh;
    const bool live = g_live_force || (vh.frame_count == 0);
    std::string full_note;
    if (upscale) full_note = " (full-res frames " + std::to_string(vh.full_w) + "x" + std::to_string(vh.full_h) + ")";
    Log("[video] stream %ux%u%s, %s, warmup=%u", vh.width, vh.height, full_note.c_str(),
        live ? "LIVE (unbounded)" : "bounded", vh.warmup);

    VideoState v;
    if (!CreateVideoResources(v, vh.width, vh.height, upscale ? vh.full_w : 0, upscale ? vh.full_h : 0))
    { Log("[video] resource creation failed"); return 3; }
    int flags = NVSDK_NGX_DLSS_Feature_Flags_MVLowRes | NVSDK_NGX_DLSS_Feature_Flags_AutoExposure |
                NVSDK_NGX_DLSS_Feature_Flags_DepthInverted;
    NVSDK_NGX_Result create_result = NVSDK_NGX_Result_Fail;
    // In nr_small mode the feature is created at the work resolution and told
    // nothing about the screen: it is handed a work-sized frame and returns a
    // work-sized one, and the scaling on both sides is ours.
    if (!CreateFeature(vh.width, vh.height, flags, &create_result,
                       (v.nr_small || !upscale) ? 0 : vh.full_w,
                       (v.nr_small || !upscale) ? 0 : vh.full_h))
    {
        // A feature-create refusal is not a reason to kill the desktop
        // overlay. Keep the worker alive and pass raw frames through (the
        // bypass path below shows v.color). This prevents the restart storm
        // and the black topmost window seen on unsupported or partially
        // spoofed GPUs. A later RNSZ can retry feature creation.
        h.feature = nullptr;
        Log("[video] NR feature unavailable (0x%08X) - SAFE PASSTHROUGH",
            static_cast<uint32_t>(create_result));
    }

    VideoFrameHeader fh = {};
    std::vector<BYTE> color, mv, output;
    bool warmup_done = false;
    uint32_t frame = 0;
    for (;;)
    {
        VideoResizeCmd rc = {};
        VideoShmCmd sc = {};
        VideoWindowCmd wc = {};
        VideoMotionCmd mc = {};
        VideoDdaCmd dc = {};
        VideoGrayCmd gc = {};
        VideoOutCmd oc = {};
        const BYTE *color_ptr = nullptr;
        const BYTE *mv_ptr = nullptr;
        const int msg = ReadVideoMessage(v, fh, color, mv, rc, sc, wc, mc, dc, gc,
                                         oc, &color_ptr, &mv_ptr);
        if (msg == 0)
        {
            if (live)
            {
                Log("[live] input stream closed after %u frames; %u direct evaluations", frame, g_eval_count);
                // Explicit NGX resource cleanup BEFORE exiting: without
                // ReleaseFeature/Shutdown1 the GPU resources (the D3D12 device,
                // NGX) are freed only when the process dies, and a quick
                // restart of a new worker conflicts with the leftovers of the
                // old one (exit 127 / a hang).
                CleanupVideoNgx();
                CloseSharedInput();
                ClosePresent();
                CloseMotionScaler();
                CloseDda();
                CloseGray();
                // The Spout2 sender too: it holds a D3D11 device, a context
                // and a 4K shared texture with an NT handle, and it was the
                // one subsystem left to the process exit while everything
                // around it was torn down explicitly (audit). Safe when the
                // bridge was never enabled - every pointer is null.
                SpoutBridgeShutdown();
                return 0;
            }
            Log("[video] truncated frame %u", frame); return 5;
        }
        if (msg == 2)
        {
            // RNSZ: reconfigure the feature at a new work size WITHOUT restarting
            // the process. The client keeps the same stream; the next frame after
            // the RACK is interpreted at the new sizes.
            if (rc.width < 64 || rc.height < 64 || rc.width > 7680 || rc.height > 4320)
            {
                Log("[video] RNSZ rejected: invalid work size %ux%u", rc.width, rc.height);
                VideoResizeAck bad = { RESIZE_ACK_MAGIC, 0u, 0xBAD00005u, 0u, fh.pts };
                if (!WriteExact(stdout, &bad, sizeof(bad))) return 10;
                continue;
            }
            const bool rup = rc.full_w > 0 && rc.full_h > 0 &&
                             (rc.full_w != rc.width || rc.full_h != rc.height);
            if (rup && (rc.full_w < rc.width || rc.full_h < rc.height ||
                        rc.full_w > 7680 || rc.full_h > 4320))
            {
                Log("[video] RNSZ rejected: invalid full size %ux%u", rc.full_w, rc.full_h);
                VideoResizeAck bad = { RESIZE_ACK_MAGIC, 0u, 0xBAD00005u, 0u, fh.pts };
                if (!WriteExact(stdout, &bad, sizeof(bad))) return 10;
                continue;
            }
            Log("[video] RNSZ: work %ux%u -> %ux%u (full %ux%u), warmup=%u",
                v.w, v.hgt, rc.width, rc.height, rc.full_w, rc.full_h, rc.warmup);
            // 1. Drain the GPU: the old feature must be idle before release.
            WaitFenceValue(h.fence, h.fence_value, 2000);
            // 2. Release the old feature and textures.
            SafeReleaseFeature(h.feature);
            h.feature = nullptr;
            ReleaseVideoTextures(v);
            // 3. New options (profile/params travel with the command).
            // VideoResizeCmd has the same packed layout as VideoHeader.
            memcpy(&g_video_options, &rc, sizeof(g_video_options));
            // 4. Recreate textures + feature at the new sizes.
            const int want_small = (rc.flags & RESIZE_FLAG_NR_SMALL) != 0 ? 1 : 0;
            if (!CreateVideoResources(v, rc.width, rc.height, rup ? rc.full_w : 0,
                                      rup ? rc.full_h : 0, want_small))
            {
                Log("[video] RNSZ: resource creation failed at %ux%u", rc.width, rc.height);
                VideoResizeAck bad = { RESIZE_ACK_MAGIC, 0u, 0x7FFFFFFFu, 0u, fh.pts };
                if (!WriteExact(stdout, &bad, sizeof(bad))) return 3;
            }
            NVSDK_NGX_Result rr = NVSDK_NGX_Result_Fail;
            if (!CreateFeature(rc.width, rc.height, flags, &rr,
                               (v.nr_small || !rup) ? 0 : rc.full_w,
                               (v.nr_small || !rup) ? 0 : rc.full_h))
            {
                h.feature = nullptr;
                Log("[video] RNSZ: feature create failed at %ux%u - SAFE PASSTHROUGH",
                    rc.width, rc.height);
            }
            warmup_done = (h.feature == nullptr);   // only warm a real NR feature
            VideoResizeAck ack = { RESIZE_ACK_MAGIC, 1u, static_cast<uint32_t>(rr), 0u, fh.pts };
            if (!WriteExact(stdout, &ack, sizeof(ack))) return 10;
            g_force_next_frame = true;   // the new setting must be shown on the next frame
            Log("[video] RNSZ applied: feature ready at %ux%u", rc.width, rc.height);
            continue;
        }
        if (msg == 3)
        {
            // SHMI: open the client's named section. From now on frames whose
            // header carries FRAME_FLAG_SHM are uploaded straight from it.
            sc.name[sizeof(sc.name) - 1] = '\0';
            CloseSharedInput();
            const size_t need = static_cast<size_t>(sc.color_bytes) +
                                static_cast<size_t>(sc.motion_bytes);
            uint32_t ok = 0;
            if (sc.color_bytes == 0 || sc.motion_bytes == 0 || need > (size_t)1 << 31)
                Log("[video] SHMI rejected: implausible sizes colour=%u motion=%u",
                    sc.color_bytes, sc.motion_bytes);
            else
            {
                g_shm_handle = OpenFileMappingA(FILE_MAP_READ, FALSE, sc.name);
                if (g_shm_handle == nullptr)
                    Log("[video] SHMI: OpenFileMapping('%s') failed, err=%lu",
                        sc.name, GetLastError());
                else
                {
                    g_shm_base = static_cast<const BYTE *>(
                        MapViewOfFile(g_shm_handle, FILE_MAP_READ, 0, 0, need));
                    if (g_shm_base == nullptr)
                    {
                        Log("[video] SHMI: MapViewOfFile(%zu) failed, err=%lu", need, GetLastError());
                        CloseHandle(g_shm_handle);
                        g_shm_handle = nullptr;
                    }
                    else
                    {
                        g_shm_bytes = need;
                        g_shm_motion_off = sc.color_bytes;
                        ok = 1;
                        Log("[video] SHMI: mapped '%s', %zu bytes (motion at +%zu)",
                            sc.name, need, g_shm_motion_off);
                    }
                }
            }
            VideoShmAck ack = { SHM_ACK_MAGIC, ok, 0u, 0u, sc.pts };
            if (!WriteExact(stdout, &ack, sizeof(ack))) return 10;
            continue;
        }
        if (msg == 4)
        {
            // WNDO: raise (or tear down) the overlay the worker presents into.
            uint32_t ok = 0;
            if ((wc.flags & WINDOW_FLAG_DISABLE) != 0 || wc.width == 0 || wc.height == 0)
            {
                ClosePresent();
                Log("[present] overlay closed; results go back to the client");
                ok = 1;
            }
            else
                ok = OpenPresent(wc.width, wc.height, wc.flags) ? 1u : 0u;
            if (ok) g_force_next_frame = true;   // a fresh window gets a picture at once
            VideoWindowAck ack = { WINDOW_ACK_MAGIC, ok, 0u, 0u, wc.pts };
            if (!WriteExact(stdout, &ack, sizeof(ack))) return 10;
            continue;
        }
        if (msg == 5)
        {
            // MOTS: from now on the motion field arrives downscaled and we
            // upscale it on the GPU.
            const uint32_t ok = OpenMotionScaler(mc.width, mc.height) ? 1u : 0u;
            VideoMotionAck ack = { MOTION_ACK_MAGIC, ok, 0u, 0u, mc.pts };
            if (!WriteExact(stdout, &ack, sizeof(ack))) return 10;
            continue;
        }
        if (msg == 6)
        {
            // DDA1: take over the capture (width==0 turns it off, back to the pipe).
            uint32_t ok = 0;
            if (dc.width == 0 || dc.height == 0)
            {
                CloseDda();
                ok = 1;
            }
            else
                ok = OpenDda(dc.width, dc.height) ? 1u : 0u;
            Log("[video] DDA1 %s (%ux%u)", ok ? "OK" : "FAIL", dc.width, dc.height);
            VideoDdaAck ack = { DDA_ACK_MAGIC, ok, 0u, 0u, dc.pts };
            if (!WriteExact(stdout, &ack, sizeof(ack))) return 10;
            continue;
        }
        if (msg == 7)
        {
            // GRAY: the client passed a reverse mapping for luminance frames.
            uint32_t ok = 0;
            if (gc.width == 0 || gc.height == 0)
            {
                CloseGray();
                ok = 1;
            }
            else
                ok = OpenGray(gc) ? 1u : 0u;
            Log("[video] GRAY %s (%ux%u)", ok ? "OK" : "FAIL", gc.width, gc.height);
            VideoGrayAck ack = { GRAY_ACK_MAGIC, ok, 0u, 0u, gc.pts };
            if (!WriteExact(stdout, &ack, sizeof(ack))) return 10;
            continue;
        }
        if (msg == 8)
        {
            // OUTS: where to put the pixels instead of the pipe.
            const uint32_t ok = OpenOut(oc) ? 1u : 0u;
            Log("[video] OUTS %s (%ux%u)", ok ? "OK" : "FAIL", oc.width, oc.height);
            VideoOutAck ack = { OUTS_ACK_MAGIC, ok, 0u, 0u, oc.pts };
            if (!WriteExact(stdout, &ack, sizeof(ack))) return 10;
            continue;
        }
        if (msg == 9)
        {
            // WGCW: capture one window instead of the desktop (hwnd == 0 off).
            const HWND hwnd = reinterpret_cast<HWND>((uintptr_t)g_wgc_cmd.hwnd);
            uint32_t ok = 0;
            if (hwnd == nullptr) { CloseWgc(); ok = 1; }
            else ok = OpenWgc(hwnd) ? 1u : 0u;
            if (ok) g_force_next_frame = true;   // the new source shows at once
            // The size the capture really produces - physical pixels, which is
            // what the client has to size its textures for.
            VideoWgcAck ack = { WGC_ACK_MAGIC, ok, ok ? g_dda_w : 0u,
                                ok ? g_dda_h : 0u, g_wgc_cmd.pts };
            Log("[video] WGCW %s (%p -> %ux%u)", ok ? "OK" : "FAIL",
                (void *)hwnd, ack.width, ack.height);
            if (!WriteExact(stdout, &ack, sizeof(ack))) return 10;
            continue;
        }
        const double t_frame = PhaseNow();
        if (CaptureActive())
        {
            // Capture mode: the colour comes from the desktop (DDA1) or from
            // one window (WGCW) straight on the GPU, motion from the frame
            // sent in (the client keeps sending pairs).
            const double t_dda = PhaseNow();
            const bool got = g_wgc_active ? WgcGrab(v) : DdaGrab(v);
            PhaseAdd(PH_DDA, t_dda);
            if (!got && !g_dda_ready)
            {
                // Not a single real desktop frame yet: keep the protocol
                // paired with an empty OUT1 and wait for the screen to change,
                // WITHOUT running NGX on an empty colour (evaluate on zero hangs).
                VideoResultHeader empty = { OUT_MAGIC, fh.index, 1u, 0u, g_last_eval_result, fh.pts };
                if (!WriteExact(stdout, &empty, sizeof(empty))) return 10;
                continue;
            }
            if (!got)
            {
                // No new frame: the desktop did not change (DDA timeout) or the
                // window did not redraw (WGC empty pool). Re-running the network
                // on the stale texture is pure waste - an idle desktop used to be
                // a full load. The client asks for the skip in bit 6 and does not
                // need pixels this frame; an empty OUT1 is the "nothing changed"
                // answer. WANT_PIXELS wins: a screenshot or a recording wants the
                // picture even when it did not change. So does anything that
                // changes what the picture WOULD show - see g_last_out_*.
                const bool want_bypass = (fh.reserved & FRAME_FLAG_BYPASS) != 0 ||
                                         h.feature == nullptr;
                const bool split_on = (fh.reserved & FRAME_FLAG_SPLIT) != 0;
                const UINT split_cw = v.upscale ? v.full_w : v.w;
                const uint32_t split_x = split_on
                    ? SplitXFromFlags(fh.reserved, split_cw) : 0u;
                const bool out_changed = want_bypass != g_last_out_bypass ||
                                         split_on != g_last_out_split_on ||
                                         (split_on && split_x != g_last_out_split_x) ||
                                         g_force_next_frame;
                const bool skip = (fh.reserved & FRAME_FLAG_SKIP_STATIC) != 0 &&
                                  (fh.reserved & FRAME_FLAG_WANT_PIXELS) == 0 &&
                                  !out_changed;
                if (skip)
                {
                    if (!g_skip_static_logged)
                    {
                        g_skip_static_logged = true;
                        Log("[skip] no new frame - the network is idle until the screen changes");
                    }
                    ++g_skip_static_count;
                    // The picture itself does not change, but in one-window mode
                    // the frame it sits in can still move - keep the overlay on it.
                    FollowCapturedWindow();
                    ReassertPresentTopmost();
                    VideoResultHeader idle = { OUT_MAGIC, fh.index, 1u, 0u, g_last_eval_result, fh.pts };
                    if (!WriteExact(stdout, &idle, sizeof(idle))) return 10;
                    continue;
                }
            }
            else if (g_skip_static_count != 0)
            {
                Log("[skip] the screen changed - %u frames skipped, the network resumes",
                    g_skip_static_count);
                g_skip_static_count = 0;
                g_skip_static_logged = false;
            }
            // This frame is being processed: remember what it will show, so the
            // next unchanged frame can tell whether anything differs.
            g_last_out_bypass = (fh.reserved & FRAME_FLAG_BYPASS) != 0 || h.feature == nullptr;
            g_last_out_split_on = (fh.reserved & FRAME_FLAG_SPLIT) != 0;
            g_last_out_split_x = g_last_out_split_on
                ? SplitXFromFlags(fh.reserved, v.upscale ? v.full_w : v.w) : 0u;
            g_force_next_frame = false;
            const double t_up = PhaseNow();
            const bool up_ok = GpuMotionExperiment() ? RunGpuMotionExperiment(v, fh.reset != 0) : UploadMotionOnly(v, mv_ptr,
                                  (fh.reserved & FRAME_FLAG_MOTION_SMALL) != 0 && g_motion_w != 0);
            PhaseAdd(PH_UPLOAD, t_up);
            if (!up_ok) return 6;
        }
        else
        {
            const double t_up = PhaseNow();
            const bool up_ok = UploadVideoFrame(v, color_ptr, mv_ptr,
                                   (fh.reserved & FRAME_FLAG_MOTION_SMALL) != 0 && g_motion_w != 0);
            PhaseAdd(PH_UPLOAD, t_up);
            if (!up_ok) return 6;
        }
        if (!warmup_done)
        {
            // No feature (SAFE PASSTHROUGH): nothing to warm up - the raw
            // frame goes out as-is. Evaluating with a null handle would
            // fault inside NGX.
            if (h.feature == nullptr)
            {
                warmup_done = true;
            }
            else
            {
                const uint32_t warmup = (std::min)(240u, (std::max)(1u, g_video_options.warmup));
                for (uint32_t i = 0; i < warmup; ++i)
                {
                    if (!EvaluateVideo(v, i == 0 ? 1 : 0)) return 7;
                }
                warmup_done = true;
                Log("[pure] direct feature 18 confirmed after %u discarded warmup frames", warmup);
            }
        }
        g_hdr_split = (fh.reserved & FRAME_FLAG_SPLIT) ?
            SplitXFromFlags(fh.reserved, v.upscale ? v.full_w : v.w) : UINT_MAX;
        const bool bypass = (fh.reserved & FRAME_FLAG_BYPASS) != 0 || h.feature == nullptr;
        if (!bypass)
        {
            const double t_eval = PhaseNow();
            const bool ev_ok = EvaluateVideo(v, (frame == 0 || fh.reset != 0) ? 1 : 0);
            PhaseAdd(PH_EVAL, t_eval);
            if (!ev_ok) return 9;
            if ((fh.reserved & FRAME_FLAG_SPLIT) != 0)
            {
                // In bypass the wipe is meaningless: both halves would be the
                // raw capture, so only on a processed frame.
                const UINT cw = v.upscale ? v.full_w : v.w;
                if (!SplitCompose(v, SplitXFromFlags(fh.reserved, cw))) return 9;
            }
        }
        if (PresentModeActive(v))
        {
            // The overlay shows the result; the client gets an empty OUT1 as
            // the "frame done" signal and never sees the pixels -- unless it
            // explicitly asked for this one frame (screenshot).
            const double t_pres = PhaseNow();
            // NR OFF: show the raw capture (v.color is already full-res) - the
            // NGX evaluate is skipped but the pipeline is alive (window, HUD).
            FollowCapturedWindow();
            ReassertPresentTopmost();
            const bool pres_ok = bypass ? PresentBypass(v) : PresentFrame(v);
            PhaseAdd(PH_PRESENT, t_pres);
            if (!pres_ok) return 9;
            // Pixels for the client (screenshot/recording) - in bypass mode
            // too: the recording needs exactly the frame that is on screen
            // (the raw capture).
            if ((fh.reserved & FRAME_FLAG_WANT_PIXELS) != 0)
            {
                bool dl_ok = false;
                if (bypass)
                    dl_ok = DownloadVideoFrame(v, output, v.color.tex,
                                               D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE,
                                               D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
                else
                    dl_ok = DownloadVideoFrame(v, output);
                if (!dl_ok) return 9;
                if (!DeliverPixels(output, fh.index, fh.pts)) return 10;
            }
            else
            {
                VideoResultHeader out = { OUT_MAGIC, fh.index, 1u, 0u, g_last_eval_result, fh.pts };
                if (!WriteExact(stdout, &out, sizeof(out))) return 10;
            }
        }
        else
        {
            // No present window: the pixels go back to the client. In bypass
            // mode the raw capture (v.color) is the frame - the same choice
            // the present branch makes above; output would hold the stale
            // NGX result from the last processed frame.
            if (bypass)
            {
                if (!DownloadVideoFrame(v, output, v.color.tex,
                                        D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE,
                                        D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE))
                    return 9;
            }
            else if (!DownloadVideoFrame(v, output)) return 9;
            if (!DeliverPixels(output, fh.index, fh.pts)) return 10;
        }
        PhaseAdd(PH_FRAME, t_frame);
        PhaseReport(bypass);
        if (frame < 3 || ((frame + 1) % 30) == 0)
            Log("[video] delivered frame %u%s", frame + 1, live ? " (live)" : "");
        ++frame;
        if (!live && frame >= vh.frame_count) break;
    }
    Log("[pure] complete: %u frames delivered, %u direct evaluations", frame, g_eval_count);
    CleanupVideoNgx();
    CloseOut();
    CloseSharedInput();
    ClosePresent();
    CloseMotionScaler();
    CloseDda();
    CloseGray();
    SpoutBridgeShutdown();
    return 0;
}

// ---------------------------------------------------------------------------
// CleanupVideoNgx: release the NGX resources of video/live mode before exit.
// Without this the GPU resources (the D3D12 device, the NGX feature) are freed
// only when the process dies - a quick worker restart conflicts with the
// leftovers (exit 127 / a hang on frame 0). Called on a normal shutdown.
// ---------------------------------------------------------------------------
static void CleanupVideoNgx()
{
    if (h.feature != nullptr)
    {
        SafeReleaseFeature(h.feature);
        h.feature = nullptr;
        Log("[video] feature released");
    }
    if (h.params != nullptr)
    {
        NVSDK_NGX_D3D12_DestroyParameters(h.params);
        h.params = nullptr;
    }
    if (h.ngx_inited)
    {
        NVSDK_NGX_D3D12_Shutdown1(h.dev);
        h.ngx_inited = false;
        Log("[video] NGX shutdown");
    }
}

// ---------------------------------------------------------------------------
// Serve mode: the real pipe server for a 32-bit game
// ---------------------------------------------------------------------------

static bool ReadFull(HANDLE pipe, void *buf, DWORD len)
{
    DWORD got = 0;
    return ReadFile(pipe, buf, len, &got, nullptr) && got == len;
}

static int Serve(DWORD game_pid)
{
    char name[128];
    sprintf_s(name, FEED_PIPE_FMT, static_cast<unsigned long>(game_pid));
    HANDLE pipe = CreateNamedPipeA(name, PIPE_ACCESS_DUPLEX, PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT,
                                   1, 1024, 1024, 0, nullptr);
    if (pipe == INVALID_HANDLE_VALUE) { Log("[host] CreateNamedPipe failed %lu", GetLastError()); return 1; }
    Log("[host] serving on %s", name);
    if (!ConnectNamedPipe(pipe, nullptr) && GetLastError() != ERROR_PIPE_CONNECTED)
    { Log("[host] ConnectNamedPipe failed %lu", GetLastError()); return 1; }

    FeedHello hello = {};
    if (!ReadFull(pipe, &hello, sizeof(hello)) || hello.magic != FEED_IPC_MAGIC)
    { Log("[host] bad hello"); return 1; }
    FeedHelloAck ack = { FEED_IPC_MAGIC, FEED_IPC_VERSION };
    DWORD put = 0;
    WriteFile(pipe, &ack, sizeof(ack), &put, nullptr);
    Log("[host] game pid %u connected (protocol v%u)", hello.pid, hello.version);

    HANDLE hgame = OpenProcess(PROCESS_DUP_HANDLE, FALSE, hello.pid);
    if (hgame == nullptr) { Log("[host] OpenProcess failed %lu", GetLastError()); return 1; }

    // Shared fences live for the whole session.
    HANDLE hin = nullptr, hout = nullptr;
    h.dev->CreateFence(0, D3D12_FENCE_FLAG_SHARED, __uuidof(ID3D12Fence), reinterpret_cast<void **>(&h.fence_in));
    h.dev->CreateFence(0, D3D12_FENCE_FLAG_SHARED, __uuidof(ID3D12Fence), reinterpret_cast<void **>(&h.fence_out));
    if (h.fence_in == nullptr || h.fence_out == nullptr ||
        FAILED(h.dev->CreateSharedHandle(h.fence_in, nullptr, GENERIC_ALL, nullptr, &hin)) ||
        FAILED(h.dev->CreateSharedHandle(h.fence_out, nullptr, GENERIC_ALL, nullptr, &hout)))
    { Log("[host] shared fence creation failed"); return 1; }

    HANDLE game_in = nullptr, game_out = nullptr;
    DuplicateHandle(GetCurrentProcess(), hin, hgame, &game_in, 0, FALSE, DUPLICATE_SAME_ACCESS);
    DuplicateHandle(GetCurrentProcess(), hout, hgame, &game_out, 0, FALSE, DUPLICATE_SAME_ACCESS);

    int flags_active = 0;
    bool transport_only = false;
    float mvsx = 1.0f, mvsy = 1.0f;
    // The DLSS 5 add-on arms its NGX hooks ~150 ms after NGX init; the first create must
    // not race that (a 15 ms miss latched STANDBY in Blacklist), so hold it briefly.
    UINT64 hold_until = GetTickCount64() + 800;
    UINT64 evaluated  = 0;
    bool   warm_done  = g_renodx_lazy;   // v45+ adopts missed creates on its own
    int    build_fails = 0;

    for (;;)
    {
        // Peek the next message type by size: Build (big) vs FrameMsg (small).
        // The pipe is byte-mode from a single writer, so read the smaller header
        // first and decide -- FeedFrameMsg and FeedBuild share no prefix, so the
        // client precedes every message with a 1-byte tag instead.
        //
        // A plain blocking ReadFile here starves the message pump (and Present)
        // whenever the game stops feeding frames -- paused, loading, a menu -- and
        // Windows shows the host window as "Not Responding". Poll instead, so the
        // window (and its ReShade overlay) stays alive and clickable at all times.
        BYTE tag = 0;
        bool tag_read = false;
        for (;;)
        {
            DWORD avail = 0;
            if (!PeekNamedPipe(pipe, nullptr, 0, nullptr, &avail, nullptr)) break;   // pipe broken
            if (avail > 0) { tag_read = ReadFull(pipe, &tag, 1); break; }
            PumpPresent();
            Sleep(8);
        }
        if (!tag_read) { Log("[host] pipe closed by the game"); break; }

        if (tag == 'B')
        {
            FeedBuild b = {};
            if (!ReadFull(pipe, &b, sizeof(b))) break;
            Log("[host] build: %ux%u color=%u output=%u hdr=%d inverted=%d", b.width, b.height,
                b.color_fmt, b.output_fmt, b.hdr, b.depth_inverted);

            // Tear down the old set.
            SafeReleaseFeature(h.feature);
            h.feature = nullptr;
            for (int i = 0; i < FEED_SLOTS; ++i)
                if (h.tex[i] != nullptr) { h.tex[i]->Release(); h.tex[i] = nullptr; }

            // Open the game's textures (duplicate the handles out of the game).
            bool ok = true;
            for (int i = 0; i < FEED_SLOTS && ok; ++i)
            {
                HANDLE local = nullptr;
                if (!DuplicateHandle(hgame, reinterpret_cast<HANDLE>(static_cast<uintptr_t>(b.tex[i])),
                                     GetCurrentProcess(), &local, 0, FALSE, DUPLICATE_SAME_ACCESS))
                { Log("[host] DuplicateHandle(tex %d) failed %lu", i, GetLastError()); ok = false; break; }
                HRESULT hr = h.dev->OpenSharedHandle(local, __uuidof(ID3D12Resource),
                                                     reinterpret_cast<void **>(&h.tex[i]));
                CloseHandle(local);
                if (FAILED(hr)) { Log("[host] OpenSharedHandle(tex %d) failed 0x%08X", i, hr); ok = false; }
            }

            NVSDK_NGX_Result rf = NVSDK_NGX_Result_Fail;
            if (ok)
            {
                h.width = b.width; h.height = b.height;
                h.color_fmt  = static_cast<DXGI_FORMAT>(b.color_fmt);
                h.output_fmt = static_cast<DXGI_FORMAT>(b.output_fmt);
                mvsx = b.mv_scale_x; mvsy = b.mv_scale_y;
                transport_only = b.transport != 0;
                flags_active = NVSDK_NGX_DLSS_Feature_Flags_MVLowRes | NVSDK_NGX_DLSS_Feature_Flags_AutoExposure;
                if (b.depth_inverted) flags_active |= NVSDK_NGX_DLSS_Feature_Flags_DepthInverted;
                if (b.hdr)            flags_active |= NVSDK_NGX_DLSS_Feature_Flags_IsHDR;
                if (b.flags_override >= 0) flags_active = b.flags_override;

                if (transport_only)
                {
                    rf = static_cast<NVSDK_NGX_Result>(1);   // no NGX in the loop at all
                    Log("[host] transport-only mode: Color will be copied to Output, no evaluate");
                }
                else
                {
                    const UINT64 now = GetTickCount64();
                    if (now < hold_until) Sleep(static_cast<DWORD>(hold_until - now));  // hook-arming grace
                    ok = CreateFeature(b.width, b.height, flags_active, &rf);
                    hold_until = GetTickCount64() + 1000;   // next create not before +1 s

                    if (ok) build_fails = 0;
                    else if (++build_fails >= 2 && ReinitNgx())
                    {
                        Log("[host] retrying the create after an NGX reinit");
                        ok = CreateFeature(b.width, b.height, flags_active, &rf);
                        if (ok) build_fails = 0;
                    }
                }
            }

            evaluated = 0;
            warm_done = transport_only || g_renodx_lazy;   // no warm-up without NGX / with v45+

            FeedBuildAck back = {};
            back.ok         = ok ? 1 : 0;
            back.ngx_result = static_cast<uint32_t>(rf);
            back.fence_in   = reinterpret_cast<uint64_t>(game_in);
            back.fence_out  = reinterpret_cast<uint64_t>(game_out);
            WriteFile(pipe, &back, sizeof(back), &put, nullptr);
        }
        else if (tag == 'F')
        {
            FeedFrameMsg fm = {};
            if (!ReadFull(pipe, &fm, sizeof(fm))) break;
            if (h.feature == nullptr && !transport_only) { h.fence_out->Signal(fm.n); continue; }

            if (!WaitFenceValue(h.fence_in, fm.n, 2000))
            { Log("[host] frame %llu: in-fence never arrived", (unsigned long long)fm.n); h.fence_out->Signal(fm.n); continue; }
            h.queue->Wait(h.fence_in, fm.n);   // belt and braces on the GPU timeline

            bool done = false;
            if (transport_only)
            {
                if (BeginCommands())
                {
                    // Deliberately copy only the LEFT half: a split screen in the game is
                    // unambiguous visual proof that the host's output reaches the screen.
                    D3D12_TEXTURE_COPY_LOCATION src = {}, dst = {};
                    src.pResource = h.tex[FEED_COLOR];
                    src.Type      = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
                    dst.pResource = h.tex[FEED_OUTPUT];
                    dst.Type      = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
                    D3D12_BOX box = { 0, 0, 0, h.width / 2, h.height, 1 };
                    h.list->CopyTextureRegion(&dst, 0, 0, 0, &src, &box);
                    EndCommands();
                    done = true;
                }
            }
            else
                done = Evaluate(h.tex[FEED_COLOR], h.tex[FEED_OUTPUT], h.tex[FEED_DEPTH], h.tex[FEED_MV],
                                h.width, h.height, fm.reset ? 1 : 0, mvsx, mvsy);

            if (done)
            {
                h.queue->Signal(h.fence_out, fm.n);
                // One warm-up re-create per build: the DLSS 5 add-on misses the very first
                // create (STANDBY latch) when its hooks armed a moment too late.
                if (!warm_done && ++evaluated >= 180)
                {
                    warm_done = true;
                    Log("[host] warm-up: re-creating the feature once");
                    WaitFenceValue(h.fence, h.fence_value, 2000);
                    NVSDK_NGX_Handle *old = h.feature;
                    h.feature = nullptr;
                    NVSDK_NGX_Result rr = NVSDK_NGX_Result_Fail;
                    if (CreateFeature(h.width, h.height, flags_active, &rr)) SafeReleaseFeature(old);
                    else { h.feature = old; Log("[host] keeping the previous feature"); }
                }
            }
            else
                h.fence_out->Signal(fm.n);     // CPU-signal so the game never hangs on us

            if (fm.n <= 3 || (fm.n % 1800) == 0)
                Log("[host] frame %llu evaluated", (unsigned long long)fm.n);
            PumpPresent();
        }
        else
        {
            Log("[host] unknown tag 0x%02X", tag);
            break;
        }
    }
    // Normal exit: release the NGX resources, otherwise a quick worker restart
    // conflicts with the leftovers (exit 127 / a hang on frame 0). The Spout2
    // sender goes the same way - a DX11 device and a 4K shared texture.
    CleanupVideoNgx();
    SpoutBridgeShutdown();
    return 0;
}

// ---------------------------------------------------------------------------

int main(int argc, char **argv)
{
    // Per-monitor DPI awareness BEFORE any window exists. Without it, on a
    // 125% desktop a window asked for as 3840x2160 is created 4800x2700
    // physical and the overlay is stretched (measured: GetWindowRect returned
    // 4800x2700 while the screen is 3840x2160).
    if (HMODULE u32 = GetModuleHandleW(L"user32.dll"))
    {
        typedef BOOL(WINAPI * PFN_SetDpiCtx)(HANDLE);
        auto set_ctx = reinterpret_cast<PFN_SetDpiCtx>(
            GetProcAddress(u32, "SetProcessDpiAwarenessContext"));
        if (set_ctx == nullptr || !set_ctx(reinterpret_cast<HANDLE>(-4)))  // PER_MONITOR_AWARE_V2
            SetProcessDPIAware();
    }

    for (int i = 1; i < argc; ++i)
        if (strcmp(argv[i], "--video") == 0 || strcmp(argv[i], "--live") == 0) g_video_mode = true;

    GetModuleFileNameA(nullptr, g_log_path, MAX_PATH);
    if (char *s = strrchr(g_log_path, '\\'))
        strcpy_s(s + 1, MAX_PATH - (s + 1 - g_log_path), "dlss5-feed-host.log");
    if (!g_video_mode) { FILE *f = nullptr; if (fopen_s(&f, g_log_path, "w") == 0 && f) fclose(f); }

    Log("dlss5-feed-host64 (built %s %s)", __DATE__, __TIME__);

    bool  test = false, hide = false, video = false;
    DWORD pid = 0;
    for (int i = 1; i < argc; ++i)
    {
        if      (strcmp(argv[i], "--test") == 0) test = true;
        else if (strcmp(argv[i], "--video") == 0) video = true;
        else if (strcmp(argv[i], "--live") == 0) { video = true; g_live_force = true; }
        else if (strcmp(argv[i], "--hide") == 0) hide = true;
        else pid = static_cast<DWORD>(strtoul(argv[i], nullptr, 10));
    }
    if (!test && !video && pid == 0)
    {
        Log("usage: dlss5-worker --test | --video | --live | <game pid> [--hide]");
        return 1;
    }
    g_show_window = !test && !video && !hide; // video/probe hosts remain hidden

    if (!InitDisguise()) return 1;
    if (!InitNgx()) { Log("[host] NGX unavailable"); return 1; }
    SpoutBridgeInit(h.dev);

    if (test) return RunTest();
    if (video) return RunVideo();
    return Serve(pid);
}
