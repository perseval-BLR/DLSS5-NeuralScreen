// nvngx.dll_ns-forwarder.dll - the module the NGX calls leave from.
//
// nvngx_dlssnr.dll identifies its caller by the return address and refuses the
// call with FAIL_PlatformError (0xBAD00002) unless the path of the CALLING
// module contains the substring "nvngx.dll". It does not look at the process
// name at all. Measured here on a 5070 Ti, four runs, each a separate process
// named ns-gate-probe.exe:
//
//   Init_Ext from the exe itself                  -> 0xBAD00002
//   Init_Ext from a DLL with the substring        -> Success, and
//                                                    CreateFeature(18) too
//   the same DLL renamed without the substring    -> 0xBAD00002
//   the substring in a DIRECTORY name             -> Success
//
// So the worker no longer has to be an executable named nvngx.dll - it only
// has to make its NGX calls from here. The file name is the entire reason this
// module exists; renaming it breaks Neural Rendering and nothing else.
//
// Nothing in this file comes from anyone else's source. The five wrappers are
// ours; what was taken from the research is a fact about how the NVIDIA
// library behaves.
#include <windows.h>
#include <d3d12.h>

#include "nvsdk_ngx.h"

using PFN_NR_InitExt = NVSDK_NGX_Result(NVSDK_CONV *)(unsigned long long, const wchar_t *,
                                                      ID3D12Device *, NVSDK_NGX_Version,
                                                      const NVSDK_NGX_Parameter *);
using PFN_NR_Create = NVSDK_NGX_Result(NVSDK_CONV *)(ID3D12GraphicsCommandList *, NVSDK_NGX_Feature,
                                                     const NVSDK_NGX_Parameter *, NVSDK_NGX_Handle **);
using PFN_NR_Evaluate = NVSDK_NGX_Result(NVSDK_CONV *)(ID3D12GraphicsCommandList *, const NVSDK_NGX_Handle *,
                                                       const NVSDK_NGX_Parameter *, PFN_NVSDK_NGX_ProgressCallback);
using PFN_NR_Release = NVSDK_NGX_Result(NVSDK_CONV *)(NVSDK_NGX_Handle *);

static HMODULE g_nr;
static PFN_NR_InitExt g_init_ext;
static PFN_NR_Create g_create;
static PFN_NR_Evaluate g_evaluate;
static PFN_NR_Release g_release;

// The wrappers must not become tail calls. With /O2 a body of the shape
// `return fn(args);` compiles to `jmp rax`: no frame is pushed here, the
// return address on the stack still belongs to the caller, and the library
// would resolve the caller to the worker executable - failing for a reason
// that has nothing to do with the name. Touching a volatile after the call
// keeps a real frame in this module.
static volatile LONG g_last;
#define FORWARD(expr) do { const NVSDK_NGX_Result r_ = (expr); \
                           InterlockedExchange(&g_last, static_cast<LONG>(r_)); \
                           return r_; } while (0)

// Load the feature library and resolve its four entry points. Returns 0 on
// success, a Win32 error when the library will not load, or -1 when an export
// is missing. Called once, from the worker, before anything else here.
extern "C" __declspec(dllexport)
int NsFwdLoad(const wchar_t *dlssnr_path)
{
    if (g_nr != nullptr) return 0;
    g_nr = LoadLibraryW(dlssnr_path);
    if (g_nr == nullptr) { const DWORD e = GetLastError(); return e != 0 ? static_cast<int>(e) : -2; }
    g_init_ext = reinterpret_cast<PFN_NR_InitExt>(GetProcAddress(g_nr, "NVSDK_NGX_D3D12_Init_Ext"));
    g_create = reinterpret_cast<PFN_NR_Create>(GetProcAddress(g_nr, "NVSDK_NGX_D3D12_CreateFeature"));
    g_evaluate = reinterpret_cast<PFN_NR_Evaluate>(GetProcAddress(g_nr, "NVSDK_NGX_D3D12_EvaluateFeature"));
    g_release = reinterpret_cast<PFN_NR_Release>(GetProcAddress(g_nr, "NVSDK_NGX_D3D12_ReleaseFeature"));
    if (g_init_ext == nullptr || g_create == nullptr || g_evaluate == nullptr || g_release == nullptr)
        return -1;
    return 0;
}

// Where this module actually is. The worker logs it, so a broken install says
// which file was loaded instead of "Neural Rendering is off".
extern "C" __declspec(dllexport)
void NsFwdPath(wchar_t *out, unsigned int cch)
{
    HMODULE self = nullptr;
    GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS
                       | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                       reinterpret_cast<LPCWSTR>(&NsFwdPath), &self);
    if (out != nullptr && cch > 0) { out[0] = L'\0'; GetModuleFileNameW(self, out, cch); }
}

// The four wrappers carry the signatures of the NGX entry points themselves,
// so the worker keeps its own function-pointer types and every call site
// downstream is unchanged.
extern "C" __declspec(dllexport)
NVSDK_NGX_Result NVSDK_CONV NsFwdInitExt(unsigned long long app_id, const wchar_t *data_path,
                                         ID3D12Device *dev, NVSDK_NGX_Version version,
                                         const NVSDK_NGX_Parameter *params)
{
    if (g_init_ext == nullptr) return NVSDK_NGX_Result_FAIL_NotInitialized;
    FORWARD(g_init_ext(app_id, data_path, dev, version, params));
}

extern "C" __declspec(dllexport)
NVSDK_NGX_Result NVSDK_CONV NsFwdCreate(ID3D12GraphicsCommandList *list, NVSDK_NGX_Feature feature,
                                        const NVSDK_NGX_Parameter *params, NVSDK_NGX_Handle **out)
{
    if (g_create == nullptr) return NVSDK_NGX_Result_FAIL_NotInitialized;
    FORWARD(g_create(list, feature, params, out));
}

extern "C" __declspec(dllexport)
NVSDK_NGX_Result NVSDK_CONV NsFwdEvaluate(ID3D12GraphicsCommandList *list, const NVSDK_NGX_Handle *feature,
                                          const NVSDK_NGX_Parameter *params,
                                          PFN_NVSDK_NGX_ProgressCallback cb)
{
    if (g_evaluate == nullptr) return NVSDK_NGX_Result_FAIL_NotInitialized;
    FORWARD(g_evaluate(list, feature, params, cb));
}

extern "C" __declspec(dllexport)
NVSDK_NGX_Result NVSDK_CONV NsFwdRelease(NVSDK_NGX_Handle *feature)
{
    if (g_release == nullptr) return NVSDK_NGX_Result_FAIL_NotInitialized;
    FORWARD(g_release(feature));
}

BOOL APIENTRY DllMain(HMODULE, DWORD, LPVOID) { return TRUE; }
