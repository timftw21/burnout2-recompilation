#version 450

invariant gl_Position;

layout(location = 0) in vec4 in_position;
layout(location = 1) in vec4 in_color;
layout(location = 2) in vec4 in_uv;
layout(location = 3) in vec4 in_secondary_color;
layout(location = 4) in float in_fog;
layout(location = 5) in vec4 in_uv0;
layout(location = 6) in vec4 in_uv1;
layout(location = 7) in vec4 in_uv2;
layout(location = 8) in vec4 in_uv3;

layout(location = 0) out vec4 frag_color;
layout(location = 1) out vec4 frag_uv;
layout(location = 2) out vec4 frag_secondary_color;
layout(location = 3) out float frag_fog;
layout(location = 4) out vec4 frag_uv1;
layout(location = 5) out vec4 frag_uv2;
layout(location = 6) out vec4 frag_uv3;

struct VertexProgramState {
    uint enabled;
    uint transform_program_start;
    uint target_width;
    uint target_height;
    uint texture_linear;
    uint texture_width;
    uint texture_height;
    uint specular_enable;
    uint fog_mode;
    uint fog_enable;
    uint fog_parameter0;
    uint fog_parameter1;
    uint vertex_stride_words;
    uint program_input_word_offset;
    uint texture_output;
    uint raw_attribute_fetch;
    uvec4 transform_program[136];
    uvec4 transform_constants[192];
    uvec4 raw_attribute_formats[4];
    uvec4 raw_attribute_base_offsets[4];
    uvec4 current_vertex_attributes[16];
    uint raw_source_index_base;
    uint reserved1;
    uint reserved2;
    uint reserved3;
    uvec4 texture_linears;
    uvec4 texture_widths;
    uvec4 texture_heights;
};

layout(std430, set = 0, binding = 9) readonly buffer VertexProgramStates {
    VertexProgramState states[];
} vertex_program_states;

layout(std430, set = 0, binding = 10) readonly buffer VertexWords {
    uint words[];
} vertex_words;

layout(std430, set = 0, binding = 11) readonly buffer RawVertexWords {
    uint words[];
} raw_vertex_resource_words;

layout(push_constant) uniform DrawPushConstants {
    uint state_index;
} draw_push;

uint field_value(uint value, uint start, uint length) {
    return (value >> start) & ((1u << length) - 1u);
}

bool finite_value(float value) {
    return !isnan(value) && !isinf(value);
}

vec4 swizzle_value(vec4 value, uint packed, bool negate) {
    vec4 result;
    for (uint lane = 0u; lane < 4u; ++lane) {
        uint shift = 6u - lane * 2u;
        result[lane] = value[(packed >> shift) & 3u];
    }
    return negate ? -result : result;
}

vec4 execute_operation(uint opcode, vec4 a, vec4 b, vec4 c) {
    precise vec4 result = vec4(0.0);
    switch (opcode) {
    case 1u:
        result = a;
        break;
    case 2u:
        result = a * b;
        break;
    case 3u:
        result = a + b;
        break;
    case 4u:
        result = a * b + c;
        break;
    case 5u: {
        precise float value = a.x * b.x + a.y * b.y + a.z * b.z;
        result = vec4(value);
        break;
    }
    case 6u: {
        precise float value = a.x * b.x + a.y * b.y + a.z * b.z + b.w;
        result = vec4(value);
        break;
    }
    case 7u: {
        precise float value = a.x * b.x + a.y * b.y + a.z * b.z
            + a.w * b.w;
        result = vec4(value);
        break;
    }
    case 8u:
        result = vec4(1.0, a.y * b.y, a.z, b.w);
        break;
    case 9u:
        result = vec4(
            b.x < a.x ? b.x : a.x,
            b.y < a.y ? b.y : a.y,
            b.z < a.z ? b.z : a.z,
            b.w < a.w ? b.w : a.w);
        break;
    case 10u:
        result = vec4(
            a.x < b.x ? b.x : a.x,
            a.y < b.y ? b.y : a.y,
            a.z < b.z ? b.z : a.z,
            a.w < b.w ? b.w : a.w);
        break;
    case 11u:
        result = vec4(
            a.x < b.x ? 1.0 : 0.0,
            a.y < b.y ? 1.0 : 0.0,
            a.z < b.z ? 1.0 : 0.0,
            a.w < b.w ? 1.0 : 0.0);
        break;
    case 12u:
        result = vec4(
            a.x >= b.x ? 1.0 : 0.0,
            a.y >= b.y ? 1.0 : 0.0,
            a.z >= b.z ? 1.0 : 0.0,
            a.w >= b.w ? 1.0 : 0.0);
        break;
    case 13u:
        result = vec4(floor(a.x + 0.001));
        break;
    case 14u:
        result = vec4(1.0 / a.x);
        break;
    case 15u: {
        precise float reciprocal = 1.0 / a.x;
        precise float clamped_reciprocal = reciprocal < -1.884467e19
            ? -1.884467e19
            : 1.884467e19 < reciprocal
                ? 1.884467e19
                : reciprocal;
        result = vec4(clamped_reciprocal);
        break;
    }
    case 16u:
        result = vec4(1.0 / sqrt(abs(a.x)));
        break;
    case 17u: {
        float base = floor(a.x);
        result = vec4(exp2(base), a.x - base, exp2(a.x), 1.0);
        break;
    }
    case 18u: {
        float value = abs(a.x);
        float exponent = floor(log2(value));
        result = vec4(
            exponent, value / exp2(exponent), log2(value), 1.0);
        break;
    }
    case 19u: {
        precise float lit_x = a.x < 0.0 ? 0.0 : a.x;
        precise float lit_y = a.y < 0.0 ? 0.0 : a.y;
        result = vec4(
            1.0,
            lit_x,
            a.x > 0.0 ? pow(lit_y, a.w) : 0.0,
            1.0);
        break;
    }
    default:
        break;
    }
    return result;
}

