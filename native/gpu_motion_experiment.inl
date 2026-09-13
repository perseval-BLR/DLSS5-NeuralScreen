// Isolated experiment. Gray readback stays enabled for exposure/reset detection.
// Three-level inverse-compositional Lucas-Kanade, current -> previous.
static bool GpuMotionExperiment()
{
    static const bool enabled = [] { char b[8] = {}; GetEnvironmentVariableA("NS_GPU_FLOW_EXPERIMENT", b, 8); return b[0]=='1'; }();
    return enabled;
}
static const char kFlowExperimentShader[] = R"hlsl(
Texture2D<float2> A : register(t0);
Texture2D<float2> B : register(t1);
Texture2D<float2> Coarse : register(t2);
RWTexture2D<float2> Out : register(u0);
SamplerState LinearClamp : register(s0);
cbuffer C : register(b0) { uint W,H,SrcW,SrcH,Factor,Reset,WorkW,WorkH; };
[numthreads(8,8,1)]
void Down(uint3 tid:SV_DispatchThreadID) {
 if(tid.x>=W || tid.y>=H) return;
 float2 sum=0;
 for(uint y=0;y<Factor;y++) for(uint x=0;x<Factor;x++) {
  int2 p=min(int2(tid.xy*Factor+uint2(x,y)),int2(SrcW-1,SrcH-1));
  sum+=float2(A.Load(int3(p,0)).x,B.Load(int3(p,0)).x);
 }
 Out[tid.xy]=sum/(Factor*Factor);
}
float2 samplePair(float2 p) { return A.SampleLevel(LinearClamp,(p+0.5)/float2(W,H),0); }
[numthreads(8,8,1)]
void Flow(uint3 tid:SV_DispatchThreadID) {
 if(tid.x>=W || tid.y>=H) return;
 if(Reset) { Out[tid.xy]=0; return; }
 float2 uv=(float2(tid.xy)+0.5)/float2(W,H);
 float2 v=Factor==4 ? float2(0,0) : Coarse.SampleLevel(LinearClamp,uv,0)*2;
 float gxx=0,gyy=0,gxy=0;
 for(int y=-2;y<=2;y++) for(int x=-2;x<=2;x++) {
  float2 p=float2(tid.xy)+float2(x,y);
  float gx=(samplePair(p+float2(1,0)).x-samplePair(p-float2(1,0)).x)*.5;
  float gy=(samplePair(p+float2(0,1)).x-samplePair(p-float2(0,1)).x)*.5;
  gxx+=gx*gx; gyy+=gy*gy; gxy+=gx*gy;
 }
 float det=gxx*gyy-gxy*gxy;
 if(det>1e-8) for(int iter=0;iter<6;iter++) {
  float2 b=0;
  for(int y=-2;y<=2;y++) for(int x=-2;x<=2;x++) {
   float2 p=float2(tid.xy)+float2(x,y);
   float gx=(samplePair(p+float2(1,0)).x-samplePair(p-float2(1,0)).x)*.5;
   float gy=(samplePair(p+float2(0,1)).x-samplePair(p-float2(0,1)).x)*.5;
   float residual=samplePair(p).x-samplePair(p+v).y;
   b+=float2(gx,gy)*residual;
  }
  float2 delta=float2(gyy*b.x-gxy*b.y,gxx*b.y-gxy*b.x)/det;
  v+=clamp(delta,-1.5,1.5);
 }
 Out[tid.xy]=clamp(v,-32,32);
}
[numthreads(8,8,1)]
void Expand(uint3 tid:SV_DispatchThreadID) {
 if(tid.x>=W || tid.y>=H) return;
 float2 v=A.SampleLevel(LinearClamp,(float2(tid.xy)+.5)/float2(W,H),0)*float2(W,H)/float2(SrcW,SrcH);
 Out[tid.xy]=length(v)<.5 ? float2(0,0) : v;
}
)hlsl";
struct FlowExperimentState {
    winrt::com_ptr<ID3D12RootSignature> rs;
    winrt::com_ptr<ID3D12PipelineState> down, flow, expand;
    winrt::com_ptr<ID3D12DescriptorHeap> heap;
    winrt::com_ptr<ID3D12Resource> previous, pairs[3], vectors[3];
    UINT w=0,hgt=0; bool valid=false;
};
static FlowExperimentState g_flow_exp;
static void DumpExperimentFlow(ID3D12Resource *texture)
{
 char folder[MAX_PATH]={};if(!GetEnvironmentVariableA("NS_FLOW_DUMP",folder,MAX_PATH)) return;
 auto desc=texture->GetDesc();D3D12_PLACED_SUBRESOURCE_FOOTPRINT fp={};UINT rows;UINT64 rowBytes,total;
 h.dev->GetCopyableFootprints(&desc,0,1,0,&fp,&rows,&rowBytes,&total);
 D3D12_HEAP_PROPERTIES hp={};hp.Type=D3D12_HEAP_TYPE_READBACK;
 D3D12_RESOURCE_DESC rd={};rd.Dimension=D3D12_RESOURCE_DIMENSION_BUFFER;rd.Width=total;rd.Height=1;
 rd.DepthOrArraySize=rd.MipLevels=1;rd.SampleDesc.Count=1;rd.Layout=D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
 winrt::com_ptr<ID3D12Resource> readback;
 if(FAILED(h.dev->CreateCommittedResource(&hp,D3D12_HEAP_FLAG_NONE,&rd,D3D12_RESOURCE_STATE_COPY_DEST,nullptr,__uuidof(ID3D12Resource),readback.put_void()))) return;
 if(!BeginCommands()) return;
 auto pre=Transition(texture,D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE,D3D12_RESOURCE_STATE_COPY_SOURCE);h.list->ResourceBarrier(1,&pre);
 D3D12_TEXTURE_COPY_LOCATION src={},dst={};src.pResource=texture;src.Type=D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
 dst.pResource=readback.get();dst.Type=D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;dst.PlacedFootprint=fp;
 h.list->CopyTextureRegion(&dst,0,0,0,&src,nullptr);
 auto post=Transition(texture,D3D12_RESOURCE_STATE_COPY_SOURCE,D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);h.list->ResourceBarrier(1,&post);
 if(!WaitFenceValue(h.fence,EndCommands(),30000)) return;
 void *data=nullptr;D3D12_RANGE rr={0,SIZE_T(total)};
 if(FAILED(readback->Map(0,&rr,&data))) return;
 static UINT index=0;char file[MAX_PATH];snprintf(file,sizeof(file),"%s/flow-%04u.bin",folder,index++);
 FILE *out=fopen(file,"wb");if(out) { for(UINT y=0;y<rows;y++) fwrite((BYTE*)data+fp.Offset+y*fp.Footprint.RowPitch,1,SIZE_T(rowBytes),out);fclose(out); }
 readback->Unmap(0,nullptr);
}
static bool InitFlowExperiment()
{
 auto &f=g_flow_exp;
 if(f.rs) return f.down && f.flow && f.expand && f.heap;
 D3D12_DESCRIPTOR_RANGE ranges[2]={};
 ranges[0]={D3D12_DESCRIPTOR_RANGE_TYPE_SRV,3,0,0,0};
 ranges[1]={D3D12_DESCRIPTOR_RANGE_TYPE_UAV,1,0,0,3};
 D3D12_ROOT_PARAMETER roots[2]={};
 roots[0].ParameterType=D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS;
 roots[0].Constants={0,0,8};
 roots[1].ParameterType=D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE;
 roots[1].DescriptorTable={2,ranges};
 D3D12_STATIC_SAMPLER_DESC sampler={};
 sampler.Filter=D3D12_FILTER_MIN_MAG_MIP_LINEAR;
 sampler.AddressU=sampler.AddressV=sampler.AddressW=D3D12_TEXTURE_ADDRESS_MODE_CLAMP;
 sampler.MaxLOD=D3D12_FLOAT32_MAX; sampler.ShaderVisibility=D3D12_SHADER_VISIBILITY_ALL;
 D3D12_ROOT_SIGNATURE_DESC desc={2,roots,1,&sampler,D3D12_ROOT_SIGNATURE_FLAG_NONE};
 winrt::com_ptr<ID3DBlob> code,errors;
 if(FAILED(D3D12SerializeRootSignature(&desc,D3D_ROOT_SIGNATURE_VERSION_1,code.put(),errors.put()))) return false;
 if(FAILED(h.dev->CreateRootSignature(0,code->GetBufferPointer(),code->GetBufferSize(),__uuidof(ID3D12RootSignature),f.rs.put_void()))) return false;
 const char *names[]={"Down","Flow","Expand"};
 ID3D12PipelineState **targets[]={f.down.put(),f.flow.put(),f.expand.put()};
 for(int i=0;i<3;i++) {
  code=nullptr;errors=nullptr;
  if(FAILED(D3DCompile(kFlowExperimentShader,sizeof(kFlowExperimentShader)-1,nullptr,nullptr,nullptr,names[i],"cs_5_0",D3DCOMPILE_OPTIMIZATION_LEVEL3,0,code.put(),errors.put()))) {
   Log("[gpu-flow] compile: %s",errors ? (const char*)errors->GetBufferPointer() : "failed"); return false;
  }
  D3D12_COMPUTE_PIPELINE_STATE_DESC pd={};pd.pRootSignature=f.rs.get();pd.CS={code->GetBufferPointer(),code->GetBufferSize()};
  if(FAILED(h.dev->CreateComputePipelineState(&pd,__uuidof(ID3D12PipelineState),(void**)targets[i]))) return false;
 }
 D3D12_DESCRIPTOR_HEAP_DESC hd={D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV,28,D3D12_DESCRIPTOR_HEAP_FLAG_SHADER_VISIBLE,0};
 return SUCCEEDED(h.dev->CreateDescriptorHeap(&hd,__uuidof(ID3D12DescriptorHeap),f.heap.put_void()));
}
static bool RunGpuMotionExperiment(VideoState &v,bool reset)
{
 if(!g_gray_uav || !InitFlowExperiment()) return false;
 static bool reported=false; if(!reported) { Log("[gpu-flow] experimental GPU Lucas-Kanade active");reported=true; }
 auto &f=g_flow_exp;
 const bool fresh=f.w!=g_gray_w || f.hgt!=g_gray_h;
 if(fresh) {
  f.w=g_gray_w;f.hgt=g_gray_h;f.valid=false;
  f.previous.attach(MakeTex(f.w,f.hgt,DXGI_FORMAT_R8_UNORM,false));
  for(int i=0;i<3;i++) {
   UINT factor=4u>>i;
   f.pairs[i].attach(MakeTex((f.w+factor-1)/factor,(f.hgt+factor-1)/factor,DXGI_FORMAT_R16G16_FLOAT,true));
   f.vectors[i].attach(MakeTex((f.w+factor-1)/factor,(f.hgt+factor-1)/factor,DXGI_FORMAT_R16G16_FLOAT,true));
   if(!f.pairs[i] || !f.vectors[i]) return false;
  }
  if(!f.previous) return false;
 }
 if(!BeginCommands()) return false;
 auto barrier=[](ID3D12Resource *r,D3D12_RESOURCE_STATES a,D3D12_RESOURCE_STATES b) {
  auto t=Transition(r,a,b);h.list->ResourceBarrier(1,&t);
 };
 barrier(g_gray_uav,D3D12_RESOURCE_STATE_UNORDERED_ACCESS,D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
 if(!f.valid) {
  barrier(g_gray_uav,D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE,D3D12_RESOURCE_STATE_COPY_SOURCE);
  barrier(f.previous.get(),fresh?D3D12_RESOURCE_STATE_COMMON:D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE,D3D12_RESOURCE_STATE_COPY_DEST);
  h.list->CopyResource(f.previous.get(),g_gray_uav);
  barrier(f.previous.get(),D3D12_RESOURCE_STATE_COPY_DEST,D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
  barrier(g_gray_uav,D3D12_RESOURCE_STATE_COPY_SOURCE,D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
 }
 ID3D12DescriptorHeap *heaps[]={f.heap.get()};h.list->SetDescriptorHeaps(1,heaps);h.list->SetComputeRootSignature(f.rs.get());
 UINT stride=h.dev->GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV),slot=0;
 auto dispatch=[&](ID3D12PipelineState *pso,ID3D12Resource *a,ID3D12Resource *b,ID3D12Resource *c,ID3D12Resource *out,UINT w,UINT ht,UINT factor) {
  auto cpu=f.heap->GetCPUDescriptorHandleForHeapStart();cpu.ptr+=SIZE_T(slot)*4*stride;
  auto gpu=f.heap->GetGPUDescriptorHandleForHeapStart();gpu.ptr+=UINT64(slot++)*4*stride;
  for(auto tex:{a,b,c}) {
   D3D12_SHADER_RESOURCE_VIEW_DESC sd={};sd.Format=tex->GetDesc().Format;sd.ViewDimension=D3D12_SRV_DIMENSION_TEXTURE2D;
   sd.Shader4ComponentMapping=D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING;sd.Texture2D.MipLevels=1;
   h.dev->CreateShaderResourceView(tex,&sd,cpu);cpu.ptr+=stride;
  }
  D3D12_UNORDERED_ACCESS_VIEW_DESC ud={};ud.Format=out->GetDesc().Format;ud.ViewDimension=D3D12_UAV_DIMENSION_TEXTURE2D;
  h.dev->CreateUnorderedAccessView(out,nullptr,&ud,cpu);
  UINT constants[]={w,ht,f.w,f.hgt,factor,UINT(reset || !f.valid),v.w,v.hgt};
  h.list->SetPipelineState(pso);h.list->SetComputeRoot32BitConstants(0,8,constants,0);h.list->SetComputeRootDescriptorTable(1,gpu);
  h.list->Dispatch((w+7)/8,(ht+7)/8,1);
 };
 for(int i=0;i<3;i++) {
  UINT factor=4u>>i,w=(f.w+factor-1)/factor,ht=(f.hgt+factor-1)/factor;
  auto pair=f.pairs[i].get(),flow=f.vectors[i].get();
  auto state=fresh?D3D12_RESOURCE_STATE_COMMON:D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE;
  barrier(pair,state,D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
  dispatch(f.down.get(),g_gray_uav,f.previous.get(),g_gray_uav,pair,w,ht,factor);
  barrier(pair,D3D12_RESOURCE_STATE_UNORDERED_ACCESS,D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
  barrier(flow,state,D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
  dispatch(f.flow.get(),pair,pair,i?f.vectors[i-1].get():pair,flow,w,ht,factor);
  barrier(flow,D3D12_RESOURCE_STATE_UNORDERED_ACCESS,D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
 }
 barrier(v.mv.tex,v.inputs_ready?D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE:D3D12_RESOURCE_STATE_COPY_DEST,D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
 dispatch(f.expand.get(),f.vectors[2].get(),f.vectors[2].get(),f.vectors[2].get(),v.mv.tex,v.w,v.hgt,1);
 barrier(v.mv.tex,D3D12_RESOURCE_STATE_UNORDERED_ACCESS,D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
 barrier(g_gray_uav,D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE,D3D12_RESOURCE_STATE_COPY_SOURCE);
 barrier(f.previous.get(),D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE,D3D12_RESOURCE_STATE_COPY_DEST);
 h.list->CopyResource(f.previous.get(),g_gray_uav);
 barrier(f.previous.get(),D3D12_RESOURCE_STATE_COPY_DEST,D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
 barrier(g_gray_uav,D3D12_RESOURCE_STATE_COPY_SOURCE,D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
 auto value=EndCommands();v.inputs_ready=true;f.valid=true;
 const bool ok=WaitFenceValue(h.fence,value,30000);
 if(ok) DumpExperimentFlow(f.vectors[2].get());
 return ok;
}
