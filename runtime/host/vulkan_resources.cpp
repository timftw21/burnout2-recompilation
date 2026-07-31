#include "vulkan_presenter_runtime.h"

namespace b2r::host::vulkan_detail {

std::vector<uint8_t> VulkanPresenter::decompress_dxt1(const RecoveredTextureResource& resource) const {
    std::vector<uint8_t> rgba(static_cast<size_t>(resource.width) * resource.height * 4u, 255u);
    const uint32_t blocks_x = (resource.width + 3u) / 4u;
    const uint32_t blocks_y = (resource.height + 3u) / 4u;
    auto expand565 = [](uint16_t value) {
        return std::array<uint8_t, 3>{
            static_cast<uint8_t>(((value >> 11u) & 31u) * 255u / 31u),
            static_cast<uint8_t>(((value >> 5u) & 63u) * 255u / 63u),
            static_cast<uint8_t>((value & 31u) * 255u / 31u),
        };
    };
    for (uint32_t block_index = 0; block_index < blocks_x * blocks_y; ++block_index) {
        const size_t offset = static_cast<size_t>(block_index) * 8u;
        if (offset + 8u > resource.payload.size()) {
            break;
        }
        // The guest upload has already converted these DXT1 surfaces to
        // row-major block order. Applying an NV2A Morton unswizzle here
        // scrambles the recovered title and publisher textures.
        const uint32_t block_x = block_index % blocks_x;
        const uint32_t block_y = block_index / blocks_x;
        const uint16_t color0 = static_cast<uint16_t>(resource.payload[offset] | (resource.payload[offset + 1u] << 8u));
        const uint16_t color1 = static_cast<uint16_t>(resource.payload[offset + 2u] | (resource.payload[offset + 3u] << 8u));
        std::array<std::array<uint8_t, 4>, 4> colors{};
        const auto rgb0 = expand565(color0);
        const auto rgb1 = expand565(color1);
        colors[0] = {rgb0[0], rgb0[1], rgb0[2], 255};
        colors[1] = {rgb1[0], rgb1[1], rgb1[2], 255};
        if (color0 > color1) {
            for (uint32_t channel = 0; channel < 3u; ++channel) {
                colors[2][channel] = static_cast<uint8_t>((2u * colors[0][channel] + colors[1][channel]) / 3u);
                colors[3][channel] = static_cast<uint8_t>((colors[0][channel] + 2u * colors[1][channel]) / 3u);
            }
            colors[2][3] = colors[3][3] = 255;
        } else {
            for (uint32_t channel = 0; channel < 3u; ++channel) {
                colors[2][channel] = static_cast<uint8_t>((colors[0][channel] + colors[1][channel]) / 2u);
            }
            colors[2][3] = 255;
            colors[3] = {0, 0, 0, 0};
        }
        uint32_t indices = 0;
        std::memcpy(&indices, resource.payload.data() + offset + 4u, sizeof(indices));
        for (uint32_t y = 0; y < 4u; ++y) {
            for (uint32_t x = 0; x < 4u; ++x) {
                const uint32_t pixel_x = block_x * 4u + x;
                const uint32_t pixel_y = block_y * 4u + y;
                if (pixel_x >= resource.width || pixel_y >= resource.height) {
                    continue;
                }
                const auto& color = colors[(indices >> (2u * (y * 4u + x))) & 3u];
                const size_t destination = (static_cast<size_t>(pixel_y) * resource.width + pixel_x) * 4u;
                std::copy(color.begin(), color.end(), rgba.begin() + destination);
            }
        }
    }
    return rgba;
}

std::vector<uint8_t> VulkanPresenter::decompress_dxt5(const RecoveredTextureResource& resource) const {
    std::vector<uint8_t> rgba(static_cast<size_t>(resource.width) * resource.height * 4u, 255u);
    const uint32_t blocks_x = (resource.width + 3u) / 4u;
    const uint32_t blocks_y = (resource.height + 3u) / 4u;
    auto expand565 = [](uint16_t value) {
        return std::array<uint8_t, 3>{
            static_cast<uint8_t>(((value >> 11u) & 31u) * 255u / 31u),
            static_cast<uint8_t>(((value >> 5u) & 63u) * 255u / 63u),
            static_cast<uint8_t>((value & 31u) * 255u / 31u),
        };
    };
    for (uint32_t block_index = 0; block_index < blocks_x * blocks_y; ++block_index) {
        const size_t offset = static_cast<size_t>(block_index) * 16u;
        if (offset + 16u > resource.payload.size()) {
            break;
        }
        const uint32_t block_x = block_index % blocks_x;
        const uint32_t block_y = block_index / blocks_x;

        std::array<uint8_t, 8> alphas{};
        alphas[0] = resource.payload[offset];
        alphas[1] = resource.payload[offset + 1u];
        if (alphas[0] > alphas[1]) {
            for (uint32_t index = 1u; index <= 6u; ++index) {
                alphas[index + 1u] = static_cast<uint8_t>(
                    ((7u - index) * alphas[0] + index * alphas[1]) / 7u);
            }
        } else {
            for (uint32_t index = 1u; index <= 4u; ++index) {
                alphas[index + 1u] = static_cast<uint8_t>(
                    ((5u - index) * alphas[0] + index * alphas[1]) / 5u);
            }
            alphas[6] = 0u;
            alphas[7] = 255u;
        }
        uint64_t alpha_indices = 0u;
        for (uint32_t byte_index = 0; byte_index < 6u; ++byte_index) {
            alpha_indices |= static_cast<uint64_t>(resource.payload[offset + 2u + byte_index])
                << (byte_index * 8u);
        }

        const size_t color_offset = offset + 8u;
        const uint16_t color0 = static_cast<uint16_t>(
            resource.payload[color_offset] | (resource.payload[color_offset + 1u] << 8u));
        const uint16_t color1 = static_cast<uint16_t>(
            resource.payload[color_offset + 2u] | (resource.payload[color_offset + 3u] << 8u));
        const auto rgb0 = expand565(color0);
        const auto rgb1 = expand565(color1);
        std::array<std::array<uint8_t, 3>, 4> colors{};
        colors[0] = rgb0;
        colors[1] = rgb1;
        for (uint32_t channel = 0; channel < 3u; ++channel) {
            colors[2][channel] = static_cast<uint8_t>((2u * rgb0[channel] + rgb1[channel]) / 3u);
            colors[3][channel] = static_cast<uint8_t>((rgb0[channel] + 2u * rgb1[channel]) / 3u);
        }
        uint32_t color_indices = 0u;
        std::memcpy(
            &color_indices,
            resource.payload.data() + color_offset + 4u,
            sizeof(color_indices));

        for (uint32_t y = 0; y < 4u; ++y) {
            for (uint32_t x = 0; x < 4u; ++x) {
                const uint32_t pixel_index = y * 4u + x;
                const uint32_t pixel_x = block_x * 4u + x;
                const uint32_t pixel_y = block_y * 4u + y;
                if (pixel_x >= resource.width || pixel_y >= resource.height) {
                    continue;
                }
                const auto& color = colors[(color_indices >> (2u * pixel_index)) & 3u];
                const uint8_t alpha = alphas[(alpha_indices >> (3u * pixel_index)) & 7u];
                const size_t destination =
                    (static_cast<size_t>(pixel_y) * resource.width + pixel_x) * 4u;
                rgba[destination] = color[0];
                rgba[destination + 1u] = color[1];
                rgba[destination + 2u] = color[2];
                rgba[destination + 3u] = alpha;
            }
        }
    }
    return rgba;
}

std::vector<std::vector<uint8_t>> VulkanPresenter::decompress_dxt_mip_chain(
    const RecoveredTextureResource& resource) const {
    const uint32_t block_bytes = resource.format == "DXT1" ? 8u : 16u;
    std::vector<std::vector<uint8_t>> mips;
    size_t payload_offset = 0u;
    uint32_t width = resource.width;
    uint32_t height = resource.height;
    while (width != 0u && height != 0u) {
        const size_t payload_size = static_cast<size_t>((width + 3u) / 4u)
            * ((height + 3u) / 4u) * block_bytes;
        if (payload_offset + payload_size > resource.payload.size()) {
            break;
        }
        RecoveredTextureResource level = resource;
        level.width = width;
        level.height = height;
        level.payload.assign(
            resource.payload.begin()
                + static_cast<std::ptrdiff_t>(payload_offset),
            resource.payload.begin()
                + static_cast<std::ptrdiff_t>(
                    payload_offset + payload_size));
        mips.push_back(resource.format == "DXT1"
            ? decompress_dxt1(level)
            : decompress_dxt5(level));
        payload_offset += payload_size;
        if (width == 1u && height == 1u) {
            break;
        }
        width = std::max(width / 2u, 1u);
        height = std::max(height / 2u, 1u);
    }
    return mips;
}

std::vector<std::vector<uint8_t>> VulkanPresenter::build_cpu_dxt_conversion_mips(
    const RecoveredTextureResource& resource) const {
    std::vector<std::vector<uint8_t>> mips =
        decompress_dxt_mip_chain(resource);
    if (mips.size() != 1u) {
        return mips;
    }
    uint32_t width = resource.width;
    uint32_t height = resource.height;
    while (width != 1u || height != 1u) {
        const std::vector<uint8_t>& source = mips.back();
        const uint32_t next_width = std::max(width / 2u, 1u);
        const uint32_t next_height = std::max(height / 2u, 1u);
        std::vector<uint8_t> next(
            static_cast<size_t>(next_width) * next_height * 4u,
            0u);
        for (uint32_t y = 0u; y < next_height; ++y) {
            for (uint32_t x = 0u; x < next_width; ++x) {
                for (uint32_t channel = 0u; channel < 4u; ++channel) {
                    uint32_t sum = 0u;
                    uint32_t count = 0u;
                    for (uint32_t dy = 0u; dy < 2u; ++dy) {
                        for (uint32_t dx = 0u; dx < 2u; ++dx) {
                            const uint32_t source_x = x * 2u + dx;
                            const uint32_t source_y = y * 2u + dy;
                            if (source_x >= width || source_y >= height) {
                                continue;
                            }
                            const size_t source_offset =
                                (static_cast<size_t>(source_y) * width
                                    + source_x) * 4u + channel;
                            sum += source[source_offset];
                            ++count;
                        }
                    }
                    const size_t destination =
                        (static_cast<size_t>(y) * next_width + x) * 4u
                        + channel;
                    next[destination] = static_cast<uint8_t>(
                        sum / std::max(count, 1u));
                }
            }
        }
        mips.push_back(std::move(next));
        width = next_width;
        height = next_height;
    }
    return mips;
}

std::vector<uint8_t> VulkanPresenter::convert_bgra8_texture(
    const RecoveredTextureResource& resource,
    bool opaque_alpha,
    bool swizzled) const {
    const size_t pixel_count = static_cast<size_t>(resource.width)
        * resource.height;
    const std::vector<uint8_t> linear = swizzled
        ? nv2a_unswizzle_texture_2d(
            resource.payload, resource.width, resource.height, 4u)
        : std::vector<uint8_t>{};
    const std::vector<uint8_t>& source = swizzled
        ? linear
        : resource.payload;
    std::vector<uint8_t> rgba(pixel_count * 4u, 0u);
    for (size_t pixel = 0; pixel < pixel_count; ++pixel) {
        const size_t offset = pixel * 4u;
        rgba[offset] = source[offset + 2u];
        rgba[offset + 1u] = source[offset + 1u];
        rgba[offset + 2u] = source[offset];
        rgba[offset + 3u] = opaque_alpha
            ? 255u
            : source[offset + 3u];
    }
    return rgba;
}

std::vector<uint8_t> VulkanPresenter::convert_r5g6b5_texture(
    const RecoveredTextureResource& resource) const {
    const size_t pixel_count = static_cast<size_t>(resource.width)
        * resource.height;
    const std::vector<uint8_t> linear = nv2a_unswizzle_texture_2d(
        resource.payload, resource.width, resource.height, 2u);
    std::vector<uint8_t> rgba(pixel_count * 4u, 255u);
    for (size_t pixel = 0; pixel < pixel_count; ++pixel) {
        uint16_t packed = 0u;
        std::memcpy(
            &packed,
            linear.data() + pixel * 2u,
            sizeof(packed));
        const size_t offset = pixel * 4u;
        rgba[offset] = static_cast<uint8_t>(
            ((packed >> 11u) & 31u) * 255u / 31u);
        rgba[offset + 1u] = static_cast<uint8_t>(
            ((packed >> 5u) & 63u) * 255u / 63u);
        rgba[offset + 2u] = static_cast<uint8_t>(
            (packed & 31u) * 255u / 31u);
    }
    return rgba;
}

void VulkanPresenter::submit_immediate(
    const std::function<void(VkCommandBuffer)>& record) {
    VkCommandBufferAllocateInfo allocation{};
    allocation.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO;
    allocation.commandPool = command_pool_;
    allocation.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY;
    allocation.commandBufferCount = 1;
    VkCommandBuffer command = VK_NULL_HANDLE;
    vk_check(vkAllocateCommandBuffers(device_, &allocation, &command), "vkAllocateCommandBuffers(immediate)");
    ++command_buffer_allocation_count_;
    VkCommandBufferBeginInfo begin{};
    begin.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO;
    begin.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
    vk_check(vkBeginCommandBuffer(command, &begin), "vkBeginCommandBuffer(immediate)");
    recording_barrier_count_ = 0u;
    record(command);
    vk_check(vkEndCommandBuffer(command), "vkEndCommandBuffer(immediate)");
    VkSubmitInfo submit{};
    submit.sType = VK_STRUCTURE_TYPE_SUBMIT_INFO;
    submit.commandBufferCount = 1;
    submit.pCommandBuffers = &command;
    vk_check(vkQueueSubmit(graphics_queue_, 1, &submit, VK_NULL_HANDLE), "vkQueueSubmit(immediate)");
    ++queue_submission_count_;
    vk_check(vkQueueWaitIdle(graphics_queue_), "vkQueueWaitIdle(immediate)");
    barrier_count_ += recording_barrier_count_;
    recording_barrier_count_ = 0u;
    vkFreeCommandBuffers(device_, command_pool_, 1, &command);
}

HostTexture VulkanPresenter::create_host_texture(
    uint32_t guest_address,
    uint32_t width,
    uint32_t height,
    std::string format,
    std::string content_hash,
    const std::vector<uint8_t>& rgba,
    VkFormat image_format,
    bool render_target_feedback,
    const std::vector<std::vector<uint8_t>>& recovered_mips,
    bool cubemap) {
    HostTexture texture{};
    texture.guest_address = guest_address;
    texture.width = width;
    texture.height = height;
    texture.format = std::move(format);
    texture.content_hash = std::move(content_hash);
    texture.image_format = image_format;
    texture.render_target_feedback = render_target_feedback;
    texture.cubemap = cubemap;
    const bool use_recovered_mips = !cubemap && recovered_mips.size() > 1u;
    std::vector<uint8_t> upload_rgba = use_recovered_mips
        ? recovered_mips.front()
        : rgba;
    std::vector<VkBufferImageCopy> upload_regions;
    uint32_t mip_width = width;
    uint32_t mip_height = height;
    size_t mip_offset = 0u;
    if (cubemap) {
        const size_t face_size = static_cast<size_t>(width) * height * 4u;
        if (upload_rgba.size() != face_size * 6u) {
            throw std::runtime_error("invalid cubemap RGBA payload");
        }
        for (uint32_t face = 0u; face < 6u; ++face) {
            VkBufferImageCopy region{};
            region.bufferOffset = face_size * face;
            region.imageSubresource.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
            region.imageSubresource.baseArrayLayer = face;
            region.imageSubresource.layerCount = 1u;
            region.imageExtent = {width, height, 1u};
            upload_regions.push_back(region);
        }
    } else while (true) {
        VkBufferImageCopy region{};
        region.bufferOffset = mip_offset;
        region.imageSubresource.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
        region.imageSubresource.mipLevel = static_cast<uint32_t>(
            upload_regions.size());
        region.imageSubresource.layerCount = 1;
        region.imageExtent = {mip_width, mip_height, 1};
        upload_regions.push_back(region);
        if (render_target_feedback
            || (mip_width == 1u && mip_height == 1u)
            || (use_recovered_mips
                && upload_regions.size() >= recovered_mips.size())) {
            break;
        }
        const uint32_t next_width = std::max(mip_width / 2u, 1u);
        const uint32_t next_height = std::max(mip_height / 2u, 1u);
        if (use_recovered_mips) {
            const std::vector<uint8_t>& next =
                recovered_mips[upload_regions.size()];
            const size_t expected_size =
                static_cast<size_t>(next_width) * next_height * 4u;
            if (next.size() != expected_size) {
                throw std::runtime_error(
                    "invalid recovered texture mip payload");
            }
            mip_offset = upload_rgba.size();
            upload_rgba.insert(
                upload_rgba.end(), next.begin(), next.end());
            mip_width = next_width;
            mip_height = next_height;
            continue;
        }
        std::vector<uint8_t> next(
            static_cast<size_t>(next_width) * next_height * 4u,
            0u);
        for (uint32_t y = 0u; y < next_height; ++y) {
            for (uint32_t x = 0u; x < next_width; ++x) {
                for (uint32_t channel = 0u; channel < 4u; ++channel) {
                    uint32_t sum = 0u;
                    uint32_t count = 0u;
                    for (uint32_t dy = 0u; dy < 2u; ++dy) {
                        for (uint32_t dx = 0u; dx < 2u; ++dx) {
                            const uint32_t source_x = x * 2u + dx;
                            const uint32_t source_y = y * 2u + dy;
                            if (source_x >= mip_width
                                || source_y >= mip_height) {
                                continue;
                            }
                            const size_t source = mip_offset
                                + (static_cast<size_t>(source_y)
                                    * mip_width + source_x) * 4u
                                + channel;
                            sum += upload_rgba[source];
                            ++count;
                        }
                    }
                    const size_t destination =
                        (static_cast<size_t>(y) * next_width + x) * 4u
                        + channel;
                    next[destination] = static_cast<uint8_t>(
                        sum / std::max(count, 1u));
                }
            }
        }
        mip_offset = upload_rgba.size();
        upload_rgba.insert(
            upload_rgba.end(), next.begin(), next.end());
        mip_width = next_width;
        mip_height = next_height;
    }
    texture.mip_levels = cubemap
        ? 1u
        : static_cast<uint32_t>(upload_regions.size());
    VkBuffer staging = VK_NULL_HANDLE;
    VkDeviceMemory staging_memory = VK_NULL_HANDLE;
    create_buffer(
        upload_rgba.size(),
        VK_BUFFER_USAGE_TRANSFER_SRC_BIT,
        VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT,
        staging,
        staging_memory);
    void* mapped = nullptr;
    vk_check(vkMapMemory(device_, staging_memory, 0, upload_rgba.size(), 0, &mapped), "vkMapMemory(texture)");
    std::memcpy(mapped, upload_rgba.data(), upload_rgba.size());
    upload_bytes_ += upload_rgba.size();
    vkUnmapMemory(device_, staging_memory);

    VkImageCreateInfo image_info{};
    image_info.sType = VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO;
    image_info.imageType = VK_IMAGE_TYPE_2D;
    image_info.extent = {width, height, 1};
    image_info.mipLevels = texture.mip_levels;
    image_info.arrayLayers = cubemap ? 6u : 1u;
    image_info.flags = cubemap ? VK_IMAGE_CREATE_CUBE_COMPATIBLE_BIT : 0u;
    image_info.format = image_format;
    image_info.tiling = VK_IMAGE_TILING_OPTIMAL;
    image_info.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
    image_info.usage = VK_IMAGE_USAGE_TRANSFER_DST_BIT
        | VK_IMAGE_USAGE_SAMPLED_BIT
        | (render_target_feedback
            ? VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT
                | VK_IMAGE_USAGE_TRANSFER_SRC_BIT
            : 0u);
    image_info.samples = VK_SAMPLE_COUNT_1_BIT;
    image_info.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
    vk_check(vkCreateImage(device_, &image_info, nullptr, &texture.image), "vkCreateImage(texture)");
    VkMemoryRequirements requirements{};
    vkGetImageMemoryRequirements(device_, texture.image, &requirements);
    VkMemoryAllocateInfo image_allocation{};
    image_allocation.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO;
    image_allocation.allocationSize = requirements.size;
    image_allocation.memoryTypeIndex = find_memory_type(requirements.memoryTypeBits, VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT);
    vk_check(vkAllocateMemory(device_, &image_allocation, nullptr, &texture.memory), "vkAllocateMemory(texture)");
    vk_check(vkBindImageMemory(device_, texture.image, texture.memory, 0), "vkBindImageMemory(texture)");

    submit_immediate([&](VkCommandBuffer command) {
        VkImageMemoryBarrier to_transfer{};
        to_transfer.sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER;
        to_transfer.srcAccessMask = 0;
        to_transfer.dstAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
        to_transfer.oldLayout = VK_IMAGE_LAYOUT_UNDEFINED;
        to_transfer.newLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;
        to_transfer.srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
        to_transfer.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
        to_transfer.image = texture.image;
        to_transfer.subresourceRange.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
        to_transfer.subresourceRange.levelCount = texture.mip_levels;
        to_transfer.subresourceRange.layerCount = cubemap ? 6u : 1u;
        ++recording_barrier_count_;
        vkCmdPipelineBarrier(command, VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT, 0, 0, nullptr, 0, nullptr, 1, &to_transfer);
        vkCmdCopyBufferToImage(
            command,
            staging,
            texture.image,
            VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
            static_cast<uint32_t>(upload_regions.size()),
            upload_regions.data());
        VkImageMemoryBarrier to_shader{};
        to_shader.sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER;
        to_shader.srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
        to_shader.dstAccessMask = VK_ACCESS_SHADER_READ_BIT;
        to_shader.oldLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;
        to_shader.newLayout = VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL;
        to_shader.srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
        to_shader.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
        to_shader.image = texture.image;
        to_shader.subresourceRange = to_transfer.subresourceRange;
        ++recording_barrier_count_;
        vkCmdPipelineBarrier(command, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT, 0, 0, nullptr, 0, nullptr, 1, &to_shader);
    });
    vkDestroyBuffer(device_, staging, nullptr);
    vkFreeMemory(device_, staging_memory, nullptr);

    VkImageViewCreateInfo view_info{};
    view_info.sType = VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO;
    view_info.image = texture.image;
    view_info.viewType = cubemap
        ? VK_IMAGE_VIEW_TYPE_CUBE
        : VK_IMAGE_VIEW_TYPE_2D;
    view_info.format = image_format;
    view_info.subresourceRange.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
    view_info.subresourceRange.levelCount = texture.mip_levels;
    view_info.subresourceRange.layerCount = cubemap ? 6u : 1u;
    vk_check(vkCreateImageView(device_, &view_info, nullptr, &texture.view), "vkCreateImageView(texture)");
    return texture;
}

bool VulkanPresenter::build_gpu_texture_conversion_job(
    const RecoveredTextureResource& resource,
    size_t host_texture_index,
    GpuTextureConversionJob& job) const {
    if ((resource.format != "DXT1" && resource.format != "DXT5")
        || resource.width == 0u
        || resource.height == 0u
        || resource.payload.size() > std::numeric_limits<uint32_t>::max()) {
        return false;
    }
    const uint32_t block_bytes = resource.format == "DXT1" ? 8u : 16u;
    uint64_t payload_offset = 0u;
    uint32_t width = resource.width;
    uint32_t height = resource.height;
    while (width != 0u && height != 0u) {
        const uint64_t payload_size = static_cast<uint64_t>(
            (width + 3u) / 4u) * ((height + 3u) / 4u) * block_bytes;
        if (payload_offset + payload_size > resource.payload.size()) {
            break;
        }
        job.mips.push_back({
            width,
            height,
            static_cast<uint32_t>(payload_offset),
            0u,
            0u,
            false,
        });
        payload_offset += payload_size;
        if (width == 1u && height == 1u) {
            break;
        }
        width = std::max(width / 2u, 1u);
        height = std::max(height / 2u, 1u);
    }
    if (job.mips.empty()) {
        return false;
    }
    // Match create_host_texture exactly: a resource containing only its
    // base level receives a complete box-filtered mip chain, while a
    // recovered multi-level chain is authoritative and stops where the
    // guest payload stops.
    if (job.mips.size() == 1u) {
        width = resource.width;
        height = resource.height;
        while (width != 1u || height != 1u) {
            width = std::max(width / 2u, 1u);
            height = std::max(height / 2u, 1u);
            job.mips.push_back({
                width,
                height,
                0u,
                0u,
                0u,
                true,
            });
        }
    }
    job.resource = &resource;
    job.host_texture_index = host_texture_index;
    return true;
}

bool VulkanPresenter::execute_gpu_texture_conversion_batch(
    std::vector<GpuTextureConversionJob>& jobs) {
    if (jobs.empty() || !ensure_texture_conversion_pipeline()) {
        return false;
    }
    const auto conversion_begin = std::chrono::steady_clock::now();
    uint64_t input_size = 0u;
    uint64_t output_size = 0u;
    uint32_t mip_count = 0u;
    uint32_t generated_mip_count = 0u;
    uint32_t dxt1_texture_count = 0u;
    uint32_t dxt5_texture_count = 0u;
    uint32_t recovered_chain_texture_count = 0u;
    uint32_t generated_chain_texture_count = 0u;
    uint64_t dxt1_input_bytes = 0u;
    uint64_t dxt5_input_bytes = 0u;
    for (GpuTextureConversionJob& job : jobs) {
        input_size = (input_size + 3u) & ~uint64_t{3u};
        if (input_size > std::numeric_limits<uint32_t>::max()
            || job.resource->payload.size()
                > std::numeric_limits<uint32_t>::max() - input_size) {
            return false;
        }
        job.input_byte_offset = static_cast<uint32_t>(input_size);
        input_size += job.resource->payload.size();
        if (job.resource->format == "DXT1") {
            ++dxt1_texture_count;
            dxt1_input_bytes += job.resource->payload.size();
        } else {
            ++dxt5_texture_count;
            dxt5_input_bytes += job.resource->payload.size();
        }
        const bool generated_chain = std::any_of(
            job.mips.begin(),
            job.mips.end(),
            [](const GpuTextureConversionMip& mip) {
                return mip.generated;
            });
        generated_chain_texture_count += generated_chain ? 1u : 0u;
        recovered_chain_texture_count +=
            job.mips.size() > 1u && !generated_chain ? 1u : 0u;
        for (size_t mip_index = 0u;
             mip_index < job.mips.size();
             ++mip_index) {
            GpuTextureConversionMip& mip = job.mips[mip_index];
            output_size = (output_size + 3u) & ~uint64_t{3u};
            const uint64_t mip_size = static_cast<uint64_t>(mip.width)
                * mip.height * 4u;
            if (output_size > std::numeric_limits<uint32_t>::max()
                || mip_size
                    > std::numeric_limits<uint32_t>::max() - output_size) {
                return false;
            }
            mip.output_byte_offset = static_cast<uint32_t>(output_size);
            if (mip.generated) {
                mip.source_output_byte_offset =
                    job.mips[mip_index - 1u].output_byte_offset;
                ++generated_mip_count;
            }
            output_size += mip_size;
            ++mip_count;
        }
    }
    input_size = (input_size + 3u) & ~uint64_t{3u};
    output_size = (output_size + 3u) & ~uint64_t{3u};
    VkPhysicalDeviceProperties properties{};
    vkGetPhysicalDeviceProperties(physical_device_, &properties);
    if (input_size == 0u
        || output_size == 0u
        || input_size > properties.limits.maxStorageBufferRange
        || output_size > properties.limits.maxStorageBufferRange) {
        return false;
    }

    GpuTextureValidationCoverage pending_validation_coverage =
        gpu_texture_validation_coverage_;
    std::vector<size_t> validation_job_indices;
    uint32_t validation_mip_count = 0u;
    uint64_t validation_byte_count = 0u;
    const bool validate_gpu_texture_conversion =
        options_.strict_render_validation
        || !options_.live_render_stream;
    if (validate_gpu_texture_conversion
        && !gpu_texture_validation_coverage_.complete()) {
        const auto generated_mips = [](
            const GpuTextureConversionJob& job) {
            return std::any_of(
                job.mips.begin(),
                job.mips.end(),
                [](const GpuTextureConversionMip& mip) {
                    return mip.generated;
                });
        };
        const auto converted_bytes = [](
            const GpuTextureConversionJob& job) {
            uint64_t bytes = 0u;
            for (const GpuTextureConversionMip& mip : job.mips) {
                bytes += static_cast<uint64_t>(mip.width)
                    * mip.height * 4u;
            }
            return bytes;
        };
        const auto add_smallest_candidate = [&](const auto& predicate) {
            size_t selected = jobs.size();
            uint64_t selected_bytes =
                std::numeric_limits<uint64_t>::max();
            for (size_t job_index = 0u;
                 job_index < jobs.size();
                 ++job_index) {
                const GpuTextureConversionJob& job = jobs[job_index];
                const uint64_t bytes = converted_bytes(job);
                if (predicate(job) && bytes < selected_bytes) {
                    selected = job_index;
                    selected_bytes = bytes;
                }
            }
            if (selected != jobs.size()
                && std::find(
                    validation_job_indices.begin(),
                    validation_job_indices.end(),
                    selected) == validation_job_indices.end()) {
                validation_job_indices.push_back(selected);
            }
        };
        if (!gpu_texture_validation_coverage_.dxt1) {
            add_smallest_candidate([](
                const GpuTextureConversionJob& job) {
                return job.resource->format == "DXT1";
            });
        }
        if (!gpu_texture_validation_coverage_.dxt5) {
            add_smallest_candidate([](
                const GpuTextureConversionJob& job) {
                return job.resource->format == "DXT5";
            });
        }
        if (!gpu_texture_validation_coverage_.recovered_mips) {
            add_smallest_candidate([&](
                const GpuTextureConversionJob& job) {
                return job.mips.size() > 1u && !generated_mips(job);
            });
        }
        if (!gpu_texture_validation_coverage_.generated_mips) {
            add_smallest_candidate([&](
                const GpuTextureConversionJob& job) {
                return generated_mips(job);
            });
        }
        for (const size_t job_index : validation_job_indices) {
            const GpuTextureConversionJob& job = jobs[job_index];
            const bool has_generated_mips = generated_mips(job);
            const bool has_recovered_mips =
                job.mips.size() > 1u && !has_generated_mips;
            validation_mip_count += static_cast<uint32_t>(
                job.mips.size());
            validation_byte_count += converted_bytes(job);
            pending_validation_coverage.dxt1 =
                pending_validation_coverage.dxt1
                || job.resource->format == "DXT1";
            pending_validation_coverage.dxt5 =
                pending_validation_coverage.dxt5
                || job.resource->format == "DXT5";
            pending_validation_coverage.recovered_mips =
                pending_validation_coverage.recovered_mips
                || has_recovered_mips;
            pending_validation_coverage.generated_mips =
                pending_validation_coverage.generated_mips
                || has_generated_mips;
        }
    }
    std::vector<VkBufferCopy> validation_copies;
    uint64_t validation_buffer_size = 0u;
    for (const size_t job_index : validation_job_indices) {
        for (const GpuTextureConversionMip& mip : jobs[job_index].mips) {
            VkBufferCopy copy{};
            copy.srcOffset = mip.output_byte_offset;
            copy.dstOffset = validation_buffer_size;
            copy.size = static_cast<uint64_t>(mip.width)
                * mip.height * 4u;
            validation_copies.push_back(copy);
            validation_buffer_size += copy.size;
        }
    }

    std::vector<uint8_t> input_bytes(static_cast<size_t>(input_size), 0u);
    for (const GpuTextureConversionJob& job : jobs) {
        std::memcpy(
            input_bytes.data() + job.input_byte_offset,
            job.resource->payload.data(),
            job.resource->payload.size());
        HostTexture& texture = host_textures_[job.host_texture_index];
        texture.image_format = VK_FORMAT_R8G8B8A8_UNORM;
        texture.mip_levels = static_cast<uint32_t>(job.mips.size());
        VkImageCreateInfo image_info{};
        image_info.sType = VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO;
        image_info.imageType = VK_IMAGE_TYPE_2D;
        image_info.extent = {texture.width, texture.height, 1u};
        image_info.mipLevels = texture.mip_levels;
        image_info.arrayLayers = 1u;
        image_info.format = texture.image_format;
        image_info.tiling = VK_IMAGE_TILING_OPTIMAL;
        image_info.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
        image_info.usage = VK_IMAGE_USAGE_TRANSFER_DST_BIT
            | VK_IMAGE_USAGE_SAMPLED_BIT;
        image_info.samples = VK_SAMPLE_COUNT_1_BIT;
        image_info.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
        vk_check(
            vkCreateImage(
                device_, &image_info, nullptr, &texture.image),
            "vkCreateImage(GPU-converted texture)");
        VkMemoryRequirements requirements{};
        vkGetImageMemoryRequirements(
            device_, texture.image, &requirements);
        VkMemoryAllocateInfo allocation{};
        allocation.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO;
        allocation.allocationSize = requirements.size;
        allocation.memoryTypeIndex = find_memory_type(
            requirements.memoryTypeBits,
            VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT);
        vk_check(
            vkAllocateMemory(
                device_, &allocation, nullptr, &texture.memory),
            "vkAllocateMemory(GPU-converted texture)");
        vk_check(
            vkBindImageMemory(
                device_, texture.image, texture.memory, 0u),
            "vkBindImageMemory(GPU-converted texture)");
        VkImageViewCreateInfo view_info{};
        view_info.sType = VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO;
        view_info.image = texture.image;
        view_info.viewType = VK_IMAGE_VIEW_TYPE_2D;
        view_info.format = texture.image_format;
        view_info.subresourceRange.aspectMask =
            VK_IMAGE_ASPECT_COLOR_BIT;
        view_info.subresourceRange.levelCount = texture.mip_levels;
        view_info.subresourceRange.layerCount = 1u;
        vk_check(
            vkCreateImageView(
                device_, &view_info, nullptr, &texture.view),
            "vkCreateImageView(GPU-converted texture)");
    }

    VkBuffer input_buffer = VK_NULL_HANDLE;
    VkDeviceMemory input_memory = VK_NULL_HANDLE;
    VkBuffer output_buffer = VK_NULL_HANDLE;
    VkDeviceMemory output_memory = VK_NULL_HANDLE;
    VkBuffer validation_buffer = VK_NULL_HANDLE;
    VkDeviceMemory validation_memory = VK_NULL_HANDLE;
    create_buffer(
        input_size,
        VK_BUFFER_USAGE_STORAGE_BUFFER_BIT,
        VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT
            | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT,
        input_buffer,
        input_memory);
    create_buffer(
        output_size,
        VK_BUFFER_USAGE_STORAGE_BUFFER_BIT
            | VK_BUFFER_USAGE_TRANSFER_SRC_BIT,
        VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT,
        output_buffer,
        output_memory);
    if (!validation_job_indices.empty()) {
        create_buffer(
            validation_buffer_size,
            VK_BUFFER_USAGE_TRANSFER_DST_BIT,
            VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT
                | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT,
            validation_buffer,
            validation_memory);
    }
    void* mapped = nullptr;
    vk_check(
        vkMapMemory(
            device_, input_memory, 0u, input_size, 0u, &mapped),
        "vkMapMemory(texture conversion input)");
    std::memcpy(mapped, input_bytes.data(), input_bytes.size());
    upload_bytes_ += input_bytes.size();
    vkUnmapMemory(device_, input_memory);

    VkDescriptorPoolSize pool_size{};
    pool_size.type = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;
    pool_size.descriptorCount = 2u;
    VkDescriptorPoolCreateInfo pool_info{};
    pool_info.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO;
    pool_info.maxSets = 1u;
    pool_info.poolSizeCount = 1u;
    pool_info.pPoolSizes = &pool_size;
    VkDescriptorPool descriptor_pool = VK_NULL_HANDLE;
    vk_check(
        vkCreateDescriptorPool(
            device_, &pool_info, nullptr, &descriptor_pool),
        "vkCreateDescriptorPool(texture conversion)");
    VkDescriptorSetAllocateInfo descriptor_allocation{};
    descriptor_allocation.sType =
        VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO;
    descriptor_allocation.descriptorPool = descriptor_pool;
    descriptor_allocation.descriptorSetCount = 1u;
    descriptor_allocation.pSetLayouts =
        &texture_convert_descriptor_layout_;
    VkDescriptorSet descriptor_set = VK_NULL_HANDLE;
    vk_check(
        vkAllocateDescriptorSets(
            device_, &descriptor_allocation, &descriptor_set),
        "vkAllocateDescriptorSets(texture conversion)");
    ++descriptor_allocation_count_;
    const std::array<VkDescriptorBufferInfo, 2> buffer_infos{{
        {input_buffer, 0u, input_size},
        {output_buffer, 0u, output_size},
    }};
    std::array<VkWriteDescriptorSet, 2> descriptor_writes{};
    for (uint32_t index = 0u; index < descriptor_writes.size(); ++index) {
        descriptor_writes[index].sType =
            VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
        descriptor_writes[index].dstSet = descriptor_set;
        descriptor_writes[index].dstBinding = index;
        descriptor_writes[index].descriptorCount = 1u;
        descriptor_writes[index].descriptorType =
            VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;
        descriptor_writes[index].pBufferInfo = &buffer_infos[index];
    }
    vkUpdateDescriptorSets(
        device_,
        static_cast<uint32_t>(descriptor_writes.size()),
        descriptor_writes.data(),
        0u,
        nullptr);

    const auto submission_begin = std::chrono::steady_clock::now();
    submit_immediate([&](VkCommandBuffer command) {
        vkCmdBindPipeline(
            command,
            VK_PIPELINE_BIND_POINT_COMPUTE,
            texture_convert_pipeline_);
        vkCmdBindDescriptorSets(
            command,
            VK_PIPELINE_BIND_POINT_COMPUTE,
            texture_convert_pipeline_layout_,
            0u,
            1u,
            &descriptor_set,
            0u,
            nullptr);
        for (const GpuTextureConversionJob& job : jobs) {
            for (size_t mip_index = 0u;
                 mip_index < job.mips.size();
                 ++mip_index) {
                const GpuTextureConversionMip& mip = job.mips[mip_index];
                if (mip.generated) {
                    VkBufferMemoryBarrier dependency{};
                    dependency.sType =
                        VK_STRUCTURE_TYPE_BUFFER_MEMORY_BARRIER;
                    dependency.srcAccessMask =
                        VK_ACCESS_SHADER_WRITE_BIT;
                    dependency.dstAccessMask =
                        VK_ACCESS_SHADER_READ_BIT
                        | VK_ACCESS_SHADER_WRITE_BIT;
                    dependency.srcQueueFamilyIndex =
                        VK_QUEUE_FAMILY_IGNORED;
                    dependency.dstQueueFamilyIndex =
                        VK_QUEUE_FAMILY_IGNORED;
                    dependency.buffer = output_buffer;
                    dependency.offset = 0u;
                    dependency.size = output_size;
                    ++recording_barrier_count_;
                    vkCmdPipelineBarrier(
                        command,
                        VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,
                        VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,
                        0u,
                        0u,
                        nullptr,
                        1u,
                        &dependency,
                        0u,
                        nullptr);
                }
                NativeTextureConvertPushConstants push{};
                push.input_byte_offset = job.input_byte_offset
                    + mip.input_byte_offset;
                push.output_word_offset = mip.output_byte_offset
                    / sizeof(uint32_t);
                push.source_word_offset = mip.source_output_byte_offset
                    / sizeof(uint32_t);
                push.width = mip.width;
                push.height = mip.height;
                if (mip.generated) {
                    const GpuTextureConversionMip& source =
                        job.mips[mip_index - 1u];
                    push.source_width = source.width;
                    push.source_height = source.height;
                    push.mode = 2u;
                } else {
                    push.mode = job.resource->format == "DXT1" ? 0u : 1u;
                }
                vkCmdPushConstants(
                    command,
                    texture_convert_pipeline_layout_,
                    VK_SHADER_STAGE_COMPUTE_BIT,
                    0u,
                    sizeof(push),
                    &push);
                vkCmdDispatch(
                    command,
                    (mip.width + 7u) / 8u,
                    (mip.height + 7u) / 8u,
                    1u);
            }
        }
        VkBufferMemoryBarrier output_ready{};
        output_ready.sType = VK_STRUCTURE_TYPE_BUFFER_MEMORY_BARRIER;
        output_ready.srcAccessMask = VK_ACCESS_SHADER_WRITE_BIT;
        output_ready.dstAccessMask = VK_ACCESS_TRANSFER_READ_BIT;
        output_ready.srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
        output_ready.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
        output_ready.buffer = output_buffer;
        output_ready.offset = 0u;
        output_ready.size = output_size;
        ++recording_barrier_count_;
        vkCmdPipelineBarrier(
            command,
            VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,
            VK_PIPELINE_STAGE_TRANSFER_BIT,
            0u,
            0u,
            nullptr,
            1u,
            &output_ready,
            0u,
            nullptr);
        if (validation_buffer != VK_NULL_HANDLE) {
            vkCmdCopyBuffer(
                command,
                output_buffer,
                validation_buffer,
                static_cast<uint32_t>(validation_copies.size()),
                validation_copies.data());
        }

        std::vector<VkImageMemoryBarrier> to_transfer;
        to_transfer.reserve(jobs.size());
        for (const GpuTextureConversionJob& job : jobs) {
            const HostTexture& texture =
                host_textures_[job.host_texture_index];
            VkImageMemoryBarrier barrier{};
            barrier.sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER;
            barrier.dstAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
            barrier.oldLayout = VK_IMAGE_LAYOUT_UNDEFINED;
            barrier.newLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;
            barrier.srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
            barrier.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
            barrier.image = texture.image;
            barrier.subresourceRange.aspectMask =
                VK_IMAGE_ASPECT_COLOR_BIT;
            barrier.subresourceRange.levelCount = texture.mip_levels;
            barrier.subresourceRange.layerCount = 1u;
            to_transfer.push_back(barrier);
        }
        ++recording_barrier_count_;
        vkCmdPipelineBarrier(
            command,
            VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT,
            VK_PIPELINE_STAGE_TRANSFER_BIT,
            0u,
            0u,
            nullptr,
            0u,
            nullptr,
            static_cast<uint32_t>(to_transfer.size()),
            to_transfer.data());
        for (const GpuTextureConversionJob& job : jobs) {
            const HostTexture& texture =
                host_textures_[job.host_texture_index];
            std::vector<VkBufferImageCopy> regions;
            regions.reserve(job.mips.size());
            for (uint32_t mip_index = 0u;
                 mip_index < job.mips.size();
                 ++mip_index) {
                const GpuTextureConversionMip& mip = job.mips[mip_index];
                VkBufferImageCopy region{};
                region.bufferOffset = mip.output_byte_offset;
                region.imageSubresource.aspectMask =
                    VK_IMAGE_ASPECT_COLOR_BIT;
                region.imageSubresource.mipLevel = mip_index;
                region.imageSubresource.layerCount = 1u;
                region.imageExtent = {mip.width, mip.height, 1u};
                regions.push_back(region);
            }
            vkCmdCopyBufferToImage(
                command,
                output_buffer,
                texture.image,
                VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
                static_cast<uint32_t>(regions.size()),
                regions.data());
        }
        std::vector<VkImageMemoryBarrier> to_shader = to_transfer;
        for (VkImageMemoryBarrier& barrier : to_shader) {
            barrier.srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
            barrier.dstAccessMask = VK_ACCESS_SHADER_READ_BIT;
            barrier.oldLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;
            barrier.newLayout = VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL;
        }
        ++recording_barrier_count_;
        vkCmdPipelineBarrier(
            command,
            VK_PIPELINE_STAGE_TRANSFER_BIT,
            VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT,
            0u,
            0u,
            nullptr,
            0u,
            nullptr,
            static_cast<uint32_t>(to_shader.size()),
            to_shader.data());
        if (validation_buffer != VK_NULL_HANDLE) {
            VkBufferMemoryBarrier validation_ready{};
            validation_ready.sType =
                VK_STRUCTURE_TYPE_BUFFER_MEMORY_BARRIER;
            validation_ready.srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
            validation_ready.dstAccessMask = VK_ACCESS_HOST_READ_BIT;
            validation_ready.srcQueueFamilyIndex =
                VK_QUEUE_FAMILY_IGNORED;
            validation_ready.dstQueueFamilyIndex =
                VK_QUEUE_FAMILY_IGNORED;
            validation_ready.buffer = validation_buffer;
            validation_ready.offset = 0u;
            validation_ready.size = validation_buffer_size;
            ++recording_barrier_count_;
            vkCmdPipelineBarrier(
                command,
                VK_PIPELINE_STAGE_TRANSFER_BIT,
                VK_PIPELINE_STAGE_HOST_BIT,
                0u,
                0u,
                nullptr,
                1u,
                &validation_ready,
                0u,
            nullptr);
        }
    });
    const uint64_t gpu_submission_us = std::chrono::duration_cast<
        std::chrono::microseconds>(
            std::chrono::steady_clock::now() - submission_begin).count();
    const uint64_t setup_us = std::chrono::duration_cast<
        std::chrono::microseconds>(
            submission_begin - conversion_begin).count();

    bool validation_passed = true;
    uint64_t validation_mismatch_bytes = 0u;
    uint32_t first_mismatch_address = 0u;
    uint32_t first_mismatch_mip = 0u;
    uint64_t first_mismatch_byte = 0u;
    uint32_t first_mismatch_expected = 0u;
    uint32_t first_mismatch_actual = 0u;
    uint64_t validation_cpu_us = 0u;
    if (validation_buffer != VK_NULL_HANDLE) {
        readback_bytes_ += static_cast<uint64_t>(validation_buffer_size);
        const auto validation_begin = std::chrono::steady_clock::now();
        void* validation_mapped = nullptr;
        vk_check(
            vkMapMemory(
                device_,
                validation_memory,
                0u,
                validation_buffer_size,
                0u,
                &validation_mapped),
            "vkMapMemory(texture conversion validation)");
        const auto* actual = static_cast<const uint8_t*>(
            validation_mapped);
        bool first_mismatch_recorded = false;
        size_t validation_copy_index = 0u;
        for (const size_t job_index : validation_job_indices) {
            const GpuTextureConversionJob& job = jobs[job_index];
            const std::vector<std::vector<uint8_t>> expected_mips =
                build_cpu_dxt_conversion_mips(*job.resource);
            if (expected_mips.size() != job.mips.size()) {
                validation_passed = false;
                ++validation_mismatch_bytes;
                if (!first_mismatch_recorded) {
                    first_mismatch_recorded = true;
                    first_mismatch_address = job.resource->address;
                    first_mismatch_mip = static_cast<uint32_t>(
                        std::min(expected_mips.size(), job.mips.size()));
                }
                validation_copy_index += job.mips.size();
                continue;
            }
            for (size_t mip_index = 0u;
                 mip_index < job.mips.size();
                 ++mip_index) {
                const GpuTextureConversionMip& mip =
                    job.mips[mip_index];
                const std::vector<uint8_t>& expected =
                    expected_mips[mip_index];
                const VkBufferCopy& validation_copy =
                    validation_copies[validation_copy_index++];
                const size_t expected_size = static_cast<size_t>(
                    mip.width) * mip.height * 4u;
                if (expected.size() != expected_size) {
                    validation_passed = false;
                    ++validation_mismatch_bytes;
                    if (!first_mismatch_recorded) {
                        first_mismatch_recorded = true;
                        first_mismatch_address = job.resource->address;
                        first_mismatch_mip = static_cast<uint32_t>(
                            mip_index);
                    }
                    continue;
                }
                const uint8_t* actual_mip = actual
                    + validation_copy.dstOffset;
                for (size_t byte_index = 0u;
                     byte_index < expected.size();
                     ++byte_index) {
                    if (actual_mip[byte_index] == expected[byte_index]) {
                        continue;
                    }
                    validation_passed = false;
                    ++validation_mismatch_bytes;
                    if (!first_mismatch_recorded) {
                        first_mismatch_recorded = true;
                        first_mismatch_address = job.resource->address;
                        first_mismatch_mip = static_cast<uint32_t>(
                            mip_index);
                        first_mismatch_byte = byte_index;
                        first_mismatch_expected = expected[byte_index];
                        first_mismatch_actual = actual_mip[byte_index];
                    }
                }
            }
        }
        vkUnmapMemory(device_, validation_memory);
        validation_cpu_us = std::chrono::duration_cast<
            std::chrono::microseconds>(
                std::chrono::steady_clock::now()
                - validation_begin).count();
        if (validation_passed) {
            gpu_texture_validation_coverage_ =
                pending_validation_coverage;
        }
        log_.emit(
            "nv2a_gpu_texture_conversion_validation",
            {
                {"passed", json_bool(validation_passed)},
                {"textures", std::to_string(
                    validation_job_indices.size())},
                {"mips", std::to_string(validation_mip_count)},
                {"bytes", std::to_string(validation_byte_count)},
                {"mismatch_bytes", std::to_string(
                    validation_mismatch_bytes)},
                {"first_mismatch_address", std::to_string(
                    first_mismatch_address)},
                {"first_mismatch_mip", std::to_string(
                    first_mismatch_mip)},
                {"first_mismatch_byte", std::to_string(
                    first_mismatch_byte)},
                {"first_mismatch_expected", std::to_string(
                    first_mismatch_expected)},
                {"first_mismatch_actual", std::to_string(
                    first_mismatch_actual)},
                {"validation_cpu_us", std::to_string(
                    validation_cpu_us)},
                {"dxt1_covered", json_bool(
                    gpu_texture_validation_coverage_.dxt1)},
                {"dxt5_covered", json_bool(
                    gpu_texture_validation_coverage_.dxt5)},
                {"recovered_mips_covered", json_bool(
                    gpu_texture_validation_coverage_.recovered_mips)},
                {"generated_mips_covered", json_bool(
                    gpu_texture_validation_coverage_.generated_mips)},
                {"coverage_complete", json_bool(
                    gpu_texture_validation_coverage_.complete())},
                {"failure_action", json_string(
                    validation_passed
                        ? "accept_gpu_batch"
                        : "destroy_gpu_batch_and_use_cpu")},
            });
    }

    vkDestroyDescriptorPool(device_, descriptor_pool, nullptr);
    if (validation_buffer != VK_NULL_HANDLE) {
        vkDestroyBuffer(device_, validation_buffer, nullptr);
        vkFreeMemory(device_, validation_memory, nullptr);
    }
    vkDestroyBuffer(device_, output_buffer, nullptr);
    vkFreeMemory(device_, output_memory, nullptr);
    vkDestroyBuffer(device_, input_buffer, nullptr);
    vkFreeMemory(device_, input_memory, nullptr);
    if (!validation_passed) {
        for (const GpuTextureConversionJob& job : jobs) {
            destroy_host_texture(
                host_textures_[job.host_texture_index]);
        }
        ++gpu_texture_conversion_rejected_batch_count_;
        return false;
    }
    last_gpu_texture_conversion_us_ = std::chrono::duration_cast<
        std::chrono::microseconds>(
            std::chrono::steady_clock::now() - conversion_begin).count();
    gpu_texture_conversion_batch_count_ += 1u;
    gpu_texture_conversion_texture_count_ += jobs.size();
    gpu_texture_conversion_mip_count_ += mip_count;
    gpu_texture_conversion_input_bytes_ += input_size;
    gpu_texture_conversion_output_bytes_ += output_size;
    log_.emit(
        "nv2a_gpu_texture_conversion_batch",
        {
            {"textures", std::to_string(jobs.size())},
            {"dxt1_textures", std::to_string(dxt1_texture_count)},
            {"dxt5_textures", std::to_string(dxt5_texture_count)},
            {"recovered_chain_textures", std::to_string(
                recovered_chain_texture_count)},
            {"generated_chain_textures", std::to_string(
                generated_chain_texture_count)},
            {"mips", std::to_string(mip_count)},
            {"generated_mips", std::to_string(generated_mip_count)},
            {"input_bytes", std::to_string(input_size)},
            {"dxt1_input_bytes", std::to_string(dxt1_input_bytes)},
            {"dxt5_input_bytes", std::to_string(dxt5_input_bytes)},
            {"output_bytes", std::to_string(output_size)},
            {"dispatches", std::to_string(mip_count)},
            {"setup_us", std::to_string(setup_us)},
            {"gpu_submission_us", std::to_string(gpu_submission_us)},
            {"validation_cpu_us", std::to_string(validation_cpu_us)},
            {"conversion_us", std::to_string(
                last_gpu_texture_conversion_us_)},
            {"cumulative_batches", std::to_string(
                gpu_texture_conversion_batch_count_)},
            {"cumulative_textures", std::to_string(
                gpu_texture_conversion_texture_count_)},
            {"cumulative_mips", std::to_string(
                gpu_texture_conversion_mip_count_)},
            {"cumulative_input_bytes", std::to_string(
                gpu_texture_conversion_input_bytes_)},
            {"cumulative_output_bytes", std::to_string(
                gpu_texture_conversion_output_bytes_)},
            {"cumulative_rejected_batches", std::to_string(
                gpu_texture_conversion_rejected_batch_count_)},
        });
    return true;
}

std::optional<HostTexture> VulkanPresenter::create_cpu_converted_host_texture(
    const RecoveredTextureResource& resource,
    const std::string& content_identity,
    bool cubemap) {
    std::vector<uint8_t> rgba;
    std::vector<std::vector<uint8_t>> recovered_mips;
    if (cubemap) {
        const size_t face_payload_size = resource.format == "DXT1"
            ? static_cast<size_t>((resource.width + 3u) / 4u)
                * ((resource.height + 3u) / 4u) * 8u
            : resource.format == "DXT3" || resource.format == "DXT5"
                ? static_cast<size_t>((resource.width + 3u) / 4u)
                    * ((resource.height + 3u) / 4u) * 16u
                : static_cast<size_t>(resource.width) * resource.height
                    * (resource.format == "R5G6B5" ? 2u : 4u);
        if (resource.payload.size() < face_payload_size * 6u
            || resource.payload.size() % 6u != 0u) {
            return std::nullopt;
        }
        const size_t face_stride = resource.payload.size() / 6u;
        if (face_stride < face_payload_size) {
            return std::nullopt;
        }
        rgba.reserve(
            static_cast<size_t>(resource.width) * resource.height * 4u * 6u);
        for (uint32_t face = 0u; face < 6u; ++face) {
            RecoveredTextureResource face_resource = resource;
            const auto face_begin = resource.payload.begin()
                + static_cast<std::ptrdiff_t>(face * face_stride);
            face_resource.payload.assign(
                face_begin,
                face_begin + static_cast<std::ptrdiff_t>(face_payload_size));
            std::vector<uint8_t> face_rgba;
            if (resource.format == "DXT1") {
                face_rgba = decompress_dxt1(face_resource);
            } else if (resource.format == "DXT5") {
                face_rgba = decompress_dxt5(face_resource);
            } else if (resource.format == "R5G6B5") {
                face_rgba = convert_r5g6b5_texture(face_resource);
            } else if (resource.format == "A8R8G8B8"
                       || resource.format == "A8R8G8B8_LINEAR") {
                face_rgba = convert_bgra8_texture(
                    face_resource,
                    false,
                    resource.format == "A8R8G8B8");
            } else if (resource.format == "X8R8G8B8"
                       || resource.format == "X8R8G8B8_LINEAR") {
                face_rgba = convert_bgra8_texture(
                    face_resource,
                    true,
                    resource.format == "X8R8G8B8");
            } else {
                return std::nullopt;
            }
            rgba.insert(rgba.end(), face_rgba.begin(), face_rgba.end());
        }
    } else if (resource.format == "DXT1" || resource.format == "DXT5") {
        recovered_mips = build_cpu_dxt_conversion_mips(resource);
        if (recovered_mips.empty()) {
            return std::nullopt;
        }
        rgba = recovered_mips.front();
    } else if (resource.format == "R5G6B5") {
        rgba = convert_r5g6b5_texture(resource);
    } else if (resource.format == "A8R8G8B8"
               || resource.format == "A8R8G8B8_LINEAR") {
        rgba = convert_bgra8_texture(
            resource,
            false,
            resource.format == "A8R8G8B8");
    } else if (resource.format == "X8R8G8B8"
               || resource.format == "X8R8G8B8_LINEAR") {
        rgba = convert_bgra8_texture(
            resource,
            true,
            resource.format == "X8R8G8B8");
    } else {
        const size_t rgba_size = static_cast<size_t>(resource.width)
            * resource.height * 4u;
        rgba.assign(
            resource.payload.begin(),
            resource.payload.begin()
                + static_cast<std::ptrdiff_t>(rgba_size));
    }
    return create_host_texture(
        resource.address,
        resource.width,
        resource.height,
        resource.format,
        content_identity,
        rgba,
        VK_FORMAT_R8G8B8A8_UNORM,
        false,
        recovered_mips,
        cubemap);
}

void VulkanPresenter::clear_moved_texture_handles(HostTexture& texture) {
    texture.image = VK_NULL_HANDLE;
    texture.memory = VK_NULL_HANDLE;
    texture.view = VK_NULL_HANDLE;
}

void VulkanPresenter::destroy_host_texture(HostTexture& texture) {
    if (texture.view) vkDestroyImageView(device_, texture.view, nullptr);
    if (texture.image) vkDestroyImage(device_, texture.image, nullptr);
    if (texture.memory) vkFreeMemory(device_, texture.memory, nullptr);
    clear_moved_texture_handles(texture);
}

std::string VulkanPresenter::texture_content_identity(
    const RecoveredTextureResource& resource) {
    if (!resource.content_hash.empty()) {
        return resource.content_hash;
    }
    uint64_t fingerprint = 14695981039346656037ull;
    for (const uint8_t byte : resource.payload) {
        fingerprint ^= byte;
        fingerprint *= 1099511628211ull;
    }
    std::ostringstream value;
    value << "fnv64:" << std::hex << std::uppercase << std::setfill('0')
          << std::setw(16) << fingerprint;
    return value.str();
}

std::pair<uint32_t, uint32_t> VulkanPresenter::draw_surface_extent(
    const NativeDraw& draw) const {
    const uint32_t clip_x = draw.surface_clip_horizontal & 0xFFFFu;
    const uint32_t clip_y = draw.surface_clip_vertical & 0xFFFFu;
    const uint32_t clip_width =
        (draw.surface_clip_horizontal >> 16u) & 0xFFFFu;
    const uint32_t clip_height =
        (draw.surface_clip_vertical >> 16u) & 0xFFFFu;
    const uint32_t color_pitch = draw.surface_pitch & 0xFFFFu;
    uint32_t width = color_pitch / 4u;
    if (width == 0u) {
        width = clip_x == 0u ? clip_width : swapchain_extent_.width;
    }
    uint32_t height = clip_y == 0u
        ? clip_height
        : swapchain_extent_.height;
    if (height == 0u) {
        height = swapchain_extent_.height;
    }
    return {width, height};
}

uint32_t VulkanPresenter::select_presented_surface_color_offset() const {
    std::unordered_map<uint32_t, uint32_t> target_counts;
    const size_t first_draw = std::min<size_t>(
        interpreted_stream_.presented_draw_begin,
        interpreted_stream_.draws.size());
    const size_t end_draw = std::min<size_t>(
        first_draw + interpreted_stream_.presented_draw_count,
        interpreted_stream_.draws.size());
    uint32_t selected = 0u;
    uint32_t selected_count = 0u;
    for (size_t draw_index = first_draw; draw_index < end_draw; ++draw_index) {
        const NativeDraw& draw = interpreted_stream_.draws[draw_index];
        const auto [width, height] = draw_surface_extent(draw);
        if (draw.surface_color_offset == 0u
            || width != swapchain_extent_.width
            || height != swapchain_extent_.height) {
            continue;
        }
        const uint32_t count = ++target_counts[draw.surface_color_offset];
        if (count >= selected_count) {
            selected = draw.surface_color_offset;
            selected_count = count;
        }
    }
    return selected;
}

bool VulkanPresenter::draw_surface_clip_is_subsurface_viewport(
    const NativeDraw& draw) const {
    const uint32_t clip_x = draw.surface_clip_horizontal & 0xFFFFu;
    const uint32_t clip_width = draw.surface_clip_horizontal >> 16u;
    const uint32_t clip_y = draw.surface_clip_vertical & 0xFFFFu;
    const uint32_t clip_height = draw.surface_clip_vertical >> 16u;
    // An atlas target occupies a strict sub-rectangle on both axes. The
    // car-select preview deliberately uses the full presented width with a
    // vertically clipped viewport, so treating every viewport smaller on
    // either axis as offscreen drops the preview body draws.
    if (clip_width == 0u || clip_height == 0u
        || clip_width >= swapchain_extent_.width
        || clip_height >= swapchain_extent_.height
        || (draw.transform_execution_mode & 3u) != 2u) {
        return false;
    }

    const float viewport_scale_x =
        float_from_u32(draw.transform_constants[58][0]);
    const float viewport_scale_y =
        float_from_u32(draw.transform_constants[58][1]);
    const float viewport_offset_x =
        float_from_u32(draw.transform_constants[59][0]);
    const float viewport_offset_y =
        float_from_u32(draw.transform_constants[59][1]);
    if (!std::isfinite(viewport_scale_x)
        || !std::isfinite(viewport_scale_y)
        || !std::isfinite(viewport_offset_x)
        || !std::isfinite(viewport_offset_y)) {
        return false;
    }

    constexpr float kViewportTolerance = 0.01f;
    const float half_width = static_cast<float>(clip_width) * 0.5f;
    const float half_height = static_cast<float>(clip_height) * 0.5f;
    return std::abs(viewport_scale_x - half_width)
            <= kViewportTolerance
        && std::abs(viewport_scale_y + half_height)
            <= kViewportTolerance
        && std::abs(
            viewport_offset_x
                - (static_cast<float>(clip_x) + half_width
                    + b2r::nv2a::kNv2aViewportSubpixelBias))
            <= kViewportTolerance
        && std::abs(
            viewport_offset_y
                - (static_cast<float>(clip_y) + half_height
                    + b2r::nv2a::kNv2aViewportSubpixelBias))
            <= kViewportTolerance;
}

bool VulkanPresenter::draw_targets_presented_surface(const NativeDraw& draw) const {
    // The title builds camera textures in atlas-shaped regions of the
    // presented allocation. SET_SURFACE_CLIP alone cannot distinguish
    // those passes from UI scissors, but their programmable viewport is
    // centered and scaled to the clip rectangle exactly.
    if (draw_surface_clip_is_subsurface_viewport(draw)) {
        return false;
    }
    if (presented_surface_color_offset_ != 0u) {
        return draw.surface_color_offset == presented_surface_color_offset_;
    }
    if (draw.surface_clip_horizontal == 0u
        || draw.surface_clip_vertical == 0u) {
        return true;
    }
    const uint32_t clip_x = draw.surface_clip_horizontal & 0xFFFFu;
    const uint32_t clip_width = draw.surface_clip_horizontal >> 16u;
    const uint32_t clip_y = draw.surface_clip_vertical & 0xFFFFu;
    const uint32_t clip_height = draw.surface_clip_vertical >> 16u;
    // SET_SURFACE_CLIP is also the title's per-widget scissor. Once the
    // viewport-matched subpasses above are excluded, retain non-zero-origin
    // clips as draws on the current surface. Only an origin-zero
    // sub-surface remains a useful fallback discriminator when an address
    // was not captured.
    if (clip_x != 0u || clip_y != 0u) {
        return true;
    }
    return clip_width >= swapchain_extent_.width
        && clip_height >= swapchain_extent_.height;
}

VkRect2D VulkanPresenter::draw_scissor(
    const NativeDraw& draw,
    VkExtent2D target_extent) const {
    const uint32_t clip_x = draw.surface_clip_horizontal & 0xFFFFu;
    const uint32_t clip_width = draw.surface_clip_horizontal >> 16u;
    const uint32_t clip_y = draw.surface_clip_vertical & 0xFFFFu;
    const uint32_t clip_height = draw.surface_clip_vertical >> 16u;
    if (clip_width == 0u || clip_height == 0u) {
        return {{0, 0}, target_extent};
    }
    const uint32_t bounded_x = std::min(clip_x, target_extent.width);
    const uint32_t bounded_y = std::min(clip_y, target_extent.height);
    return {
        {
            static_cast<int32_t>(bounded_x),
            static_cast<int32_t>(bounded_y),
        },
        {
            std::min(clip_width, target_extent.width - bounded_x),
            std::min(clip_height, target_extent.height - bounded_y),
        },
    };
}

bool VulkanPresenter::draw_has_supported_host_transform(const NativeDraw& draw) {
    const uint32_t mode = draw.transform_execution_mode & 3u;
    return mode == 2u
        || (mode == 0u && fixed_function_composite_matrix_valid(draw));
}

const std::vector<RenderTargetFeedbackSpec>&
VulkanPresenter::presented_render_target_feedback_specs() const {
    if (feedback_spec_cache_generation_ == render_work_generation_) {
        ++feedback_spec_cache_hit_count_;
        return feedback_spec_cache_;
    }
    const auto build_begin = std::chrono::steady_clock::now();
    feedback_spec_cache_.clear();
    std::vector<RenderTargetFeedbackSpec>& specs = feedback_spec_cache_;
    auto append_unique = [&](RenderTargetFeedbackSpec spec) {
        if (spec.address == 0u
            || spec.width == 0u
            || spec.height == 0u) {
            return;
        }
        const auto duplicate = std::find_if(
            specs.begin(),
            specs.end(),
            [&](const RenderTargetFeedbackSpec& existing) {
                return existing.address == spec.address
                    && existing.width == spec.width
                    && existing.height == spec.height
                    && existing.format == spec.format;
            });
        if (duplicate == specs.end()) {
            specs.push_back(std::move(spec));
        }
    };

    const size_t first_draw = std::min<size_t>(
        interpreted_stream_.presented_draw_begin,
        interpreted_stream_.draws.size());
    const size_t end_draw = std::min<size_t>(
        first_draw + interpreted_stream_.presented_draw_count,
        interpreted_stream_.draws.size());
    for (size_t draw_index = first_draw; draw_index < end_draw; ++draw_index) {
        const size_t presented_index = draw_index - first_draw;
        if (presented_index < options_.presented_draw_begin
            || presented_index >= options_.presented_draw_end) {
            continue;
        }
        const NativeDraw& draw = interpreted_stream_.draws[draw_index];
        if (!draw_targets_presented_surface(draw)) {
            continue;
        }
        const auto [width, height] = draw_surface_extent(draw);
        std::string format = "A8R8G8B8_LINEAR";
        if (draw.texture_enabled
            && draw.texture_stage < draw.texture_formats.size()
            && ((draw.texture_formats[draw.texture_stage] >> 8u) & 0xFFu)
                == 0x1Eu) {
            format = "X8R8G8B8_LINEAR";
        }
        append_unique({
            draw.surface_color_offset,
            width,
            height,
            format,
        });

        if (!draw.texture_enabled
            || draw.texture_address == 0u
            || draw.texture_stage >= draw.texture_formats.size()) {
            continue;
        }
        const uint32_t format_raw =
            draw.texture_formats[draw.texture_stage];
        if (!nv2a_texture_format_is_linear(format_raw)) {
            continue;
        }
        const auto [texture_width, texture_height] = nv2a_texture_extent(
            format_raw,
            draw.texture_image_rects[draw.texture_stage]);
        const uint64_t surface_bytes = static_cast<uint64_t>(width)
            * static_cast<uint64_t>(height) * 4u;
        const uint64_t address_delta =
            draw.texture_address > draw.surface_color_offset
            ? static_cast<uint64_t>(
                draw.texture_address - draw.surface_color_offset)
            : static_cast<uint64_t>(
                draw.surface_color_offset - draw.texture_address);
        if (texture_width == width
            && texture_height == height
            && address_delta == surface_bytes) {
            append_unique({
                draw.texture_address,
                texture_width,
                texture_height,
                ((format_raw >> 8u) & 0xFFu) == 0x1Eu
                    ? "X8R8G8B8_LINEAR"
                    : "A8R8G8B8_LINEAR",
            });
        }
    }

    // The title renders camera views into clipped regions of the presented
    // allocation before copying those regions into dependent textures. Keep
    // a GPU-backed version of that allocation and replay the clipped writes
    // into it before dependent offscreen passes run.
    for (RenderTargetFeedbackSpec& spec : specs) {
        if (nv2a_canonical_resource_address(spec.address)
            != nv2a_canonical_resource_address(
                presented_surface_color_offset_)) {
            continue;
        }
        const auto atlas_producer = std::find_if(
            interpreted_stream_.draws.begin()
                + static_cast<std::ptrdiff_t>(first_draw),
            interpreted_stream_.draws.begin()
                + static_cast<std::ptrdiff_t>(end_draw),
            [&](const NativeDraw& draw) {
                return !draw_targets_presented_surface(draw)
                    && draw_surface_clip_is_subsurface_viewport(draw)
                    && nv2a_canonical_resource_address(
                        draw.surface_color_offset)
                        == nv2a_canonical_resource_address(spec.address);
            });
        if (atlas_producer != interpreted_stream_.draws.begin()
                + static_cast<std::ptrdiff_t>(end_draw)) {
            spec.producer_address = atlas_producer->surface_color_offset;
            spec.offscreen_produced = true;
        }
    }

    // Surface offsets and texture DMA offsets can name the same allocation
    // through different NV2A windows. Retain any offscreen surface that is
    // sampled later in the frame so the producer pass can be replayed into
    // the host texture instead of uploading stale CPU backing bytes.
    std::map<std::array<uint32_t, 3>, const NativeDraw*>
        latest_offscreen_producers;
    std::unordered_map<uint32_t, const NativeDraw*>
        latest_offscreen_producers_by_address;
    for (size_t consumer_index = first_draw;
         consumer_index < end_draw;
         ++consumer_index) {
        const NativeDraw& consumer =
            interpreted_stream_.draws[consumer_index];
        for (uint32_t stage = 0u;
             stage < consumer.texture_formats.size();
             ++stage) {
            const uint32_t texture_mode =
                (consumer.shader_stage_program >> (stage * 5u)) & 0x1Fu;
            const uint32_t texture_address = consumer.texture_offsets[stage];
            if (texture_mode == 0u
                || (consumer.texture_controls[stage] & (1u << 30u)) == 0u
                || texture_address == 0u) {
                continue;
            }
            const uint32_t format_raw = consumer.texture_formats[stage];
            const uint32_t color_format = (format_raw >> 8u) & 0xFFu;
            std::string format;
            switch (color_format) {
            case 0x05u: format = "R5G6B5"; break;
            case 0x06u: format = "A8R8G8B8"; break;
            case 0x07u: format = "X8R8G8B8"; break;
            case 0x12u: format = "A8R8G8B8_LINEAR"; break;
            case 0x1Eu: format = "X8R8G8B8_LINEAR"; break;
            default: break;
            }
            if (!format.empty()) {
                const auto [texture_width, texture_height] =
                    nv2a_texture_extent(
                        format_raw,
                        consumer.texture_image_rects[stage]);
                if (nv2a_texture_format_is_cubemap(format_raw)) {
                    const uint32_t bytes_per_pixel =
                        b2r::nv2a::nv2a_texture_uncompressed_bytes_per_pixel(
                            format_raw);
                    const uint64_t face_stride = static_cast<uint64_t>(
                        texture_width) * texture_height * bytes_per_pixel;
                    std::array<uint32_t, 6> face_producers{};
                    bool complete = bytes_per_pixel != 0u
                        && face_stride <= std::numeric_limits<uint32_t>::max();
                    for (uint32_t face = 0u;
                         complete && face < face_producers.size();
                         ++face) {
                        const uint64_t face_address =
                            static_cast<uint64_t>(texture_address)
                                + face_stride * face;
                        if (face_address
                            > std::numeric_limits<uint32_t>::max()) {
                            complete = false;
                            break;
                        }
                        const auto producer =
                            latest_offscreen_producers_by_address.find(
                                nv2a_canonical_resource_address(
                                    static_cast<uint32_t>(face_address)));
                        if (producer
                            == latest_offscreen_producers_by_address.end()) {
                            complete = false;
                            break;
                        }
                        const uint32_t clip_width =
                            producer->second->surface_clip_horizontal >> 16u;
                        const uint32_t clip_height =
                            producer->second->surface_clip_vertical >> 16u;
                        if (clip_width != texture_width
                            || clip_height != texture_height) {
                            complete = false;
                            break;
                        }
                        face_producers[face] =
                            producer->second->surface_color_offset;
                    }
                    if (complete) {
                        RenderTargetFeedbackSpec spec{};
                        spec.address = texture_address;
                        spec.width = texture_width;
                        spec.height = texture_height;
                        spec.format = std::move(format);
                        spec.producer_address = face_producers.front();
                        spec.offscreen_produced = true;
                        spec.cubemap = true;
                        spec.cubemap_face_producer_addresses =
                            face_producers;
                        append_unique(std::move(spec));
                    }
                    continue;
                }
                const std::array<uint32_t, 3> producer_key{
                    nv2a_canonical_resource_address(
                        texture_address),
                    texture_width,
                    texture_height,
                };
                const auto producer = latest_offscreen_producers.find(
                    producer_key);
                if (producer != latest_offscreen_producers.end()) {
                    append_unique({
                        texture_address,
                        texture_width,
                        texture_height,
                        std::move(format),
                        producer->second->surface_color_offset,
                        true,
                    });
                }
            }
        }
        if (!draw_targets_presented_surface(consumer)) {
            const auto [surface_width, surface_height] =
                draw_surface_extent(consumer);
            latest_offscreen_producers[{
                nv2a_canonical_resource_address(
                    consumer.surface_color_offset),
                surface_width,
                surface_height,
            }] = &consumer;
            latest_offscreen_producers_by_address[
                nv2a_canonical_resource_address(
                    consumer.surface_color_offset)] = &consumer;
        }
    }
    feedback_spec_cache_generation_ = render_work_generation_;
    ++feedback_spec_cache_build_count_;
    last_feedback_spec_build_us_ = std::chrono::duration_cast<
        std::chrono::microseconds>(
            std::chrono::steady_clock::now() - build_begin).count();
    return feedback_spec_cache_;
}

bool VulkanPresenter::render_target_feedback_texture_matches_spec(
    const HostTexture& texture,
    const RenderTargetFeedbackSpec& spec) {
    return texture.render_target_feedback
        && texture.guest_address == spec.address
        && texture.width == spec.width
        && texture.height == spec.height
        && texture.format == spec.format
        && texture.cubemap == spec.cubemap;
}

void VulkanPresenter::cache_render_target_feedback_texture(HostTexture& texture) {
    if (!texture.render_target_feedback
        || texture.image == VK_NULL_HANDLE
        || texture.memory == VK_NULL_HANDLE
        || texture.view == VK_NULL_HANDLE) {
        return;
    }
    const auto duplicate = std::find_if(
        render_target_feedback_image_cache_.begin(),
        render_target_feedback_image_cache_.end(),
        [&](const HostTexture& cached) {
            return cached.guest_address == texture.guest_address
                && cached.width == texture.width
                && cached.height == texture.height
                && cached.format == texture.format
                && cached.cubemap == texture.cubemap;
        });
    if (duplicate != render_target_feedback_image_cache_.end()) {
        destroy_host_texture(*duplicate);
        render_target_feedback_image_cache_.erase(duplicate);
        ++last_render_target_feedback_image_cache_eviction_count_;
        ++render_target_feedback_image_cache_eviction_count_;
    }
    render_target_feedback_image_cache_.push_back(std::move(texture));
    clear_moved_texture_handles(texture);
    ++last_render_target_feedback_image_cache_store_count_;
    ++render_target_feedback_image_cache_store_count_;
}

void VulkanPresenter::trim_render_target_feedback_image_cache() {
    while (render_target_feedback_image_cache_.size()
           > kRenderTargetFeedbackImageCacheCapacity) {
        destroy_host_texture(render_target_feedback_image_cache_.front());
        render_target_feedback_image_cache_.erase(
            render_target_feedback_image_cache_.begin());
        ++last_render_target_feedback_image_cache_eviction_count_;
        ++render_target_feedback_image_cache_eviction_count_;
    }
}

bool VulkanPresenter::render_target_feedback_refresh_required() const {
    const std::vector<RenderTargetFeedbackSpec>& specs =
        presented_render_target_feedback_specs();
    const size_t active_count = static_cast<size_t>(std::count_if(
        host_textures_.begin(),
        host_textures_.end(),
        [](const HostTexture& texture) {
            return texture.render_target_feedback;
        }));
    if (active_count != specs.size()) {
        return true;
    }
    return std::any_of(
        specs.begin(),
        specs.end(),
        [&](const RenderTargetFeedbackSpec& spec) {
            return std::none_of(
                host_textures_.begin(),
                host_textures_.end(),
                [&](const HostTexture& texture) {
                    return render_target_feedback_texture_matches_spec(
                        texture,
                        spec);
                });
        });
}

HostTexture VulkanPresenter::create_render_target_feedback_texture(
    const RenderTargetFeedbackSpec& spec) {
    const size_t layer_count = spec.cubemap ? 6u : 1u;
    return create_host_texture(
        spec.address,
        spec.width,
        spec.height,
        spec.format,
        "render-target-feedback",
        std::vector<uint8_t>(
            static_cast<size_t>(spec.width) * spec.height * 4u
                * layer_count,
            0u),
        swapchain_format_,
        true,
        {},
        spec.cubemap);
}

void VulkanPresenter::destroy_offscreen_render_targets() {
    for (OffscreenRenderTarget& target : offscreen_render_targets_) {
        if (target.framebuffer != VK_NULL_HANDLE) {
            vkDestroyFramebuffer(device_, target.framebuffer, nullptr);
            target.framebuffer = VK_NULL_HANDLE;
        }
        if (target.owns_color_view
            && target.color_view != VK_NULL_HANDLE) {
            vkDestroyImageView(device_, target.color_view, nullptr);
        }
        destroy_depth_attachment(
            target.depth_image,
            target.depth_memory,
            target.depth_view);
        target.color_image = VK_NULL_HANDLE;
        target.color_view = VK_NULL_HANDLE;
        target.owns_color_view = false;
    }
    offscreen_render_targets_.clear();
}

bool VulkanPresenter::offscreen_render_targets_match_presented_specs() const {
    const std::vector<RenderTargetFeedbackSpec>& specs =
        presented_render_target_feedback_specs();
    std::vector<RenderTargetFeedbackSpec> expected;
    for (const RenderTargetFeedbackSpec& spec : specs) {
        if (spec.offscreen_produced) {
            const uint32_t target_count = spec.cubemap ? 6u : 1u;
            expected.insert(expected.end(), target_count, spec);
        }
    }
    if (expected.size() != offscreen_render_targets_.size()) {
        return false;
    }
    for (size_t index = 0; index < expected.size(); ++index) {
        const OffscreenRenderTarget& target = offscreen_render_targets_[index];
        if (!(target.spec == expected[index])
            || target.framebuffer == VK_NULL_HANDLE
            || target.color_image == VK_NULL_HANDLE
            || target.color_view == VK_NULL_HANDLE
            || target.depth_image == VK_NULL_HANDLE
            || target.depth_memory == VK_NULL_HANDLE
            || target.depth_view == VK_NULL_HANDLE) {
            return false;
        }
        const auto backing_texture = std::find_if(
            host_textures_.begin(),
            host_textures_.end(),
            [&](const HostTexture& texture) {
                return texture.image != VK_NULL_HANDLE
                    && texture.memory != VK_NULL_HANDLE
                    && texture.image == target.color_image
                    && render_target_feedback_texture_matches_spec(
                        texture,
                        expected[index]);
            });
        if (backing_texture == host_textures_.end()) {
            return false;
        }
    }
    return true;
}

void VulkanPresenter::create_offscreen_render_targets() {
    destroy_offscreen_render_targets();
    const std::vector<RenderTargetFeedbackSpec>& specs =
        presented_render_target_feedback_specs();
    offscreen_render_targets_.reserve(specs.size());
    for (const RenderTargetFeedbackSpec& spec : specs) {
        if (!spec.offscreen_produced) {
            continue;
        }
        const auto texture = std::find_if(
            host_textures_.begin(),
            host_textures_.end(),
            [&](const HostTexture& candidate) {
                return render_target_feedback_texture_matches_spec(
                    candidate, spec);
            });
        if (texture == host_textures_.end()) {
            continue;
        }
        const uint32_t target_count = spec.cubemap ? 6u : 1u;
        for (uint32_t layer = 0u; layer < target_count; ++layer) {
            OffscreenRenderTarget target{};
            target.spec = spec;
            target.array_layer = layer;
            target.producer_address = spec.cubemap
                ? spec.cubemap_face_producer_addresses[layer]
                : spec.producer_address;
            target.color_image = texture->image;
            target.color_view = texture->view;
            try {
                if (spec.cubemap) {
                    VkImageViewCreateInfo view_info{};
                    view_info.sType =
                        VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO;
                    view_info.image = texture->image;
                    view_info.viewType = VK_IMAGE_VIEW_TYPE_2D;
                    view_info.format = texture->image_format;
                    view_info.subresourceRange.aspectMask =
                        VK_IMAGE_ASPECT_COLOR_BIT;
                    view_info.subresourceRange.baseArrayLayer = layer;
                    view_info.subresourceRange.layerCount = 1u;
                    view_info.subresourceRange.levelCount = 1u;
                    vk_check(
                        vkCreateImageView(
                            device_,
                            &view_info,
                            nullptr,
                            &target.color_view),
                        "vkCreateImageView(offscreen cube face)");
                    target.owns_color_view = true;
                }
                create_depth_attachment(
                    spec.width,
                    spec.height,
                    target.depth_image,
                    target.depth_memory,
                    target.depth_view);
                const std::array<VkImageView, 2> attachments = {
                    target.color_view,
                    target.depth_view,
                };
                VkFramebufferCreateInfo create_info{};
                create_info.sType = VK_STRUCTURE_TYPE_FRAMEBUFFER_CREATE_INFO;
                create_info.renderPass = render_pass_;
                create_info.attachmentCount = static_cast<uint32_t>(
                    attachments.size());
                create_info.pAttachments = attachments.data();
                create_info.width = spec.width;
                create_info.height = spec.height;
                create_info.layers = 1;
                vk_check(
                    vkCreateFramebuffer(
                        device_,
                        &create_info,
                        nullptr,
                        &target.framebuffer),
                    "vkCreateFramebuffer(offscreen)");
            } catch (...) {
                if (target.owns_color_view
                    && target.color_view != VK_NULL_HANDLE) {
                    vkDestroyImageView(
                        device_, target.color_view, nullptr);
                }
                destroy_depth_attachment(
                    target.depth_image,
                    target.depth_memory,
                    target.depth_view);
                throw;
            }
            offscreen_render_targets_.push_back(std::move(target));
        }
    }
    log_.emit(
        "nv2a_offscreen_render_targets_created",
        {
            {"count", std::to_string(offscreen_render_targets_.size())},
            {"cubemap_face_count", std::to_string(std::count_if(
                offscreen_render_targets_.begin(),
                offscreen_render_targets_.end(),
                [](const OffscreenRenderTarget& target) {
                    return target.spec.cubemap;
                }))},
            {"dedicated_depth_count", std::to_string(
                std::count_if(
                    offscreen_render_targets_.begin(),
                    offscreen_render_targets_.end(),
                    [](const OffscreenRenderTarget& target) {
                        return target.depth_view != VK_NULL_HANDLE;
                    }))},
            {"shares_presented_depth", json_bool(false)},
        });
}

void VulkanPresenter::destroy_host_texture_bindings() {
    if (texture_descriptor_pool_) {
        vkDestroyDescriptorPool(device_, texture_descriptor_pool_, nullptr);
        texture_descriptor_pool_ = VK_NULL_HANDLE;
    }
    for (const HostTextureBinding& binding : host_texture_bindings_) {
        for (const HostTextureStageBinding& stage : binding.stages) {
            if (stage.sampler) {
                vkDestroySampler(device_, stage.sampler, nullptr);
            }
        }
    }
    host_texture_bindings_.clear();
}

size_t VulkanPresenter::host_texture_index_for_draw(const NativeDraw& draw) const {
    return host_texture_index_for_stage(draw, draw.texture_stage);
}

size_t VulkanPresenter::host_texture_index_for_stage(
    const NativeDraw& draw,
    uint32_t stage) const {
    if (stage >= draw.texture_formats.size()) {
        return 0u;
    }
    const auto match = std::find_if(
        host_textures_.begin(),
        host_textures_.end(),
        [&](const HostTexture& texture) {
            return host_texture_matches_stage(texture, draw, stage);
        });
    return match == host_textures_.end()
        ? 0u
        : static_cast<size_t>(match - host_textures_.begin());
}

std::vector<HostTextureBindingSpec>
VulkanPresenter::required_host_texture_bindings() const {
    std::vector<HostTextureBindingSpec> required(1u);
    const size_t first_draw = std::min<size_t>(
        interpreted_stream_.presented_draw_begin,
        interpreted_stream_.draws.size());
    const size_t end_draw = std::min<size_t>(
        first_draw + interpreted_stream_.presented_draw_count,
        interpreted_stream_.draws.size());
    const std::vector<RenderTargetFeedbackSpec>& feedback_specs =
        presented_render_target_feedback_specs();
    for (size_t draw_index = first_draw; draw_index < end_draw; ++draw_index) {
        const size_t presented_index = draw_index - first_draw;
        if (presented_index < options_.presented_draw_begin
            || presented_index >= options_.presented_draw_end) {
            continue;
        }
        const NativeDraw& draw = interpreted_stream_.draws[draw_index];
        const bool targets_offscreen_feedback = std::any_of(
            feedback_specs.begin(),
            feedback_specs.end(),
            [&](const RenderTargetFeedbackSpec& target) {
                return target.offscreen_produced
                    && target.producer_matches(
                        draw.surface_color_offset);
            });
        if ((!draw_targets_presented_surface(draw)
                && !targets_offscreen_feedback)
            || !draw_has_supported_host_transform(draw)
            || !native_pipeline_primitive_supported(draw.primitive)
            || draw.vertex_count == 0u) {
            continue;
        }
        HostTextureBindingSpec spec{};
        for (uint32_t stage = 0u; stage < spec.stages.size(); ++stage) {
            const uint32_t texture_mode =
                (draw.shader_stage_program >> (stage * 5u)) & 0x1Fu;
            const bool enabled = texture_mode != 0u
                && (draw.texture_controls[stage] & (1u << 30u)) != 0u;
            spec.stages[stage] = {
                enabled ? host_texture_index_for_stage(draw, stage) : 0u,
                enabled ? draw.texture_addresses[stage] : 0u,
                enabled ? draw.texture_formats[stage] : 0u,
                enabled ? draw.texture_controls[stage] : 0u,
                enabled ? draw.texture_filters[stage] : 0u,
            };
        }
        if (std::find(required.begin(), required.end(), spec)
            == required.end()) {
            required.push_back(spec);
        }
    }
    return required;
}

void VulkanPresenter::refresh_host_texture_bindings() {
    const auto update_begin = std::chrono::steady_clock::now();
    last_texture_binding_update_us_ = 0;
    last_texture_binding_set_reused_ = false;
    last_texture_binding_image_descriptor_update_count_ = 0u;
    last_texture_binding_descriptor_set_allocation_count_ = 0u;
    if (host_textures_.empty()) {
        destroy_host_texture_bindings();
        last_texture_binding_update_us_ = std::chrono::duration_cast<
            std::chrono::microseconds>(
                std::chrono::steady_clock::now() - update_begin).count();
        return;
    }
    const std::vector<HostTextureBindingSpec> required =
        required_host_texture_bindings();
    const bool layout_unchanged = texture_descriptor_pool_ != VK_NULL_HANDLE
        && required.size() == host_texture_bindings_.size()
        && std::equal(
            required.begin(), required.end(),
            host_texture_bindings_.begin(),
            [&](const HostTextureBindingSpec& spec,
                const HostTextureBinding& binding) {
                for (uint32_t stage = 0u; stage < spec.stages.size(); ++stage) {
                    const auto& expected = spec.stages[stage];
                    const auto& actual = binding.stages[stage];
                    if (expected.texture_index >= host_textures_.size()
                        || expected.texture_index != actual.texture_index
                        || expected.address != actual.address
                        || expected.format != actual.format
                        || expected.control != actual.control
                        || expected.filter != actual.filter
                        || actual.texture_mip_levels
                            != host_textures_[expected.texture_index].mip_levels) {
                        return false;
                    }
                }
                return true;
            });
    if (layout_unchanged) {
        uint32_t image_descriptor_update_count = 0u;
        uint32_t repeat_binding_count = 0u;
        uint32_t mirrored_repeat_binding_count = 0u;
        for (HostTextureBinding& binding : host_texture_bindings_) {
            std::array<VkDescriptorImageInfo, 8> image_infos{};
            std::array<VkWriteDescriptorSet, 8> writes{};
            uint32_t write_count = 0u;
            for (uint32_t stage = 0u; stage < binding.stages.size(); ++stage) {
                HostTextureStageBinding& stage_binding = binding.stages[stage];
                const HostTexture& texture =
                    host_textures_[stage_binding.texture_index];
                const VkImageView view_2d = texture.cubemap
                    ? host_textures_[0].view
                    : texture.view;
                const VkImageView view_cube = texture.cubemap
                    ? texture.view
                    : host_textures_[1].view;
                const std::array<VkImageView, 2> views{view_2d, view_cube};
                std::array<VkImageView*, 2> cached_views{
                    &stage_binding.texture_2d_view,
                    &stage_binding.texture_cube_view,
                };
                for (uint32_t dimension = 0u; dimension < 2u; ++dimension) {
                    if (*cached_views[dimension] == views[dimension]) {
                        continue;
                    }
                    image_infos[write_count] = {
                        stage_binding.sampler,
                        views[dimension],
                        VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,
                    };
                    VkWriteDescriptorSet& write = writes[write_count];
                    write.sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
                    write.dstSet = binding.descriptor_set;
                    write.dstBinding = stage + dimension * 4u;
                    write.descriptorCount = 1u;
                    write.descriptorType =
                        VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER;
                    write.pImageInfo = &image_infos[write_count];
                    *cached_views[dimension] = views[dimension];
                    ++write_count;
                }
                const uint32_t u_mode = stage_binding.address & 7u;
                const uint32_t v_mode =
                    (stage_binding.address >> 8u) & 7u;
                repeat_binding_count +=
                    u_mode == 1u || v_mode == 1u ? 1u : 0u;
                mirrored_repeat_binding_count +=
                    u_mode == 2u || v_mode == 2u ? 1u : 0u;
            }
            if (write_count != 0u) {
                vkUpdateDescriptorSets(
                    device_, write_count, writes.data(), 0u, nullptr);
                image_descriptor_update_count += write_count;
            }
        }
        ++texture_binding_set_reuse_count_;
        texture_binding_image_descriptor_update_count_ +=
            image_descriptor_update_count;
        last_texture_binding_set_reused_ = true;
        last_texture_binding_image_descriptor_update_count_ =
            image_descriptor_update_count;
        last_texture_binding_update_us_ = std::chrono::duration_cast<
            std::chrono::microseconds>(
                std::chrono::steady_clock::now() - update_begin).count();
        log_.emit(
            "nv2a_texture_sampler_bindings_refreshed",
            {
                {"bindings", std::to_string(host_texture_bindings_.size())},
                {"binding_set_reused", json_bool(true)},
                {"image_descriptor_updates", std::to_string(
                    image_descriptor_update_count)},
                {"descriptor_sets_allocated", "0"},
                {"update_us", std::to_string(
                    last_texture_binding_update_us_)},
                {"repeat_bindings", std::to_string(repeat_binding_count)},
                {"mirrored_repeat_bindings", std::to_string(
                    mirrored_repeat_binding_count)},
            });
        return;
    }
    destroy_host_texture_bindings();
    host_texture_bindings_.reserve(required.size());
    for (const HostTextureBindingSpec& spec : required) {
        HostTextureBinding binding{};
        for (uint32_t stage = 0u; stage < spec.stages.size(); ++stage) {
            const HostTextureStageBindingSpec& stage_spec = spec.stages[stage];
            HostTextureStageBinding& stage_binding = binding.stages[stage];
            stage_binding.texture_index = stage_spec.texture_index;
            stage_binding.address = stage_spec.address;
            stage_binding.format = stage_spec.format;
            stage_binding.control = stage_spec.control;
            stage_binding.filter = stage_spec.filter;
            const HostTexture& texture =
                host_textures_[stage_spec.texture_index];
            stage_binding.texture_mip_levels = texture.mip_levels;
            stage_binding.texture_2d_view = texture.cubemap
                ? host_textures_[0].view
                : texture.view;
            stage_binding.texture_cube_view = texture.cubemap
                ? texture.view
                : host_textures_[1].view;
            VkSamplerCreateInfo sampler_info{};
            sampler_info.sType = VK_STRUCTURE_TYPE_SAMPLER_CREATE_INFO;
            const uint32_t min_filter = (stage_spec.filter >> 16u) & 0xFFu;
            const uint32_t mag_filter = (stage_spec.filter >> 24u) & 0x0Fu;
            sampler_info.magFilter = mag_filter == 1u
                ? VK_FILTER_NEAREST : VK_FILTER_LINEAR;
            sampler_info.minFilter = min_filter == 1u
                    || min_filter == 3u || min_filter == 5u
                ? VK_FILTER_NEAREST : VK_FILTER_LINEAR;
            sampler_info.mipmapMode = min_filter == 5u || min_filter == 6u
                ? VK_SAMPLER_MIPMAP_MODE_LINEAR
                : VK_SAMPLER_MIPMAP_MODE_NEAREST;
            sampler_info.addressModeU =
                nv2a_sampler_address_mode(stage_spec.address);
            sampler_info.addressModeV =
                nv2a_sampler_address_mode(stage_spec.address >> 8u);
            sampler_info.addressModeW =
                nv2a_sampler_address_mode(stage_spec.address >> 16u);
            sampler_info.borderColor =
                VK_BORDER_COLOR_FLOAT_TRANSPARENT_BLACK;
            const uint32_t requested_mip_levels =
                (stage_spec.format >> 16u) & 0x0Fu;
            const uint32_t available_mip_levels = std::min(
                texture.mip_levels,
                std::max(requested_mip_levels, 1u));
            sampler_info.maxLod = min_filter >= 3u
                ? static_cast<float>(available_mip_levels - 1u)
                : 0.0f;
            int32_t lod_bias = static_cast<int32_t>(
                stage_spec.filter & 0x1FFFu);
            if ((lod_bias & 0x1000) != 0) {
                lod_bias -= 0x2000;
            }
            sampler_info.mipLodBias = static_cast<float>(lod_bias) / 256.0f;
            vk_check(
                vkCreateSampler(
                    device_,
                    &sampler_info,
                    nullptr,
                    &stage_binding.sampler),
                "vkCreateSampler(texture stage binding)");
        }
        host_texture_bindings_.push_back(binding);
    }

    const uint32_t descriptor_count = static_cast<uint32_t>(
        host_texture_bindings_.size());
    std::array<VkDescriptorPoolSize, 2> pool_sizes{{
        {
            VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER,
            descriptor_count * 8u,
        },
        {
            VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,
            descriptor_count * 4u,
        },
    }};
    VkDescriptorPoolCreateInfo pool_info{};
    pool_info.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO;
    pool_info.maxSets = descriptor_count;
    pool_info.poolSizeCount = static_cast<uint32_t>(pool_sizes.size());
    pool_info.pPoolSizes = pool_sizes.data();
    vk_check(
        vkCreateDescriptorPool(
            device_, &pool_info, nullptr, &texture_descriptor_pool_),
        "vkCreateDescriptorPool");
    std::vector<VkDescriptorSetLayout> layouts(
        host_texture_bindings_.size(), texture_descriptor_layout_);
    std::vector<VkDescriptorSet> descriptor_sets(
        host_texture_bindings_.size());
    VkDescriptorSetAllocateInfo descriptor_allocation{};
    descriptor_allocation.sType =
        VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO;
    descriptor_allocation.descriptorPool = texture_descriptor_pool_;
    descriptor_allocation.descriptorSetCount = descriptor_count;
    descriptor_allocation.pSetLayouts = layouts.data();
    vk_check(
        vkAllocateDescriptorSets(
            device_, &descriptor_allocation, descriptor_sets.data()),
        "vkAllocateDescriptorSets");
    descriptor_allocation_count_ += descriptor_count;
    uint32_t repeat_binding_count = 0u;
    uint32_t mirrored_repeat_binding_count = 0u;
    for (size_t index = 0; index < host_texture_bindings_.size(); ++index) {
        HostTextureBinding& binding = host_texture_bindings_[index];
        binding.descriptor_set = descriptor_sets[index];
        std::array<VkDescriptorImageInfo, 8> image_infos{};
        for (uint32_t stage = 0u; stage < binding.stages.size(); ++stage) {
            const HostTextureStageBinding& stage_binding =
                binding.stages[stage];
            image_infos[stage] = {
                stage_binding.sampler,
                stage_binding.texture_2d_view,
                VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,
            };
            image_infos[4u + stage] = {
                stage_binding.sampler,
                stage_binding.texture_cube_view,
                VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,
            };
        }
        VkDescriptorBufferInfo fragment_buffer_info{
            fragment_state_buffer_,
            0,
            fragment_state_buffer_size_};
        VkDescriptorBufferInfo vertex_program_buffer_info{
            vertex_program_state_buffer_,
            0,
            vertex_program_state_buffer_size_};
        VkDescriptorBufferInfo vertex_buffer_info{
            vertex_buffer_,
            0,
            vertex_buffer_size_};
        VkDescriptorBufferInfo raw_resource_buffer_info{
            raw_vertex_resource_buffer_,
            0,
            raw_vertex_resource_buffer_size_};
        std::array<VkWriteDescriptorSet, 12> writes{};
        for (uint32_t image = 0u; image < image_infos.size(); ++image) {
            writes[image].sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
            writes[image].dstSet = binding.descriptor_set;
            writes[image].dstBinding = image;
            writes[image].descriptorCount = 1u;
            writes[image].descriptorType =
                VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER;
            writes[image].pImageInfo = &image_infos[image];
        }
        const std::array<VkDescriptorBufferInfo*, 4> buffer_infos{
            &fragment_buffer_info,
            &vertex_program_buffer_info,
            &vertex_buffer_info,
            &raw_resource_buffer_info,
        };
        for (uint32_t buffer = 0u; buffer < buffer_infos.size(); ++buffer) {
            VkWriteDescriptorSet& write = writes[8u + buffer];
            write.sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
            write.dstSet = binding.descriptor_set;
            write.dstBinding = 8u + buffer;
            write.descriptorCount = 1u;
            write.descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;
            write.pBufferInfo = buffer_infos[buffer];
        }
        vkUpdateDescriptorSets(
            device_,
            static_cast<uint32_t>(writes.size()),
            writes.data(),
            0,
            nullptr);
        for (const HostTextureStageBinding& stage : binding.stages) {
            const uint32_t u_mode = stage.address & 7u;
            const uint32_t v_mode = (stage.address >> 8u) & 7u;
            repeat_binding_count +=
                u_mode == 1u || v_mode == 1u ? 1u : 0u;
            mirrored_repeat_binding_count +=
                u_mode == 2u || v_mode == 2u ? 1u : 0u;
        }
    }
    ++texture_binding_set_rebuild_count_;
    texture_binding_image_descriptor_update_count_ += descriptor_count * 8u;
    texture_binding_descriptor_set_allocation_count_ += descriptor_count;
    last_texture_binding_image_descriptor_update_count_ = descriptor_count * 8u;
    last_texture_binding_descriptor_set_allocation_count_ =
        descriptor_count;
    last_texture_binding_update_us_ = std::chrono::duration_cast<
        std::chrono::microseconds>(
            std::chrono::steady_clock::now() - update_begin).count();
    log_.emit(
        "nv2a_texture_sampler_bindings_refreshed",
        {
            {"bindings", std::to_string(host_texture_bindings_.size())},
            {"binding_set_reused", json_bool(false)},
            {"image_descriptor_updates", std::to_string(
                descriptor_count * 8u)},
            {"descriptor_sets_allocated", std::to_string(
                descriptor_count)},
            {"update_us", std::to_string(
                last_texture_binding_update_us_)},
            {"repeat_bindings", std::to_string(repeat_binding_count)},
            {"mirrored_repeat_bindings", std::to_string(mirrored_repeat_binding_count)},
        });
}

void VulkanPresenter::refresh_host_textures(bool retain_unlisted_resources) {
    const auto refresh_begin = std::chrono::steady_clock::now();
    last_texture_refresh_us_ = 0u;
    last_texture_indexed_lookup_count_ = 0u;
    last_texture_indexed_lookup_candidate_count_ = 0u;
    last_texture_constant_lookup_count_ = 0u;
    last_render_target_feedback_image_cache_hit_count_ = 0u;
    last_render_target_feedback_image_cache_miss_count_ = 0u;
    last_render_target_feedback_image_cache_store_count_ = 0u;
    last_render_target_feedback_image_cache_eviction_count_ = 0u;
    std::vector<HostTexture> previous = std::move(host_textures_);
    host_textures_.clear();
    last_render_target_feedback_pruned_count_ = 0u;
    const std::vector<RenderTargetFeedbackSpec>& feedback_specs =
        presented_render_target_feedback_specs();
    host_textures_.reserve(
        recovered_source_.textures.size()
            + feedback_specs.size()
            + previous.size()
            + 1u);
    uint32_t reused_texture_count = 0;
    uint32_t uploaded_texture_count = 0;
    uint32_t feedback_texture_count = 0;
    uint32_t gpu_converted_texture_count = 0;
    uint32_t cpu_converted_texture_count = 0;
    uint64_t cpu_texture_path_us = 0u;
    std::vector<GpuTextureConversionJob> gpu_conversion_jobs;
    std::unordered_map<uint32_t, std::vector<size_t>>
        previous_indices_by_address;
    previous_indices_by_address.reserve(previous.size() * 2u + 1u);
    for (size_t index = 0u; index < previous.size(); ++index) {
        previous_indices_by_address[previous[index].guest_address]
            .push_back(index);
    }
    std::unordered_map<uint32_t, std::vector<size_t>>
        active_indices_by_address;
    active_indices_by_address.reserve(
        recovered_source_.textures.size() * 2u + 1u);
    std::unordered_set<uint32_t> feedback_canonical_addresses;
    feedback_canonical_addresses.reserve(feedback_specs.size() * 2u + 1u);
    for (const RenderTargetFeedbackSpec& spec : feedback_specs) {
        feedback_canonical_addresses.insert(
            nv2a_canonical_resource_address(spec.address));
    }

    const auto find_previous_index =
        [&](uint32_t address, const auto& matches) -> size_t {
            ++last_texture_indexed_lookup_count_;
            const auto bucket = previous_indices_by_address.find(address);
            if (bucket == previous_indices_by_address.end()) {
                return previous.size();
            }
            for (const size_t index : bucket->second) {
                ++last_texture_indexed_lookup_candidate_count_;
                if (matches(previous[index])) {
                    return index;
                }
            }
            return previous.size();
        };
    const auto index_active_texture = [&]() {
        const size_t index = host_textures_.size() - 1u;
        const HostTexture& texture = host_textures_[index];
        if (!texture.render_target_feedback) {
            active_indices_by_address[texture.guest_address]
                .push_back(index);
        }
    };
    const auto active_texture_already_present =
        [&](uint32_t address,
            uint32_t width,
            uint32_t height,
            const std::string& format,
            const std::string& content_hash) {
            ++last_texture_indexed_lookup_count_;
            const auto bucket = active_indices_by_address.find(address);
            if (bucket == active_indices_by_address.end()) {
                return false;
            }
            for (const size_t index : bucket->second) {
                ++last_texture_indexed_lookup_candidate_count_;
                const HostTexture& texture = host_textures_[index];
                if (texture.width == width
                    && texture.height == height
                    && texture.format == format
                    && texture.content_hash == content_hash) {
                    return true;
                }
            }
            return false;
        };

    auto retain_matching = [&](uint32_t address,
                               uint32_t width,
                               uint32_t height,
                               const std::string& format,
                               const std::string& content_hash) -> bool {
        const size_t match = find_previous_index(
            address,
            [&](const HostTexture& texture) {
                return texture.image != VK_NULL_HANDLE
                    && texture.width == width
                    && texture.height == height
                    && texture.format == format
                    && texture.content_hash == content_hash;
            });
        if (match == previous.size()) {
            return false;
        }
        host_textures_.push_back(std::move(previous[match]));
        clear_moved_texture_handles(previous[match]);
        index_active_texture();
        ++reused_texture_count;
        return true;
    };

    if (!retain_matching(0u, 1u, 1u, "fallback", "white")) {
        host_textures_.push_back(create_host_texture(
            0u, 1u, 1u, "fallback", "white", {255, 255, 255, 255}));
        index_active_texture();
        ++uploaded_texture_count;
    }
    if (!retain_matching(0u, 1u, 1u, "fallback", "white-cubemap")) {
        host_textures_.push_back(create_host_texture(
            0u,
            1u,
            1u,
            "fallback",
            "white-cubemap",
            std::vector<uint8_t>(6u * 4u, 255u),
            VK_FORMAT_R8G8B8A8_UNORM,
            false,
            {},
            true));
        index_active_texture();
        ++uploaded_texture_count;
    }
    for (const RenderTargetFeedbackSpec& spec : feedback_specs) {
        const size_t retained = find_previous_index(
            spec.address,
            [&](const HostTexture& texture) {
                return texture.image != VK_NULL_HANDLE
                    && render_target_feedback_texture_matches_spec(
                        texture,
                        spec);
            });
        if (retained != previous.size()) {
            host_textures_.push_back(std::move(previous[retained]));
            clear_moved_texture_handles(previous[retained]);
            ++reused_texture_count;
            ++feedback_texture_count;
            continue;
        }
        const auto cached = std::find_if(
            render_target_feedback_image_cache_.begin(),
            render_target_feedback_image_cache_.end(),
            [&](const HostTexture& texture) {
                return texture.image != VK_NULL_HANDLE
                    && texture.memory != VK_NULL_HANDLE
                    && texture.view != VK_NULL_HANDLE
                    && render_target_feedback_texture_matches_spec(
                        texture,
                        spec);
            });
        if (cached != render_target_feedback_image_cache_.end()) {
            host_textures_.push_back(std::move(*cached));
            clear_moved_texture_handles(*cached);
            render_target_feedback_image_cache_.erase(cached);
            ++reused_texture_count;
            ++feedback_texture_count;
            ++last_render_target_feedback_image_cache_hit_count_;
            ++render_target_feedback_image_cache_hit_count_;
            continue;
        }
        ++last_render_target_feedback_image_cache_miss_count_;
        ++render_target_feedback_image_cache_miss_count_;
        host_textures_.push_back(
            create_render_target_feedback_texture(spec));
        ++uploaded_texture_count;
        ++feedback_texture_count;
    }
    for (const RecoveredTextureResource& resource : recovered_source_.textures) {
        if (resource.format == "VERTEX_BUFFER") {
            continue;
        }
        ++last_texture_constant_lookup_count_;
        const bool replaced_by_feedback =
            feedback_canonical_addresses.find(
                nv2a_canonical_resource_address(resource.address))
            != feedback_canonical_addresses.end();
        if (replaced_by_feedback) {
            continue;
        }
        const bool cubemap = std::any_of(
            interpreted_stream_.draws.begin(),
            interpreted_stream_.draws.end(),
            [&](const NativeDraw& draw) {
                for (uint32_t stage = 0u;
                     stage < draw.texture_formats.size();
                     ++stage) {
                    if (nv2a_canonical_resource_address(
                            draw.texture_offsets[stage])
                            == nv2a_canonical_resource_address(
                                resource.address)
                        && nv2a_texture_format_is_cubemap(
                            draw.texture_formats[stage])
                        && nv2a_texture_format_matches(
                            resource.format,
                            draw.texture_formats[stage])) {
                        return true;
                    }
                }
                return false;
            });
        const std::string content_identity =
            texture_content_identity(resource)
            + (cubemap ? ":cubemap" : "");
        const size_t rgba_size = static_cast<size_t>(resource.width)
            * resource.height * 4u;
        const size_t r5g6b5_size = static_cast<size_t>(resource.width)
            * resource.height * 2u;
        const size_t required_layer_count = cubemap ? 6u : 1u;
        const bool supported = resource.format == "DXT1"
            || resource.format == "DXT5"
            || (resource.format == "R5G6B5"
                && resource.payload.size()
                    >= r5g6b5_size * required_layer_count)
            || resource.payload.size() >= rgba_size * required_layer_count;
        if (!supported) {
            ++unsupported_texture_resource_count_;
            log_.emit(
                "unsupported_texture_resource",
                {
                    {"address", std::to_string(resource.address)},
                    {"format", json_string(resource.format)},
                    {"width", std::to_string(resource.width)},
                    {"height", std::to_string(resource.height)},
                    {"payload_bytes", std::to_string(resource.payload.size())},
                });
            continue;
        }
        const bool already_retained = active_texture_already_present(
            resource.address,
            resource.width,
            resource.height,
            resource.format,
            content_identity);
        if (already_retained
            || retain_matching(
                resource.address,
                resource.width,
                resource.height,
                resource.format,
                content_identity)) {
            continue;
        }
        GpuTextureConversionJob gpu_job{};
        if (!cubemap
            && build_gpu_texture_conversion_job(
                resource,
                host_textures_.size(),
                gpu_job)
            && ensure_texture_conversion_pipeline()) {
            HostTexture placeholder{};
            placeholder.guest_address = resource.address;
            placeholder.width = resource.width;
            placeholder.height = resource.height;
            placeholder.format = resource.format;
            placeholder.content_hash = content_identity;
            host_textures_.push_back(std::move(placeholder));
            index_active_texture();
            gpu_conversion_jobs.push_back(std::move(gpu_job));
            ++uploaded_texture_count;
            continue;
        }
        const auto cpu_begin = std::chrono::steady_clock::now();
        std::optional<HostTexture> texture =
            create_cpu_converted_host_texture(
                resource, content_identity, cubemap);
        cpu_texture_path_us += std::chrono::duration_cast<
            std::chrono::microseconds>(
                std::chrono::steady_clock::now() - cpu_begin).count();
        if (!texture.has_value()) {
            ++unsupported_texture_resource_count_;
            continue;
        }
        host_textures_.push_back(std::move(*texture));
        index_active_texture();
        ++uploaded_texture_count;
        ++cpu_converted_texture_count;
    }
    if (!gpu_conversion_jobs.empty()) {
        if (execute_gpu_texture_conversion_batch(gpu_conversion_jobs)) {
            gpu_converted_texture_count = static_cast<uint32_t>(
                gpu_conversion_jobs.size());
        } else {
            for (const GpuTextureConversionJob& job :
                 gpu_conversion_jobs) {
                const auto cpu_begin = std::chrono::steady_clock::now();
                std::optional<HostTexture> texture =
                    create_cpu_converted_host_texture(
                        *job.resource,
                        host_textures_[job.host_texture_index].content_hash,
                        false);
                cpu_texture_path_us += std::chrono::duration_cast<
                    std::chrono::microseconds>(
                        std::chrono::steady_clock::now() - cpu_begin).count();
                if (!texture.has_value()) {
                    throw std::runtime_error(
                        "GPU texture conversion fallback rejected a validated resource");
                }
                host_textures_[job.host_texture_index] =
                    std::move(*texture);
                ++cpu_converted_texture_count;
            }
        }
    }
    if (retain_unlisted_resources) {
        for (HostTexture& texture : previous) {
            if (texture.image == VK_NULL_HANDLE
                || texture.render_target_feedback) {
                continue;
            }
            ++last_texture_constant_lookup_count_;
            const bool replaced_by_feedback =
                feedback_canonical_addresses.find(
                    nv2a_canonical_resource_address(
                        texture.guest_address))
                != feedback_canonical_addresses.end();
            if (replaced_by_feedback) {
                continue;
            }
            host_textures_.push_back(std::move(texture));
            clear_moved_texture_handles(texture);
            ++reused_texture_count;
        }
    }
    for (HostTexture& texture : previous) {
        if (texture.render_target_feedback
            && texture.image != VK_NULL_HANDLE) {
            ++last_render_target_feedback_pruned_count_;
            cache_render_target_feedback_texture(texture);
            continue;
        }
        destroy_host_texture(texture);
    }

    // Retained images keep their descriptor sets and samplers. The binding
    // refresh rewrites only image descriptors whose backing view changed;
    // layout or mip-count changes still rebuild the complete set.
    refresh_host_texture_bindings();
    // Descriptor updates above retire every reference to an inactive
    // feedback view. Capacity eviction is therefore safe only after the
    // active descriptor set no longer points at that image.
    trim_render_target_feedback_image_cache();
    last_texture_refresh_us_ = std::chrono::duration_cast<
        std::chrono::microseconds>(
            std::chrono::steady_clock::now() - refresh_begin).count();
    log_.emit(
        "nv2a_texture_resources_refreshed",
        {
            {"textures", std::to_string(host_textures_.size() - 1u)},
            {"reused", std::to_string(reused_texture_count)},
            {"uploaded", std::to_string(uploaded_texture_count)},
            {"gpu_converted", std::to_string(
                gpu_converted_texture_count)},
            {"cpu_converted", std::to_string(
                cpu_converted_texture_count)},
            {"cpu_texture_path_us", std::to_string(
                cpu_texture_path_us)},
            {"refresh_us", std::to_string(last_texture_refresh_us_)},
            {"indexed_lookup_count", std::to_string(
                last_texture_indexed_lookup_count_)},
            {"indexed_lookup_candidates", std::to_string(
                last_texture_indexed_lookup_candidate_count_)},
            {"constant_lookup_count", std::to_string(
                last_texture_constant_lookup_count_)},
            {"gpu_texture_path_us", std::to_string(
                gpu_converted_texture_count == 0u
                    ? 0u
                    : last_gpu_texture_conversion_us_)},
            {"gpu_conversion_backend", json_string(
                options_.cpu_texture_conversion
                    ? "cpu_forced"
                    : queue_family_.supports_compute
                        ? "compute"
                        : "cpu_queue_fallback")},
            {"render_target_feedback_textures", std::to_string(feedback_texture_count)},
            {"render_target_feedback_required", std::to_string(feedback_specs.size())},
            {"render_target_feedback_pruned", std::to_string(
                last_render_target_feedback_pruned_count_)},
            {"render_target_feedback_image_cache_hits", std::to_string(
                last_render_target_feedback_image_cache_hit_count_)},
            {"render_target_feedback_image_cache_misses", std::to_string(
                last_render_target_feedback_image_cache_miss_count_)},
            {"render_target_feedback_image_cache_stores", std::to_string(
                last_render_target_feedback_image_cache_store_count_)},
            {"render_target_feedback_image_cache_evictions", std::to_string(
                last_render_target_feedback_image_cache_eviction_count_)},
            {"render_target_feedback_image_cache_resident", std::to_string(
                render_target_feedback_image_cache_.size())},
            {"render_target_feedback_image_cache_capacity", std::to_string(
                kRenderTargetFeedbackImageCacheCapacity)},
            {"render_target_feedback_image_cache_hits_total", std::to_string(
                render_target_feedback_image_cache_hit_count_)},
            {"render_target_feedback_image_cache_misses_total", std::to_string(
                render_target_feedback_image_cache_miss_count_)},
            {"render_target_feedback_image_cache_stores_total", std::to_string(
                render_target_feedback_image_cache_store_count_)},
            {"render_target_feedback_image_cache_evictions_total", std::to_string(
                render_target_feedback_image_cache_eviction_count_)},
        });
}

}  // namespace b2r::host::vulkan_detail