void write_mask(inout vec4 destination, vec4 value, uint mask) {
    if ((mask & 8u) != 0u) destination.x = value.x;
    if ((mask & 4u) != 0u) destination.y = value.y;
    if ((mask & 2u) != 0u) destination.z = value.z;
    if ((mask & 1u) != 0u) destination.w = value.w;
}

uint raw_vertex_byte(uint byte_offset) {
    uint packed = raw_vertex_resource_words.words[byte_offset >> 2u];
    return (packed >> ((byte_offset & 3u) * 8u)) & 0xFFu;
}

uint raw_vertex_u16(uint byte_offset) {
    return raw_vertex_byte(byte_offset)
        | (raw_vertex_byte(byte_offset + 1u) << 8u);
}

uint raw_vertex_u32(uint byte_offset) {
    if ((byte_offset & 3u) == 0u) {
        return raw_vertex_resource_words.words[byte_offset >> 2u];
    }
    return raw_vertex_byte(byte_offset)
        | (raw_vertex_byte(byte_offset + 1u) << 8u)
        | (raw_vertex_byte(byte_offset + 2u) << 16u)
        | (raw_vertex_byte(byte_offset + 3u) << 24u);
}

int sign_extend_raw_vertex_component(uint value, uint bits) {
    uint shift = 32u - bits;
    return int(value << shift) >> int(shift);
}

vec4 decode_raw_vertex_attribute(
    uint byte_offset,
    uint format_raw,
    vec4 current_value) {
    uint type = format_raw & 0xFu;
    uint components = (format_raw >> 4u) & 0xFu;
    vec4 value = current_value;
    if (type == 2u) {
        for (uint component = 0u; component < 4u; ++component) {
            if (component < components) {
                value[component] = uintBitsToFloat(raw_vertex_u32(
                    byte_offset + component * 4u));
            }
        }
    } else if (type == 0u) {
        value = vec4(
            float(raw_vertex_byte(byte_offset + 2u)) / 255.0,
            float(raw_vertex_byte(byte_offset + 1u)) / 255.0,
            float(raw_vertex_byte(byte_offset)) / 255.0,
            float(raw_vertex_byte(byte_offset + 3u)) / 255.0);
    } else if (type == 4u) {
        for (uint component = 0u; component < 4u; ++component) {
            if (component < components) {
                value[component] = float(raw_vertex_byte(
                    byte_offset + component)) / 255.0;
            }
        }
    } else if (type == 1u || type == 5u) {
        for (uint component = 0u; component < 4u; ++component) {
            if (component < components) {
                uint raw = raw_vertex_u16(byte_offset + component * 2u);
                int signed_value = raw >= 0x8000u
                    ? int(raw) - 0x10000
                    : int(raw);
                value[component] = max(
                    -1.0, float(signed_value) / 32767.0);
            }
        }
    } else if (type == 6u) {
        uint packed = raw_vertex_u32(byte_offset);
        value.x = max(
            -1.0,
            float(sign_extend_raw_vertex_component(
                packed & 0x7FFu, 11u)) / 1023.0);
        value.y = max(
            -1.0,
            float(sign_extend_raw_vertex_component(
                (packed >> 11u) & 0x7FFu, 11u)) / 1023.0);
        value.z = max(
            -1.0,
            float(sign_extend_raw_vertex_component(
                (packed >> 22u) & 0x3FFu, 10u)) / 511.0);
    }
    return value;
}

