// Included after the capture bridge. All resources here belong to the worker's
// D3D12 device and are used on its single, fence-serialized command queue.
static ID3D12RootSignature *g_hdr_rs = nullptr;
static ID3D12PipelineState *g_hdr_pso = nullptr;
static ID3D12DescriptorHeap *g_hdr_heap = nullptr;
static ID3D12Resource *g_hdr_output = nullptr;

static void CloseHdrResources()
{
    if (g_hdr_output) { g_hdr_output->Release(); g_hdr_output = nullptr; }
    if (g_hdr_heap) { g_hdr_heap->Release(); g_hdr_heap = nullptr; }
    if (g_hdr_pso) { g_hdr_pso->Release(); g_hdr_pso = nullptr; }
    if (g_hdr_rs) { g_hdr_rs->Release(); g_hdr_rs = nullptr; }
}

static bool EnsurePresentFormat(bool hdr, bool pq)
{
    DXGI_SWAP_CHAIN_DESC1 desc = {};
    if (FAILED(g_present_swap->GetDesc1(&desc))) return false;
    const auto format = pq ? DXGI_FORMAT_R10G10B10A2_UNORM : hdr ? DXGI_FORMAT_R16G16B16A16_FLOAT : DXGI_FORMAT_R8G8B8A8_UNORM;
    const auto space = pq ? DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020 : hdr ? DXGI_COLOR_SPACE_RGB_FULL_G10_NONE_P709
                           : DXGI_COLOR_SPACE_RGB_FULL_G22_NONE_P709;
    if (desc.Format != format)
    {
        CloseFgResources();
        // PresentFrame/PresentBypass release each back buffer and wait for GPU
        // work before returning; no old buffer reference survives here.
        // This failure is fatal on either path, and deliberately so: both
        // presents CopyResource into the back buffer, and a copy between
        // mismatched formats is not a wrong picture, it is a removed device.
        if (FAILED(g_present_swap->ResizeBuffers(0, 0, 0, format, desc.Flags)))
        { Log("[hdr] swap chain format change failed"); return false; }
        Log("[hdr] presentation=%s", pq ? "HDR10 PQ (DLSS-G)" : hdr ? "FP16 scRGB" : "8-bit SDR");
        g_present_space_set = false;
    }
    // Once per swap chain, not once per frame. The space only changes with
    // the format, and the call above is the only thing that changes it.
    if (g_present_space_set && g_present_space == space) return true;
    g_present_space_set = true;
    g_present_space = space;
    UINT support = 0;
    if (FAILED(g_present_swap->CheckColorSpaceSupport(space, &support)) ||
        !(support & DXGI_SWAP_CHAIN_COLOR_SPACE_SUPPORT_FLAG_PRESENT) ||
        FAILED(g_present_swap->SetColorSpace1(space)))
    {
        Log("[hdr] presentation colour space %u unsupported", (unsigned)space);
        // On the HDR path the colour space IS the feature: an scRGB buffer
        // presented as if it were sRGB is worse than no HDR at all, so the
        // frame is refused and the capture falls back. On the SDR path it
        // is the space the chain was created with - the picture is right
        // without the call ever being made, and this program made presents
        // for a year without making it. Refusing here would turn "too
        // bright" into "nothing at all" on somebody's machine.
        return hdr ? false : true;
    }
    return true;
}

