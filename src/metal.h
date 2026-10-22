#pragma once

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    char name[512];
    int available, apple7, mac2, supported;
} B2MetalDevice;

B2MetalDevice b2_metal_device(void);

#ifdef __cplusplus
}
#endif
