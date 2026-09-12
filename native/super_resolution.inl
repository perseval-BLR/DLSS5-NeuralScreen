// Experimental SR on desktop captures: flat depth, estimated motion, no fabricated jitter.
static struct SrState {
    HMODULE module = nullptr;
    PFN_NR_Create create = nullptr;
    PFN_NR_Evaluate evaluate = nullptr;
    PFN_NR_Release release = nullptr;
    NVSDK_NGX_Parameter *params = nullptr;
    NVSDK_NGX_Handle *feature = nullptr;
    VideoTex depth;
    winrt::com_ptr<ID3D12Resource> raw, nr_in, nr_out, input, motion, output;
    winrt::com_ptr<ID3D12DescriptorHeap> heap;
    unsigned scale = 65;
    UINT w = 0, height = 0, ow = 0, oh = 0;
    UINT nw = 0, nh = 0;
    bool initialized = false, failed = false, history = false;
    int ui = -1;
} g_sr;

static void CloseSrResources()
{
    // Shared scaler caches must not retain descriptors for released textures;
    // a later allocation can reuse the same COM pointer with a different size.
    g_scale4_src_bound = g_scale4_dst_bound = nullptr;
    g_scale4_src_bound2 = g_scale4_dst_bound2 = nullptr;
    g_res_native_bound = g_res_in_bound = g_res_out_bound = g_res_dst_bound = nullptr;
    if (g_sr.feature) { g_sr.release(g_sr.feature); g_sr.feature = nullptr; }
    if (g_sr.depth.tex) { g_sr.depth.tex->Release(); g_sr.depth.tex = nullptr; }
    if (g_sr.depth.upload) { g_sr.depth.upload->Release(); g_sr.depth.upload = nullptr; }
    g_sr.input = nullptr; g_sr.motion = nullptr; g_sr.output = nullptr; g_sr.heap = nullptr;
    g_sr.raw = nullptr; g_sr.nr_in = nullptr; g_sr.nr_out = nullptr;
    g_sr.history = false;
}

static bool SrRequested()
{
    char value[8] = {};
    GetEnvironmentVariableA("NS_DLSS_SR", value, sizeof(value));
    return (g_sr.ui < 0 ? strcmp(value,"1")==0 : g_sr.ui != 0) && !g_sr.failed;
}

static void ConfigureSrFrame(uint32_t flags)
{
    if (!(flags & 0x4000)) return;
    const int enabled = (flags & 0x2000) != 0;
    if (g_sr.ui == enabled) return;
    CloseFgResources();
    CloseSrResources();
    g_sr.ui = enabled; g_sr.failed = false;
    g_force_next_frame = true;
    Log("[sr] UI: %s", enabled ? "on" : "off");
}

