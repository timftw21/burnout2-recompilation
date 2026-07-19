#version 450

layout(location = 0) in vec4 in_position;
layout(location = 1) in vec4 in_color;
layout(location = 2) in vec4 in_uv;
layout(location = 3) in vec4 in_secondary_color;
layout(location = 4) in float in_fog;

layout(location = 0) out vec4 frag_color;
layout(location = 1) out vec4 frag_uv;
layout(location = 2) out vec4 frag_secondary_color;
layout(location = 3) out float frag_fog;

void main() {
    gl_Position = in_position;
    frag_color = clamp(in_color, 0.0, 1.0);
    frag_uv = in_uv;
    frag_secondary_color = clamp(in_secondary_color, 0.0, 1.0);
    frag_fog = in_fog;
}
