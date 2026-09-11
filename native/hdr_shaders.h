#pragma once

// scRGB uses linear BT.709 primaries; 1.0 is 80 nits. The neural runtime
// continues to receive SDR. Keep its quantized input as the residual anchor.
#define NS_HDR_COLOR_FUNCTIONS \
    "float3 ToLinear(float3 x) { return float3(" \
    "x.r <= .04045 ? x.r / 12.92 : pow(max((x.r + .055) / 1.055, 0), 2.4)," \
    "x.g <= .04045 ? x.g / 12.92 : pow(max((x.g + .055) / 1.055, 0), 2.4)," \
    "x.b <= .04045 ? x.b / 12.92 : pow(max((x.b + .055) / 1.055, 0), 2.4)); }\n" \
    "float3 ToSrgb(float3 x) { x = max(x, 0); return float3(" \
    "x.r <= .0031308 ? x.r * 12.92 : 1.055 * pow(x.r, 1.0/2.4) - .055," \
    "x.g <= .0031308 ? x.g * 12.92 : 1.055 * pow(x.g, 1.0/2.4) - .055," \
    "x.b <= .0031308 ? x.b * 12.92 : 1.055 * pow(x.b, 1.0/2.4) - .055); }\n" \
    "float Peak(float3 x) { return max(0, max(x.r, max(x.g, x.b))); }\n"

static const char kHdrCaptureHlsl[] =
    NS_HDR_COLOR_FUNCTIONS
    "Texture2D<float4> src : register(t0);\n"
    "RWTexture2D<float4> dst : register(u0);\n"
    "cbuffer Params : register(b0) { uint isFloat; float white; };\n"
    "[numthreads(8,8,1)] void CSMain(uint3 p : SV_DispatchThreadID) {\n"
    " uint w,h; dst.GetDimensions(w,h); if(p.x>=w || p.y>=h) return;\n"
    " float4 c=src.Load(int3(p.xy,0));\n"
    " if(isFloat) c=float4(ToSrgb(max(c.rgb,0)/(white+Peak(c.rgb))),1);\n"
    " dst[p.xy]=c; }\n";

static const char kHdrCompositeHlsl[] =
    NS_HDR_COLOR_FUNCTIONS
    "Texture2D<float4> nativeFrame : register(t0);\n"
    "Texture2D<float4> proxyIn : register(t1);\n"
    "Texture2D<float4> proxyOut : register(t2);\n"
    "RWTexture2D<float4> dst : register(u0);\n"
    "cbuffer Params : register(b0) { float white; uint bypass; uint split; uint hdrDisplay; };\n"
    "[numthreads(8,8,1)] void CSMain(uint3 p : SV_DispatchThreadID) {\n"
    " uint w,h; dst.GetDimensions(w,h); if(p.x>=w || p.y>=h) return;\n"
    " float3 original=nativeFrame.Load(int3(p.xy,0)).rgb;\n"
    " float3 a=ToLinear(proxyIn.Load(int3(p.xy,0)).rgb);\n"
    " float3 b=ToLinear(proxyOut.Load(int3(p.xy,0)).rgb);\n"
    " bool raw=bypass || (split!=0xffffffff && p.x<split);\n"
    // No inverse tone mapping: it becomes singular near white. Lift a bounded
    // linear residual using the same scale as capture, preserving signed gamut
    // and highlights exactly when the neural edit is zero.
    " float3 result=raw ? original : original+(white+Peak(original))*clamp(b-a,-.25,.25);\n"
    " if(!bypass && split!=0xffffffff && p.x>=split && p.x<split+2)\n"
    "   result=white*ToLinear(float3(.25,.65,1));\n"
    " if(!hdrDisplay) result=raw ? a : b;\n"
    " dst[p.xy]=float4(clamp(result,-65504,65504),1); }\n";