vec4 fetch_source(
    uint type,
    uint temporary_index,
    uint input_index,
    uint constant_index,
    uint packed_swizzle,
    bool negate,
    bool relative,
    float address,
    vec4 inputs[16],
    vec4 temporary[12],
    vec4 outputs[13]) {
    vec4 value = vec4(0.0);
    if (type == 1u) {
        value = temporary_index == 12u
            ? outputs[0]
            : temporary[temporary_index];
    } else if (type == 2u) {
        value = inputs[input_index];
    } else if (type == 3u) {
        int index = int(constant_index)
            + (relative ? int(address) : 0);
        if (index >= 0 && index < 192) {
            value = uintBitsToFloat(
                vertex_program_states.states[draw_push.state_index]
                    .transform_constants[index]);
        }
    }
    return swizzle_value(value, packed_swizzle, negate);
}

uint ilu_operation(uint opcode) {
    const uint operations[8] = uint[8](0u, 1u, 14u, 15u, 16u, 17u, 18u, 19u);
    return operations[opcode];
}

bool execute_vertex_program(
    vec4 inputs[16],
    out vec4 outputs[13],
    out uint output_masks[13]) {
    vec4 temporary[12];
    for (uint index = 0u; index < 12u; ++index) {
        temporary[index] = vec4(0.0);
    }
    for (uint index = 0u; index < 13u; ++index) {
        outputs[index] = vec4(0.0);
        output_masks[index] = 0u;
    }
    float address = 0.0;
    uint start = vertex_program_states.states[draw_push.state_index]
        .transform_program_start;
    for (uint instruction = start; instruction < 136u; ++instruction) {
        uvec4 token = vertex_program_states.states[draw_push.state_index]
            .transform_program[instruction];
        uint mac_opcode = field_value(token.y, 21u, 4u);
        uint ilu_opcode = field_value(token.y, 25u, 3u);
        if (mac_opcode > 13u || ilu_opcode > 7u) {
            return false;
        }
        uint input_index = field_value(token.y, 9u, 4u);
        uint constant_index = field_value(token.y, 13u, 8u);
        bool relative = field_value(token.w, 1u, 1u) != 0u;
        vec4 a = fetch_source(
            field_value(token.z, 26u, 2u),
            field_value(token.z, 28u, 4u),
            input_index,
            constant_index,
            field_value(token.y, 0u, 8u),
            field_value(token.y, 8u, 1u) != 0u,
            relative,
            address,
            inputs,
            temporary,
            outputs);
        vec4 b = fetch_source(
            field_value(token.z, 11u, 2u),
            field_value(token.z, 13u, 4u),
            input_index,
            constant_index,
            field_value(token.z, 17u, 8u),
            field_value(token.z, 25u, 1u) != 0u,
            relative,
            address,
            inputs,
            temporary,
            outputs);
        uint c_register = field_value(token.z, 0u, 2u) * 4u
            + field_value(token.w, 30u, 2u);
        vec4 c = fetch_source(
            field_value(token.w, 28u, 2u),
            c_register,
            input_index,
            constant_index,
            field_value(token.z, 2u, 8u),
            field_value(token.z, 10u, 1u) != 0u,
            relative,
            address,
            inputs,
            temporary,
            outputs);

        precise vec4 mac = mac_opcode == 3u
            ? execute_operation(mac_opcode, a, c, vec4(0.0))
            : execute_operation(mac_opcode, a, b, c);
        precise vec4 ilu = execute_operation(
            ilu_operation(ilu_opcode), c, vec4(0.0), vec4(0.0));

        uint temporary_index = field_value(token.w, 20u, 4u);
        uint mac_mask = field_value(token.w, 24u, 4u);
        uint ilu_mask = field_value(token.w, 16u, 4u);
        if (mac_opcode == 13u) {
            address = mac.x;
        } else if (mac_mask != 0u
                   && !(ilu_opcode != 0u && temporary_index == 1u)) {
            if (temporary_index == 12u) {
                write_mask(outputs[0], mac, mac_mask);
                output_masks[0] |= mac_mask;
            } else if (temporary_index < 12u) {
                write_mask(temporary[temporary_index], mac, mac_mask);
            }
        }
        if (ilu_mask != 0u) {
            uint ilu_temporary = mac_opcode != 0u ? 1u : temporary_index;
            if (ilu_temporary == 12u) {
                write_mask(outputs[0], ilu, ilu_mask);
                output_masks[0] |= ilu_mask;
            } else if (ilu_temporary < 12u) {
                write_mask(temporary[ilu_temporary], ilu, ilu_mask);
            }
        }

        uint output_mask = field_value(token.w, 12u, 4u);
        if (output_mask != 0u) {
            // Compatibility filtering keeps context-constant writes on CPU.
            if (field_value(token.w, 11u, 1u) == 0u) {
                return false;
            }
            uint output_index = field_value(token.w, 3u, 8u);
            if (output_index >= 13u) {
                return false;
            }
            vec4 value = field_value(token.w, 2u, 1u) != 0u ? ilu : mac;
            write_mask(outputs[output_index], value, output_mask);
            output_masks[output_index] |= output_mask;
        }
        if (field_value(token.w, 0u, 1u) != 0u) {
            return true;
        }
    }
    return false;
}

