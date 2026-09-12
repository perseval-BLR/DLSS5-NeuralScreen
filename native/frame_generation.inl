// Experimental desktop DLSS-G. Capture has no engine depth: use a flat plane
// and the estimated motion field. Keep this opt-in; UI/occlusions can distort.
static PFN_NR_Evaluate g_fg_evaluate = nullptr;
static NVSDK_NGX_Result FgEvaluateBridge(ID3D12GraphicsCommandList *list,
    NVSDK_NGX_Handle *handle, NVSDK_NGX_Parameter *params, PFN_NVSDK_NGX_ProgressCallback cb)
{ return g_fg_evaluate(list, handle, params, cb); }
#define NVSDK_NGX_D3D12_EvaluateFeature_C FgEvaluateBridge
#include "include/nvsdk_ngx_helpers_dlssg.h"
#undef NVSDK_NGX_D3D12_EvaluateFeature_C

struct FgSlot {
    winrt::com_ptr<ID3D12Resource> real, interpolated[3];
    unsigned count = 1;
    int state = 0; // free, writing, ready, presenting; protected by mutex
    UINT64 sequence = 0;
    bool interpolate = false;
    double interval = 0.016;
};
static struct FgState {
    HMODULE module = nullptr;
    PFN_NR_Create create = nullptr;
    PFN_NR_Release release = nullptr;
    NVSDK_NGX_Parameter *params = nullptr;
    NVSDK_NGX_Handle *feature = nullptr;
    VideoTex depth;
    winrt::com_ptr<ID3D12Resource> output[3];
    winrt::com_ptr<ID3D12Resource> disable, disable_readback;
    UINT w = 0, height = 0, mw = 0, mh = 0;
    DXGI_FORMAT format = DXGI_FORMAT_UNKNOWN;
    FgSlot slots[3];
    std::mutex mutex;
    std::condition_variable wake;
    std::thread thread;
    std::atomic<bool> stop{false}, failed{false};
    UINT64 sequence = 0;
    std::chrono::steady_clock::time_point last;
    bool history = false;
    ~FgState() { stop = true; wake.notify_all(); if (thread.joinable()) thread.join(); }
} g_fg;

static int g_fg_ui_enabled = -1;
static unsigned g_fg_count = 1;

static bool FgRequested()
{
    static const bool requested = [] {
        char value[8] = {};
        return GetEnvironmentVariableA("NS_FRAMEGEN", value, sizeof(value)) == 1 && value[0] == '1';
    }();
    return (g_fg_ui_enabled < 0 ? requested : g_fg_ui_enabled != 0) && !g_fg.failed;
}

static void StopFgPresentation()
{
    g_fg.stop = true;
    g_fg.wake.notify_all();
    if (g_fg.thread.joinable()) g_fg.thread.join();
    for (auto &slot : g_fg.slots) { slot.real = nullptr; for (auto &image : slot.interpolated) image = nullptr; slot.state = 0; }
    g_fg.history = false;
}

static void CloseFgResources()
{
    StopFgPresentation();
    if (g_fg.feature) { g_fg.release(g_fg.feature); g_fg.feature = nullptr; }
    if (g_fg.depth.tex) { g_fg.depth.tex->Release(); g_fg.depth.tex = nullptr; }
    if (g_fg.depth.upload) { g_fg.depth.upload->Release(); g_fg.depth.upload = nullptr; }
    for (auto &image : g_fg.output) image = nullptr;
    g_fg.disable = nullptr; g_fg.disable_readback = nullptr;
    g_fg.w = g_fg.height = 0;
}

