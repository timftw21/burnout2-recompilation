#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#include "metal.h"

B2MetalDevice b2_metal_device(void) {
    B2MetalDevice info = {0};
    @autoreleasepool {
        // Match DXMT's first adapter: prefer the last discrete GPU, if present.
        NSArray<id<MTLDevice>> *devices = MTLCopyAllDevices();
        id<MTLDevice> device = devices.firstObject;
        for (id<MTLDevice> candidate in devices)
            if (!candidate.hasUnifiedMemory) device = candidate;
        if (device) {
            info.available = 1;
            info.apple7 = [device supportsFamily:MTLGPUFamilyApple7];
            info.mac2 = [device supportsFamily:MTLGPUFamilyMac2];
            info.supported = info.apple7 || info.mac2;
            [device.name getCString:info.name maxLength:sizeof(info.name) encoding:NSUTF8StringEncoding];
        }
    }
    return info;
}
