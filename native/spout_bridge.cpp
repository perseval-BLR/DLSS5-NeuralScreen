// Spout2 bridge implementation - see spout_bridge.h.
#include "spout_bridge.h"
#include "spout/SpoutDX.h"

#include <dxgi1_2.h>

#include <cstdio>
#include <cstdlib>

static ID3D11Device *g_d11 = nullptr;
static ID3D11DeviceContext *g_ctx = nullptr;
static ID3D11Texture2D *g_shared = nullptr;
static HANDLE g_nt = nullptr;
static ID3D12Resource *g_d12 = nullptr;
static ID3D12Device *g_dev12 = nullptr;
static spoutDX *g_spout = nullptr;
static bool g_enabled = false;
static UINT g_w = 0, g_h = 0;

bool SpoutBridgeInit(ID3D12Device *dev)
{
    if (g_enabled) return true;
    char v[8] = {};
    const DWORD got = GetEnvironmentVariableA("NS_SPOUT", v, sizeof(v));
    if (got == 0 || got >= sizeof(v) || v[0] != '1')
        return false;   // disabled - the bridge costs nothing

    g_dev12 = dev;
    // The D3D11 device has to be on the SAME adapter as the worker. It was
    // created on the default one, which is adapter 0: with NS_GPU pointing
    // at another card, every present then copied the frame across the bus
    // into a texture living on a different GPU. It works - the resource is
    // shared with an NT handle - and it costs milliseconds a frame for
    // nothing, silently, on exactly the machines that had to choose a card
    // in the first place (audit).
    const LUID want = dev->GetAdapterLuid();
    IDXGIFactory1 *factory = nullptr;
    IDXGIAdapter1 *adapter = nullptr;
    if (SUCCEEDED(CreateDXGIFactory1(__uuidof(IDXGIFactory1),
                                     reinterpret_cast<void **>(&factory))))
    {
        IDXGIAdapter1 *candidate = nullptr;
        for (UINT i = 0; factory->EnumAdapters1(i, &candidate) != DXGI_ERROR_NOT_FOUND; ++i)
        {
            DXGI_ADAPTER_DESC1 ad = {};
            if (candidate == nullptr) continue;
            if (SUCCEEDED(candidate->GetDesc1(&ad)) &&
                ad.AdapterLuid.LowPart == want.LowPart &&
                ad.AdapterLuid.HighPart == want.HighPart)
            { adapter = candidate; break; }
            candidate->Release();
            candidate = nullptr;
        }
    }
    if (adapter == nullptr)
        fprintf(stderr, "[spout] the worker's adapter was not found by LUID - "
                        "falling back to the default one\n");
    D3D_FEATURE_LEVEL fl = D3D_FEATURE_LEVEL_11_0;
    // With an explicit adapter the driver type MUST be UNKNOWN - passing
    // both is an invalid-argument failure, not a preference.
    HRESULT hr = D3D11CreateDevice(adapter,
                                   adapter != nullptr ? D3D_DRIVER_TYPE_UNKNOWN
                                                      : D3D_DRIVER_TYPE_HARDWARE,
                                   nullptr, D3D11_CREATE_DEVICE_BGRA_SUPPORT, &fl, 1,
                                   D3D11_SDK_VERSION, &g_d11, nullptr, &g_ctx);
    if (adapter != nullptr) adapter->Release();
    if (factory != nullptr) factory->Release();
    if (FAILED(hr)) { fprintf(stderr, "[spout] D3D11 device failed 0x%08X\n", (unsigned)hr); return false; }
    {
        // Say which card it landed on: a mismatch here is invisible in the
        // picture and shows only as frames that cost more than they should.
        IDXGIDevice *dxgi_dev = nullptr;
        IDXGIAdapter *got = nullptr;
        DXGI_ADAPTER_DESC gd = {};
        if (SUCCEEDED(g_d11->QueryInterface(__uuidof(IDXGIDevice),
                                            reinterpret_cast<void **>(&dxgi_dev))) &&
            SUCCEEDED(dxgi_dev->GetAdapter(&got)) && SUCCEEDED(got->GetDesc(&gd)))
            fprintf(stderr, "[spout] D3D11 on %ls (luid %08lX:%08lX, the worker's: "
                            "%08lX:%08lX)\n", gd.Description,
                    (unsigned long)gd.AdapterLuid.HighPart, (unsigned long)gd.AdapterLuid.LowPart,
                    (unsigned long)want.HighPart, (unsigned long)want.LowPart);
        if (got != nullptr) got->Release();
        if (dxgi_dev != nullptr) dxgi_dev->Release();
    }

    g_spout = new spoutDX();
    if (!g_spout->OpenDirectX11(g_d11))
    { fprintf(stderr, "[spout] OpenDirectX11 failed\n"); SpoutBridgeShutdown(); return false; }
    g_spout->SetSenderName("NeuralScreen");
    g_spout->SetSenderFormat(DXGI_FORMAT_R8G8B8A8_UNORM);

    g_enabled = true;
    fprintf(stderr, "[spout] bridge enabled (NS_SPOUT=1)\n");
    return true;
}