static bool EnsureHdrPipeline(UINT w, UINT height, bool pq)
{
    const auto format = pq ? DXGI_FORMAT_R10G10B10A2_UNORM : DXGI_FORMAT_R16G16B16A16_FLOAT;
    if (g_hdr_output)
    {
        const auto d = g_hdr_output->GetDesc();
        if (d.Width == w && d.Height == height && d.Format == format) return true;
    }
    // Either a size change or a half-built attempt from last time: both
    // start from nothing.
    CloseHdrResources();
    winrt::com_ptr<ID3DBlob> code, errors, signature;
    HRESULT hr = D3DCompile(kHdrCompositeHlsl, sizeof(kHdrCompositeHlsl)-1,
        "hdr-composite", nullptr, nullptr, "CSMain", "cs_5_0", 0, 0, code.put(), errors.put());
    if (FAILED(hr))
    { Log("[hdr] compile: %s", errors ? (char *)errors->GetBufferPointer() : "failed"); return false; }
    D3D12_DESCRIPTOR_RANGE ranges[2] = {
        {D3D12_DESCRIPTOR_RANGE_TYPE_SRV, 3, 0, 0, 0},
        {D3D12_DESCRIPTOR_RANGE_TYPE_UAV, 1, 0, 0, 3}};
    D3D12_ROOT_PARAMETER params[2] = {};
    params[0].ParameterType = D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE;
    params[0].DescriptorTable = {2, ranges};
    params[1].ParameterType = D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS;
    params[1].Constants = {0, 0, 4};
    D3D12_ROOT_SIGNATURE_DESC rs = {2, params};
    if (FAILED(D3D12SerializeRootSignature(&rs, D3D_ROOT_SIGNATURE_VERSION_1, signature.put(), errors.put())))
        return false;
    if (FAILED(h.dev->CreateRootSignature(0, signature->GetBufferPointer(), signature->GetBufferSize(),
                                         IID_PPV_ARGS(&g_hdr_rs)))) return false;
    D3D12_COMPUTE_PIPELINE_STATE_DESC ps = {};
    ps.pRootSignature = g_hdr_rs;
    ps.CS = {code->GetBufferPointer(), code->GetBufferSize()};
    if (FAILED(h.dev->CreateComputePipelineState(&ps, IID_PPV_ARGS(&g_hdr_pso)))) return false;
    D3D12_DESCRIPTOR_HEAP_DESC heap = {};
    heap.Type = D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV;
    heap.NumDescriptors = 4;
    heap.Flags = D3D12_DESCRIPTOR_HEAP_FLAG_SHADER_VISIBLE;
    if (FAILED(h.dev->CreateDescriptorHeap(&heap, IID_PPV_ARGS(&g_hdr_heap)))) return false;
    g_hdr_output = MakeTex(w, height, format, true);
    return g_hdr_output != nullptr;
}

