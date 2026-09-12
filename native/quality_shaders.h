#pragma once

// Area coverage for reductions; bilinear reconstruction for enlargements.
// Filtering uses the same SDR proxy encoding as NR/SR, including alpha.
static const char kScaleHlsl4[] = R"hlsl(
Texture2D<float4> gSrc : register(t0);
RWTexture2D<float4> gDst : register(u0);
cbuffer Sizes : register(b0) { uint gDstW; uint gDstH; uint gSrcW; uint gSrcH; };
[numthreads(8,8,1)]
void CSMain(uint3 id : SV_DispatchThreadID) {
    if (id.x >= gDstW || id.y >= gDstH) return;
    int2 hi = int2(gSrcW-1,gSrcH-1);
    if (gSrcW > gDstW || gSrcH > gDstH) {
        float2 lo = float2(id.xy)*float2(gSrcW,gSrcH)/float2(gDstW,gDstH);
        float2 end = float2(id.xy+1)*float2(gSrcW,gSrcH)/float2(gDstW,gDstH);
        float4 sum = 0;
        [loop] for (int y=int(floor(lo.y)); y<int(ceil(end.y)); ++y) {
            float wy=max(0,min(end.y,float(y+1))-max(lo.y,float(y)));
            [loop] for (int x=int(floor(lo.x)); x<int(ceil(end.x)); ++x) {
                float wx=max(0,min(end.x,float(x+1))-max(lo.x,float(x)));
                sum += gSrc[clamp(int2(x,y),int2(0,0),hi)]*wx*wy;
            }
        }
        gDst[id.xy]=sum/((end.x-lo.x)*(end.y-lo.y));
        return;
    }
    float2 p=(float2(id.xy)+.5)*float2(gSrcW,gSrcH)/float2(gDstW,gDstH)-.5;
    int2 a=int2(floor(p)); float2 f=frac(p);
    float4 tl=gSrc[clamp(a,int2(0,0),hi)];
    float4 tr=gSrc[clamp(a+int2(1,0),int2(0,0),hi)];
    float4 bl=gSrc[clamp(a+int2(0,1),int2(0,0),hi)];
    float4 br=gSrc[clamp(a+int2(1,1),int2(0,0),hi)];
    gDst[id.xy]=lerp(lerp(tl,tr,f.x),lerp(bl,br,f.x),f.y);
}
)hlsl";

// Mild unsharp filter, constrained to the neighbourhood's channel range.
// No new extrema/overshoot, unchanged alpha and exactly unchanged flat areas.
static const char kSharpenHlsl[] = R"hlsl(
Texture2D<float4> gSrc : register(t0);
RWTexture2D<float4> gDst : register(u0);
cbuffer Sizes : register(b0) { uint gW; uint gH; uint unusedW; uint unusedH; };
[numthreads(8,8,1)]
void CSMain(uint3 id : SV_DispatchThreadID) {
    if(id.x>=gW || id.y>=gH) return;
    int2 p=int2(id.xy), hi=int2(gW-1,gH-1);
    float4 c=gSrc[p];
    float3 n=gSrc[clamp(p+int2(0,-1),int2(0,0),hi)].rgb;
    float3 s=gSrc[clamp(p+int2(0,1),int2(0,0),hi)].rgb;
    float3 e=gSrc[clamp(p+int2(1,0),int2(0,0),hi)].rgb;
    float3 w=gSrc[clamp(p+int2(-1,0),int2(0,0),hi)].rgb;
    float3 low=min(c.rgb,min(min(n,s),min(e,w)));
    float3 high=max(c.rgb,max(max(n,s),max(e,w)));
    float3 detail=c.rgb-(n+s+e+w)*.25;
    gDst[p]=float4(clamp(c.rgb+.3*detail,low,high),c.a);
}
)hlsl";