void SpoutBridgeCopy(ID3D12GraphicsCommandList *list, ID3D12Resource *src,
                     UINT w, UINT h)
{
    if (!g_enabled || src == nullptr) return;
    if (g_shared == nullptr || w != g_w || h != g_h)
    {
        // (Re)create the shared texture at the new size.
        if (g_shared) { g_shared->Release(); g_shared = nullptr; }
        if (g_d12) { g_d12->Release(); g_d12 = nullptr; }
        if (g_nt) { CloseHandle(g_nt); g_nt = nullptr; }

        D3D11_TEXTURE2D_DESC sd = {};
        sd.Width = w; sd.Height = h; sd.MipLevels = 1; sd.ArraySize = 1;
        sd.Format = DXGI_FORMAT_R8G8B8A8_UNORM; sd.SampleDesc.Count = 1;
        sd.Usage = D3D11_USAGE_DEFAULT;
        sd.BindFlags = D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_RENDER_TARGET;
        sd.MiscFlags = D3D11_RESOURCE_MISC_SHARED | D3D11_RESOURCE_MISC_SHARED_NTHANDLE;
        if (FAILED(g_d11->CreateTexture2D(&sd, nullptr, &g_shared)))
        { fprintf(stderr, "[spout] shared texture failed\n"); return; }
        IDXGIResource1 *r1 = nullptr;
        if (FAILED(g_shared->QueryInterface(__uuidof(IDXGIResource1), (void **)&r1)) ||
            FAILED(r1->CreateSharedHandle(nullptr,
                                          DXGI_SHARED_RESOURCE_READ | DXGI_SHARED_RESOURCE_WRITE,
                                          nullptr, &g_nt)))
        { fprintf(stderr, "[spout] NT handle failed\n"); if (r1) r1->Release(); return; }
        r1->Release();
        // Open the same texture in D3D12 so the worker can copy into it.
        if (FAILED(g_dev12->OpenSharedHandle(g_nt, __uuidof(ID3D12Resource),
                                             (void **)&g_d12)))
        { fprintf(stderr, "[spout] OpenSharedHandle failed\n"); return; }
        g_w = w; g_h = h;
        fprintf(stderr, "[spout] shared texture %ux%u created\n", w, h);
    }
    if (g_d12 == nullptr) { fprintf(stderr, "[spout] g_d12 null - skipping copy\n"); return; }
    // Copy src -> g_d12 inside the caller's command list.
    D3D12_RESOURCE_BARRIER pre = {};
    pre.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
    pre.Transition.pResource = g_d12;
    pre.Transition.StateBefore = D3D12_RESOURCE_STATE_COMMON;
    pre.Transition.StateAfter = D3D12_RESOURCE_STATE_COPY_DEST;
    list->ResourceBarrier(1, &pre);
    list->CopyResource(g_d12, src);
    D3D12_RESOURCE_BARRIER post = {};
    post.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
    post.Transition.pResource = g_d12;
    post.Transition.StateBefore = D3D12_RESOURCE_STATE_COPY_DEST;
    post.Transition.StateAfter = D3D12_RESOURCE_STATE_COMMON;
    list->ResourceBarrier(1, &post);
}

void SpoutBridgeSend()
{
    if (!g_enabled || g_shared == nullptr) return;
    if (!g_spout->SendTexture(g_shared))
        fprintf(stderr, "[spout] SendTexture failed\n");
}

void SpoutBridgeShutdown()
{
    if (g_spout) { g_spout->ReleaseSender(); delete g_spout; g_spout = nullptr; }
    if (g_shared) { g_shared->Release(); g_shared = nullptr; }
    if (g_d12) { g_d12->Release(); g_d12 = nullptr; }
    if (g_nt) { CloseHandle(g_nt); g_nt = nullptr; }
    if (g_ctx) { g_ctx->Release(); g_ctx = nullptr; }
    if (g_d11) { g_d11->Release(); g_d11 = nullptr; }
    g_enabled = false;
}
