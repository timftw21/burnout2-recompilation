#version 450

layout(location = 0) in vec2 in_position;
layout(location = 1) in vec4 in_color;
layout(location = 2) in vec2 in_uv;

layout(location = 0) out vec4 frag_color;
layout(location = 1) out vec2 frag_uv;

void main() {
    gl_Position = vec4(in_position, 0.0, 1.0);
    frag_color = in_color;
    frag_uv = in_uv;
}
