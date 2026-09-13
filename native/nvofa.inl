// NVOFA is provided by the display driver. No optical-flow runtime is bundled.
#include "include/nvofa/nvOpticalFlowD3D12.h"

static bool NvofaRequested()
{
    char value[16] = {};
    return GetEnvironmentVariableA("NS_MOTION_BACKEND", value, sizeof(value)) && strcmp(value, "nvofa") == 0;
}

static struct NvofaState {
    HMODULE library = nullptr;
    NV_OF_D3D12_API_FUNCTION_LIST api{};
    NvOFHandle session = nullptr;
    winrt::com_ptr<ID3D12Fence> fence;
    UINT64 value = 0;
    winrt::com_ptr<ID3D12Resource> inputs[2], flow, cost;
    NvOFGPUBufferHandle registered[4] = {};
    winrt::com_ptr<ID3D12RootSignature> root;
    winrt::com_ptr<ID3D12PipelineState> expand;
    winrt::com_ptr<ID3D12DescriptorHeap> heap;
    UINT width = 0, height = 0, grid = 4, current = 0;
    bool valid = false, failed = false;
    unsigned sequence = 0;
} g_nvofa;

static void CloseNvofa()
{
    auto &f = g_nvofa;
    // Both engines must have relinquished resources before unregister/release.
    if (f.session) {
        if (h.fence) WaitFenceValue(h.fence, h.fence_value, 30000);
        if (f.fence) WaitFenceValue(f.fence.get(), f.value, 30000);
        for (auto &buffer : f.registered) if (buffer) {
            NV_OF_UNREGISTER_RESOURCE_PARAMS_D3D12 p{}; p.hOFGpuBuffer = buffer;
            f.api.nvOFUnregisterResourceD3D12(&p); buffer = nullptr;
        }
    }
    // Release registered D3D12 resources before destroying their driver session.
    // Reversing this order faults on the mid-stream fallback path (RTX 5080).
    for (auto &input : f.inputs) input = nullptr;
    f.flow = nullptr; f.cost = nullptr; f.root = nullptr; f.expand = nullptr;
    f.heap = nullptr;
    if (f.session) { f.api.nvOFDestroy(f.session); f.session = nullptr; }
    f.fence = nullptr; f.value = 0;
    f.width = f.height = f.current = 0; f.valid = false;
    if (f.library) { FreeLibrary(f.library); f.library = nullptr; }
    f.api = {};
}

static bool NvofaError(const char *operation, NV_OF_STATUS status)
{
    char detail[512] = {}; uint32_t size = sizeof(detail);
    if (g_nvofa.session && g_nvofa.api.nvOFGetLastError)
        g_nvofa.api.nvOFGetLastError(g_nvofa.session, detail, &size);
    Log("[nvofa] unavailable: %s (%u) %s; using CPU DIS", operation, unsigned(status), detail);
    g_nvofa.failed = true;
    CloseNvofa();
    return false;
}

static std::vector<uint32_t> NvofaCaps(NV_OF_CAPS capability)
{
    auto &f = g_nvofa; uint32_t count = 0;
    if (f.api.nvOFGetCaps(f.session, capability, nullptr, &count) != NV_OF_SUCCESS || count > 128) return {};
    std::vector<uint32_t> values(count);
    if (!count || f.api.nvOFGetCaps(f.session, capability, values.data(), &count) != NV_OF_SUCCESS) return {};
    return values;
}

static bool NvofaSupportsFormat(NV_OF_BUFFER_USAGE usage, DXGI_FORMAT format)
{
    auto &f = g_nvofa; uint32_t count = 0;
    if (f.api.nvOFGetSurfaceFormatCountD3D12(f.session, usage, NV_OF_MODE_OPTICALFLOW, &count) != NV_OF_SUCCESS || count > 128) return false;
    std::vector<DXGI_FORMAT> formats(count);
    if (!count || f.api.nvOFGetSurfaceFormatD3D12(f.session, usage, NV_OF_MODE_OPTICALFLOW, formats.data()) != NV_OF_SUCCESS) return false;
    return std::find(formats.begin(), formats.end(), format) != formats.end();
}