static bool EnsureSr(VideoState &v)
{
    if (!SrRequested()) { g_sr.history = false; return false; }
    const UINT ow = v.upscale ? v.full_w : v.w, oh = v.upscale ? v.full_h : v.hgt;
    UINT sw = std::max(64u, ((ow * g_sr.scale + 100) / 200) * 2);
    UINT sh = std::max(64u, ((oh * g_sr.scale + 100) / 200) * 2);
    SafeProcessingSize(ow,oh,sw,sh);
    // Boost is relative to the already reduced SR input, independently of
    // the motion grid. No full-size NR/composite is needed on this path.
    UINT nw = v.nr_small ? std::max(64u, UINT((uint64_t(sw)*v.nr_w + ow)/(2*ow))*2) : sw;
    UINT nh = v.nr_small ? std::max(64u, UINT((uint64_t(sh)*v.nr_h + oh)/(2*oh))*2) : sh;
    SafeProcessingSize(sw,sh,nw,nh);
    if (g_sr.feature && g_sr.w == sw && g_sr.height == sh &&
        g_sr.ow == ow && g_sr.oh == oh && g_sr.nw == nw && g_sr.nh == nh) return true;
    CloseSrResources();
    if (!g_sr.initialized)
    {
        wchar_t directory[MAX_PATH] = {}, path[MAX_PATH] = {};
        GetModuleFileNameW(nullptr, directory, MAX_PATH);
        if (auto slash = wcsrchr(directory,L'\\')) *(slash+1)=0;
        wcscpy_s(path,directory);wcscat_s(path,L"nvngx_dlss.dll");
        if (!g_sr.module) g_sr.module = LoadLibraryW(path);
        if (!g_sr.module) { Log("[sr] DLL load failed: %lu",GetLastError()); g_sr.failed=true; return false; }
        auto init = reinterpret_cast<PFN_NR_InitExt>(GetProcAddress(g_sr.module,"NVSDK_NGX_D3D12_Init_Ext"));
        g_sr.create = reinterpret_cast<PFN_NR_Create>(GetProcAddress(g_sr.module,"NVSDK_NGX_D3D12_CreateFeature"));
        g_sr.evaluate = reinterpret_cast<PFN_NR_Evaluate>(GetProcAddress(g_sr.module,"NVSDK_NGX_D3D12_EvaluateFeature"));
        g_sr.release = reinterpret_cast<PFN_NR_Release>(GetProcAddress(g_sr.module,"NVSDK_NGX_D3D12_ReleaseFeature"));
        if (!init || !g_sr.create || !g_sr.evaluate || !g_sr.release ||
            (!g_sr.params && NVSDK_NGX_FAILED(NVSDK_NGX_D3D12_AllocateParameters(&g_sr.params))))
        { Log("[sr] API unavailable; keeping NR output"); g_sr.failed=true; return false; }
        auto result=init(0x1000000ULL,directory,h.dev,NVSDK_NGX_Version_API,g_sr.params);
        Log("[sr] Init_Ext -> 0x%08X",result);
        if (NVSDK_NGX_FAILED(result)) { g_sr.failed=true; return false; }
        g_sr.initialized=true;
    }
    auto p=g_sr.params;p->Reset();
    p->Set(NVSDK_NGX_Parameter_CreationNodeMask,1u);
    p->Set(NVSDK_NGX_Parameter_VisibilityNodeMask,1u);
    p->Set(NVSDK_NGX_Parameter_Width,sw);p->Set(NVSDK_NGX_Parameter_Height,sh);
    p->Set(NVSDK_NGX_Parameter_OutWidth,ow);p->Set(NVSDK_NGX_Parameter_OutHeight,oh);
    const float ratio=float(sw)/ow;
    p->Set(NVSDK_NGX_Parameter_PerfQualityValue,int(ratio>=1.f ? NVSDK_NGX_PerfQuality_Value_DLAA : ratio>=.58f ? NVSDK_NGX_PerfQuality_Value_MaxQuality :
        ratio>=.5f ? NVSDK_NGX_PerfQuality_Value_Balanced : NVSDK_NGX_PerfQuality_Value_MaxPerf));
    p->Set(NVSDK_NGX_Parameter_DLSS_Feature_Create_Flags,
        int(NVSDK_NGX_DLSS_Feature_Flags_MVLowRes | NVSDK_NGX_DLSS_Feature_Flags_AutoExposure));
    if (!BeginCommands()) { g_sr.failed=true;return false; }
    auto result=g_sr.create(h.list,NVSDK_NGX_Feature_SuperSampling,p,&g_sr.feature);
    if (!WaitFenceValue(h.fence,EndCommands(),30000) || NVSDK_NGX_FAILED(result) || !g_sr.feature)
    { Log("[sr] CreateFeature failed 0x%08X; keeping NR output",result);g_sr.failed=true;CloseSrResources();return false; }
    if (!CreateVideoTex(g_sr.depth,sw,sh,DXGI_FORMAT_R32_FLOAT,sw*4))
    { g_sr.failed=true;CloseSrResources();return false; }
    std::vector<float> depth(size_t(sw)*sh,.5f);
    if (!FillUpload(g_sr.depth,reinterpret_cast<BYTE*>(depth.data()),sw*4,sh) || !BeginCommands())
    { g_sr.failed=true;CloseSrResources();return false; }
    CopyUpload(h.list,g_sr.depth);
    auto b=Transition(g_sr.depth.tex,D3D12_RESOURCE_STATE_COPY_DEST,D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
    h.list->ResourceBarrier(1,&b);
    if (!WaitFenceValue(h.fence,EndCommands(),30000)) { g_sr.failed=true;CloseSrResources();return false; }
    if (!EnsureScalePipeline()) { g_sr.failed=true;CloseSrResources();return false; }
    g_sr.input.attach(MakeTex(sw,sh,DXGI_FORMAT_R8G8B8A8_UNORM,true));
    g_sr.raw.attach(MakeTex(sw,sh,DXGI_FORMAT_R8G8B8A8_UNORM,true));
    g_sr.nr_in.attach(MakeTex(nw,nh,DXGI_FORMAT_R8G8B8A8_UNORM,true));
    g_sr.nr_out.attach(MakeTex(nw,nh,DXGI_FORMAT_R8G8B8A8_UNORM,true));
    g_sr.motion.attach(MakeTex(sw,sh,DXGI_FORMAT_R16G16_FLOAT,true));
    g_sr.output.attach(MakeTex(ow,oh,DXGI_FORMAT_R8G8B8A8_UNORM,true));
    D3D12_DESCRIPTOR_HEAP_DESC hd = {};
    hd.Type=D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV;hd.NumDescriptors=8;hd.Flags=D3D12_DESCRIPTOR_HEAP_FLAG_SHADER_VISIBLE;
    if (!g_sr.raw || !g_sr.nr_in || !g_sr.nr_out || !g_sr.input || !g_sr.motion || !g_sr.output || FAILED(h.dev->CreateDescriptorHeap(&hd,IID_PPV_ARGS(g_sr.heap.put()))) || !BeginCommands())
    { g_sr.failed=true;CloseSrResources();return false; }
    D3D12_RESOURCE_BARRIER init[] = {
        Transition(g_sr.raw.get(),D3D12_RESOURCE_STATE_COMMON,D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE),
        Transition(g_sr.nr_in.get(),D3D12_RESOURCE_STATE_COMMON,D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE),
        Transition(g_sr.nr_out.get(),D3D12_RESOURCE_STATE_COMMON,D3D12_RESOURCE_STATE_UNORDERED_ACCESS),
        Transition(g_sr.input.get(),D3D12_RESOURCE_STATE_COMMON,D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE),
        Transition(g_sr.motion.get(),D3D12_RESOURCE_STATE_COMMON,D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE),
        Transition(g_sr.output.get(),D3D12_RESOURCE_STATE_COMMON,D3D12_RESOURCE_STATE_UNORDERED_ACCESS)};
    h.list->ResourceBarrier(6,init);
    if (!WaitFenceValue(h.fence,EndCommands(),30000)) { g_sr.failed=true;CloseSrResources();return false; }
    auto cpu=g_sr.heap->GetCPUDescriptorHandleForHeapStart();
    const UINT stride=h.dev->GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV);
    ID3D12Resource *sources[]={v.color.tex,v.mv.tex,g_sr.input.get(),g_sr.output.get()};
    ID3D12Resource *destinations[]={g_sr.raw.get(),g_sr.motion.get(),v.output,v.output};
    for (unsigned i=0;i<4;++i) {
        D3D12_SHADER_RESOURCE_VIEW_DESC srv={};srv.Format=sources[i]->GetDesc().Format;
        srv.ViewDimension=D3D12_SRV_DIMENSION_TEXTURE2D;srv.Shader4ComponentMapping=D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING;srv.Texture2D.MipLevels=1;
        h.dev->CreateShaderResourceView(sources[i],&srv,cpu);cpu.ptr+=stride;
        D3D12_UNORDERED_ACCESS_VIEW_DESC uav={};uav.Format=destinations[i]->GetDesc().Format;uav.ViewDimension=D3D12_UAV_DIMENSION_TEXTURE2D;
        h.dev->CreateUnorderedAccessView(destinations[i],nullptr,&uav,cpu);cpu.ptr+=stride;
    }
    g_sr.w=sw;g_sr.height=sh;g_sr.ow=ow;g_sr.oh=oh;
    g_sr.nw=nw;g_sr.nh=nh;
    Log("[sr] ready: capture %ux%u -> reduced %ux%u -> NR %ux%u -> SR input %ux%u -> %ux%u",
        ow,oh,sw,sh,nw,nh,sw,sh,ow,oh);
    return true;
}

