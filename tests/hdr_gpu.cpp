// Run the production HDR shaders on WARP: no HDR monitor or NVIDIA runtime
// required. All comparisons use GPU readback, including FP16 quantization.
#define NOMINMAX
#include <windows.h>
#include <d3d11.h>
#include <d3dcompiler.h>
#include <wrl/client.h>
#include <DirectXPackedVector.h>
#include <array>
#include <vector>
#include <cstdio>
#include <cmath>
#include <stdexcept>
#include <cstring>
#include "../native/hdr_shaders.h"
#include "../native/hdr_display.h"
using Microsoft::WRL::ComPtr;
using namespace DirectX::PackedVector;
constexpr UINT W = 17, H = 9; // also exercises dispatch bounds
using Pixel = std::array<float, 4>;
static ComPtr<ID3D11Device> dev;
static ComPtr<ID3D11DeviceContext> ctx;
static void check(bool ok, const char *what) { if (!ok) throw std::runtime_error(what); }
static void hr(HRESULT v) { check(SUCCEEDED(v), "Direct3D call failed"); }
static ComPtr<ID3D11Texture2D> texture(DXGI_FORMAT fmt, const void *data, UINT bpp, bool output=false)
{
    D3D11_TEXTURE2D_DESC d = {};
    d.Width=W; d.Height=H; d.MipLevels=1; d.ArraySize=1; d.Format=fmt; d.SampleDesc.Count=1;
    d.BindFlags=D3D11_BIND_SHADER_RESOURCE | (output ? D3D11_BIND_UNORDERED_ACCESS : 0);
    D3D11_SUBRESOURCE_DATA init={data,W*bpp,0};
    ComPtr<ID3D11Texture2D> t; hr(dev->CreateTexture2D(&d,data ? &init : nullptr,&t)); return t;
}
static std::vector<unsigned char> read(ID3D11Texture2D *t, UINT bpp)
{
    D3D11_TEXTURE2D_DESC d; t->GetDesc(&d); d.BindFlags=0;
    d.Usage=D3D11_USAGE_STAGING; d.CPUAccessFlags=D3D11_CPU_ACCESS_READ;
    ComPtr<ID3D11Texture2D> staging; hr(dev->CreateTexture2D(&d,nullptr,&staging));
    ctx->CopyResource(staging.Get(),t);
    D3D11_MAPPED_SUBRESOURCE m; hr(ctx->Map(staging.Get(),0,D3D11_MAP_READ,0,&m));
    std::vector<unsigned char> bytes(W*H*bpp);
    for(UINT y=0;y<H;++y) memcpy(bytes.data()+y*W*bpp,(char*)m.pData+y*m.RowPitch,W*bpp);
    ctx->Unmap(staging.Get(),0); return bytes;
}
static void dispatch(const char *shader, std::vector<ID3D11Texture2D*> inputs,
                     ID3D11Texture2D *out, const void *constants)
{
    ComPtr<ID3DBlob> code,errors;
    HRESULT result=D3DCompile(shader,strlen(shader),"hdr-test",nullptr,nullptr,"CSMain","cs_5_0",
                             D3DCOMPILE_WARNINGS_ARE_ERRORS,0,&code,&errors);
    if(FAILED(result) && errors) fprintf(stderr,"%s\n",(char*)errors->GetBufferPointer());
    hr(result);
    ComPtr<ID3D11ComputeShader> cs; hr(dev->CreateComputeShader(code->GetBufferPointer(),code->GetBufferSize(),nullptr,&cs));
    std::vector<ComPtr<ID3D11ShaderResourceView>> views(inputs.size());
    std::vector<ID3D11ShaderResourceView*> ptrs;
    for(size_t i=0;i<inputs.size();++i) { hr(dev->CreateShaderResourceView(inputs[i],nullptr,&views[i])); ptrs.push_back(views[i].Get()); }
    ComPtr<ID3D11UnorderedAccessView> uav; hr(dev->CreateUnorderedAccessView(out,nullptr,&uav));
    D3D11_BUFFER_DESC bd={}; bd.ByteWidth=16; bd.Usage=D3D11_USAGE_DEFAULT; bd.BindFlags=D3D11_BIND_CONSTANT_BUFFER;
    D3D11_SUBRESOURCE_DATA data={constants,0,0}; ComPtr<ID3D11Buffer> cb; hr(dev->CreateBuffer(&bd,&data,&cb));
    ctx->CSSetShader(cs.Get(),nullptr,0); ctx->CSSetConstantBuffers(0,1,cb.GetAddressOf());
    ctx->CSSetShaderResources(0,(UINT)ptrs.size(),ptrs.data());
    ctx->CSSetUnorderedAccessViews(0,1,uav.GetAddressOf(),nullptr);
    ctx->Dispatch((W+7)/8,(H+7)/8,1);
    ctx->ClearState();
}
int main()
{
    try {
        D3D_FEATURE_LEVEL fl=D3D_FEATURE_LEVEL_11_0;
        hr(D3D11CreateDevice(nullptr,D3D_DRIVER_TYPE_WARP,nullptr,0,&fl,1,D3D11_SDK_VERSION,&dev,nullptr,&ctx));
        std::vector<HALF> raw(W*H*4);
        const Pixel samples[]={{0,0,0,1},{.001f,.001f,.001f,1},{.18f,.18f,.18f,1},
            {1,1,1,1},{2.5f,2.5f,2.5f,1},{12.5f,12.5f,12.5f,1},{125,125,125,1},
            {-.25f,4,1,1},{10,.01f,2,1},{65504,65504,65504,1}};
        for(UINT i=0;i<W*H;++i) for(UINT c=0;c<4;++c) raw[i*4+c]=XMConvertFloatToHalf(samples[i%10][c]);
        auto native=texture(DXGI_FORMAT_R16G16B16A16_FLOAT,raw.data(),8);
        auto proxy=texture(DXGI_FORMAT_R8G8B8A8_UNORM,nullptr,4,true);
        struct { UINT fp; float white; UINT pad[2]; } cap={1,2.5f,{}};
        dispatch(kHdrCaptureHlsl,{native.Get()},proxy.Get(),&cap);
        auto proxyBytes=read(proxy.Get(),4);
        check(proxyBytes[0]==0 && proxyBytes[1]==0,"black lifted in capture");
        check(proxyBytes[4*3] < proxyBytes[4*4] && proxyBytes[4*4] < proxyBytes[4*5] &&
              proxyBytes[4*5] < proxyBytes[4*6],"HDR highlights clipped in proxy");
        check(proxyBytes[4*7]==0,"negative gamut must be clamped only in SDR proxy");
        auto result=texture(DXGI_FORMAT_R16G16B16A16_FLOAT,nullptr,8,true);
        struct { float white; UINT bypass,split,hdr; } comp={2.5f,0,UINT_MAX,1};
        dispatch(kHdrCompositeHlsl,{native.Get(),proxy.Get(),proxy.Get()},result.Get(),&comp);
        auto unchanged=read(result.Get(),8);
        check(memcmp(unchanged.data(),raw.data(),unchanged.size())==0,"zero edit must preserve original FP16 bit for bit");
        auto edits=proxyBytes;
        for(UINT i=0;i<W*H;++i) for(UINT c=0;c<3;++c) edits[i*4+c]=255;
        auto edited=texture(DXGI_FORMAT_R8G8B8A8_UNORM,edits.data(),4);
        comp.bypass=1;
        dispatch(kHdrCompositeHlsl,{native.Get(),proxy.Get(),edited.Get()},result.Get(),&comp);
        auto bypass=read(result.Get(),8);
        check(memcmp(bypass.data(),raw.data(),bypass.size())==0,"bypass changed HDR original");
        comp.bypass=0; comp.split=8;
        dispatch(kHdrCompositeHlsl,{native.Get(),proxy.Get(),edited.Get()},result.Get(),&comp);
        auto split=read(result.Get(),8);
        for(UINT y=0;y<H;++y) check(memcmp(split.data()+y*W*8,raw.data()+y*W*4,8*8)==0,"wipe changed raw side");
        const HALF *values=(const HALF*)split.data();
        bool changed=false;
        for(UINT i=0;i<W*H*4;++i) {
            float f=XMConvertHalfToFloat(values[i]);
            check(std::isfinite(f),"HDR composite produced NaN/Inf");
            if(i%4<3 && values[i]!=raw[i]) changed=true;
        }
        check(changed,"neural edit was discarded");
        comp.hdr=0; comp.split=UINT_MAX;
        dispatch(kHdrCompositeHlsl,{native.Get(),proxy.Get(),edited.Get()},result.Get(),&comp);
        auto sdr=read(result.Get(),8);
        for(UINT i=0;i<W*H*4;++i) check(XMConvertHalfToFloat(((HALF*)sdr.data())[i])==1.0f,"SDR output must be linear unit white");
        // The old BGRA path must remain a channel-correct, byte-exact copy.
        std::vector<unsigned char> bgra(W*H*4);
        for(UINT i=0;i<W*H;++i) {bgra[i*4]=17; bgra[i*4+1]=93; bgra[i*4+2]=201; bgra[i*4+3]=255;}
        auto sdrInput=texture(DXGI_FORMAT_B8G8R8A8_UNORM,bgra.data(),4);
        cap.fp=0; dispatch(kHdrCaptureHlsl,{sdrInput.Get()},proxy.Get(),&cap);
        auto rgba=read(proxy.Get(),4);
        for(UINT i=0;i<W*H;++i) check(rgba[i*4]==201 && rgba[i*4+1]==93 && rgba[i*4+2]==17 && rgba[i*4+3]==255,"SDR channel regression");
        auto display=QueryHdrDisplay(MonitorFromPoint(POINT{0,0},MONITOR_DEFAULTTOPRIMARY));
        printf("Display probe: HDR=%d, SDR white=%.1f nits\n",display.enabled,display.white*80);
        puts("PASS: HDR shader compilation, highlights, signed gamut, zero-edit identity, bypass, wipe, finite edits, SDR output and channel order (WARP)");
        return 0;
    } catch(const std::exception &e) { fprintf(stderr,"FAIL: %s\n",e.what()); return 1; }
}