static const char kNvofaExpand[] = R"(
Texture2D<int2> flow : register(t0);
RWTexture2D<float2> motion : register(u0);
cbuffer Params : register(b0) { uint w,h,iw,ih,grid,reset; };
[numthreads(8,8,1)] void CSMain(uint3 p : SV_DispatchThreadID) {
    if(p.x>=w || p.y>=h) return;
    uint fw,fh; flow.GetDimensions(fw,fh);
    float2 q=(float2(p.xy)+.5)*float2(iw,ih)/float2(w,h)/grid-.5;
    int2 a=int2(floor(q)); float2 t=frac(q); int2 limit=int2(fw-1,fh-1);
    float2 v=lerp(lerp(float2(flow.Load(int3(clamp(a,0,limit),0))),
                       float2(flow.Load(int3(clamp(a+int2(1,0),0,limit),0))),t.x),
                  lerp(float2(flow.Load(int3(clamp(a+int2(0,1),0,limit),0))),
                       float2(flow.Load(int3(clamp(a+1,0,limit),0))),t.x),t.y);
    v=v/32.0*float2(w,h)/float2(iw,ih);
    motion[p.xy]=reset || dot(v,v)<.25 ? float2(0,0) : v;
})";

static bool EnsureNvofa(UINT width, UINT height)
{
    auto &f = g_nvofa;
    if (f.failed) return false;
    if (f.session && f.width == width && f.height == height) return true;
    CloseNvofa();
    f.library = LoadLibraryExW(L"nvofapi64.dll", nullptr, LOAD_LIBRARY_SEARCH_SYSTEM32);
    if (!f.library) return NvofaError("driver API missing", NV_OF_ERR_OF_NOT_AVAILABLE);
    auto create = reinterpret_cast<decltype(&NvOFAPICreateInstanceD3D12)>(GetProcAddress(f.library, "NvOFAPICreateInstanceD3D12"));
    if (!create) return NvofaError("D3D12 entry point missing", NV_OF_ERR_OF_NOT_AVAILABLE);
    auto status = create(NV_OF_API_VERSION, &f.api);
    if (status != NV_OF_SUCCESS) return NvofaError("API version", status);
    if (!f.api.nvCreateOpticalFlowD3D12 || !f.api.nvOFInit || !f.api.nvOFDestroy ||
        !f.api.nvOFGetCaps || !f.api.nvOFGetSurfaceFormatCountD3D12 ||
        !f.api.nvOFGetSurfaceFormatD3D12 || !f.api.nvOFRegisterResourceD3D12 ||
        !f.api.nvOFUnregisterResourceD3D12 || !f.api.nvOFExecuteD3D12)
        return NvofaError("incomplete API table", NV_OF_ERR_OF_NOT_AVAILABLE);
    status = f.api.nvCreateOpticalFlowD3D12(h.dev, &f.session);
    if (status != NV_OF_SUCCESS) return NvofaError("create session", status);
    auto grids = NvofaCaps(NV_OF_CAPS_SUPPORTED_OUTPUT_GRID_SIZES);
    auto minw = NvofaCaps(NV_OF_CAPS_WIDTH_MIN), minh = NvofaCaps(NV_OF_CAPS_HEIGHT_MIN);
    auto maxw = NvofaCaps(NV_OF_CAPS_WIDTH_MAX), maxh = NvofaCaps(NV_OF_CAPS_HEIGHT_MAX);
    if (grids.empty() || minw.empty() || minh.empty() || maxw.empty() || maxh.empty() ||
        width < minw[0] || height < minh[0] || width > maxw[0] || height > maxh[0])
        return NvofaError("unsupported input dimensions", NV_OF_ERR_UNSUPPORTED_FEATURE);
    f.grid = std::find(grids.begin(), grids.end(), 4u) != grids.end() ? 4u : grids.front();
    if (f.grid != 1 && f.grid != 2 && f.grid != 4)
        return NvofaError("unsupported output grid", NV_OF_ERR_UNSUPPORTED_FEATURE);
    if (!NvofaSupportsFormat(NV_OF_BUFFER_USAGE_INPUT, DXGI_FORMAT_R8_UNORM) ||
        !NvofaSupportsFormat(NV_OF_BUFFER_USAGE_OUTPUT, DXGI_FORMAT_R16G16_SINT) ||
        !NvofaSupportsFormat(NV_OF_BUFFER_USAGE_COST, DXGI_FORMAT_R8_UINT))
        return NvofaError("unsupported flow/gray/cost format", NV_OF_ERR_UNSUPPORTED_FEATURE);
    NV_OF_INIT_PARAMS params{};
    params.width = width; params.height = height;
    params.outGridSize = static_cast<NV_OF_OUTPUT_VECTOR_GRID_SIZE>(f.grid);
    params.mode = NV_OF_MODE_OPTICALFLOW; params.perfLevel = NV_OF_PERF_LEVEL_FAST;
    params.enableOutputCost = NV_OF_TRUE; params.inputBufferFormat = NV_OF_BUFFER_FORMAT_GRAYSCALE8;
    status = f.api.nvOFInit(f.session, &params);
    if (status != NV_OF_SUCCESS) return NvofaError("initialize", status);
    if (FAILED(h.dev->CreateFence(0, D3D12_FENCE_FLAG_NONE, IID_PPV_ARGS(f.fence.put()))))
        return NvofaError("create fence", NV_OF_ERR_OUT_OF_MEMORY);
    const UINT fw = (width + f.grid - 1) / f.grid, fh = (height + f.grid - 1) / f.grid;
    for (auto &input : f.inputs) input.attach(MakeTex(width, height, DXGI_FORMAT_R8_UNORM, false));
    f.flow.attach(MakeTex(fw, fh, DXGI_FORMAT_R16G16_SINT, true));
    f.cost.attach(MakeTex(fw, fh, DXGI_FORMAT_R8_UINT, true));
    ID3D12Resource *resources[] = {f.inputs[0].get(), f.inputs[1].get(), f.flow.get(), f.cost.get()};
    for (unsigned i = 0; i < 4; ++i) {
        if (!resources[i]) return NvofaError("allocate buffers", NV_OF_ERR_OUT_OF_MEMORY);
        NV_OF_REGISTER_RESOURCE_PARAMS_D3D12 p{};
        p.resource = resources[i]; p.hOFGpuBuffer = &f.registered[i];
        p.inputFencePoint = {h.fence, h.fence_value};
        p.outputFencePoint = {f.fence.get(), ++f.value};
        status = f.api.nvOFRegisterResourceD3D12(f.session, &p);
        if (status != NV_OF_SUCCESS) { --f.value; return NvofaError("register buffer", status); }
        if (!WaitFenceValue(f.fence.get(), f.value, 30000)) return NvofaError("register fence", NV_OF_ERR_GENERIC);
    }
    D3D12_DESCRIPTOR_RANGE ranges[2] = {{D3D12_DESCRIPTOR_RANGE_TYPE_SRV,1,0,0,0}, {D3D12_DESCRIPTOR_RANGE_TYPE_UAV,1,0,0,1}};
    D3D12_ROOT_PARAMETER roots[2] = {};
    roots[0].ParameterType = D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS; roots[0].Constants = {0,0,6};
    roots[1].ParameterType = D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE; roots[1].DescriptorTable = {2,ranges};
    D3D12_ROOT_SIGNATURE_DESC rd = {2,roots,0,nullptr,D3D12_ROOT_SIGNATURE_FLAG_NONE};
    winrt::com_ptr<ID3DBlob> code, errors;
    if (FAILED(D3D12SerializeRootSignature(&rd, D3D_ROOT_SIGNATURE_VERSION_1, code.put(), errors.put())) ||
        FAILED(h.dev->CreateRootSignature(0, code->GetBufferPointer(), code->GetBufferSize(), IID_PPV_ARGS(f.root.put()))))
        return NvofaError("root signature", NV_OF_ERR_GENERIC);
    code = nullptr; errors = nullptr;
    if (FAILED(D3DCompile(kNvofaExpand, sizeof(kNvofaExpand)-1, nullptr, nullptr, nullptr, "CSMain", "cs_5_0", D3DCOMPILE_OPTIMIZATION_LEVEL3, 0, code.put(), errors.put())))
        return NvofaError("expand shader", NV_OF_ERR_GENERIC);
    D3D12_COMPUTE_PIPELINE_STATE_DESC pd{}; pd.pRootSignature = f.root.get(); pd.CS = {code->GetBufferPointer(),code->GetBufferSize()};
    D3D12_DESCRIPTOR_HEAP_DESC hd = {D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV,2,D3D12_DESCRIPTOR_HEAP_FLAG_SHADER_VISIBLE,0};
    if (FAILED(h.dev->CreateComputePipelineState(&pd, IID_PPV_ARGS(f.expand.put()))) ||
        FAILED(h.dev->CreateDescriptorHeap(&hd, IID_PPV_ARGS(f.heap.put())))) return NvofaError("expand pipeline", NV_OF_ERR_GENERIC);
    f.width = width; f.height = height;
    Log("[nvofa] active: %ux%u, grid=%u, cost=8-bit; driver D3D12 API", width, height, f.grid);
    return true;
}

