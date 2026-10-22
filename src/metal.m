#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#include "metal.h"

B2MetalDevice b2_metal_device(void) {
    B2MetalDevice info = {0};
    @autoreleasepool {
        id<MTLDevice> device = MTLCreateSystemDefaultDevice();
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
