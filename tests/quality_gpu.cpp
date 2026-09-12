// Reuse the WARP shader/readback harness; exercise production quality shaders.
#define main hdr_tests_main
#include "hdr_gpu.cpp"
#undef main
#include "../native/quality_shaders.h"
#include <algorithm>

int main() {
    try {
        D3D_FEATURE_LEVEL fl=D3D_FEATURE_LEVEL_11_0;
        hr(D3D11CreateDevice(nullptr,D3D_DRIVER_TYPE_WARP,nullptr,0,&fl,1,D3D11_SDK_VERSION,&dev,nullptr,&ctx));
        std::vector<Pixel> pixels(W*H);
        for(UINT y=0;y<H;++y) for(UINT x=0;x<W;++x) {
            float c=((x+2*y)%3)==0 ? 1.f : 0.f;
            pixels[y*W+x]={c,.2f,.7f,.45f};
        }
        auto src=texture(DXGI_FORMAT_R32G32B32A32_FLOAT,pixels.data(),16);
        auto dst=texture(DXGI_FORMAT_R32G32B32A32_FLOAT,nullptr,16,true);
        // Fractional footprint 17x9 -> 5x3; verify exact area coverage on GPU.
        UINT dims[]={5,3,W,H};
        dispatch(kScaleHlsl4,{src.Get()},dst.Get(),dims);
        auto bytes=read(dst.Get(),16);auto out=reinterpret_cast<const Pixel*>(bytes.data());
        for(UINT y=0;y<3;++y) for(UINT x=0;x<5;++x) {
            double lo=x*double(W)/5, end=(x+1)*double(W)/5, expected=0;
            for(UINT yy=y*3;yy<(y+1)*3;++yy) for(UINT xx=0;xx<W;++xx)
                expected+=pixels[yy*W+xx][0]*std::max(0.,std::min(end,double(xx+1))-std::max(lo,double(xx)));
            expected/=(end-lo)*3;
            check(std::abs(out[y*W+x][0]-expected)<1e-5,"area coverage mismatch");
            check(std::abs(out[y*W+x][3]-.45f)<1e-5,"area changed flat alpha");
        }
        // 1:1 path must be identity.
        dims[0]=W;dims[1]=H;
        dispatch(kScaleHlsl4,{src.Get()},dst.Get(),dims);
        bytes=read(dst.Get(),16);out=reinterpret_cast<const Pixel*>(bytes.data());
        for(UINT i=0;i<W*H;++i) for(UINT c=0;c<4;++c)
            check(std::abs(out[i][c]-pixels[i][c])<1e-6,"1:1 scaling changed pixels");
        // A softened edge: sharpening must increase contrast without new extrema.
        for(UINT y=0;y<H;++y) for(UINT x=0;x<W;++x) {
            float v=x<6 ? .2f : x==6 ? .3f : x==7 ? .7f : .8f;
            pixels[y*W+x]={v,v,v,.45f};
        }
        src=texture(DXGI_FORMAT_R32G32B32A32_FLOAT,pixels.data(),16);
        dispatch(kSharpenHlsl,{src.Get()},dst.Get(),dims);
        bytes=read(dst.Get(),16);out=reinterpret_cast<const Pixel*>(bytes.data());
        for(UINT y=0;y<H;++y) for(UINT x=0;x<W;++x) {
            float low=pixels[y*W+x][0],high=low;
            for(int dx=-1;dx<=1;++dx) { float v=pixels[y*W+std::clamp(int(x)+dx,0,int(W)-1)][0];low=std::min(low,v);high=std::max(high,v); }
            check(out[y*W+x][0]>=low-1e-6 && out[y*W+x][0]<=high+1e-6,"sharpening overshoot");
            check(out[y*W+x][3]==.45f,"sharpening changed alpha");
        }
        check(out[7][0]-out[6][0]>.4f,"soft edge was not sharpened");
        check(out[0][0]==.2f && out[W-1][0]==.8f,"flat area changed");
        puts("PASS: GPU area reduction, 1:1 identity, sharper edges, bounded output and alpha");
        return 0;
    } catch(const std::exception &e) { fprintf(stderr,"FAIL: %s\n",e.what());return 1; }
}