static void DumpNvofa(VideoState &v); // opt-in regression readback, defined below

static bool RunNvofa(VideoState &v, bool reset, UINT64 *submitted)
{
    auto &f = g_nvofa;
    if (submitted) *submitted = 0;
    if (!g_gray_mapped || !g_gray_uav || !EnsureNvofa(g_gray_w, g_gray_h)) return false;
    // Fault injection for the real fallback test; absent in ordinary launches.
    char fail_at[16] = {};
    if (GetEnvironmentVariableA("NS_NVOFA_TEST_FAIL_AT", fail_at, sizeof(fail_at)) &&
        f.sequence == strtoul(fail_at, nullptr, 10))
        return NvofaError("injected execute failure", NV_OF_ERR_GENERIC);
    if (!BeginCommands()) return NvofaError("begin input copy", NV_OF_ERR_GENERIC);
    auto barrier = [](ID3D12Resource *r, D3D12_RESOURCE_STATES a, D3D12_RESOURCE_STATES b) { auto t=Transition(r,a,b);h.list->ResourceBarrier(1,&t); };
    barrier(g_gray_uav, D3D12_RESOURCE_STATE_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_COPY_SOURCE);
    for (unsigned i = 0; i < 2; ++i) if (i == f.current || !f.valid || reset) {
        barrier(f.inputs[i].get(), D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_COPY_DEST);
        h.list->CopyResource(f.inputs[i].get(), g_gray_uav);
        barrier(f.inputs[i].get(), D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_COMMON);
    }
    barrier(g_gray_uav, D3D12_RESOURCE_STATE_COPY_SOURCE, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
    const UINT64 copied = EndCommands(); if (!copied) return NvofaError("submit input copy", NV_OF_ERR_GENERIC);
    NV_OF_FENCE_POINT ready = {h.fence,copied}, complete = {f.fence.get(),++f.value};
    NV_OF_EXECUTE_INPUT_PARAMS_D3D12 in{};
    // Forward flow is input -> reference. Neural motion requires current -> previous.
    in.inputFrame = f.registered[f.current]; in.referenceFrame = f.registered[1-f.current];
    in.disableTemporalHints = reset || !f.valid ? NV_OF_TRUE : NV_OF_FALSE;
    in.numFencePoints = 1; in.fencePoint = &ready;
    NV_OF_EXECUTE_OUTPUT_PARAMS_D3D12 out{};
    out.outputBuffer = f.registered[2]; out.outputCostBuffer = f.registered[3]; out.fencePoint = &complete;
    auto status = f.api.nvOFExecuteD3D12(f.session, &in, &out);
    if (status != NV_OF_SUCCESS) { --f.value; return NvofaError("execute", status); }
    if (FAILED(h.queue->Wait(f.fence.get(),f.value)) || !BeginCommands())
        return NvofaError("wait for optical flow", NV_OF_ERR_GENERIC);
    barrier(f.flow.get(), D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
    barrier(v.mv.tex, v.inputs_ready ? D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE : D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
    auto cpu=f.heap->GetCPUDescriptorHandleForHeapStart();
    D3D12_SHADER_RESOURCE_VIEW_DESC sd{}; sd.Format=DXGI_FORMAT_R16G16_SINT;sd.ViewDimension=D3D12_SRV_DIMENSION_TEXTURE2D;sd.Shader4ComponentMapping=D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING;sd.Texture2D.MipLevels=1;
    h.dev->CreateShaderResourceView(f.flow.get(),&sd,cpu);cpu.ptr+=h.dev->GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV);
    D3D12_UNORDERED_ACCESS_VIEW_DESC ud{};ud.Format=DXGI_FORMAT_R16G16_FLOAT;ud.ViewDimension=D3D12_UAV_DIMENSION_TEXTURE2D;
    h.dev->CreateUnorderedAccessView(v.mv.tex,nullptr,&ud,cpu);
    ID3D12DescriptorHeap *heaps[]={f.heap.get()};h.list->SetDescriptorHeaps(1,heaps);
    h.list->SetComputeRootSignature(f.root.get());h.list->SetPipelineState(f.expand.get());
    UINT constants[]={v.w,v.hgt,f.width,f.height,f.grid,UINT(reset || !f.valid)};
    h.list->SetComputeRoot32BitConstants(0,6,constants,0);h.list->SetComputeRootDescriptorTable(1,f.heap->GetGPUDescriptorHandleForHeapStart());
    h.list->Dispatch((v.w+7)/8,(v.hgt+7)/8,1);
    barrier(v.mv.tex,D3D12_RESOURCE_STATE_UNORDERED_ACCESS,D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
    barrier(f.flow.get(),D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE,D3D12_RESOURCE_STATE_COMMON);
    const UINT64 done=EndCommands();if(!done)return NvofaError("submit expansion", NV_OF_ERR_GENERIC);
    v.inputs_ready=true;f.valid=true;f.current=1-f.current;
    if (submitted) *submitted=done;
    else if (!WaitFenceValue(h.fence,done,30000)) return NvofaError("expansion fence", NV_OF_ERR_GENERIC);
    DumpNvofa(v); ++f.sequence;
    return true;
}

static void DumpNvofa(VideoState &v)
{
    char folder[MAX_PATH]={}; if(!GetEnvironmentVariableA("NS_NVOFA_DUMP",folder,MAX_PATH)) return;
    auto &f=g_nvofa;
    for (auto pair : {std::make_pair(f.flow.get(),"flow"),std::make_pair(f.cost.get(),"cost"),std::make_pair(v.mv.tex,"motion")}) {
        auto desc=pair.first->GetDesc();D3D12_PLACED_SUBRESOURCE_FOOTPRINT fp{};UINT rows;UINT64 rowbytes,bytes;
        h.dev->GetCopyableFootprints(&desc,0,1,0,&fp,&rows,&rowbytes,&bytes);
        D3D12_HEAP_PROPERTIES hp{};hp.Type=D3D12_HEAP_TYPE_READBACK;
        D3D12_RESOURCE_DESC rd{};rd.Dimension=D3D12_RESOURCE_DIMENSION_BUFFER;rd.Width=bytes;rd.Height=1;rd.DepthOrArraySize=rd.MipLevels=1;rd.SampleDesc.Count=1;rd.Layout=D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
        winrt::com_ptr<ID3D12Resource> rb;if(FAILED(h.dev->CreateCommittedResource(&hp,D3D12_HEAP_FLAG_NONE,&rd,D3D12_RESOURCE_STATE_COPY_DEST,nullptr,IID_PPV_ARGS(rb.put()))) || !BeginCommands())return;
        const auto state = pair.first == v.mv.tex ? D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE : D3D12_RESOURCE_STATE_COMMON;
        auto pre=Transition(pair.first,state,D3D12_RESOURCE_STATE_COPY_SOURCE);h.list->ResourceBarrier(1,&pre);
        D3D12_TEXTURE_COPY_LOCATION a{},b{};a.pResource=pair.first;a.Type=D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;b.pResource=rb.get();b.Type=D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;b.PlacedFootprint=fp;
        h.list->CopyTextureRegion(&b,0,0,0,&a,nullptr);
        auto post=Transition(pair.first,D3D12_RESOURCE_STATE_COPY_SOURCE,state);h.list->ResourceBarrier(1,&post);
        if(!WaitFenceValue(h.fence,EndCommands(),30000))return;
        BYTE *data=nullptr;D3D12_RANGE read={0,SIZE_T(bytes)},written={0,0};if(FAILED(rb->Map(0,&read,reinterpret_cast<void**>(&data))))return;
        char path[MAX_PATH];sprintf_s(path,"%s/%s-%04u.bin",folder,pair.second,f.sequence);FILE *file=nullptr;
        if(!fopen_s(&file,path,"wb") && file) {for(UINT y=0;y<rows;++y)fwrite(data+SIZE_T(y)*fp.Footprint.RowPitch,1,SIZE_T(rowbytes),file);fclose(file);}
        rb->Unmap(0,&written);
    }
}
