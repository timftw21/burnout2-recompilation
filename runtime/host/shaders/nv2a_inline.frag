#version 450

layout(set = 0, binding = 0) uniform sampler2D texture0;

struct NativeFragmentState {
    uint alpha_test_enable;
    uint alpha_function;
    uint alpha_reference;
    uint texture_mode;
    uint texture_alpha_kill;
    uint texture_opaque_alpha;
    uint texture_stage;
    uint combiner_control;
    uint shader_stage_program;
    uint combiner_color_inputs[8];
    uint combiner_color_outputs[8];
    uint combiner_alpha_inputs[8];
    uint combiner_alpha_outputs[8];
    uint combiner_factors0[8];
    uint combiner_factors1[8];
    uint final_combiner_inputs0;
    uint final_combiner_inputs1;
    uint final_combiner_factor0;
    uint final_combiner_factor1;
    uint fog_color;
    uint fog_enable;
};

layout(std430, set = 0, binding = 1) readonly buffer DrawStates {
    NativeFragmentState states[];
} draw_states;

layout(push_constant) uniform DrawStateIndex {
    uint state_index;
} draw;

layout(location = 0) in vec4 frag_color;
layout(location = 1) in vec4 frag_uv;
layout(location = 2) in vec4 frag_secondary_color;
layout(location = 3) in float frag_fog;
layout(location = 0) out vec4 out_color;

vec4 unpackArgb(uint packed) {
    return vec4(
        float((packed >> 16u) & 255u),
        float((packed >> 8u) & 255u),
        float(packed & 255u),
        float((packed >> 24u) & 255u)) / 255.0;
}

vec4 unpackFogColor(uint packed) {
    return vec4(
        float(packed & 255u),
        float((packed >> 8u) & 255u),
        float((packed >> 16u) & 255u),
        float((packed >> 24u) & 255u)) / 255.0;
}

vec4 readRegister(
    uint register_index,
    uint stage,
    bool final_stage,
    vec4 registers[16],
    vec4 ef_product,
    uint state_index) {
    NativeFragmentState state = draw_states.states[state_index];
    if (register_index == 0u) {
        return vec4(0.0);
    }
    if (register_index == 1u || register_index == 2u) {
        uint packed = 0u;
        if (final_stage) {
            packed = register_index == 1u
                ? state.final_combiner_factor0
                : state.final_combiner_factor1;
        } else {
            uint flags = state.combiner_control >> 8u;
            bool unique = register_index == 1u
                ? (flags & 0x10u) != 0u
                : (flags & 0x100u) != 0u;
            uint factor_stage = unique ? min(stage, 7u) : 0u;
            packed = register_index == 1u
                ? state.combiner_factors0[factor_stage]
                : state.combiner_factors1[factor_stage];
        }
        return unpackArgb(packed);
    }
    if (register_index == 14u) {
        uint flags = state.final_combiner_inputs1 & 255u;
        vec3 v1 = (flags & 0x40u) != 0u
            ? vec3(1.0) - registers[5].rgb
            : registers[5].rgb;
        vec3 r0 = (flags & 0x20u) != 0u
            ? vec3(1.0) - registers[12].rgb
            : registers[12].rgb;
        vec3 sum = v1 + r0;
        if ((flags & 0x80u) != 0u) {
            sum = clamp(sum, 0.0, 1.0);
        }
        return vec4(sum, 0.0);
    }
    if (register_index == 15u) {
        return ef_product;
    }
    return register_index < 16u
        ? registers[register_index]
        : vec4(0.0);
}

vec3 applyInputMapping(vec3 value, uint mapping) {
    if (mapping == 0x00u) return max(value, vec3(0.0));
    if (mapping == 0x20u) return vec3(1.0) - clamp(value, 0.0, 1.0);
    if (mapping == 0x40u) return 2.0 * max(value, vec3(0.0)) - 1.0;
    if (mapping == 0x60u) return -2.0 * max(value, vec3(0.0)) + 1.0;
    if (mapping == 0x80u) return max(value, vec3(0.0)) - 0.5;
    if (mapping == 0xA0u) return -max(value, vec3(0.0)) + 0.5;
    if (mapping == 0xC0u) return value;
    return -value;
}

float applyInputMapping(float value, uint mapping) {
    if (mapping == 0x00u) return max(value, 0.0);
    if (mapping == 0x20u) return 1.0 - clamp(value, 0.0, 1.0);
    if (mapping == 0x40u) return 2.0 * max(value, 0.0) - 1.0;
    if (mapping == 0x60u) return -2.0 * max(value, 0.0) + 1.0;
    if (mapping == 0x80u) return max(value, 0.0) - 0.5;
    if (mapping == 0xA0u) return -max(value, 0.0) + 0.5;
    if (mapping == 0xC0u) return value;
    return -value;
}