static bool PresentHdr(VideoState &v, bool bypass)
{
    const bool framegen = FgRequested() && !bypass;
    if (!framegen) StopFgPresentation();
    const UINT w = v.upscale ? v.full_w : v.w;
    const UINT height = v.upscale ? v.full_h : v.hgt;
    if (!g_dda_d12 || !g_dda_ready) return false;
    const auto native_desc = g_dda_d12->GetDesc();
    if (native_desc.Width != w || native_desc.Height != height)
    { Log("[hdr] capture/output size mismatch; refusing stale HDR frame"); return false; }
    if (!EnsurePresentFormat(true, framegen) || !EnsureHdrPipeline(w, height, framegen)) return false;

    const UINT stride = h.dev->GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV);
    auto cpu = g_hdr_heap->GetCPUDescriptorHandleForHeapStart();
    // A bypass must never read an uninitialized neural output.
    ID3D12Resource *inputs[] = {g_dda_d12, v.color.tex, bypass ? v.color.tex : v.output};
    for (auto *input : inputs)
    {
        D3D12_SHADER_RESOURCE_VIEW_DESC srv = {};
        srv.Format = input->GetDesc().Format;
        srv.ViewDimension = D3D12_SRV_DIMENSION_TEXTURE2D;
        srv.Shader4ComponentMapping = D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING;
        srv.Texture2D.MipLevels = 1;
        h.dev->CreateShaderResourceView(input, &srv, cpu);
        cpu.ptr += stride;
    }
    D3D12_UNORDERED_ACCESS_VIEW_DESC uav = {};
    uav.Format = g_hdr_output->GetDesc().Format;
    uav.ViewDimension = D3D12_UAV_DIMENSION_TEXTURE2D;
    h.dev->CreateUnorderedAccessView(g_hdr_output, nullptr, &uav, cpu);
    winrt::com_ptr<ID3D12Resource> bb;
    if (!framegen && FAILED(g_present_swap->GetBuffer(g_present_swap->GetCurrentBackBufferIndex(),
                                         __uuidof(ID3D12Resource), bb.put_void()))) return false;
    if (!BeginCommands()) return false;
    D3D12_RESOURCE_BARRIER pre[] = {
        Transition(g_hdr_output, D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_UNORDERED_ACCESS),
        Transition(g_dda_d12, D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE),
        Transition(v.output, D3D12_RESOURCE_STATE_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE)};
    h.list->ResourceBarrier(bypass ? 2 : 3, pre);
    h.list->SetDescriptorHeaps(1, &g_hdr_heap);
    h.list->SetComputeRootSignature(g_hdr_rs);
    h.list->SetPipelineState(g_hdr_pso);
    h.list->SetComputeRootDescriptorTable(0, g_hdr_heap->GetGPUDescriptorHandleForHeapStart());
    struct { float white; UINT bypass, split, hdr; } constants = {
        g_hdr_frame_white, bypass ? 1u : 0u, g_hdr_split,
        (g_capture_display.enabled ? 1u : 0u) | (framegen ? 2u : 0u)};
    h.list->SetComputeRoot32BitConstants(1, 4, &constants, 0);
    h.list->Dispatch((w+7)/8, (height+7)/8, 1);
    D3D12_RESOURCE_BARRIER copy[] = {
        Transition(g_hdr_output, D3D12_RESOURCE_STATE_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_COPY_SOURCE),
        Transition(bb.get(), D3D12_RESOURCE_STATE_PRESENT, D3D12_RESOURCE_STATE_COPY_DEST)};
    h.list->ResourceBarrier(framegen ? 1 : 2, copy);
    if (!framegen) h.list->CopyResource(bb.get(), g_hdr_output);
    D3D12_RESOURCE_BARRIER post[] = {
        Transition(g_hdr_output, D3D12_RESOURCE_STATE_COPY_SOURCE, D3D12_RESOURCE_STATE_COMMON),
        Transition(bb.get(), D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_PRESENT),
        Transition(g_dda_d12, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE, D3D12_RESOURCE_STATE_COMMON),
        Transition(v.output, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE, D3D12_RESOURCE_STATE_UNORDERED_ACCESS)};
    h.list->ResourceBarrier(1, post);
    if (!framegen) h.list->ResourceBarrier(1, post + 1);
    h.list->ResourceBarrier(bypass ? 1 : 2, post + 2);
    // Existing Spout consumers and the Python recording protocol are SDR.
    ID3D12Resource *export_src = bypass ? v.color.tex : v.output;
    auto rest = bypass ? D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE : D3D12_RESOURCE_STATE_UNORDERED_ACCESS;
    auto export_pre = Transition(export_src, rest, D3D12_RESOURCE_STATE_COPY_SOURCE);
    h.list->ResourceBarrier(1, &export_pre);
    SpoutBridgeCopy(h.list, export_src, w, height);
    auto export_post = Transition(export_src, D3D12_RESOURCE_STATE_COPY_SOURCE, rest);
    h.list->ResourceBarrier(1, &export_post);
    const auto fence = EndCommands();
    if (!WaitFenceValue(h.fence, fence, 2000)) return false;
    if (framegen)
    {
        if (FgPresent(v, g_hdr_output, D3D12_RESOURCE_STATE_COMMON)) return true;
        return PresentHdr(v, bypass); // failed FG has disabled itself; ordinary output
    }
    const bool ok = SUCCEEDED(g_present_swap->Present(0, 0));
    if (ok) { RevealOnFirstPresent(); SpoutBridgeSend(); }
    return ok;
}