static void SrScale(unsigned pair, UINT sw, UINT sh, UINT dw, UINT dh, bool motion=false)
{
    auto heap=g_sr.heap.get();h.list->SetDescriptorHeaps(1,&heap);
    h.list->SetComputeRootSignature(g_scale_rs);
    h.list->SetPipelineState(motion ? g_scale_pso : g_scale4_pso);
    const UINT sizes[]={dw,dh,sw,sh};
    h.list->SetComputeRoot32BitConstants(0,4,sizes,0);
    auto gpu=g_sr.heap->GetGPUDescriptorHandleForHeapStart();
    gpu.ptr+=UINT64(pair)*2*h.dev->GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV);
    h.list->SetComputeRootDescriptorTable(1,gpu);h.list->Dispatch((dw+7)/8,(dh+7)/8,1);
}

static void PrepareSrInput(VideoState &v)
{
    D3D12_RESOURCE_BARRIER pre[]={
        Transition(g_sr.raw.get(),D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE,D3D12_RESOURCE_STATE_UNORDERED_ACCESS),
        Transition(g_sr.motion.get(),D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE,D3D12_RESOURCE_STATE_UNORDERED_ACCESS)};
    h.list->ResourceBarrier(2,pre);
    SrScale(0,g_sr.ow,g_sr.oh,g_sr.w,g_sr.height);
    SrScale(1,v.w,v.hgt,g_sr.w,g_sr.height,true);
    D3D12_RESOURCE_BARRIER post[]={
        Transition(g_sr.raw.get(),D3D12_RESOURCE_STATE_UNORDERED_ACCESS,D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE),
        Transition(g_sr.nr_in.get(),D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE,D3D12_RESOURCE_STATE_UNORDERED_ACCESS),
        Transition(g_sr.motion.get(),D3D12_RESOURCE_STATE_UNORDERED_ACCESS,D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE)};
    h.list->ResourceBarrier(3,post);
    ScaleColorInto(g_sr.raw.get(),g_sr.w,g_sr.height,g_sr.nr_in.get(),g_sr.nw,g_sr.nh,0);
    auto ready=Transition(g_sr.nr_in.get(),D3D12_RESOURCE_STATE_UNORDERED_ACCESS,D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
    h.list->ResourceBarrier(1,&ready);
}

static void ComposeSrInput(VideoState &v)
{
    D3D12_RESOURCE_BARRIER before[]={
        Transition(g_sr.nr_out.get(),D3D12_RESOURCE_STATE_UNORDERED_ACCESS,D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE),
        Transition(g_sr.input.get(),D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE,D3D12_RESOURCE_STATE_UNORDERED_ACCESS)};
    h.list->ResourceBarrier(2,before);
    if (v.nr_small && v.residual)
        ResidualCompose(g_sr.raw.get(),g_sr.nr_in.get(),g_sr.nr_out.get(),g_sr.w,g_sr.height,g_sr.input.get(),v.residual_strength);
    else
        ScaleColorInto(g_sr.nr_out.get(),g_sr.nw,g_sr.nh,g_sr.input.get(),g_sr.w,g_sr.height,1);
    D3D12_RESOURCE_BARRIER after[]={
        Transition(g_sr.nr_out.get(),D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE,D3D12_RESOURCE_STATE_UNORDERED_ACCESS),
        Transition(g_sr.input.get(),D3D12_RESOURCE_STATE_UNORDERED_ACCESS,D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE)};
    h.list->ResourceBarrier(2,after);
}

// Only the final reconstruction follows NR; the input reduction precedes it.
static bool EvaluateSr(VideoState &v, int reset)
{
    auto p=g_sr.params;p->Reset();
    p->Set(NVSDK_NGX_Parameter_Color,g_sr.input.get());p->Set(NVSDK_NGX_Parameter_Output,g_sr.output.get());
    p->Set(NVSDK_NGX_Parameter_Depth,g_sr.depth.tex);p->Set(NVSDK_NGX_Parameter_MotionVectors,g_sr.motion.get());
    p->Set(NVSDK_NGX_Parameter_Reset,int(reset || !g_sr.history));
    p->Set(NVSDK_NGX_Parameter_Jitter_Offset_X,0.f);p->Set(NVSDK_NGX_Parameter_Jitter_Offset_Y,0.f);
    p->Set(NVSDK_NGX_Parameter_MV_Scale_X,float(g_sr.w)/v.w);p->Set(NVSDK_NGX_Parameter_MV_Scale_Y,float(g_sr.height)/v.hgt);
    p->Set(NVSDK_NGX_Parameter_DLSS_Render_Subrect_Dimensions_Width,g_sr.w);
    p->Set(NVSDK_NGX_Parameter_DLSS_Render_Subrect_Dimensions_Height,g_sr.height);
    p->Set(NVSDK_NGX_Parameter_DLSS_Pre_Exposure,1.f);
    p->Set(NVSDK_NGX_Parameter_DLSS_Exposure_Scale,1.f);
    auto result=g_sr.evaluate(h.list,g_sr.feature,p,nullptr);
    if (NVSDK_NGX_FAILED(result))
    {
        Log("[sr] Evaluate failed 0x%08X; scaling reduced NR output",result);
        g_sr.failed=true;
        SrScale(2,g_sr.w,g_sr.height,g_sr.ow,g_sr.oh);
        return false;
    }
    auto sharp_pre=Transition(g_sr.output.get(),D3D12_RESOURCE_STATE_UNORDERED_ACCESS,D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
    h.list->ResourceBarrier(1,&sharp_pre);
    auto heap=g_sr.heap.get();h.list->SetDescriptorHeaps(1,&heap);
    h.list->SetComputeRootSignature(g_scale_rs);h.list->SetPipelineState(g_sharpen_pso);
    const UINT sizes[]={g_sr.ow,g_sr.oh,g_sr.ow,g_sr.oh};
    h.list->SetComputeRoot32BitConstants(0,4,sizes,0);
    auto gpu=g_sr.heap->GetGPUDescriptorHandleForHeapStart();
    gpu.ptr+=6ull*h.dev->GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV);
    h.list->SetComputeRootDescriptorTable(1,gpu);h.list->Dispatch((g_sr.ow+7)/8,(g_sr.oh+7)/8,1);
    auto sharp_post=Transition(g_sr.output.get(),D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE,D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
    h.list->ResourceBarrier(1,&sharp_post);
    if (!g_sr.history) Log("[sr] first evaluation succeeded");
    g_sr.history=true;
    return true;
}