vec3 colorInput(
    uint input_word,
    uint stage,
    bool final_stage,
    vec4 registers[16],
    vec4 ef_product,
    uint state_index) {
    vec4 source = readRegister(
        input_word & 15u,
        stage,
        final_stage,
        registers,
        ef_product,
        state_index);
    vec3 value = (input_word & 0x10u) != 0u
        ? source.aaa
        : source.rgb;
    return applyInputMapping(value, input_word & 0xE0u);
}

float alphaInput(
    uint input_word,
    uint stage,
    bool final_stage,
    vec4 registers[16],
    vec4 ef_product,
    uint state_index) {
    vec4 source = readRegister(
        input_word & 15u,
        stage,
        final_stage,
        registers,
        ef_product,
        state_index);
    float value = (input_word & 0x10u) != 0u
        ? source.a
        : source.b;
    return applyInputMapping(value, input_word & 0xE0u);
}

vec3 applyOutputMapping(vec3 value, uint mapping) {
    if (mapping == 0x08u) return value - 0.5;
    if (mapping == 0x10u) return value * 2.0;
    if (mapping == 0x18u) return (value - 0.5) * 2.0;
    if (mapping == 0x20u) return value * 4.0;
    if (mapping == 0x30u) return value * 0.5;
    return value;
}

float applyOutputMapping(float value, uint mapping) {
    if (mapping == 0x08u) return value - 0.5;
    if (mapping == 0x10u) return value * 2.0;
    if (mapping == 0x18u) return (value - 0.5) * 2.0;
    if (mapping == 0x20u) return value * 4.0;
    if (mapping == 0x30u) return value * 0.5;
    return value;
}

void writeRgb(uint destination, vec3 value, inout vec4 registers[16]) {
    if (destination != 0u && destination < 16u) {
        registers[destination].rgb = clamp(value, -1.0, 1.0);
    }
}

void writeAlpha(uint destination, float value, inout vec4 registers[16]) {
    if (destination != 0u && destination < 16u) {
        registers[destination].a = clamp(value, -1.0, 1.0);
    }
}

void runCombinerStage(
    uint stage,
    uint state_index,
    inout vec4 registers[16]) {
    NativeFragmentState state = draw_states.states[state_index];
    uint color_inputs = state.combiner_color_inputs[stage];
    uint alpha_inputs = state.combiner_alpha_inputs[stage];
    uint color_output = state.combiner_color_outputs[stage];
    uint alpha_output = state.combiner_alpha_outputs[stage];
    uint color_flags = color_output >> 12u;
    uint alpha_flags = alpha_output >> 12u;
    vec4 no_ef_product = vec4(0.0);

    vec3 color_a = colorInput(
        (color_inputs >> 24u) & 255u,
        stage, false, registers, no_ef_product, state_index);
    vec3 color_b = colorInput(
        (color_inputs >> 16u) & 255u,
        stage, false, registers, no_ef_product, state_index);
    vec3 color_c = colorInput(
        (color_inputs >> 8u) & 255u,
        stage, false, registers, no_ef_product, state_index);
    vec3 color_d = colorInput(
        color_inputs & 255u,
        stage, false, registers, no_ef_product, state_index);
    vec3 color_ab = (color_flags & 2u) != 0u
        ? vec3(dot(color_a, color_b))
        : color_a * color_b;
    vec3 color_cd = (color_flags & 1u) != 0u
        ? vec3(dot(color_c, color_d))
        : color_c * color_d;

    float alpha_a = alphaInput(
        (alpha_inputs >> 24u) & 255u,
        stage, false, registers, no_ef_product, state_index);
    float alpha_b = alphaInput(
        (alpha_inputs >> 16u) & 255u,
        stage, false, registers, no_ef_product, state_index);
    float alpha_c = alphaInput(
        (alpha_inputs >> 8u) & 255u,
        stage, false, registers, no_ef_product, state_index);
    float alpha_d = alphaInput(
        alpha_inputs & 255u,
        stage, false, registers, no_ef_product, state_index);
    float alpha_ab = alpha_a * alpha_b;
    float alpha_cd = alpha_c * alpha_d;

    bool mux_select_cd = ((state.combiner_control >> 8u) & 1u) != 0u
        ? registers[12].a >= 0.5
        : (uint(registers[12].a * 255.0) & 1u) != 0u;
    vec3 color_muxsum = (color_flags & 4u) == 0u
        ? color_ab + color_cd
        : (mux_select_cd ? color_cd : color_ab);
    float alpha_muxsum = (alpha_flags & 4u) == 0u
        ? alpha_ab + alpha_cd
        : (mux_select_cd ? alpha_cd : alpha_ab);
    uint color_mapping = color_flags & 0x38u;
    uint alpha_mapping = alpha_flags & 0x38u;

    uint color_cd_destination = color_output & 15u;
    uint color_ab_destination = (color_output >> 4u) & 15u;
    uint color_muxsum_destination = (color_output >> 8u) & 15u;
    writeRgb(
        color_ab_destination,
        applyOutputMapping(color_ab, color_mapping),
        registers);
    if (color_ab_destination != 0u && (color_flags & 0x80u) != 0u) {
        writeAlpha(color_ab_destination, color_ab.b, registers);
    }
    writeRgb(
        color_cd_destination,
        applyOutputMapping(color_cd, color_mapping),
        registers);
    if (color_cd_destination != 0u && (color_flags & 0x40u) != 0u) {
        writeAlpha(color_cd_destination, color_cd.b, registers);
    }
    writeRgb(
        color_muxsum_destination,
        applyOutputMapping(color_muxsum, color_mapping),
        registers);

    writeAlpha(
        (alpha_output >> 4u) & 15u,
        applyOutputMapping(alpha_ab, alpha_mapping),
        registers);
    writeAlpha(
        alpha_output & 15u,
        applyOutputMapping(alpha_cd, alpha_mapping),
        registers);
    writeAlpha(
        (alpha_output >> 8u) & 15u,
        applyOutputMapping(alpha_muxsum, alpha_mapping),
        registers);
}