// The presenter has its own command list/allocator/fence. The producer never
// waits for frame pacing. Slots being displayed cannot be recycled by it.
static void FgPresenter()
{
    winrt::com_ptr<ID3D12CommandAllocator> alloc;
    winrt::com_ptr<ID3D12GraphicsCommandList> list;
    winrt::com_ptr<ID3D12Fence> fence;
    if (FAILED(h.dev->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, IID_PPV_ARGS(alloc.put()))) ||
        FAILED(h.dev->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, alloc.get(), nullptr, IID_PPV_ARGS(list.put()))) ||
        FAILED(h.dev->CreateFence(0, D3D12_FENCE_FLAG_NONE, IID_PPV_ARGS(fence.put()))))
    { g_fg.failed = true; return; }
    list->Close();
    HANDLE event = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    if (!event) { g_fg.failed = true; return; }
    UINT64 value = 0, previous = 0, shown = 0;
    auto report = std::chrono::steady_clock::now();
    auto present = [&](ID3D12Resource *source) {
        winrt::com_ptr<ID3D12Resource> bb;
        if (FAILED(g_present_swap->GetBuffer(g_present_swap->GetCurrentBackBufferIndex(), IID_PPV_ARGS(bb.put()))) ||
            FAILED(alloc->Reset()) || FAILED(list->Reset(alloc.get(), nullptr))) return false;
        auto pre = Transition(bb.get(), D3D12_RESOURCE_STATE_PRESENT, D3D12_RESOURCE_STATE_COPY_DEST);
        list->ResourceBarrier(1, &pre);
        list->CopyResource(bb.get(), source);
        auto post = Transition(bb.get(), D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_PRESENT);
        list->ResourceBarrier(1, &post);
        if (FAILED(list->Close())) return false;
        ID3D12CommandList *commands[] = {list.get()};
        h.queue->ExecuteCommandLists(1, commands);
        if (FAILED(h.queue->Signal(fence.get(), ++value)) ||
            FAILED(fence->SetEventOnCompletion(value, event)) ||
            WaitForSingleObject(event, 2000) != WAIT_OBJECT_0 ||
            fence->GetCompletedValue() < value) return false;
        if (FAILED(g_present_swap->Present(0, 0))) return false;
        RevealOnFirstPresent();
        ++shown;
        return true;
    };
    while (!g_fg.stop && !g_fg.failed)
    {
        FgSlot *chosen = nullptr;
        {
            std::unique_lock<std::mutex> lock(g_fg.mutex);
            g_fg.wake.wait(lock, [] {
                if (g_fg.stop) return true;
                for (auto &s : g_fg.slots) if (s.state == 2) return true;
                return false;
            });
            if (g_fg.stop) break;
            for (auto &s : g_fg.slots)
                if (s.state == 2 && (!chosen || s.sequence > chosen->sequence)) chosen = &s;
            for (auto &s : g_fg.slots) if (s.state == 2 && &s != chosen) s.state = 0;
            chosen->state = 3;
        }
        const auto start = std::chrono::steady_clock::now();
        // If a frame was dropped, do not interpolate against an unseen frame.
        if (chosen->interpolate && chosen->sequence == previous + 1)
        {
            for (unsigned index = 0; index < chosen->count && !g_fg.stop && !g_fg.failed; ++index)
            {
                const auto deadline = start + std::chrono::duration<double>(
                    chosen->interval * (index + 1) / (chosen->count + 1));
                // A delayed GPU copy must not cause a burst of obsolete generated frames.
                if (std::chrono::steady_clock::now() >= deadline) continue;
                if (!present(chosen->interpolated[index].get())) g_fg.failed = true;
                std::unique_lock<std::mutex> lock(g_fg.mutex);
                if (g_fg.wake.wait_until(lock, deadline, [&] {
                    if (g_fg.stop) return true;
                    for (auto &slot : g_fg.slots)
                        if (slot.state == 2 && slot.sequence > chosen->sequence) return true;
                    return false;
                })) break;
            }
        }
        if (!g_fg.stop && !g_fg.failed && !present(chosen->real.get())) g_fg.failed = true;
        previous = chosen->sequence;
        {
            std::lock_guard<std::mutex> lock(g_fg.mutex);
            chosen->state = 0;
        }
        const double elapsed = std::chrono::duration<double>(start - report).count();
        if (elapsed >= 2.0)
        {
            Log("[fg] displayed %.1f FPS (real + generated); experimental flat-depth guides", shown / elapsed);
            shown = 0; report = start;
        }
    }
    CloseHandle(event);
    if (g_fg.failed) Log("[fg] presenter failed; returning to ordinary output");
}