float programmable_fog_factor(float fog_distance) {
    uint state_index = draw_push.state_index;
    float parameter0 = uintBitsToFloat(
        vertex_program_states.states[state_index].fog_parameter0);
    float parameter1 = uintBitsToFloat(
        vertex_program_states.states[state_index].fog_parameter1);
    if (parameter0 == 0.0 || parameter1 == 0.0
        || !finite_value(parameter0) || !finite_value(parameter1)) {
        return 1.0;
    }
    uint mode = vertex_program_states.states[state_index].fog_mode;
    float factor;
    if (mode == 0x2601u || mode == 0x0804u) {
        factor = parameter0 + fog_distance * parameter1 - 1.0;
    } else if (mode == 0x0800u || mode == 0x0802u) {
        factor = parameter0
            + exp2(fog_distance * parameter1 * 16.0) - 1.5;
    } else if (mode == 0x0801u || mode == 0x0803u) {
        factor = parameter0
            + exp2(-fog_distance * fog_distance
                * parameter1 * parameter1 * 32.0)
            - 1.5;
    } else {
        return 1.0;
    }
    if (mode == 0x0802u || mode == 0x0803u || mode == 0x0804u) {
        factor = abs(factor);
    }
    return finite_value(factor) ? factor : 1.0;
}

void main() {
    uint state_index = draw_push.state_index;
    if (vertex_program_states.states[state_index].enabled == 0u) {
        gl_Position = in_position;
        frag_color = clamp(in_color, 0.0, 1.0);
        frag_uv = vertex_program_states.states[state_index]
                .texture_widths[0] != 0u
            ? in_uv0
            : in_uv;
        frag_uv1 = in_uv1;
        frag_uv2 = in_uv2;
        frag_uv3 = in_uv3;
        frag_secondary_color = clamp(in_secondary_color, 0.0, 1.0);
        frag_fog = in_fog;
        return;
    }

    bool raw_attribute_fetch = vertex_program_states.states[state_index]
        .raw_attribute_fetch != 0u;
    vec4 inputs[16];
    if (raw_attribute_fetch) {
        uint source_index = raw_vertex_resource_words.words[
            vertex_program_states.states[state_index].raw_source_index_base
                + uint(gl_VertexIndex)];
        for (uint input_index = 0u; input_index < 16u; ++input_index) {
            inputs[input_index] = uintBitsToFloat(
                vertex_program_states.states[state_index]
                    .current_vertex_attributes[input_index]);
            uint row = input_index >> 2u;
            uint lane = input_index & 3u;
            uint format_raw = vertex_program_states.states[state_index]
                .raw_attribute_formats[row][lane];
            if (((format_raw >> 4u) & 0xFu) != 0u) {
                uint byte_offset = vertex_program_states.states[state_index]
                    .raw_attribute_base_offsets[row][lane]
                    + source_index * (format_raw >> 8u);
                inputs[input_index] = decode_raw_vertex_attribute(
                    byte_offset, format_raw, inputs[input_index]);
            }
        }
    } else {
        uint vertex_base = uint(gl_VertexIndex)
            * vertex_program_states.states[state_index].vertex_stride_words
            + vertex_program_states.states[state_index]
                .program_input_word_offset;
        for (uint input_index = 0u; input_index < 16u; ++input_index) {
            uint input_base = vertex_base + input_index * 4u;
            inputs[input_index] = uintBitsToFloat(uvec4(
                vertex_words.words[input_base],
                vertex_words.words[input_base + 1u],
                vertex_words.words[input_base + 2u],
                vertex_words.words[input_base + 3u]));
        }
    }

    vec4 outputs[13];
    uint output_masks[13];
    bool program_valid = execute_vertex_program(
        inputs, outputs, output_masks);

    vec4 position = raw_attribute_fetch ? inputs[0] : in_position;
    vec4 color = raw_attribute_fetch ? inputs[3] : in_color;
    vec4 secondary_color = raw_attribute_fetch
        ? inputs[4]
        : in_secondary_color;
    vec4 textures[4];
    textures[0] = raw_attribute_fetch ? inputs[9] : in_uv0;
    textures[1] = raw_attribute_fetch ? inputs[10] : in_uv1;
    textures[2] = raw_attribute_fetch ? inputs[11] : in_uv2;
    textures[3] = raw_attribute_fetch ? inputs[12] : in_uv3;
    float fog = raw_attribute_fetch ? 1.0 : in_fog;
    bool program_position_valid = false;
    if (program_valid) {
        bool position_xyz_written = (output_masks[0] & 14u) == 14u;
        bool position_w_written = (output_masks[0] & 1u) != 0u;
        bool position_w_usable = position_w_written
            && finite_value(outputs[0].w)
            && abs(outputs[0].w) > 0.000001;
        if ((output_masks[0] & 12u) == 12u
            && finite_value(outputs[0].x)
            && finite_value(outputs[0].y)) {
            position.x = outputs[0].x;
            position.y = outputs[0].y;
            if ((output_masks[0] & 2u) != 0u) {
                float depth_scale = uintBitsToFloat(
                    vertex_program_states.states[state_index]
                        .transform_constants[58].z);
                position.z = finite_value(depth_scale)
                        && abs(depth_scale) > 0.000001
                    ? outputs[0].z / depth_scale
                    : outputs[0].z;
            }
            if (position_w_usable) {
                position.w = outputs[0].w;
            } else if (position_xyz_written) {
                // A missing, non-finite, or zero homogeneous component does
                // not invalidate otherwise complete screen-space oPos.xyz.
                // Default it instead of taking the z=0 fallback.
                position.w = 1.0;
            }
            program_position_valid = position_xyz_written
                && finite_value(position.z)
                && finite_value(position.w);
        }

        color = vec4(0.0, 0.0, 0.0, 1.0);
        if (output_masks[3] != 0u) {
            write_mask(color, outputs[3], output_masks[3]);
        }
        secondary_color = vec4(0.0, 0.0, 0.0, 1.0);
        if (vertex_program_states.states[state_index].specular_enable != 0u
            && output_masks[4] != 0u) {
            write_mask(secondary_color, outputs[4], output_masks[4]);
        }
        if (vertex_program_states.states[state_index].fog_enable != 0u) {
            uint fog_mask = output_masks[5];
            float fog_distance = (fog_mask & 8u) != 0u
                ? outputs[5].x
                : (fog_mask & 4u) != 0u
                    ? outputs[5].y
                    : (fog_mask & 2u) != 0u
                        ? outputs[5].z
                        : (fog_mask & 1u) != 0u
                            ? outputs[5].w
                            : 0.0;
            fog = programmable_fog_factor(fog_distance);
        }
        for (uint stage = 0u; stage < 4u; ++stage) {
            uint texture_output = 9u + stage;
            if (output_masks[texture_output] != 0u) {
                write_mask(textures[stage], outputs[texture_output],
                    output_masks[texture_output]);
            }
        }
    }

    for (uint stage = 0u; stage < 4u; ++stage) {
        if (vertex_program_states.states[state_index].texture_linears[stage] != 0u
            && vertex_program_states.states[state_index].texture_widths[stage] != 0u
            && vertex_program_states.states[state_index].texture_heights[stage] != 0u) {
            textures[stage].x /= float(
                vertex_program_states.states[state_index].texture_widths[stage]);
            textures[stage].y /= float(
                vertex_program_states.states[state_index].texture_heights[stage]);
        }
    }

    if (program_position_valid) {
        // NV2A viewport constants target its 0.53125 subpixel center. Move
        // transformed screen coordinates onto Vulkan's 0.5 pixel centers.
        position.xy -= vec2(0.03125);
    }
    position.x = position.x * 2.0
        / float(vertex_program_states.states[state_index].target_width) - 1.0;
    position.y = position.y * 2.0
        / float(vertex_program_states.states[state_index].target_height) - 1.0;
    if (program_position_valid) {
        position.xyz *= position.w;
    } else {
        position.z = 0.0;
        position.w = 1.0;
    }

    gl_Position = position;
    frag_color = clamp(color, 0.0, 1.0);
    frag_uv = textures[0];
    frag_uv1 = textures[1];
    frag_uv2 = textures[2];
    frag_uv3 = textures[3];
    frag_secondary_color = clamp(secondary_color, 0.0, 1.0);
    frag_fog = fog;
}