vec4 runFinalCombiner(
    uint state_index,
    vec4 registers[16]) {
    NativeFragmentState state = draw_states.states[state_index];
    uint inputs0 = state.final_combiner_inputs0;
    uint inputs1 = state.final_combiner_inputs1;
    vec4 no_ef_product = vec4(0.0);
    vec3 e = colorInput(
        (inputs1 >> 24u) & 255u,
        8u, true, registers, no_ef_product, state_index);
    vec3 f = colorInput(
        (inputs1 >> 16u) & 255u,
        8u, true, registers, no_ef_product, state_index);
    vec4 ef_product = vec4(e * f, 0.0);
    vec3 a = colorInput(
        (inputs0 >> 24u) & 255u,
        8u, true, registers, ef_product, state_index);
    vec3 b = colorInput(
        (inputs0 >> 16u) & 255u,
        8u, true, registers, ef_product, state_index);
    vec3 c = colorInput(
        (inputs0 >> 8u) & 255u,
        8u, true, registers, ef_product, state_index);
    vec3 d = colorInput(
        inputs0 & 255u,
        8u, true, registers, ef_product, state_index);
    float g = alphaInput(
        (inputs1 >> 8u) & 255u,
        8u, true, registers, ef_product, state_index);
    return vec4(d + mix(c, b, a), g);
}

void main() {
    NativeFragmentState state = draw_states.states[draw.state_index];
    vec4 registers[16];
    for (uint index = 0u; index < 16u; ++index) {
        registers[index] = vec4(0.0);
    }
    registers[3] = vec4(
        unpackFogColor(state.fog_color).rgb,
        clamp(frag_fog, 0.0, 1.0));
    registers[4] = frag_color;
    registers[5] = frag_secondary_color;

    vec4 sampled = vec4(0.0);
    if (state.texture_mode != 0u) {
        sampled = state.texture_mode == 1u
            ? textureProj(texture0, frag_uv.xyw)
            : texture(texture0, frag_uv.xy);
        if (state.texture_opaque_alpha != 0u) {
            sampled.a = 1.0;
        }
        registers[8u + min(state.texture_stage, 3u)] = sampled;
    }
    if (state.texture_mode != 0u
        && state.texture_alpha_kill != 0u
        && sampled.a == 0.0) {
        discard;
    }
    uint stage0_mode = state.shader_stage_program & 0x1Fu;
    registers[12].a = stage0_mode != 0u ? registers[8].a : 1.0;

    uint stage_count = min(state.combiner_control & 255u, 8u);
    for (uint stage = 0u; stage < stage_count; ++stage) {
        runCombinerStage(stage, draw.state_index, registers);
    }
    out_color = (state.final_combiner_inputs0 != 0u
            || state.final_combiner_inputs1 != 0u)
        ? runFinalCombiner(draw.state_index, registers)
        : registers[12];

    if (state.alpha_test_enable == 0u) {
        return;
    }
    uint alpha = uint(round(clamp(out_color.a, 0.0, 1.0) * 255.0));
    uint function = state.alpha_function & 15u;
    bool passed = function == 1u ? alpha < state.alpha_reference
        : function == 2u ? alpha == state.alpha_reference
        : function == 3u ? alpha <= state.alpha_reference
        : function == 4u ? alpha > state.alpha_reference
        : function == 5u ? alpha != state.alpha_reference
        : function == 6u ? alpha >= state.alpha_reference
        : function == 7u;
    if (!passed) {
        discard;
    }
}