static bool EnsureFg(VideoState &v, DXGI_FORMAT format)
{
    const UINT w = v.upscale ? v.full_w : v.w, height = v.upscale ? v.full_h : v.hgt;
    if (g_fg.feature && g_fg.w == w && g_fg.height == height && g_fg.mw == v.w &&
        g_fg.mh == v.hgt && g_fg.format == format && g_fg.thread.joinable()) return true;
    CloseFgResources();
    if (!g_fg.module)
    {
        wchar_t path[MAX_PATH] = {};
        GetModuleFileNameW(nullptr, path, MAX_PATH);
        if (auto slash = wcsrchr(path, L'\\')) *(slash + 1) = 0;
        wchar_t directory[MAX_PATH]; wcscpy_s(directory, path);
        wcscat_s(path, L"nvngx_dlssg.dll");
        g_fg.module = LoadLibraryW(path);
        if (!g_fg.module) { Log("[fg] nvngx_dlssg.dll load failed: %lu", GetLastError()); return false; }
        auto init = reinterpret_cast<PFN_NR_InitExt>(GetProcAddress(g_fg.module, "NVSDK_NGX_D3D12_Init_Ext"));
        g_fg.create = reinterpret_cast<PFN_NR_Create>(GetProcAddress(g_fg.module, "NVSDK_NGX_D3D12_CreateFeature"));
        g_fg.release = reinterpret_cast<PFN_NR_Release>(GetProcAddress(g_fg.module, "NVSDK_NGX_D3D12_ReleaseFeature"));
        g_fg_evaluate = reinterpret_cast<PFN_NR_Evaluate>(GetProcAddress(g_fg.module, "NVSDK_NGX_D3D12_EvaluateFeature"));
        if (!init || !g_fg.create || !g_fg.release || !g_fg_evaluate ||
            NVSDK_NGX_FAILED(NVSDK_NGX_D3D12_AllocateParameters(&g_fg.params))) return false;
        const auto result = init(0x1000000ULL, directory, h.dev, NVSDK_NGX_Version_API, g_fg.params);
        Log("[fg] Init_Ext -> 0x%08X", result);
        if (NVSDK_NGX_FAILED(result)) return false;
    }
    auto p = g_fg.params;
    p->Reset();
    p->Set("CreationNodeMask", 1u); p->Set("VisibilityNodeMask", 1u);
    p->Set("Width", w); p->Set("Height", height);
    p->Set(NVSDK_NGX_DLSSG_Parameter_BackbufferFormat, (unsigned)format);
    p->Set(NVSDK_NGX_DLSSG_Parameter_InternalWidth, v.w);
    p->Set(NVSDK_NGX_DLSSG_Parameter_InternalHeight, v.hgt);
    p->Set(NVSDK_NGX_DLSSG_Parameter_DynamicResolution, 0u);
    if (!BeginCommands()) return false;
    const auto result = g_fg.create(h.list, NVSDK_NGX_Feature_FrameGeneration, p, &g_fg.feature);
    if (!WaitFenceValue(h.fence, EndCommands(), 30000) || NVSDK_NGX_FAILED(result) || !g_fg.feature)
    { Log("[fg] CreateFeature failed 0x%08X", result); return false; }
    if (!CreateVideoTex(g_fg.depth, v.w, v.hgt, DXGI_FORMAT_R32_FLOAT, v.w * 4)) return false;
    std::vector<float> depth(static_cast<size_t>(v.w) * v.hgt, 0.5f);
    if (!FillUpload(g_fg.depth, reinterpret_cast<BYTE *>(depth.data()), v.w * 4, v.hgt) || !BeginCommands()) return false;
    CopyUpload(h.list, g_fg.depth);
    auto b = Transition(g_fg.depth.tex, D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
    h.list->ResourceBarrier(1, &b);
    if (!WaitFenceValue(h.fence, EndCommands(), 30000)) return false;
    for (unsigned i = 0; i < g_fg_count; ++i)
    {
        g_fg.output[i].attach(MakeTex(w, height, format, true));
        if (!g_fg.output[i]) return false;
    }
    for (auto &slot : g_fg.slots)
    {
        slot.real.attach(MakeTex(w, height, format, false));
        if (!slot.real) return false;
        for (unsigned i = 0; i < g_fg_count; ++i)
        {
            slot.interpolated[i].attach(MakeTex(w, height, format, false));
            if (!slot.interpolated[i]) return false;
        }
    }
    if (!BeginCommands()) return false;
    for (auto &slot : g_fg.slots) for (unsigned i = 0; i <= g_fg_count; ++i)
    {
        auto texture = i ? slot.interpolated[i - 1].get() : slot.real.get();
        auto to_copy = Transition(texture, D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_COPY_SOURCE);
        h.list->ResourceBarrier(1, &to_copy);
    }
    if (!WaitFenceValue(h.fence, EndCommands(), 30000)) return false;
    D3D12_RESOURCE_DESC buffer = {};
    buffer.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
    buffer.Width = 12; buffer.Height = 1; buffer.DepthOrArraySize = 1;
    buffer.MipLevels = 1; buffer.SampleDesc.Count = 1;
    buffer.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
    buffer.Flags = D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS;
    D3D12_HEAP_PROPERTIES heap = {}; heap.Type = D3D12_HEAP_TYPE_DEFAULT;
    if (FAILED(h.dev->CreateCommittedResource(&heap, D3D12_HEAP_FLAG_NONE, &buffer,
        D3D12_RESOURCE_STATE_UNORDERED_ACCESS, nullptr, IID_PPV_ARGS(g_fg.disable.put())))) return false;
    heap.Type = D3D12_HEAP_TYPE_READBACK; buffer.Flags = D3D12_RESOURCE_FLAG_NONE;
    if (FAILED(h.dev->CreateCommittedResource(&heap, D3D12_HEAP_FLAG_NONE, &buffer,
        D3D12_RESOURCE_STATE_COPY_DEST, nullptr, IID_PPV_ARGS(g_fg.disable_readback.put())))) return false;
    g_fg.w = w; g_fg.height = height; g_fg.mw = v.w; g_fg.mh = v.hgt; g_fg.format = format;
    g_fg.stop = false;
    g_fg.thread = std::thread(FgPresenter);
    Log("[fg] %ux enabled at %ux%u, format=%u; flat depth and estimated motion (experimental)", g_fg_count + 1, w, height, format);
    return true;
}

// Opt-in pixel regression capture, never enabled in ordinary runs.
static void FgDump(ID3D12Resource *source, D3D12_RESOURCE_STATES state, unsigned index)
{
    char dir[MAX_PATH] = {};
    if (!GetEnvironmentVariableA("NS_FG_DUMP", dir, sizeof(dir)) ||
        (g_fg.sequence != 30 && g_fg.sequence != 150 && g_fg.sequence != 300)) return;
    auto desc = source->GetDesc();
    D3D12_PLACED_SUBRESOURCE_FOOTPRINT fp = {};
    UINT rows; UINT64 rowbytes, bytes;
    h.dev->GetCopyableFootprints(&desc, 0, 1, 0, &fp, &rows, &rowbytes, &bytes);
    D3D12_HEAP_PROPERTIES heap = {}; heap.Type = D3D12_HEAP_TYPE_READBACK;
    D3D12_RESOURCE_DESC bd = {}; bd.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
    bd.Width = bytes; bd.Height = 1; bd.DepthOrArraySize = bd.MipLevels = 1;
    bd.SampleDesc.Count = 1; bd.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
    winrt::com_ptr<ID3D12Resource> rb;
    if (FAILED(h.dev->CreateCommittedResource(&heap, D3D12_HEAP_FLAG_NONE, &bd,
        D3D12_RESOURCE_STATE_COPY_DEST, nullptr, IID_PPV_ARGS(rb.put()))) || !BeginCommands()) return;
    auto pre = Transition(source, state, D3D12_RESOURCE_STATE_COPY_SOURCE);
    h.list->ResourceBarrier(1, &pre);
    D3D12_TEXTURE_COPY_LOCATION from = {}, to = {};
    from.pResource = source; from.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
    to.pResource = rb.get(); to.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT; to.PlacedFootprint = fp;
    h.list->CopyTextureRegion(&to, 0, 0, 0, &from, nullptr);
    auto post = Transition(source, D3D12_RESOURCE_STATE_COPY_SOURCE, state);
    h.list->ResourceBarrier(1, &post);
    if (!WaitFenceValue(h.fence, EndCommands(), 30000)) return;
    BYTE *data = nullptr; D3D12_RANGE read = {0, static_cast<SIZE_T>(bytes)}, written = {0, 0};
    if (FAILED(rb->Map(0, &read, reinterpret_cast<void **>(&data)))) return;
    char path[MAX_PATH]; sprintf_s(path, "%s/fg-%llu-%u.raw", dir, g_fg.sequence, index);
    FILE *file = nullptr;
    if (!fopen_s(&file, path, "wb") && file) {
        for (UINT y = 0; y < rows; ++y) fwrite(data + size_t(y) * fp.Footprint.RowPitch, 1, size_t(rowbytes), file);
        fclose(file);
    }
    rb->Unmap(0, &written);
}

static bool FgPresent(VideoState &v, ID3D12Resource *color, D3D12_RESOURCE_STATES state)
{
    if (!FgRequested()) { StopFgPresentation(); return false; }
    if (!EnsureFg(v, color->GetDesc().Format))
    { g_fg.failed = true; CloseFgResources(); return false; }
    const auto now = std::chrono::steady_clock::now();
    const double interval = g_fg.history ? std::chrono::duration<double>(now - g_fg.last).count() : 0.016;
    const bool interpolate = g_fg.history && !g_fg_reset && interval < 0.1;
    g_fg.last = now;
    auto p = g_fg.params;
    p->Reset();
    NVSDK_NGX_DLSSG_Opt_Eval_Params opt = {};
    for (int i = 0; i < 4; ++i)
        opt.cameraViewToClip[i][i] = opt.clipToCameraView[i][i] = opt.clipToLensClip[i][i] =
        opt.clipToPrevClip[i][i] = opt.prevClipToClip[i][i] = 1.0f;
    opt.mvecScale[0] = 1.f / v.w; opt.mvecScale[1] = 1.f / v.hgt;
    opt.cameraUp[1] = opt.cameraRight[0] = opt.cameraFwd[2] = 1;
    opt.cameraNear = 0.1f; opt.cameraFar = 1000; opt.cameraFOV = 1;
    opt.cameraAspectRatio = float(g_fg.w) / g_fg.height;
    opt.cameraMotionIncluded = opt.orthoProjection = opt.motionVectorsDilated = true;
    opt.colorBuffersHDR = g_fg.format == DXGI_FORMAT_R10G10B10A2_UNORM;
    opt.reset = !interpolate;
    opt.mvecsSubrectSize = opt.depthSubrectSize = {v.w, v.hgt};
    opt.backbufferSubrectSize = opt.outputInterpSubrectSize = {g_fg.w, g_fg.height};
    NVSDK_NGX_D3D12_DLSSG_Eval_Params ep = {};
    ep.pBackbuffer = color; ep.pMVecs = v.mv.tex; ep.pDepth = g_fg.depth.tex;
    if (!BeginCommands()) { g_fg.failed = true; CloseFgResources(); return false; }
    bool allow_interpolation = true;
    p->Set(NVSDK_NGX_DLSSG_Parameter_BackbufferFrameID, static_cast<unsigned long long>(g_fg.sequence + 1));
    opt.multiFrameCount = g_fg_count;
    for (unsigned index = 0; index < g_fg_count; ++index)
    {
        opt.multiFrameIndex = index + 1;
        ep.pOutputInterpFrame = g_fg.output[index].get();
        ep.pOutputDisableInterpolation = g_fg.disable.get();
        auto pre = Transition(color, state, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
        if (state != D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE) h.list->ResourceBarrier(1, &pre);
        auto out_pre = Transition(g_fg.output[index].get(), D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
        h.list->ResourceBarrier(1, &out_pre);
        const auto result = NGX_D3D12_EVALUATE_DLSSG(h.list, g_fg.feature, p, &ep, &opt);
        auto disable_pre = Transition(g_fg.disable.get(), D3D12_RESOURCE_STATE_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_COPY_SOURCE);
        h.list->ResourceBarrier(1, &disable_pre);
        h.list->CopyBufferRegion(g_fg.disable_readback.get(), index * 4, g_fg.disable.get(), 0, 4);
        auto disable_post = Transition(g_fg.disable.get(), D3D12_RESOURCE_STATE_COPY_SOURCE, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
        h.list->ResourceBarrier(1, &disable_post);
        auto out_post = Transition(g_fg.output[index].get(), D3D12_RESOURCE_STATE_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_COMMON);
        h.list->ResourceBarrier(1, &out_post);
        auto post = Transition(color, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE, state);
        if (state != D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE) h.list->ResourceBarrier(1, &post);
        if (NVSDK_NGX_FAILED(result))
        {
            WaitFenceValue(h.fence, EndCommands(), 30000);
            Log("[fg] Evaluate failed 0x%08X; falling back", result);
            g_fg.failed = true; CloseFgResources(); return false;
        }
    }
    FgSlot *slot = nullptr;
    {
        std::lock_guard<std::mutex> lock(g_fg.mutex);
        for (auto &s : g_fg.slots) if (s.state == 0) { slot = &s; break; }
        if (!slot) for (auto &s : g_fg.slots) if (s.state == 2) { slot = &s; break; }
        if (!slot) { WaitFenceValue(h.fence, EndCommands(), 30000); return true; }
        slot->state = 1;
    }
    for (unsigned i = 0; i <= g_fg_count; ++i)
    {
        auto source = i ? g_fg.output[i - 1].get() : color;
        auto destination = i ? slot->interpolated[i - 1].get() : slot->real.get();
        const auto rest = i ? D3D12_RESOURCE_STATE_COMMON : state;
        D3D12_RESOURCE_BARRIER before[] = {
            Transition(source, rest, D3D12_RESOURCE_STATE_COPY_SOURCE),
            Transition(destination, D3D12_RESOURCE_STATE_COPY_SOURCE, D3D12_RESOURCE_STATE_COPY_DEST)};
        h.list->ResourceBarrier(2, before);
        h.list->CopyResource(destination, source);
        D3D12_RESOURCE_BARRIER after[] = {
            Transition(source, D3D12_RESOURCE_STATE_COPY_SOURCE, rest),
            Transition(destination, D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_COPY_SOURCE)};
        h.list->ResourceBarrier(2, after);
    }
    // Export remains the real SDR neural frame, at the processing rate.
    auto spout_pre = Transition(v.output, D3D12_RESOURCE_STATE_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_COPY_SOURCE);
    h.list->ResourceBarrier(1, &spout_pre);
    SpoutBridgeCopy(h.list, v.output, g_fg.w, g_fg.height);
    auto spout_post = Transition(v.output, D3D12_RESOURCE_STATE_COPY_SOURCE, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
    h.list->ResourceBarrier(1, &spout_post);
    if (!WaitFenceValue(h.fence, EndCommands(), 30000))
    { g_fg.failed = true; CloseFgResources(); return false; }
    BYTE *disabled = nullptr;
    D3D12_RANGE read = {0, g_fg_count * 4}, written = {0, 0};
    if (SUCCEEDED(g_fg.disable_readback->Map(0, &read, reinterpret_cast<void **>(&disabled))))
    {
        for (unsigned i = 0; i < g_fg_count; ++i)
            allow_interpolation = allow_interpolation && disabled[i * 4] == 0;
        g_fg.disable_readback->Unmap(0, &written);
    }
    else allow_interpolation = false;
    FgDump(color, state, 0);
    for (unsigned i = 0; i < g_fg_count; ++i) FgDump(g_fg.output[i].get(), D3D12_RESOURCE_STATE_COMMON, i + 1);
    SpoutBridgeSend();
    {
        std::lock_guard<std::mutex> lock(g_fg.mutex);
        slot->sequence = ++g_fg.sequence;
        slot->count = g_fg_count;
        slot->interpolate = interpolate && allow_interpolation;
        slot->interval = std::clamp(interval, 0.004, 0.05);
        slot->state = 2;
    }
    g_fg.history = true;
    g_fg.wake.notify_one();
    return true;
}

// Explicit UI settings ride in each frame header, so toggling does not restart NR.
static void ConfigureFgFrame(uint32_t flags)
{
    if (!(flags & 0x800)) return; // older clients may still use NS_FRAMEGEN
    const int enabled = (flags & 0x100) != 0;
    const unsigned count = std::min(3u, ((flags >> 9) & 3u) + 1u);
    if (g_fg_ui_enabled == enabled && g_fg_count == count) return;
    CloseFgResources();
    g_fg_ui_enabled = enabled;
    g_fg_count = count;
    g_fg.failed = false;
    g_force_next_frame = true;
    Log("[fg] UI: %s, %ux", enabled ? "on" : "off", count + 1);
}
