#include "vulkan_presenter_runtime.h"

namespace b2r::host::vulkan_detail {

void VulkanPresenter::create_instance() {
    VkApplicationInfo app_info{};
    app_info.sType = VK_STRUCTURE_TYPE_APPLICATION_INFO;
    app_info.pApplicationName = "b2_recomp_first_frame";
    app_info.applicationVersion = VK_MAKE_VERSION(0, 6, 0);
    app_info.pEngineName = "b2_recomp";
    app_info.engineVersion = VK_MAKE_VERSION(0, 6, 0);
    app_info.apiVersion = VK_API_VERSION_1_1;

    const std::vector<const char*> extensions =
        platform_.vulkan_instance_extensions();

    VkInstanceCreateInfo create_info{};
    create_info.sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO;
    create_info.pApplicationInfo = &app_info;
    create_info.enabledExtensionCount = static_cast<uint32_t>(extensions.size());
    create_info.ppEnabledExtensionNames = extensions.data();

    vk_check(vkCreateInstance(&create_info, nullptr, &instance_), "vkCreateInstance");
    log_.emit(
        "vulkan_instance_created",
        {
            {"api_version", json_string("1.1")},
            {"surface_provider", json_string("sdl3")},
            {"surface_extension_count", std::to_string(extensions.size())},
        });
}

void VulkanPresenter::pick_physical_device() {
    uint32_t device_count = 0;
    vk_check(vkEnumeratePhysicalDevices(instance_, &device_count, nullptr), "vkEnumeratePhysicalDevices");
    if (device_count == 0) {
        throw std::runtime_error("no Vulkan physical devices are available");
    }
    std::vector<VkPhysicalDevice> devices(device_count);
    vk_check(vkEnumeratePhysicalDevices(instance_, &device_count, devices.data()), "vkEnumeratePhysicalDevices");

    for (VkPhysicalDevice candidate : devices) {
        std::optional<QueueFamilySelection> queue_family = find_queue_family(candidate);
        if (!queue_family.has_value() || !device_supports_swapchain(candidate)) {
            continue;
        }
        const SwapchainSupport support = query_swapchain_support(candidate);
        if (support.formats.empty() || support.present_modes.empty()) {
            continue;
        }
        physical_device_ = candidate;
        queue_family_ = *queue_family;
        VkPhysicalDeviceProperties properties{};
        vkGetPhysicalDeviceProperties(candidate, &properties);
        log_.emit(
            "physical_device_selected",
            {
                {"name", json_string(properties.deviceName)},
                {"vendor_id", std::to_string(properties.vendorID)},
                {"device_id", std::to_string(properties.deviceID)},
                {"queue_family", std::to_string(queue_family_.index)},
            });
        return;
    }
    throw std::runtime_error("no Vulkan device supports graphics presentation and swapchain");
}

std::optional<QueueFamilySelection> VulkanPresenter::find_queue_family(VkPhysicalDevice device) const {
    uint32_t count = 0;
    vkGetPhysicalDeviceQueueFamilyProperties(device, &count, nullptr);
    std::vector<VkQueueFamilyProperties> families(count);
    vkGetPhysicalDeviceQueueFamilyProperties(device, &count, families.data());
    for (uint32_t index = 0; index < count; ++index) {
        VkBool32 present_supported = VK_FALSE;
        vkGetPhysicalDeviceSurfaceSupportKHR(device, index, surface_, &present_supported);
        if ((families[index].queueFlags & VK_QUEUE_GRAPHICS_BIT)
            && present_supported == VK_TRUE) {
            return QueueFamilySelection{
                index,
                (families[index].queueFlags & VK_QUEUE_COMPUTE_BIT) != 0u,
            };
        }
    }
    return std::nullopt;
}

bool VulkanPresenter::device_supports_swapchain(VkPhysicalDevice device) const {
    uint32_t extension_count = 0;
    vkEnumerateDeviceExtensionProperties(device, nullptr, &extension_count, nullptr);
    std::vector<VkExtensionProperties> extensions(extension_count);
    vkEnumerateDeviceExtensionProperties(device, nullptr, &extension_count, extensions.data());
    return std::any_of(
        extensions.begin(),
        extensions.end(),
        [](const VkExtensionProperties& item) {
            return std::string(item.extensionName) == VK_KHR_SWAPCHAIN_EXTENSION_NAME;
        });
}

SwapchainSupport VulkanPresenter::query_swapchain_support(VkPhysicalDevice device) const {
    SwapchainSupport support{};
    vk_check(
        vkGetPhysicalDeviceSurfaceCapabilitiesKHR(device, surface_, &support.capabilities),
        "vkGetPhysicalDeviceSurfaceCapabilitiesKHR");

    uint32_t format_count = 0;
    vk_check(
        vkGetPhysicalDeviceSurfaceFormatsKHR(device, surface_, &format_count, nullptr),
        "vkGetPhysicalDeviceSurfaceFormatsKHR");
    support.formats.resize(format_count);
    if (format_count > 0) {
        vk_check(
            vkGetPhysicalDeviceSurfaceFormatsKHR(device, surface_, &format_count, support.formats.data()),
            "vkGetPhysicalDeviceSurfaceFormatsKHR");
    }

    uint32_t present_mode_count = 0;
    vk_check(
        vkGetPhysicalDeviceSurfacePresentModesKHR(device, surface_, &present_mode_count, nullptr),
        "vkGetPhysicalDeviceSurfacePresentModesKHR");
    support.present_modes.resize(present_mode_count);
    if (present_mode_count > 0) {
        vk_check(
            vkGetPhysicalDeviceSurfacePresentModesKHR(
                device, surface_, &present_mode_count, support.present_modes.data()),
            "vkGetPhysicalDeviceSurfacePresentModesKHR");
    }
    return support;
}

void VulkanPresenter::create_logical_device() {
    float queue_priority = 1.0f;
    VkDeviceQueueCreateInfo queue_create_info{};
    queue_create_info.sType = VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO;
    queue_create_info.queueFamilyIndex = queue_family_.index;
    queue_create_info.queueCount = 1;
    queue_create_info.pQueuePriorities = &queue_priority;

    const std::vector<const char*> extensions = {VK_KHR_SWAPCHAIN_EXTENSION_NAME};
    VkDeviceCreateInfo create_info{};
    create_info.sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO;
    create_info.queueCreateInfoCount = 1;
    create_info.pQueueCreateInfos = &queue_create_info;
    create_info.enabledExtensionCount = static_cast<uint32_t>(extensions.size());
    create_info.ppEnabledExtensionNames = extensions.data();

    vk_check(vkCreateDevice(physical_device_, &create_info, nullptr, &device_), "vkCreateDevice");
    vkGetDeviceQueue(device_, queue_family_.index, 0, &graphics_queue_);
    log_.emit(
        "logical_device_created",
        {
            {"queue_family", std::to_string(queue_family_.index)},
            {"compute_supported", json_bool(
                queue_family_.supports_compute)},
        });
}

void VulkanPresenter::create_pipeline_cache() {
    std::vector<uint8_t> initial_data;
    if (!options_.pipeline_cache.empty()) {
        std::ifstream input(options_.pipeline_cache, std::ios::binary);
        if (input) {
            input.seekg(0, std::ios::end);
            const std::streamoff size = input.tellg();
            if (size > 0 && size <= 256 * 1024 * 1024) {
                initial_data.resize(static_cast<size_t>(size));
                input.seekg(0, std::ios::beg);
                input.read(
                    reinterpret_cast<char*>(initial_data.data()),
                    static_cast<std::streamsize>(initial_data.size()));
                if (!input) {
                    initial_data.clear();
                }
            }
        }
    }
    VkPipelineCacheCreateInfo cache_info{};
    cache_info.sType = VK_STRUCTURE_TYPE_PIPELINE_CACHE_CREATE_INFO;
    cache_info.initialDataSize = initial_data.size();
    cache_info.pInitialData = initial_data.empty()
        ? nullptr
        : initial_data.data();
    VkResult result = vkCreatePipelineCache(
        device_, &cache_info, nullptr, &pipeline_cache_);
    if (result != VK_SUCCESS && !initial_data.empty()) {
        pipeline_cache_ = VK_NULL_HANDLE;
        cache_info.initialDataSize = 0u;
        cache_info.pInitialData = nullptr;
        result = vkCreatePipelineCache(
            device_, &cache_info, nullptr, &pipeline_cache_);
        pipeline_cache_rejected_ = true;
    }
    vk_check(result, "vkCreatePipelineCache");
    pipeline_cache_loaded_bytes_ = pipeline_cache_rejected_
        ? 0u
        : initial_data.size();
    log_.emit(
        "vulkan_pipeline_cache_opened",
        {
            {"path", options_.pipeline_cache.empty()
                ? "null" : json_string(options_.pipeline_cache.string())},
            {"loaded_bytes", std::to_string(
                pipeline_cache_loaded_bytes_)},
            {"initial_data_rejected", json_bool(
                pipeline_cache_rejected_)},
        });
}

void VulkanPresenter::persist_pipeline_cache() {
    if (pipeline_cache_ == VK_NULL_HANDLE) {
        return;
    }
    size_t byte_count = 0u;
    VkResult result = vkGetPipelineCacheData(
        device_, pipeline_cache_, &byte_count, nullptr);
    std::vector<uint8_t> data;
    if (result == VK_SUCCESS && byte_count != 0u) {
        data.resize(byte_count);
        result = vkGetPipelineCacheData(
            device_, pipeline_cache_, &byte_count, data.data());
        if (result == VK_SUCCESS) {
            data.resize(byte_count);
        }
    }
    bool written = false;
    if (result == VK_SUCCESS
        && !data.empty()
        && !options_.pipeline_cache.empty()) {
        std::error_code directory_error;
        if (options_.pipeline_cache.has_parent_path()) {
            std::filesystem::create_directories(
                options_.pipeline_cache.parent_path(),
                directory_error);
        }
        const std::filesystem::path temporary =
            options_.pipeline_cache.string() + ".tmp";
        if (!directory_error) {
            std::ofstream output(
                temporary,
                std::ios::binary | std::ios::trunc);
            output.write(
                reinterpret_cast<const char*>(data.data()),
                static_cast<std::streamsize>(data.size()));
            output.close();
            written = static_cast<bool>(output)
                && MoveFileExW(
                    temporary.c_str(),
                    options_.pipeline_cache.c_str(),
                    MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH);
        }
        if (!written) {
            std::error_code ignored;
            std::filesystem::remove(temporary, ignored);
        }
    }
    pipeline_cache_saved_bytes_ = written ? data.size() : 0u;
    log_.emit(
        "vulkan_pipeline_cache_saved",
        {
            {"path", options_.pipeline_cache.empty()
                ? "null" : json_string(options_.pipeline_cache.string())},
            {"bytes", std::to_string(pipeline_cache_saved_bytes_)},
            {"written", json_bool(written)},
        });
}

void VulkanPresenter::create_swapchain() {
    const SwapchainSupport support = query_swapchain_support(physical_device_);
    const VkSurfaceFormatKHR surface_format = choose_surface_format(support.formats);
    const VkPresentModeKHR present_mode = VK_PRESENT_MODE_FIFO_KHR;
    const VkExtent2D extent = choose_extent(support.capabilities);
    uint32_t image_count = support.capabilities.minImageCount + 1;
    if (support.capabilities.maxImageCount > 0) {
        image_count = std::min(image_count, support.capabilities.maxImageCount);
    }

    VkSwapchainCreateInfoKHR create_info{};
    create_info.sType = VK_STRUCTURE_TYPE_SWAPCHAIN_CREATE_INFO_KHR;
    create_info.surface = surface_;
    create_info.minImageCount = image_count;
    create_info.imageFormat = surface_format.format;
    create_info.imageColorSpace = surface_format.colorSpace;
    create_info.imageExtent = extent;
    create_info.imageArrayLayers = 1;
    if ((support.capabilities.supportedUsageFlags
            & VK_IMAGE_USAGE_TRANSFER_SRC_BIT) == 0) {
        throw std::runtime_error(
            "surface does not support swapchain transfer-source feedback");
    }
    create_info.imageUsage = VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT
        | VK_IMAGE_USAGE_TRANSFER_SRC_BIT;
    create_info.imageSharingMode = VK_SHARING_MODE_EXCLUSIVE;
    create_info.preTransform = support.capabilities.currentTransform;
    create_info.compositeAlpha = VK_COMPOSITE_ALPHA_OPAQUE_BIT_KHR;
    create_info.presentMode = present_mode;
    create_info.clipped = VK_TRUE;
    create_info.oldSwapchain = VK_NULL_HANDLE;

    vk_check(vkCreateSwapchainKHR(device_, &create_info, nullptr, &swapchain_), "vkCreateSwapchainKHR");
    swapchain_format_ = surface_format.format;
    swapchain_extent_ = extent;

    vk_check(vkGetSwapchainImagesKHR(device_, swapchain_, &image_count, nullptr), "vkGetSwapchainImagesKHR");
    swapchain_images_.resize(image_count);
    vk_check(
        vkGetSwapchainImagesKHR(device_, swapchain_, &image_count, swapchain_images_.data()),
        "vkGetSwapchainImagesKHR");

    log_.emit(
        "swapchain_created",
        {
            {"image_count", std::to_string(image_count)},
            {"width", std::to_string(extent.width)},
            {"height", std::to_string(extent.height)},
            {"present_mode", json_string("fifo")},
            {"format", std::to_string(surface_format.format)},
            {"color_space", std::to_string(surface_format.colorSpace)},
            {"xbox_combiner_unorm_output", json_bool(
                surface_format.format == VK_FORMAT_B8G8R8A8_UNORM
                || surface_format.format == VK_FORMAT_R8G8B8A8_UNORM)},
        });
}

VkSurfaceFormatKHR VulkanPresenter::choose_surface_format(const std::vector<VkSurfaceFormatKHR>& formats) const {
    const auto preferred = std::find_if(
        formats.begin(),
        formats.end(),
        [](const VkSurfaceFormatKHR& format) {
            // NV2A register combiners operate on the title's packed
            // 8-bit texture/color values. Sampling those resources as
            // UNORM and then targeting an sRGB attachment applies an
            // extra transfer function and produces the washed-out live
            // image. Preserve the Xbox combiner-domain values here; the
            // display color space still performs normal presentation.
            return format.format == VK_FORMAT_B8G8R8A8_UNORM
                && format.colorSpace == VK_COLOR_SPACE_SRGB_NONLINEAR_KHR;
        });
    if (preferred != formats.end()) {
        return *preferred;
    }
    const auto srgb_fallback = std::find_if(
        formats.begin(),
        formats.end(),
        [](const VkSurfaceFormatKHR& format) {
            return format.format == VK_FORMAT_B8G8R8A8_SRGB
                && format.colorSpace == VK_COLOR_SPACE_SRGB_NONLINEAR_KHR;
        });
    if (srgb_fallback != formats.end()) {
        return *srgb_fallback;
    }
    return formats.front();
}

VkExtent2D VulkanPresenter::choose_extent(const VkSurfaceCapabilitiesKHR& capabilities) const {
    if (capabilities.currentExtent.width != UINT32_MAX) {
        return capabilities.currentExtent;
    }
    VkExtent2D extent{options_.width, options_.height};
    extent.width = std::clamp(
        extent.width,
        capabilities.minImageExtent.width,
        capabilities.maxImageExtent.width);
    extent.height = std::clamp(
        extent.height,
        capabilities.minImageExtent.height,
        capabilities.maxImageExtent.height);
    return extent;
}

void VulkanPresenter::create_image_views() {
    swapchain_image_views_.resize(swapchain_images_.size());
    for (size_t index = 0; index < swapchain_images_.size(); ++index) {
        VkImageViewCreateInfo create_info{};
        create_info.sType = VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO;
        create_info.image = swapchain_images_[index];
        create_info.viewType = VK_IMAGE_VIEW_TYPE_2D;
        create_info.format = swapchain_format_;
        create_info.components.r = VK_COMPONENT_SWIZZLE_IDENTITY;
        create_info.components.g = VK_COMPONENT_SWIZZLE_IDENTITY;
        create_info.components.b = VK_COMPONENT_SWIZZLE_IDENTITY;
        create_info.components.a = VK_COMPONENT_SWIZZLE_IDENTITY;
        create_info.subresourceRange.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
        create_info.subresourceRange.baseMipLevel = 0;
        create_info.subresourceRange.levelCount = 1;
        create_info.subresourceRange.baseArrayLayer = 0;
        create_info.subresourceRange.layerCount = 1;
        vk_check(
            vkCreateImageView(device_, &create_info, nullptr, &swapchain_image_views_[index]),
            "vkCreateImageView");
    }
    log_.emit("image_views_created", {{"count", std::to_string(swapchain_image_views_.size())}});
}

void VulkanPresenter::destroy_depth_attachment(
    VkImage& image,
    VkDeviceMemory& memory,
    VkImageView& view) {
    if (view != VK_NULL_HANDLE) {
        vkDestroyImageView(device_, view, nullptr);
        view = VK_NULL_HANDLE;
    }
    if (image != VK_NULL_HANDLE) {
        vkDestroyImage(device_, image, nullptr);
        image = VK_NULL_HANDLE;
    }
    if (memory != VK_NULL_HANDLE) {
        vkFreeMemory(device_, memory, nullptr);
        memory = VK_NULL_HANDLE;
    }
}

void VulkanPresenter::create_depth_attachment(
    uint32_t width,
    uint32_t height,
    VkImage& image,
    VkDeviceMemory& memory,
    VkImageView& view) {
    try {
        VkImageCreateInfo image_info{};
        image_info.sType = VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO;
        image_info.imageType = VK_IMAGE_TYPE_2D;
        image_info.extent = {width, height, 1u};
        image_info.mipLevels = 1;
        image_info.arrayLayers = 1;
        image_info.format = depth_format_;
        image_info.tiling = VK_IMAGE_TILING_OPTIMAL;
        image_info.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
        image_info.usage = VK_IMAGE_USAGE_DEPTH_STENCIL_ATTACHMENT_BIT;
        image_info.samples = VK_SAMPLE_COUNT_1_BIT;
        image_info.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
        vk_check(
            vkCreateImage(device_, &image_info, nullptr, &image),
            "vkCreateImage(depth attachment)");

        VkMemoryRequirements requirements{};
        vkGetImageMemoryRequirements(device_, image, &requirements);
        VkMemoryAllocateInfo allocation{};
        allocation.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO;
        allocation.allocationSize = requirements.size;
        allocation.memoryTypeIndex = find_memory_type(
            requirements.memoryTypeBits,
            VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT);
        vk_check(
            vkAllocateMemory(device_, &allocation, nullptr, &memory),
            "vkAllocateMemory(depth attachment)");
        vk_check(
            vkBindImageMemory(device_, image, memory, 0),
            "vkBindImageMemory(depth attachment)");

        VkImageViewCreateInfo view_info{};
        view_info.sType = VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO;
        view_info.image = image;
        view_info.viewType = VK_IMAGE_VIEW_TYPE_2D;
        view_info.format = depth_format_;
        view_info.subresourceRange.aspectMask = VK_IMAGE_ASPECT_DEPTH_BIT;
        view_info.subresourceRange.levelCount = 1;
        view_info.subresourceRange.layerCount = 1;
        vk_check(
            vkCreateImageView(device_, &view_info, nullptr, &view),
            "vkCreateImageView(depth attachment)");
    } catch (...) {
        destroy_depth_attachment(image, memory, view);
        throw;
    }
}

void VulkanPresenter::create_depth_resources() {
    static constexpr std::array<VkFormat, 3> candidates = {
        VK_FORMAT_D32_SFLOAT,
        VK_FORMAT_D24_UNORM_S8_UINT,
        VK_FORMAT_D16_UNORM,
    };
    for (const VkFormat candidate : candidates) {
        VkFormatProperties properties{};
        vkGetPhysicalDeviceFormatProperties(
            physical_device_, candidate, &properties);
        if ((properties.optimalTilingFeatures
             & VK_FORMAT_FEATURE_DEPTH_STENCIL_ATTACHMENT_BIT) != 0u) {
            depth_format_ = candidate;
            break;
        }
    }
    if (depth_format_ == VK_FORMAT_UNDEFINED) {
        throw std::runtime_error("no supported Vulkan depth format");
    }
    create_depth_attachment(
        swapchain_extent_.width,
        swapchain_extent_.height,
        depth_image_,
        depth_memory_,
        depth_image_view_);
    log_.emit(
        "depth_resources_created",
        {{"format", std::to_string(depth_format_)}});
}

void VulkanPresenter::create_render_pass() {
    VkAttachmentDescription color_attachment{};
    color_attachment.format = swapchain_format_;
    color_attachment.samples = VK_SAMPLE_COUNT_1_BIT;
    color_attachment.loadOp = VK_ATTACHMENT_LOAD_OP_CLEAR;
    color_attachment.storeOp = VK_ATTACHMENT_STORE_OP_STORE;
    color_attachment.stencilLoadOp = VK_ATTACHMENT_LOAD_OP_DONT_CARE;
    color_attachment.stencilStoreOp = VK_ATTACHMENT_STORE_OP_DONT_CARE;
    color_attachment.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
    color_attachment.finalLayout = VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL;

    VkAttachmentDescription depth_attachment{};
    depth_attachment.format = depth_format_;
    depth_attachment.samples = VK_SAMPLE_COUNT_1_BIT;
    depth_attachment.loadOp = VK_ATTACHMENT_LOAD_OP_CLEAR;
    depth_attachment.storeOp = VK_ATTACHMENT_STORE_OP_DONT_CARE;
    depth_attachment.stencilLoadOp = VK_ATTACHMENT_LOAD_OP_DONT_CARE;
    depth_attachment.stencilStoreOp = VK_ATTACHMENT_STORE_OP_DONT_CARE;
    depth_attachment.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
    depth_attachment.finalLayout =
        VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL;

    VkAttachmentReference color_ref{};
    color_ref.attachment = 0;
    color_ref.layout = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;

    VkAttachmentReference depth_ref{};
    depth_ref.attachment = 1;
    depth_ref.layout = VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL;

    VkSubpassDescription subpass{};
    subpass.pipelineBindPoint = VK_PIPELINE_BIND_POINT_GRAPHICS;
    subpass.colorAttachmentCount = 1;
    subpass.pColorAttachments = &color_ref;
    subpass.pDepthStencilAttachment = &depth_ref;

    VkSubpassDependency dependency{};
    dependency.srcSubpass = VK_SUBPASS_EXTERNAL;
    dependency.dstSubpass = 0;
    dependency.srcStageMask = VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT
        | VK_PIPELINE_STAGE_EARLY_FRAGMENT_TESTS_BIT
        | VK_PIPELINE_STAGE_LATE_FRAGMENT_TESTS_BIT;
    dependency.dstStageMask = VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT
        | VK_PIPELINE_STAGE_EARLY_FRAGMENT_TESTS_BIT
        | VK_PIPELINE_STAGE_LATE_FRAGMENT_TESTS_BIT;
    dependency.srcAccessMask = VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT
        | VK_ACCESS_DEPTH_STENCIL_ATTACHMENT_WRITE_BIT;
    dependency.dstAccessMask = VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT
        | VK_ACCESS_DEPTH_STENCIL_ATTACHMENT_WRITE_BIT;

    const std::array<VkAttachmentDescription, 2> attachments = {
        color_attachment,
        depth_attachment,
    };

    VkRenderPassCreateInfo create_info{};
    create_info.sType = VK_STRUCTURE_TYPE_RENDER_PASS_CREATE_INFO;
    create_info.attachmentCount = static_cast<uint32_t>(attachments.size());
    create_info.pAttachments = attachments.data();
    create_info.subpassCount = 1;
    create_info.pSubpasses = &subpass;
    create_info.dependencyCount = 1;
    create_info.pDependencies = &dependency;
    vk_check(vkCreateRenderPass(device_, &create_info, nullptr, &render_pass_), "vkCreateRenderPass");
    log_.emit("render_pass_created");
}

void VulkanPresenter::create_framebuffers() {
    framebuffers_.resize(swapchain_image_views_.size());
    for (size_t index = 0; index < swapchain_image_views_.size(); ++index) {
        VkImageView attachments[] = {
            swapchain_image_views_[index],
            depth_image_view_,
        };
        VkFramebufferCreateInfo create_info{};
        create_info.sType = VK_STRUCTURE_TYPE_FRAMEBUFFER_CREATE_INFO;
        create_info.renderPass = render_pass_;
        create_info.attachmentCount = 2;
        create_info.pAttachments = attachments;
        create_info.width = swapchain_extent_.width;
        create_info.height = swapchain_extent_.height;
        create_info.layers = 1;
        vk_check(
            vkCreateFramebuffer(device_, &create_info, nullptr, &framebuffers_[index]),
            "vkCreateFramebuffer");
    }
    log_.emit("framebuffers_created", {{"count", std::to_string(framebuffers_.size())}});
}

void VulkanPresenter::create_command_pool() {
    VkCommandPoolCreateInfo create_info{};
    create_info.sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO;
    create_info.flags = VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT;
    create_info.queueFamilyIndex = queue_family_.index;
    vk_check(vkCreateCommandPool(device_, &create_info, nullptr, &command_pool_), "vkCreateCommandPool");
    log_.emit("command_pool_created");
}

void VulkanPresenter::create_gpu_timing_resources() {
    uint32_t family_count = 0u;
    vkGetPhysicalDeviceQueueFamilyProperties(
        physical_device_, &family_count, nullptr);
    std::vector<VkQueueFamilyProperties> families(family_count);
    vkGetPhysicalDeviceQueueFamilyProperties(
        physical_device_, &family_count, families.data());
    if (queue_family_.index >= families.size()
        || families[queue_family_.index].timestampValidBits == 0u
        || framebuffers_.empty()) {
        log_.emit("gpu_frame_timing_unavailable");
        return;
    }
    VkPhysicalDeviceProperties properties{};
    vkGetPhysicalDeviceProperties(physical_device_, &properties);
    gpu_timestamp_period_ns_ = properties.limits.timestampPeriod;
    gpu_timestamp_valid_bits_ =
        families[queue_family_.index].timestampValidBits;
    VkQueryPoolCreateInfo create_info{};
    create_info.sType = VK_STRUCTURE_TYPE_QUERY_POOL_CREATE_INFO;
    create_info.queryType = VK_QUERY_TYPE_TIMESTAMP;
    create_info.queryCount = static_cast<uint32_t>(framebuffers_.size()) * 2u;
    vk_check(
        vkCreateQueryPool(device_, &create_info, nullptr, &gpu_timing_query_pool_),
        "vkCreateQueryPool(frame timing)");
    log_.emit(
        "gpu_frame_timing_ready",
        {
            {"timestamp_period_ns", json_float(gpu_timestamp_period_ns_)},
            {"timestamp_valid_bits", std::to_string(
                gpu_timestamp_valid_bits_)},
        });
}

uint32_t VulkanPresenter::find_memory_type(uint32_t type_bits, VkMemoryPropertyFlags properties) const {
    VkPhysicalDeviceMemoryProperties memory_properties{};
    vkGetPhysicalDeviceMemoryProperties(physical_device_, &memory_properties);
    for (uint32_t index = 0; index < memory_properties.memoryTypeCount; ++index) {
        if ((type_bits & (1u << index)) != 0
            && (memory_properties.memoryTypes[index].propertyFlags & properties) == properties) {
            return index;
        }
    }
    throw std::runtime_error("no compatible Vulkan host memory type for frame readback");
}

void VulkanPresenter::create_readback_buffer() {
    if (!readback_enabled()) {
        return;
    }
    if (swapchain_format_ != VK_FORMAT_B8G8R8A8_SRGB
        && swapchain_format_ != VK_FORMAT_B8G8R8A8_UNORM
        && swapchain_format_ != VK_FORMAT_R8G8B8A8_SRGB
        && swapchain_format_ != VK_FORMAT_R8G8B8A8_UNORM) {
        throw std::runtime_error("frame readback requires an 8-bit BGRA or RGBA swapchain format");
    }
    readback_size_ = static_cast<VkDeviceSize>(swapchain_extent_.width)
        * static_cast<VkDeviceSize>(swapchain_extent_.height) * 4u;
    VkBufferCreateInfo buffer_info{};
    buffer_info.sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO;
    buffer_info.size = readback_size_;
    buffer_info.usage = VK_BUFFER_USAGE_TRANSFER_DST_BIT;
    buffer_info.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
    vk_check(vkCreateBuffer(device_, &buffer_info, nullptr, &readback_buffer_), "vkCreateBuffer(readback)");

    VkMemoryRequirements requirements{};
    vkGetBufferMemoryRequirements(device_, readback_buffer_, &requirements);
    VkMemoryAllocateInfo allocate_info{};
    allocate_info.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO;
    allocate_info.allocationSize = requirements.size;
    allocate_info.memoryTypeIndex = find_memory_type(
        requirements.memoryTypeBits,
        VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT);
    vk_check(vkAllocateMemory(device_, &allocate_info, nullptr, &readback_memory_), "vkAllocateMemory(readback)");
    vk_check(vkBindBufferMemory(device_, readback_buffer_, readback_memory_, 0), "vkBindBufferMemory(readback)");
    log_.emit(
        "frame_readback_ready",
        {
            {"bytes", std::to_string(readback_size_)},
            {"output", json_string(options_.screenshot.string())},
            {"hotkey_directory", json_string(options_.hotkey_screenshot_directory.string())},
        });
}

std::vector<uint32_t> VulkanPresenter::read_spirv(const std::filesystem::path& path) const {
    std::ifstream file(path, std::ios::binary | std::ios::ate);
    if (!file) {
        throw std::runtime_error("cannot open SPIR-V shader: " + path.string());
    }
    const std::streamsize size = file.tellg();
    if (size <= 0 || size % 4 != 0) {
        throw std::runtime_error("invalid SPIR-V shader size: " + path.string());
    }
    file.seekg(0);
    std::vector<uint32_t> code(static_cast<size_t>(size) / 4u);
    file.read(reinterpret_cast<char*>(code.data()), size);
    return code;
}

VkShaderModule VulkanPresenter::create_shader_module(const std::filesystem::path& path) const {
    const std::vector<uint32_t> code = read_spirv(path);
    VkShaderModuleCreateInfo create_info{};
    create_info.sType = VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO;
    create_info.codeSize = code.size() * sizeof(uint32_t);
    create_info.pCode = code.data();
    VkShaderModule module = VK_NULL_HANDLE;
    vk_check(vkCreateShaderModule(device_, &create_info, nullptr, &module), "vkCreateShaderModule");
    return module;
}

bool VulkanPresenter::ensure_texture_conversion_pipeline() {
    if (options_.cpu_texture_conversion
        || !queue_family_.supports_compute
        || options_.texture_convert_shader.empty()) {
        return false;
    }
    if (texture_convert_pipeline_ != VK_NULL_HANDLE) {
        return true;
    }
    std::array<VkDescriptorSetLayoutBinding, 2> bindings{};
    for (uint32_t index = 0u; index < bindings.size(); ++index) {
        bindings[index].binding = index;
        bindings[index].descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;
        bindings[index].descriptorCount = 1u;
        bindings[index].stageFlags = VK_SHADER_STAGE_COMPUTE_BIT;
    }
    VkDescriptorSetLayoutCreateInfo descriptor_info{};
    descriptor_info.sType =
        VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO;
    descriptor_info.bindingCount = static_cast<uint32_t>(bindings.size());
    descriptor_info.pBindings = bindings.data();
    vk_check(
        vkCreateDescriptorSetLayout(
            device_,
            &descriptor_info,
            nullptr,
            &texture_convert_descriptor_layout_),
        "vkCreateDescriptorSetLayout(texture conversion)");

    VkPushConstantRange push_constants{};
    push_constants.stageFlags = VK_SHADER_STAGE_COMPUTE_BIT;
    push_constants.size = sizeof(NativeTextureConvertPushConstants);
    VkPipelineLayoutCreateInfo layout_info{};
    layout_info.sType = VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO;
    layout_info.setLayoutCount = 1u;
    layout_info.pSetLayouts = &texture_convert_descriptor_layout_;
    layout_info.pushConstantRangeCount = 1u;
    layout_info.pPushConstantRanges = &push_constants;
    vk_check(
        vkCreatePipelineLayout(
            device_,
            &layout_info,
            nullptr,
            &texture_convert_pipeline_layout_),
        "vkCreatePipelineLayout(texture conversion)");

    const auto shader_module_begin = std::chrono::steady_clock::now();
    const VkShaderModule shader = create_shader_module(
        options_.texture_convert_shader);
    const auto shader_module_end = std::chrono::steady_clock::now();
    VkPipelineShaderStageCreateInfo stage{};
    stage.sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO;
    stage.stage = VK_SHADER_STAGE_COMPUTE_BIT;
    stage.module = shader;
    stage.pName = "main";
    VkComputePipelineCreateInfo pipeline_info{};
    pipeline_info.sType = VK_STRUCTURE_TYPE_COMPUTE_PIPELINE_CREATE_INFO;
    pipeline_info.stage = stage;
    pipeline_info.layout = texture_convert_pipeline_layout_;
    const auto pipeline_create_begin = std::chrono::steady_clock::now();
    const VkResult create_result = vkCreateComputePipelines(
        device_,
        pipeline_cache_,
        1u,
        &pipeline_info,
        nullptr,
        &texture_convert_pipeline_);
    const auto pipeline_create_end = std::chrono::steady_clock::now();
    vkDestroyShaderModule(device_, shader, nullptr);
    vk_check(create_result, "vkCreateComputePipelines(texture conversion)");
    ++pipeline_creation_count_;
    ++pipeline_cache_miss_count_;
    log_.emit(
        "nv2a_gpu_texture_conversion_pipeline_created",
        {
            {"queue_family", std::to_string(queue_family_.index)},
            {"shader", json_string(
                options_.texture_convert_shader.string())},
            {"shader_module_us", std::to_string(
                std::chrono::duration_cast<std::chrono::microseconds>(
                    shader_module_end - shader_module_begin).count())},
            {"pipeline_create_us", std::to_string(
                std::chrono::duration_cast<std::chrono::microseconds>(
                    pipeline_create_end - pipeline_create_begin).count())},
        });
    return true;
}

void VulkanPresenter::create_native_graphics_pipeline() {
    last_pipeline_state_discovery_us_ = 0u;
    last_pipeline_candidate_draw_count_ = 0u;
    last_pipeline_unique_state_count_ = 0u;
    last_pipeline_missing_state_count_ = 0u;
    if (!texture_descriptor_layout_) {
        std::array<VkDescriptorSetLayoutBinding, 5> bindings{};
        bindings[0].binding = 0;
        bindings[0].descriptorType =
            VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER;
        bindings[0].descriptorCount = 1;
        bindings[0].stageFlags = VK_SHADER_STAGE_FRAGMENT_BIT;
        bindings[1].binding = 1;
        bindings[1].descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;
        bindings[1].descriptorCount = 1;
        bindings[1].stageFlags = VK_SHADER_STAGE_FRAGMENT_BIT;
        bindings[2].binding = 2;
        bindings[2].descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;
        bindings[2].descriptorCount = 1;
        bindings[2].stageFlags = VK_SHADER_STAGE_VERTEX_BIT;
        bindings[3].binding = 3;
        bindings[3].descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;
        bindings[3].descriptorCount = 1;
        bindings[3].stageFlags = VK_SHADER_STAGE_VERTEX_BIT;
        bindings[4].binding = 4;
        bindings[4].descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;
        bindings[4].descriptorCount = 1;
        bindings[4].stageFlags = VK_SHADER_STAGE_VERTEX_BIT;
        VkDescriptorSetLayoutCreateInfo descriptor_info{};
        descriptor_info.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO;
        descriptor_info.bindingCount = static_cast<uint32_t>(
            bindings.size());
        descriptor_info.pBindings = bindings.data();
        vk_check(
            vkCreateDescriptorSetLayout(device_, &descriptor_info, nullptr, &texture_descriptor_layout_),
            "vkCreateDescriptorSetLayout");

        VkPipelineLayoutCreateInfo layout_info{};
        layout_info.sType = VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO;
        layout_info.setLayoutCount = 1;
        layout_info.pSetLayouts = &texture_descriptor_layout_;
        VkPushConstantRange push_constant_range{};
        push_constant_range.stageFlags =
            VK_SHADER_STAGE_VERTEX_BIT | VK_SHADER_STAGE_FRAGMENT_BIT;
        push_constant_range.size = sizeof(NativeFragmentPushConstants);
        layout_info.pushConstantRangeCount = 1;
        layout_info.pPushConstantRanges = &push_constant_range;
        vk_check(vkCreatePipelineLayout(device_, &layout_info, nullptr, &pipeline_layout_), "vkCreatePipelineLayout");
    }

    const auto discovery_begin = std::chrono::steady_clock::now();
    std::vector<NativePipelineState> states;
    std::unordered_set<NativePipelineState, NativePipelineStateHash>
        unique_states;
    const size_t first_draw = std::min<size_t>(interpreted_stream_.presented_draw_begin, interpreted_stream_.draws.size());
    const size_t end_draw = std::min<size_t>(first_draw + interpreted_stream_.presented_draw_count, interpreted_stream_.draws.size());
    states.reserve(end_draw - first_draw + 1u);
    unique_states.reserve((end_draw - first_draw) * 2u + 1u);
    const auto append_unique_state = [&](NativePipelineState state) {
        if (unique_states.insert(state).second) {
            states.push_back(std::move(state));
        }
    };
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
            [&](const RenderTargetFeedbackSpec& spec) {
                return spec.offscreen_produced
                    && nv2a_canonical_resource_address(
                        spec.producer_address)
                        == nv2a_canonical_resource_address(
                            draw.surface_color_offset);
            });
        if ((!draw_targets_presented_surface(draw)
                && !targets_offscreen_feedback)
            || !draw_has_supported_host_transform(draw)
            || (draw.primitive != 5u && draw.primitive != 6u)) {
            continue;
        }
        ++last_pipeline_candidate_draw_count_;
        append_unique_state(pipeline_state_for_draw(draw));
    }
    if (!recovered_frontend_text_.empty()) {
        append_unique_state(frontend_text_pipeline_state());
    }
    if (states.empty()) {
        append_unique_state({});
    }
    last_pipeline_unique_state_count_ = static_cast<uint32_t>(
        states.size());
    std::unordered_set<NativePipelineState, NativePipelineStateHash>
        resident_states;
    resident_states.reserve(graphics_pipelines_.size() * 2u + 1u);
    for (const HostPipeline& pipeline : graphics_pipelines_) {
        resident_states.insert(pipeline.state);
    }
    states.erase(
        std::remove_if(
            states.begin(), states.end(),
            [&](const NativePipelineState& state) {
                return resident_states.find(state)
                    != resident_states.end();
            }),
        states.end());
    last_pipeline_missing_state_count_ = static_cast<uint32_t>(
        states.size());
    last_pipeline_state_discovery_us_ = std::chrono::duration_cast<
        std::chrono::microseconds>(
            std::chrono::steady_clock::now() - discovery_begin).count();
    if (states.empty()) {
        log_.emit(
            "nv2a_graphics_pipeline_cache_hit",
            {
                {"pipeline_count", std::to_string(graphics_pipelines_.size())},
                {"candidate_draws", std::to_string(
                    last_pipeline_candidate_draw_count_)},
                {"unique_states", std::to_string(
                    last_pipeline_unique_state_count_)},
                {"state_discovery_us", std::to_string(
                    last_pipeline_state_discovery_us_)},
            });
        return;
    }

    const auto shader_module_begin = std::chrono::steady_clock::now();
    const VkShaderModule vertex_shader = create_shader_module(options_.vertex_shader);
    const VkShaderModule fragment_shader = create_shader_module(options_.fragment_shader);
    const auto shader_module_end = std::chrono::steady_clock::now();
    VkPipelineShaderStageCreateInfo stages[2]{};
    stages[0].sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO;
    stages[0].stage = VK_SHADER_STAGE_VERTEX_BIT;
    stages[0].module = vertex_shader;
    stages[0].pName = "main";
    stages[1].sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO;
    stages[1].stage = VK_SHADER_STAGE_FRAGMENT_BIT;
    stages[1].module = fragment_shader;
    stages[1].pName = "main";

    VkVertexInputBindingDescription binding{};
    binding.binding = 0;
    binding.stride = sizeof(NativeVertex);
    binding.inputRate = VK_VERTEX_INPUT_RATE_VERTEX;
    std::array<VkVertexInputAttributeDescription, 5> attributes{};
    attributes[0] = {0, 0, VK_FORMAT_R32G32B32A32_SFLOAT, static_cast<uint32_t>(offsetof(NativeVertex, x))};
    attributes[1] = {1, 0, VK_FORMAT_R32G32B32A32_SFLOAT, static_cast<uint32_t>(offsetof(NativeVertex, r))};
    attributes[2] = {2, 0, VK_FORMAT_R32G32B32A32_SFLOAT, static_cast<uint32_t>(offsetof(NativeVertex, u))};
    attributes[3] = {3, 0, VK_FORMAT_R32G32B32A32_SFLOAT, static_cast<uint32_t>(offsetof(NativeVertex, secondary_r))};
    attributes[4] = {4, 0, VK_FORMAT_R32_SFLOAT, static_cast<uint32_t>(offsetof(NativeVertex, fog))};
    VkPipelineVertexInputStateCreateInfo vertex_input{};
    vertex_input.sType = VK_STRUCTURE_TYPE_PIPELINE_VERTEX_INPUT_STATE_CREATE_INFO;
    vertex_input.vertexBindingDescriptionCount = 1;
    vertex_input.pVertexBindingDescriptions = &binding;
    vertex_input.vertexAttributeDescriptionCount = static_cast<uint32_t>(attributes.size());
    vertex_input.pVertexAttributeDescriptions = attributes.data();

    VkPipelineInputAssemblyStateCreateInfo assembly{};
    assembly.sType = VK_STRUCTURE_TYPE_PIPELINE_INPUT_ASSEMBLY_STATE_CREATE_INFO;
    assembly.topology = VK_PRIMITIVE_TOPOLOGY_TRIANGLE_STRIP;
    VkViewport viewport{0.0f, 0.0f, static_cast<float>(swapchain_extent_.width), static_cast<float>(swapchain_extent_.height), 0.0f, 1.0f};
    VkRect2D scissor{{0, 0}, swapchain_extent_};
    VkPipelineViewportStateCreateInfo viewport_state{};
    viewport_state.sType = VK_STRUCTURE_TYPE_PIPELINE_VIEWPORT_STATE_CREATE_INFO;
    viewport_state.viewportCount = 1;
    viewport_state.pViewports = &viewport;
    viewport_state.scissorCount = 1;
    viewport_state.pScissors = &scissor;
    constexpr std::array<VkDynamicState, 2> dynamic_states = {
        VK_DYNAMIC_STATE_VIEWPORT,
        VK_DYNAMIC_STATE_SCISSOR,
    };
    VkPipelineDynamicStateCreateInfo dynamic_state{};
    dynamic_state.sType = VK_STRUCTURE_TYPE_PIPELINE_DYNAMIC_STATE_CREATE_INFO;
    dynamic_state.dynamicStateCount = static_cast<uint32_t>(
        dynamic_states.size());
    dynamic_state.pDynamicStates = dynamic_states.data();
    VkPipelineRasterizationStateCreateInfo raster{};
    raster.sType = VK_STRUCTURE_TYPE_PIPELINE_RASTERIZATION_STATE_CREATE_INFO;
    raster.polygonMode = VK_POLYGON_MODE_FILL;
    raster.cullMode = VK_CULL_MODE_NONE;
    raster.frontFace = VK_FRONT_FACE_CLOCKWISE;
    raster.lineWidth = 1.0f;
    VkPipelineMultisampleStateCreateInfo multisample{};
    multisample.sType = VK_STRUCTURE_TYPE_PIPELINE_MULTISAMPLE_STATE_CREATE_INFO;
    multisample.rasterizationSamples = VK_SAMPLE_COUNT_1_BIT;
    VkPipelineDepthStencilStateCreateInfo depth_stencil{};
    depth_stencil.sType =
        VK_STRUCTURE_TYPE_PIPELINE_DEPTH_STENCIL_STATE_CREATE_INFO;
    depth_stencil.depthBoundsTestEnable = VK_FALSE;
    depth_stencil.stencilTestEnable = VK_FALSE;
    VkPipelineColorBlendAttachmentState blend_attachment{};
    VkPipelineColorBlendStateCreateInfo blend{};
    blend.sType = VK_STRUCTURE_TYPE_PIPELINE_COLOR_BLEND_STATE_CREATE_INFO;
    blend.attachmentCount = 1;
    blend.pAttachments = &blend_attachment;
    VkGraphicsPipelineCreateInfo pipeline_info{};
    pipeline_info.sType = VK_STRUCTURE_TYPE_GRAPHICS_PIPELINE_CREATE_INFO;
    pipeline_info.stageCount = 2;
    pipeline_info.pStages = stages;
    pipeline_info.pVertexInputState = &vertex_input;
    pipeline_info.pInputAssemblyState = &assembly;
    pipeline_info.pViewportState = &viewport_state;
    pipeline_info.pRasterizationState = &raster;
    pipeline_info.pMultisampleState = &multisample;
    pipeline_info.pDepthStencilState = &depth_stencil;
    pipeline_info.pColorBlendState = &blend;
    pipeline_info.pDynamicState = &dynamic_state;
    pipeline_info.layout = pipeline_layout_;
    pipeline_info.renderPass = render_pass_;
    pipeline_info.subpass = 0;
    const size_t pipeline_count = states.size();
    std::vector<VkPipelineVertexInputStateCreateInfo> vertex_inputs(
        pipeline_count, vertex_input);
    std::vector<VkPipelineInputAssemblyStateCreateInfo> assemblies(
        pipeline_count, assembly);
    std::vector<VkPipelineRasterizationStateCreateInfo> rasters(
        pipeline_count, raster);
    std::vector<VkPipelineDepthStencilStateCreateInfo> depth_stencils(
        pipeline_count, depth_stencil);
    std::vector<VkPipelineColorBlendAttachmentState> blend_attachments(
        pipeline_count, blend_attachment);
    std::vector<VkPipelineColorBlendStateCreateInfo> blends(
        pipeline_count, blend);
    std::vector<VkGraphicsPipelineCreateInfo> pipeline_infos(
        pipeline_count, pipeline_info);
    std::vector<VkPipeline> pipelines(
        pipeline_count, VK_NULL_HANDLE);
    for (size_t index = 0; index < pipeline_count; ++index) {
        const NativePipelineState& state = states[index];
        VkPipelineVertexInputStateCreateInfo& state_vertex_input =
            vertex_inputs[index];
        state_vertex_input.vertexBindingDescriptionCount =
            state.raw_attribute_fetch ? 0u : 1u;
        state_vertex_input.vertexAttributeDescriptionCount =
            state.raw_attribute_fetch
            ? 0u
            : static_cast<uint32_t>(attributes.size());
        assemblies[index].topology = state.primitive == 5u
            ? VK_PRIMITIVE_TOPOLOGY_TRIANGLE_LIST
            : VK_PRIMITIVE_TOPOLOGY_TRIANGLE_STRIP;
        VkPipelineColorBlendAttachmentState& state_blend =
            blend_attachments[index];
        state_blend.blendEnable = state.blend_enable ? VK_TRUE : VK_FALSE;
        state_blend.srcColorBlendFactor = nv2a_blend_factor(
            state.blend_source_factor,
            VK_BLEND_FACTOR_ONE);
        state_blend.dstColorBlendFactor = nv2a_blend_factor(
            state.blend_destination_factor,
            VK_BLEND_FACTOR_ZERO);
        state_blend.colorBlendOp = nv2a_blend_op(state.blend_equation);
        state_blend.srcAlphaBlendFactor = state_blend.srcColorBlendFactor;
        state_blend.dstAlphaBlendFactor = state_blend.dstColorBlendFactor;
        state_blend.alphaBlendOp = state_blend.colorBlendOp;
        state_blend.colorWriteMask = nv2a_color_write_mask(state.color_mask);
        depth_stencils[index].depthTestEnable =
            state.depth_test_enable ? VK_TRUE : VK_FALSE;
        depth_stencils[index].depthWriteEnable =
            state.depth_write_enable ? VK_TRUE : VK_FALSE;
        depth_stencils[index].depthCompareOp =
            nv2a_depth_compare_op(state.depth_function);
        rasters[index].cullMode =
            nv2a_cull_mode(state.cull_face_enable, state.cull_face);
        rasters[index].frontFace = nv2a_front_face(state.front_face);
        blends[index].pAttachments = &blend_attachments[index];
        pipeline_infos[index].pVertexInputState = &vertex_inputs[index];
        pipeline_infos[index].pInputAssemblyState = &assemblies[index];
        pipeline_infos[index].pRasterizationState = &rasters[index];
        pipeline_infos[index].pDepthStencilState = &depth_stencils[index];
        pipeline_infos[index].pColorBlendState = &blends[index];
    }
    const auto pipeline_create_begin = std::chrono::steady_clock::now();
    const VkResult create_result = vkCreateGraphicsPipelines(
        device_,
        pipeline_cache_,
        static_cast<uint32_t>(pipeline_infos.size()),
        pipeline_infos.data(),
        nullptr,
        pipelines.data());
    const auto pipeline_create_end = std::chrono::steady_clock::now();
    if (create_result != VK_SUCCESS) {
        for (VkPipeline pipeline : pipelines) {
            if (pipeline != VK_NULL_HANDLE) {
                vkDestroyPipeline(device_, pipeline, nullptr);
            }
        }
        vkDestroyShaderModule(device_, fragment_shader, nullptr);
        vkDestroyShaderModule(device_, vertex_shader, nullptr);
        vk_check(create_result, "vkCreateGraphicsPipelines");
    }
    for (size_t index = 0; index < pipeline_count; ++index) {
        graphics_pipelines_.push_back({states[index], pipelines[index]});
    }
    pipeline_creation_count_ += pipeline_count;
    pipeline_cache_miss_count_ += pipeline_count;
    vkDestroyShaderModule(device_, fragment_shader, nullptr);
    vkDestroyShaderModule(device_, vertex_shader, nullptr);
    log_.emit(
        "nv2a_graphics_pipeline_created",
        {
            {"created_count", std::to_string(states.size())},
            {"pipeline_count", std::to_string(graphics_pipelines_.size())},
            {"candidate_draws", std::to_string(
                last_pipeline_candidate_draw_count_)},
            {"unique_states", std::to_string(
                last_pipeline_unique_state_count_)},
            {"state_discovery_us", std::to_string(
                last_pipeline_state_discovery_us_)},
            {"shader_module_us", std::to_string(
                std::chrono::duration_cast<std::chrono::microseconds>(
                    shader_module_end - shader_module_begin).count())},
            {"pipeline_create_us", std::to_string(
                std::chrono::duration_cast<std::chrono::microseconds>(
                    pipeline_create_end - pipeline_create_begin).count())},
            {"batched", json_bool(true)},
        });
}

void VulkanPresenter::create_buffer(
    VkDeviceSize size,
    VkBufferUsageFlags usage,
    VkMemoryPropertyFlags properties,
    VkBuffer& buffer,
    VkDeviceMemory& memory) {
    VkBufferCreateInfo buffer_info{};
    buffer_info.sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO;
    buffer_info.size = size;
    buffer_info.usage = usage;
    buffer_info.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
    vk_check(vkCreateBuffer(device_, &buffer_info, nullptr, &buffer), "vkCreateBuffer(native)");
    VkMemoryRequirements requirements{};
    vkGetBufferMemoryRequirements(device_, buffer, &requirements);
    VkMemoryAllocateInfo allocation{};
    allocation.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO;
    allocation.allocationSize = requirements.size;
    allocation.memoryTypeIndex = find_memory_type(requirements.memoryTypeBits, properties);
    vk_check(vkAllocateMemory(device_, &allocation, nullptr, &memory), "vkAllocateMemory(native)");
    vk_check(vkBindBufferMemory(device_, buffer, memory, 0), "vkBindBufferMemory(native)");
}

void VulkanPresenter::refresh_raw_vertex_buffers() {
    const auto upload_begin = std::chrono::steady_clock::now();
    const std::vector<uint8_t>& resource_bytes =
        gpu_raw_vertex_resource_cache_.gpu_raw_vertex_bytes;
    const VkDeviceSize index_offset =
        resource_bytes.size();
    const VkDeviceSize required_size = std::max<VkDeviceSize>(
        index_offset
            + interpreted_stream_.gpu_raw_vertex_indices.size()
                * sizeof(uint32_t),
        4u);
    const VkDeviceSize minimum_live_size = options_.live_render_stream
        ? index_offset + 1024u * 1024u
        : required_size;
    bool buffer_replaced = false;
    if (raw_vertex_resource_buffer_ == VK_NULL_HANDLE
        || required_size > raw_vertex_resource_buffer_size_) {
        destroy_host_texture_bindings();
        if (raw_vertex_resource_mapped_ != nullptr) {
            vkUnmapMemory(device_, raw_vertex_resource_memory_);
            raw_vertex_resource_mapped_ = nullptr;
        }
        if (raw_vertex_resource_buffer_ != VK_NULL_HANDLE) {
            vkDestroyBuffer(
                device_, raw_vertex_resource_buffer_, nullptr);
        }
        if (raw_vertex_resource_memory_ != VK_NULL_HANDLE) {
            vkFreeMemory(
                device_, raw_vertex_resource_memory_, nullptr);
        }
        raw_vertex_resource_buffer_size_ = std::max(
            required_size, minimum_live_size);
        create_buffer(
            raw_vertex_resource_buffer_size_,
            VK_BUFFER_USAGE_STORAGE_BUFFER_BIT,
            VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT
                | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT,
            raw_vertex_resource_buffer_,
            raw_vertex_resource_memory_);
        vk_check(
            vkMapMemory(
                device_,
                raw_vertex_resource_memory_,
                0,
                raw_vertex_resource_buffer_size_,
                0,
                &raw_vertex_resource_mapped_),
            "vkMapMemory(raw vertex resources)");
        buffer_replaced = true;
    }
    const uint32_t pending_dirty_range_count = static_cast<uint32_t>(
        gpu_raw_vertex_resource_cache_.dirty_ranges.size());
    uint64_t resource_upload_bytes = 0u;
    uint32_t resource_upload_range_count = 0u;
    if (buffer_replaced && !resource_bytes.empty()) {
        std::memcpy(
            raw_vertex_resource_mapped_,
            resource_bytes.data(),
            resource_bytes.size());
        resource_upload_bytes = resource_bytes.size();
        resource_upload_range_count = 1u;
    } else if (!buffer_replaced) {
        for (const GpuRawVertexDirtyRange& range :
             gpu_raw_vertex_resource_cache_.dirty_ranges) {
            std::memcpy(
                static_cast<uint8_t*>(raw_vertex_resource_mapped_)
                    + range.offset,
                resource_bytes.data() + range.offset,
                range.size);
            resource_upload_bytes += range.size;
            ++resource_upload_range_count;
        }
    }
    gpu_raw_vertex_resource_cache_.dirty_ranges.clear();
    if (interpreted_stream_.gpu_raw_vertex_indices.empty()) {
        if (required_size == 4u) {
            std::memset(raw_vertex_resource_mapped_, 0, 4u);
        }
    } else {
        std::memcpy(
            static_cast<uint8_t*>(raw_vertex_resource_mapped_)
                + index_offset,
            interpreted_stream_.gpu_raw_vertex_indices.data(),
            interpreted_stream_.gpu_raw_vertex_indices.size()
                * sizeof(uint32_t));
    }
    const uint64_t index_upload_bytes =
        interpreted_stream_.gpu_raw_vertex_indices.size()
            * sizeof(uint32_t);
    upload_bytes_ += resource_upload_bytes + index_upload_bytes;
    raw_vertex_resource_upload_bytes_ += resource_upload_bytes;
    raw_vertex_index_upload_bytes_ += index_upload_bytes;
    last_raw_vertex_upload_us_ = std::chrono::duration_cast<
        std::chrono::microseconds>(
            std::chrono::steady_clock::now() - upload_begin).count();
    log_.emit(
        "nv2a_raw_vertex_buffers_refreshed",
        {
            {"reload", std::to_string(live_render_reload_count_ + 1u)},
            {"manifest_guest_flip_count", std::to_string(
                current_manifest_guest_flip_count_)},
            {"resources_unchanged", json_bool(
                recovered_source_.resources_unchanged)},
            {"resource_bytes", std::to_string(
                resource_bytes.size())},
            {"source_indices", std::to_string(
                interpreted_stream_.gpu_raw_vertex_indices.size())},
            {"gpu_draws", std::to_string(
                interpreted_stream_.gpu_raw_attribute_draw_count)},
            {"gpu_vertices", std::to_string(
                interpreted_stream_.gpu_raw_attribute_vertex_count)},
            {"buffer_replaced", json_bool(buffer_replaced)},
            {"layout_rebuilt", json_bool(
                gpu_raw_vertex_resource_cache_.layout_rebuilt)},
            {"cache_refreshes", std::to_string(
                gpu_raw_vertex_resource_cache_.refresh_count)},
            {"layout_rebuilds", std::to_string(
                gpu_raw_vertex_resource_cache_.layout_rebuild_count)},
            {"resource_count", std::to_string(
                gpu_raw_vertex_resource_cache_.resource_count)},
            {"compared_resources", std::to_string(
                gpu_raw_vertex_resource_cache_.compared_resource_count)},
            {"changed_resources", std::to_string(
                gpu_raw_vertex_resource_cache_.changed_resource_count)},
            {"reused_resources", std::to_string(
                gpu_raw_vertex_resource_cache_.reused_resource_count)},
            {"compared_bytes", std::to_string(
                gpu_raw_vertex_resource_cache_.compared_bytes)},
            {"pending_dirty_ranges", std::to_string(
                pending_dirty_range_count)},
            {"dirty_bytes", std::to_string(
                gpu_raw_vertex_resource_cache_.dirty_bytes)},
            {"cache_refresh_us", std::to_string(
                gpu_raw_vertex_resource_cache_.refresh_us)},
            {"resource_upload_ranges", std::to_string(
                resource_upload_range_count)},
            {"resource_upload_bytes", std::to_string(
                resource_upload_bytes)},
            {"index_upload_bytes", std::to_string(index_upload_bytes)},
            {"cumulative_resource_upload_bytes", std::to_string(
                raw_vertex_resource_upload_bytes_)},
            {"cumulative_index_upload_bytes", std::to_string(
                raw_vertex_index_upload_bytes_)},
            {"resource_bytes_uploaded", json_bool(
                resource_upload_bytes != 0u)},
            {"upload_us", std::to_string(last_raw_vertex_upload_us_)},
        });
}

void VulkanPresenter::refresh_fragment_states() {
    const size_t first_draw = std::min<size_t>(
        interpreted_stream_.presented_draw_begin,
        interpreted_stream_.draws.size());
    const size_t end_draw = std::min<size_t>(
        first_draw + interpreted_stream_.presented_draw_count,
        interpreted_stream_.draws.size());
    std::vector<NativeFragmentState> states;
    states.reserve(std::max<size_t>(end_draw - first_draw, 1u));
    for (size_t draw_index = first_draw;
         draw_index < end_draw;
         ++draw_index) {
        states.push_back(fragment_state_for_draw(
            interpreted_stream_.draws[draw_index]));
    }
    frontend_text_fragment_state_index_ = std::numeric_limits<uint32_t>::max();
    if (!recovered_frontend_text_.empty()) {
        frontend_text_fragment_state_index_ = static_cast<uint32_t>(
            states.size());
        states.push_back(frontend_text_fragment_state());
    }
    if (states.empty()) {
        states.emplace_back();
    }

    const VkDeviceSize required_size = states.size()
        * sizeof(NativeFragmentState);
    bool buffer_replaced = false;
    if (fragment_state_buffer_ == VK_NULL_HANDLE
        || required_size > fragment_state_buffer_size_) {
        destroy_host_texture_bindings();
        if (fragment_state_mapped_ != nullptr) {
            vkUnmapMemory(device_, fragment_state_memory_);
            fragment_state_mapped_ = nullptr;
        }
        if (fragment_state_buffer_ != VK_NULL_HANDLE) {
            vkDestroyBuffer(device_, fragment_state_buffer_, nullptr);
            fragment_state_buffer_ = VK_NULL_HANDLE;
        }
        if (fragment_state_memory_ != VK_NULL_HANDLE) {
            vkFreeMemory(device_, fragment_state_memory_, nullptr);
            fragment_state_memory_ = VK_NULL_HANDLE;
        }
        const VkDeviceSize minimum_live_size = options_.live_render_stream
            ? 4096u * sizeof(NativeFragmentState)
            : required_size;
        fragment_state_buffer_size_ = std::max(
            required_size, minimum_live_size);
        create_buffer(
            fragment_state_buffer_size_,
            VK_BUFFER_USAGE_STORAGE_BUFFER_BIT,
            VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT
                | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT,
            fragment_state_buffer_,
            fragment_state_memory_);
        vk_check(
            vkMapMemory(
                device_,
                fragment_state_memory_,
                0,
                fragment_state_buffer_size_,
                0,
                &fragment_state_mapped_),
            "vkMapMemory(fragment states)");
        buffer_replaced = true;
    }
    std::memcpy(
        fragment_state_mapped_,
        states.data(),
        static_cast<size_t>(required_size));
    upload_bytes_ += static_cast<uint64_t>(required_size);
    fragment_state_count_ = static_cast<uint32_t>(states.size());
    log_.emit(
        "nv2a_fragment_states_refreshed",
        {
            {"states", std::to_string(fragment_state_count_)},
            {"state_bytes", std::to_string(required_size)},
            {"buffer_bytes", std::to_string(fragment_state_buffer_size_)},
            {"buffer_replaced", json_bool(buffer_replaced)},
        });
}

void VulkanPresenter::refresh_vertex_program_states() {
    const auto upload_begin = std::chrono::steady_clock::now();
    const size_t first_draw = std::min<size_t>(
        interpreted_stream_.presented_draw_begin,
        interpreted_stream_.draws.size());
    const size_t end_draw = std::min<size_t>(
        first_draw + interpreted_stream_.presented_draw_count,
        interpreted_stream_.draws.size());
    const std::vector<RenderTargetFeedbackSpec>& feedback_specs =
        presented_render_target_feedback_specs();
    const bool enable_gpu_programs = !options_.cpu_vertex_programs
        && !options_.analyze_render_stream_only;
    std::vector<NativeVertexProgramState> states;
    states.reserve(std::max<size_t>(end_draw - first_draw, 1u));
    gpu_vertex_program_draw_count_ = 0u;
    gpu_vertex_program_vertex_count_ = 0u;
    gpu_raw_attribute_draw_count_ = 0u;
    gpu_raw_attribute_vertex_count_ = 0u;
    cpu_vertex_program_fallback_draw_count_ = 0u;
    cpu_vertex_program_fallback_vertex_count_ = 0u;
    for (size_t draw_index = first_draw;
         draw_index < end_draw;
         ++draw_index) {
        const NativeDraw& draw = interpreted_stream_.draws[draw_index];
        VkExtent2D target_extent = swapchain_extent_;
        if (!draw_targets_presented_surface(draw)) {
            const auto target = std::find_if(
                feedback_specs.begin(),
                feedback_specs.end(),
                [&](const RenderTargetFeedbackSpec& spec) {
                    return spec.offscreen_produced
                        && nv2a_canonical_resource_address(
                            spec.producer_address)
                            == nv2a_canonical_resource_address(
                                draw.surface_color_offset);
                });
            if (target != feedback_specs.end()) {
                target_extent = {target->width, target->height};
            }
        }
        NativeVertexProgramState state = vertex_program_state_for_draw(
            draw,
            target_extent,
            enable_gpu_programs);
        if ((draw.transform_execution_mode & 3u) == 2u) {
            if (state.enabled != 0u) {
                ++gpu_vertex_program_draw_count_;
                gpu_vertex_program_vertex_count_ += draw.vertex_count;
                if (draw.gpu_raw_attribute_fetch) {
                    ++gpu_raw_attribute_draw_count_;
                    gpu_raw_attribute_vertex_count_ += draw.vertex_count;
                }
            } else {
                ++cpu_vertex_program_fallback_draw_count_;
                cpu_vertex_program_fallback_vertex_count_ +=
                    draw.vertex_count;
            }
        }
        states.push_back(std::move(state));
    }
    if (!recovered_frontend_text_.empty()) {
        states.emplace_back();
    }
    if (states.empty()) {
        states.emplace_back();
    }

    const VkDeviceSize required_size = states.size()
        * sizeof(NativeVertexProgramState);
    bool buffer_replaced = false;
    if (vertex_program_state_buffer_ == VK_NULL_HANDLE
        || required_size > vertex_program_state_buffer_size_) {
        destroy_host_texture_bindings();
        if (vertex_program_state_mapped_ != nullptr) {
            vkUnmapMemory(device_, vertex_program_state_memory_);
            vertex_program_state_mapped_ = nullptr;
        }
        if (vertex_program_state_buffer_ != VK_NULL_HANDLE) {
            vkDestroyBuffer(
                device_, vertex_program_state_buffer_, nullptr);
            vertex_program_state_buffer_ = VK_NULL_HANDLE;
        }
        if (vertex_program_state_memory_ != VK_NULL_HANDLE) {
            vkFreeMemory(
                device_, vertex_program_state_memory_, nullptr);
            vertex_program_state_memory_ = VK_NULL_HANDLE;
        }
        const VkDeviceSize minimum_live_size = options_.live_render_stream
            ? 1024u * sizeof(NativeVertexProgramState)
            : required_size;
        vertex_program_state_buffer_size_ = std::max(
            required_size,
            minimum_live_size);
        create_buffer(
            vertex_program_state_buffer_size_,
            VK_BUFFER_USAGE_STORAGE_BUFFER_BIT,
            VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT
                | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT,
            vertex_program_state_buffer_,
            vertex_program_state_memory_);
        vk_check(
            vkMapMemory(
                device_,
                vertex_program_state_memory_,
                0,
                vertex_program_state_buffer_size_,
                0,
                &vertex_program_state_mapped_),
            "vkMapMemory(vertex program states)");
        buffer_replaced = true;
    }
    std::memcpy(
        vertex_program_state_mapped_,
        states.data(),
        static_cast<size_t>(required_size));
    upload_bytes_ += static_cast<uint64_t>(required_size);
    vertex_program_state_count_ = static_cast<uint32_t>(states.size());
    last_vertex_state_upload_us_ = std::chrono::duration_cast<
        std::chrono::microseconds>(
            std::chrono::steady_clock::now() - upload_begin).count();
    log_.emit(
        "nv2a_vertex_program_states_refreshed",
        {
            {"states", std::to_string(vertex_program_state_count_)},
            {"state_bytes", std::to_string(required_size)},
            {"buffer_bytes", std::to_string(
                vertex_program_state_buffer_size_)},
            {"buffer_replaced", json_bool(buffer_replaced)},
            {"gpu_draws", std::to_string(
                gpu_vertex_program_draw_count_)},
            {"gpu_vertices", std::to_string(
                gpu_vertex_program_vertex_count_)},
            {"gpu_raw_attribute_draws", std::to_string(
                gpu_raw_attribute_draw_count_)},
            {"gpu_raw_attribute_vertices", std::to_string(
                gpu_raw_attribute_vertex_count_)},
            {"cpu_fallback_draws", std::to_string(
                cpu_vertex_program_fallback_draw_count_)},
            {"cpu_fallback_vertices", std::to_string(
                cpu_vertex_program_fallback_vertex_count_)},
            {"upload_us", std::to_string(last_vertex_state_upload_us_)},
        });
}

void VulkanPresenter::create_native_render_resources(
    bool create_textures,
    bool reuse_offscreen_render_targets) {
    const auto native_resource_begin = std::chrono::steady_clock::now();
    last_vertex_resource_prepare_us_ = 0u;
    last_state_resource_prepare_us_ = 0u;
    last_texture_resource_prepare_us_ = 0u;
    last_offscreen_resource_prepare_us_ = 0u;
    last_resource_bookkeeping_us_ = 0u;
    last_texture_refresh_us_ = 0u;
    last_texture_indexed_lookup_count_ = 0u;
    last_texture_indexed_lookup_candidate_count_ = 0u;
    last_texture_constant_lookup_count_ = 0u;
    last_render_target_feedback_image_cache_hit_count_ = 0u;
    last_render_target_feedback_image_cache_miss_count_ = 0u;
    last_render_target_feedback_image_cache_store_count_ = 0u;
    last_render_target_feedback_image_cache_eviction_count_ = 0u;
    if (create_textures) {
        // This diagnostic belongs to the resident resource generation.
        // Command-only reloads must not erase a failure from the texture
        // payload that is still installed.
        unsupported_texture_resource_count_ = 0;
    }
    last_vertex_map_us_ = 0;
    last_vertex_copy_us_ = 0;
    // Resource generations still run the lightweight validation below.
    // Do not also force the per-draw, per-vertex, and transform-token event
    // flood; sample that diagnostic detail at a fixed cadence instead.
    const bool diagnostic_sample_due = log_.enabled()
        && (!options_.live_render_stream
            || (options_.strict_render_validation
                && (live_render_reload_count_ == 0u
                    || live_render_reload_count_ % 120u == 0u)));
    last_presented_diagnostics_sampled_ = diagnostic_sample_due;
    last_render_validation_us_ = 0;
    std::vector<NativeVertex> vertices = prepare_presented_vertices(
        diagnostic_sample_due);
    frontend_text_rectangle_count_ = append_frontend_text_vertices(
        vertices, recovered_frontend_text_);
    uploaded_vertex_count_ = static_cast<uint32_t>(vertices.size());
    const VkDeviceSize uploaded_size =
        vertices.size() * sizeof(NativeVertex);
    const VkDeviceSize required_size = std::max<VkDeviceSize>(
        uploaded_size,
        sizeof(NativeVertex));
    if (vertex_buffer_ != VK_NULL_HANDLE
        && required_size > vertex_buffer_size_) {
        destroy_host_texture_bindings();
        if (vertex_mapped_ != nullptr) {
            vkUnmapMemory(device_, vertex_memory_);
            vertex_mapped_ = nullptr;
        }
        vkDestroyBuffer(device_, vertex_buffer_, nullptr);
        vkFreeMemory(device_, vertex_memory_, nullptr);
        vertex_buffer_ = VK_NULL_HANDLE;
        vertex_memory_ = VK_NULL_HANDLE;
        vertex_buffer_size_ = 0u;
    }
    if (vertex_buffer_ == VK_NULL_HANDLE) {
        const VkDeviceSize allocation_size = options_.live_render_stream
            ? std::max<VkDeviceSize>(required_size, 1024u * 1024u)
            : required_size;
        vertex_buffer_size_ = allocation_size;
        create_buffer(
            vertex_buffer_size_,
            VK_BUFFER_USAGE_VERTEX_BUFFER_BIT
                | VK_BUFFER_USAGE_STORAGE_BUFFER_BIT,
            VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT
                | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT,
            vertex_buffer_,
            vertex_memory_);
    }
    if (!vertex_mapped_) {
        const auto map_begin = std::chrono::steady_clock::now();
        vk_check(
            vkMapMemory(
                device_,
                vertex_memory_,
                0,
                vertex_buffer_size_,
                0,
                &vertex_mapped_),
            "vkMapMemory(vertices)");
        last_vertex_map_us_ = std::chrono::duration_cast<
            std::chrono::microseconds>(
                std::chrono::steady_clock::now() - map_begin).count();
    }
    if (uploaded_size != 0u) {
        const auto copy_begin = std::chrono::steady_clock::now();
        std::memcpy(
            vertex_mapped_,
            vertices.data(),
            static_cast<size_t>(uploaded_size));
        upload_bytes_ += static_cast<uint64_t>(uploaded_size);
        last_vertex_copy_us_ = std::chrono::duration_cast<
            std::chrono::microseconds>(
                std::chrono::steady_clock::now() - copy_begin).count();
    } else {
        std::memset(vertex_mapped_, 0, sizeof(NativeVertex));
    }

    last_vertex_resource_prepare_us_ = std::chrono::duration_cast<
        std::chrono::microseconds>(
            std::chrono::steady_clock::now()
            - native_resource_begin).count();
    const auto state_resource_begin = std::chrono::steady_clock::now();
    refresh_raw_vertex_buffers();
    refresh_vertex_program_states();
    refresh_fragment_states();
    last_state_resource_prepare_us_ = std::chrono::duration_cast<
        std::chrono::microseconds>(
            std::chrono::steady_clock::now()
            - state_resource_begin).count();

    const auto texture_resource_begin = std::chrono::steady_clock::now();
    last_render_target_feedback_pruned_count_ = 0u;
    const bool feedback_refresh_required =
        render_target_feedback_refresh_required();
    if (reuse_offscreen_render_targets
        && !offscreen_render_targets_match_presented_specs()) {
        throw std::runtime_error(
            "cannot reuse offscreen targets with stale attachments");
    }
    if (create_textures || feedback_refresh_required) {
        refresh_host_textures(!create_textures);
    } else {
        refresh_host_texture_bindings();
    }
    if (reuse_offscreen_render_targets
        && !offscreen_render_targets_match_presented_specs()) {
        throw std::runtime_error(
            "offscreen target backing texture changed during refresh");
    }
    last_texture_resource_prepare_us_ = std::chrono::duration_cast<
        std::chrono::microseconds>(
            std::chrono::steady_clock::now()
            - texture_resource_begin).count();
    const auto offscreen_resource_begin =
        std::chrono::steady_clock::now();
    last_offscreen_render_targets_reused_ = reuse_offscreen_render_targets;
    if (!reuse_offscreen_render_targets) {
        create_offscreen_render_targets();
    } else {
        log_.emit(
            "nv2a_offscreen_render_targets_reused",
            {{"count", std::to_string(offscreen_render_targets_.size())}});
    }
    last_offscreen_resource_prepare_us_ = std::chrono::duration_cast<
        std::chrono::microseconds>(
            std::chrono::steady_clock::now()
            - offscreen_resource_begin).count();
    const auto resource_bookkeeping_begin =
        std::chrono::steady_clock::now();
    const std::vector<RenderTargetFeedbackSpec>& feedback_specs =
        presented_render_target_feedback_specs();
    const uint32_t render_target_feedback_texture_count =
        static_cast<uint32_t>(std::count_if(
            host_textures_.begin(),
            host_textures_.end(),
            [](const HostTexture& texture) {
                return texture.render_target_feedback;
            }));
    const uint32_t missing_render_target_feedback_texture_count =
        static_cast<uint32_t>(std::count_if(
            feedback_specs.begin(),
            feedback_specs.end(),
            [&](const RenderTargetFeedbackSpec& spec) {
                return std::none_of(
                    host_textures_.begin(),
                    host_textures_.end(),
                    [&](const HostTexture& texture) {
                        return render_target_feedback_texture_matches_spec(
                            texture,
                            spec);
                    });
            }));
    const uint32_t stale_render_target_feedback_texture_count =
        static_cast<uint32_t>(std::count_if(
            host_textures_.begin(),
            host_textures_.end(),
            [&](const HostTexture& texture) {
                return texture.render_target_feedback
                    && std::none_of(
                        feedback_specs.begin(),
                        feedback_specs.end(),
                        [&](const RenderTargetFeedbackSpec& spec) {
                            return render_target_feedback_texture_matches_spec(
                                texture,
                                spec);
                        });
            }));
    std::ostringstream required_feedback_addresses;
    required_feedback_addresses << '[';
    for (size_t index = 0; index < feedback_specs.size(); ++index) {
        if (index != 0u) {
            required_feedback_addresses << ',';
        }
        required_feedback_addresses << feedback_specs[index].address;
    }
    required_feedback_addresses << ']';
    std::ostringstream active_feedback_addresses;
    active_feedback_addresses << '[';
    bool first_feedback_address = true;
    for (const HostTexture& texture : host_textures_) {
        if (!texture.render_target_feedback) {
            continue;
        }
        if (!first_feedback_address) {
            active_feedback_addresses << ',';
        }
        active_feedback_addresses << texture.guest_address;
        first_feedback_address = false;
    }
    active_feedback_addresses << ']';
    log_.emit(
        "nv2a_native_resources_created",
        {
            {"vertices", std::to_string(interpreted_stream_.vertices.size())},
            {"uploaded_vertices", std::to_string(uploaded_vertex_count_)},
            {"uploaded_vertex_base", std::to_string(uploaded_vertex_base_)},
            {"draws", std::to_string(interpreted_stream_.draws.size())},
            {"presented_draws", std::to_string(interpreted_stream_.presented_draw_count)},
            {"guest_flips", std::to_string(interpreted_stream_.flip_count)},
            {"manifest_guest_flip_count", std::to_string(current_manifest_guest_flip_count_)},
            {"manifest_guest_steps", std::to_string(current_manifest_guest_steps_)},
            {"textures", std::to_string(host_textures_.size() - 1u)},
            {"render_target_feedback_textures", std::to_string(render_target_feedback_texture_count)},
            {"render_target_feedback_required", std::to_string(feedback_specs.size())},
            {"render_target_feedback_missing", std::to_string(
                missing_render_target_feedback_texture_count)},
            {"render_target_feedback_stale", std::to_string(
                stale_render_target_feedback_texture_count)},
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
            {"render_target_feedback_required_addresses",
                required_feedback_addresses.str()},
            {"render_target_feedback_active_addresses",
                active_feedback_addresses.str()},
            {"render_target_feedback_refreshed", json_bool(feedback_refresh_required)},
            {"offscreen_render_targets_reused", json_bool(
                last_offscreen_render_targets_reused_)},
            {"presented_diagnostics_sampled", json_bool(
                last_presented_diagnostics_sampled_)},
            {"presented_half_quad_recovered", json_bool(presented_half_quad_recovered_)},
            {"presented_overscan_height_recovered", json_bool(presented_overscan_height_recovered_)},
            {"presented_vertex_program_transformed_count", std::to_string(presented_vertex_program_transformed_count_)},
            {"presented_fixed_function_transformed_count", std::to_string(presented_fixed_function_transformed_count_)},
            {"presented_linear_texture_normalized_vertex_count", std::to_string(presented_linear_texture_normalized_vertex_count_)},
            {"offscreen_render_target_draw_count", std::to_string(offscreen_render_target_draw_count_)},
            {"offscreen_render_target_transformed_vertex_count", std::to_string(offscreen_render_target_transformed_vertex_count_)},
            {"presented_surface_color_offset", std::to_string(presented_surface_color_offset_)},
            {"indexed_array_draws", std::to_string(interpreted_stream_.indexed_array_draw_count)},
            {"indexed_array_elements", std::to_string(interpreted_stream_.indexed_array_element_count)},
            {"materialized_indexed_draws", std::to_string(interpreted_stream_.materialized_indexed_draw_count)},
            {"materialized_indexed_vertices", std::to_string(interpreted_stream_.materialized_indexed_vertex_count)},
            {"missing_indexed_resource_draws", std::to_string(interpreted_stream_.missing_indexed_resource_draw_count)},
            {"vertex_transform_us", std::to_string(last_vertex_transform_us_)},
            {"vertex_state_upload_us", std::to_string(
                last_vertex_state_upload_us_)},
            {"raw_vertex_upload_us", std::to_string(
                last_raw_vertex_upload_us_)},
            {"gpu_raw_attribute_draws", std::to_string(
                gpu_raw_attribute_draw_count_)},
            {"gpu_raw_attribute_vertices", std::to_string(
                gpu_raw_attribute_vertex_count_)},
            {"expanded_vertex_bytes_avoided", std::to_string(
                static_cast<uint64_t>(
                    gpu_raw_attribute_vertex_count_)
                    * sizeof(NativeVertex))},
            {"gpu_vertex_program_draws", std::to_string(
                gpu_vertex_program_draw_count_)},
            {"gpu_vertex_program_vertices", std::to_string(
                gpu_vertex_program_vertex_count_)},
            {"cpu_vertex_program_fallback_draws", std::to_string(
                cpu_vertex_program_fallback_draw_count_)},
            {"cpu_vertex_program_fallback_vertices", std::to_string(
                cpu_vertex_program_fallback_vertex_count_)},
            {"vertex_map_us", std::to_string(last_vertex_map_us_)},
            {"vertex_copy_us", std::to_string(last_vertex_copy_us_)},
        });
    last_resource_bookkeeping_us_ = std::chrono::duration_cast<
        std::chrono::microseconds>(
            std::chrono::steady_clock::now()
            - resource_bookkeeping_begin).count();
    const auto render_validation_begin =
        std::chrono::steady_clock::now();
    if (!log_.enabled() && !options_.strict_render_validation) {
        return;
    }
    const size_t diagnostic_first_draw = std::min<size_t>(
        interpreted_stream_.presented_draw_begin,
        interpreted_stream_.draws.size());
    const size_t diagnostic_end_draw = std::min<size_t>(
        diagnostic_first_draw + interpreted_stream_.presented_draw_count,
        interpreted_stream_.draws.size());
    uint32_t textured_presented_draw_count = 0;
    uint32_t unmatched_presented_texture_draw_count = 0;
    uint32_t unsupported_presented_primitive_count = 0;
    uint32_t missing_presented_indexed_resource_draw_count = 0;
    uint32_t filtered_fixed_function_indexed_draw_count = 0;
    uint32_t filtered_fixed_function_draw_count = 0;
    uint32_t offscreen_render_target_draw_count = 0;
    uint32_t valid_geometry_draw_count = 0;
    uint32_t invalid_geometry_range_count = 0;
    uint32_t gpu_raw_attribute_draw_count = 0;
    uint32_t non_finite_position_draw_count = 0;
    uint32_t collapsed_x_draw_count = 0;
    uint32_t collapsed_y_draw_count = 0;
    uint32_t zero_area_draw_count = 0;
    uint32_t exact_center_origin_draw_count = 0;
    uint32_t fully_offscreen_draw_count = 0;
    uint32_t outside_viewport_draw_count = 0;
    uint64_t presented_vertex_count = 0;
    uint32_t fullscreen_draw_count = 0;
    uint32_t textured_fullscreen_draw_count = 0;
    uint32_t untextured_presented_draw_count = 0;
    uint32_t alpha_only_presented_draw_count = 0;
    uint32_t zero_alpha_presented_draw_count = 0;
    std::vector<uint32_t> presented_texture_addresses;
    int64_t first_zero_area_presented_index = -1;
    int64_t first_center_origin_presented_index = -1;
    bool overall_bounds_valid = false;
    float overall_min_x = 0.0f;
    float overall_max_x = 0.0f;
    float overall_min_y = 0.0f;
    float overall_max_y = 0.0f;
    const bool emit_presented_details = presented_diagnostics_valid_
        && presented_diagnostics_generation_ == render_work_generation_;
    if (emit_presented_details) {
        emit_presented_vertex_transform_diagnostics(true);
        emit_presented_render_state_diagnostics();
    }
    for (size_t draw_index = diagnostic_first_draw;
         draw_index < diagnostic_end_draw;
         ++draw_index) {
        const NativeDraw& draw = interpreted_stream_.draws[draw_index];
        if (!draw_targets_presented_surface(draw)) {
            ++offscreen_render_target_draw_count;
            continue;
        }
        const bool draw_textured = draw.texture_enabled
            && draw.texture_address != 0u;
        if (!draw_textured) {
            ++untextured_presented_draw_count;
        }
        const VkColorComponentFlags write_mask = nv2a_color_write_mask(
            draw.color_mask);
        if ((write_mask & VK_COLOR_COMPONENT_A_BIT) != 0u
            && (write_mask & (VK_COLOR_COMPONENT_R_BIT
                | VK_COLOR_COMPONENT_G_BIT
                | VK_COLOR_COMPONENT_B_BIT)) == 0u) {
            ++alpha_only_presented_draw_count;
        }
        if (draw.primitive != 5u && draw.primitive != 6u) {
            ++unsupported_presented_primitive_count;
        }
        if (draw.indexed_array && draw.vertex_count == 0u) {
            ++missing_presented_indexed_resource_draw_count;
        }
        if (!draw_has_supported_host_transform(draw)) {
            ++filtered_fixed_function_draw_count;
            filtered_fixed_function_indexed_draw_count +=
                draw.indexed_array ? 1u : 0u;
        }
        if (draw.gpu_raw_attribute_fetch) {
            ++valid_geometry_draw_count;
            ++gpu_raw_attribute_draw_count;
            presented_vertex_count += draw.vertex_count;
        } else if (draw.vertex_count == 0u ||
            draw.first_vertex + draw.vertex_count > interpreted_stream_.vertices.size()) {
            ++invalid_geometry_range_count;
        } else {
            ++valid_geometry_draw_count;
            presented_vertex_count += draw.vertex_count;
            bool draw_bounds_valid = false;
            bool non_finite = false;
            bool exact_center_origin = true;
            float max_a = 0.0f;
            float min_x = 0.0f;
            float max_x = 0.0f;
            float min_y = 0.0f;
            float max_y = 0.0f;
            for (uint32_t vertex_index = 0; vertex_index < draw.vertex_count; ++vertex_index) {
                const NativeVertex& vertex =
                    interpreted_stream_.vertices[draw.first_vertex + vertex_index];
                if (!std::isfinite(vertex.x) || !std::isfinite(vertex.y)) {
                    non_finite = true;
                    exact_center_origin = false;
                    continue;
                }
                exact_center_origin = exact_center_origin
                    && vertex.raw_x_bits == 0x43A00000u
                    && (vertex.raw_y_bits & 0x7FFFFFFFu) == 0u;
                max_a = std::max(max_a, vertex.a);
                if (!draw_bounds_valid) {
                    min_x = max_x = vertex.x;
                    min_y = max_y = vertex.y;
                    draw_bounds_valid = true;
                } else {
                    min_x = std::min(min_x, vertex.x);
                    max_x = std::max(max_x, vertex.x);
                    min_y = std::min(min_y, vertex.y);
                    max_y = std::max(max_y, vertex.y);
                }
            }
            if (non_finite) {
                ++non_finite_position_draw_count;
            }
            if (draw_bounds_valid) {
                if (max_a <= 0.0001f) {
                    ++zero_alpha_presented_draw_count;
                }
                if (!overall_bounds_valid) {
                    overall_min_x = min_x;
                    overall_max_x = max_x;
                    overall_min_y = min_y;
                    overall_max_y = max_y;
                    overall_bounds_valid = true;
                } else {
                    overall_min_x = std::min(overall_min_x, min_x);
                    overall_max_x = std::max(overall_max_x, max_x);
                    overall_min_y = std::min(overall_min_y, min_y);
                    overall_max_y = std::max(overall_max_y, max_y);
                }
                const bool collapsed_x = std::abs(max_x - min_x) <= 0.0001f;
                const bool collapsed_y = std::abs(max_y - min_y) <= 0.0001f;
                if (collapsed_x) {
                    ++collapsed_x_draw_count;
                }
                if (collapsed_y) {
                    ++collapsed_y_draw_count;
                }
                if (collapsed_x && collapsed_y) {
                    ++zero_area_draw_count;
                    if (first_zero_area_presented_index < 0) {
                        first_zero_area_presented_index = static_cast<int64_t>(
                            draw_index - diagnostic_first_draw);
                    }
                }
                if (exact_center_origin) {
                    ++exact_center_origin_draw_count;
                    if (first_center_origin_presented_index < 0) {
                        first_center_origin_presented_index = static_cast<int64_t>(
                            draw_index - diagnostic_first_draw);
                    }
                }
                if (max_x < 0.0f || min_x > static_cast<float>(options_.width)
                    || max_y < 0.0f || min_y > static_cast<float>(options_.height)) {
                    ++fully_offscreen_draw_count;
                }
                if (min_x < 0.0f || max_x > static_cast<float>(options_.width)
                    || min_y < 0.0f || max_y > static_cast<float>(options_.height)) {
                    ++outside_viewport_draw_count;
                }
                const bool fullscreen = min_x <= 0.5f
                    && max_x >= static_cast<float>(options_.width) - 0.5f
                    && min_y <= 0.5f
                    && max_y >= static_cast<float>(options_.height) - 0.5f;
                if (fullscreen) {
                    ++fullscreen_draw_count;
                    if (draw_textured) {
                        ++textured_fullscreen_draw_count;
                    }
                }
            }
        }
        if (!draw_textured) {
            continue;
        }
        ++textured_presented_draw_count;
        if (std::find(
                presented_texture_addresses.begin(),
                presented_texture_addresses.end(),
                draw.texture_address) == presented_texture_addresses.end()) {
            presented_texture_addresses.push_back(draw.texture_address);
        }
        const bool texture_matched = std::any_of(
            host_textures_.begin() + 1,
            host_textures_.end(),
            [&](const HostTexture& texture) {
                return host_texture_matches_draw(texture, draw);
            });
        if (!texture_matched) {
            ++unmatched_presented_texture_draw_count;
        }
    }
    std::ostringstream presented_texture_addresses_json;
    presented_texture_addresses_json << '[';
    for (size_t index = 0; index < presented_texture_addresses.size(); ++index) {
        if (index != 0u) {
            presented_texture_addresses_json << ',';
        }
        presented_texture_addresses_json << presented_texture_addresses[index];
    }
    presented_texture_addresses_json << ']';
    log_.emit(
        "nv2a_presented_geometry_anomalies",
        {
            {"reload", std::to_string(options_.live_render_stream ? live_render_reload_count_ + 1u : 0u)},
            {"source_commands", std::to_string(
                options_.live_render_stream
                    ? interpreted_source_command_count_
                    : recovered_source_.commands.size())},
            {"guest_flips", std::to_string(interpreted_stream_.flip_count)},
            {"manifest_guest_flip_count", std::to_string(current_manifest_guest_flip_count_)},
            {"manifest_guest_steps", std::to_string(current_manifest_guest_steps_)},
            {"presented_draw_count", std::to_string(diagnostic_end_draw - diagnostic_first_draw)},
            {"offscreen_render_target_draw_count", std::to_string(offscreen_render_target_draw_count)},
            {"presented_vertex_count", std::to_string(presented_vertex_count)},
            {"fullscreen_draw_count", std::to_string(fullscreen_draw_count)},
            {"textured_fullscreen_draw_count", std::to_string(textured_fullscreen_draw_count)},
            {"untextured_presented_draw_count", std::to_string(untextured_presented_draw_count)},
            {"alpha_only_presented_draw_count", std::to_string(alpha_only_presented_draw_count)},
            {"zero_alpha_presented_draw_count", std::to_string(zero_alpha_presented_draw_count)},
            {"presented_texture_addresses", presented_texture_addresses_json.str()},
            {"valid_geometry_draw_count", std::to_string(valid_geometry_draw_count)},
            {"gpu_raw_attribute_draw_count", std::to_string(
                gpu_raw_attribute_draw_count)},
            {"invalid_geometry_range_count", std::to_string(invalid_geometry_range_count)},
            {"filtered_fixed_function_draw_count", std::to_string(filtered_fixed_function_draw_count)},
            {"filtered_fixed_function_indexed_draw_count", std::to_string(filtered_fixed_function_indexed_draw_count)},
            {"non_finite_position_draw_count", std::to_string(non_finite_position_draw_count)},
            {"collapsed_x_draw_count", std::to_string(collapsed_x_draw_count)},
            {"collapsed_y_draw_count", std::to_string(collapsed_y_draw_count)},
            {"zero_area_draw_count", std::to_string(zero_area_draw_count)},
            {"exact_center_origin_draw_count", std::to_string(exact_center_origin_draw_count)},
            {"fully_offscreen_draw_count", std::to_string(fully_offscreen_draw_count)},
            {"outside_viewport_draw_count", std::to_string(outside_viewport_draw_count)},
            {"first_zero_area_presented_index", std::to_string(first_zero_area_presented_index)},
            {"first_center_origin_presented_index", std::to_string(first_center_origin_presented_index)},
            {"overall_min_x", overall_bounds_valid ? json_float(overall_min_x) : "null"},
            {"overall_max_x", overall_bounds_valid ? json_float(overall_max_x) : "null"},
            {"overall_min_y", overall_bounds_valid ? json_float(overall_min_y) : "null"},
            {"overall_max_y", overall_bounds_valid ? json_float(overall_max_y) : "null"},
            {"anomalous", json_bool(
                invalid_geometry_range_count != 0u
                || non_finite_position_draw_count != 0u
                || zero_area_draw_count != 0u
                || exact_center_origin_draw_count != 0u
                || filtered_fixed_function_draw_count != 0u)},
        });
    const size_t vertex_buffer_resource_count = std::count_if(
        recovered_source_.textures.begin(),
        recovered_source_.textures.end(),
        [](const RecoveredTextureResource& resource) {
            return resource.format == "VERTEX_BUFFER";
        });
    log_.emit(
        "render_validation",
        {
            {"strict", json_bool(options_.strict_render_validation)},
            {"manifest_guest_flip_count", std::to_string(current_manifest_guest_flip_count_)},
            {"manifest_guest_steps", std::to_string(current_manifest_guest_steps_)},
            {"texture_resource_count", std::to_string(
                recovered_source_.textures.size()
                    - vertex_buffer_resource_count)},
            {"vertex_buffer_resource_count", std::to_string(
                vertex_buffer_resource_count)},
            {"offscreen_render_target_draw_count", std::to_string(offscreen_render_target_draw_count)},
            {"unsupported_texture_resource_count", std::to_string(unsupported_texture_resource_count_)},
            {"textured_presented_draw_count", std::to_string(textured_presented_draw_count)},
            {"unmatched_presented_texture_draw_count", std::to_string(unmatched_presented_texture_draw_count)},
            {"unsupported_presented_primitive_count", std::to_string(unsupported_presented_primitive_count)},
            {"missing_presented_indexed_resource_draw_count", std::to_string(missing_presented_indexed_resource_draw_count)},
            {"unsupported_draw_arrays_count", std::to_string(interpreted_stream_.unsupported_draw_arrays_count)},
            {"passed", json_bool(
                unsupported_texture_resource_count_ == 0u
                && unmatched_presented_texture_draw_count == 0u
                && unsupported_presented_primitive_count == 0u
                && missing_presented_indexed_resource_draw_count == 0u
                && interpreted_stream_.unsupported_draw_arrays_count == 0u)},
        });
    if (options_.strict_render_validation
        && (unsupported_texture_resource_count_ != 0u
            || unmatched_presented_texture_draw_count != 0u
            || unsupported_presented_primitive_count != 0u
            || missing_presented_indexed_resource_draw_count != 0u
            || interpreted_stream_.unsupported_draw_arrays_count != 0u)) {
        std::ostringstream message;
        message << "strict render validation failed: "
                << unsupported_texture_resource_count_ << " unsupported texture resources, "
                << unmatched_presented_texture_draw_count << " unmatched textured presented draws, "
                << unsupported_presented_primitive_count << " unsupported presented primitives, "
                << missing_presented_indexed_resource_draw_count << " missing presented indexed resources, "
                << interpreted_stream_.unsupported_draw_arrays_count << " unsupported DRAW_ARRAYS methods";
        throw std::runtime_error(message.str());
    }
    if (emit_presented_details) {
    for (size_t draw_index = diagnostic_first_draw;
         draw_index < diagnostic_end_draw;
         ++draw_index) {
        const NativeDraw& draw = interpreted_stream_.draws[draw_index];
        if (draw.vertex_count == 0u ||
            draw.first_vertex + draw.vertex_count > interpreted_stream_.vertices.size()) {
            continue;
        }
        const bool targets_presented_surface =
            draw_targets_presented_surface(draw);
        const auto [surface_width, surface_height] =
            draw_surface_extent(draw);
        const NativeVertex& first = interpreted_stream_.vertices[draw.first_vertex];
        float min_x = first.x;
        float max_x = first.x;
        float min_y = first.y;
        float max_y = first.y;
        float min_u = first.u;
        float max_u = first.u;
        float min_v = first.v;
        float max_v = first.v;
        float min_r = first.r;
        float max_r = first.r;
        float min_g = first.g;
        float max_g = first.g;
        float min_b = first.b;
        float max_b = first.b;
        float min_a = first.a;
        float max_a = first.a;
        for (uint32_t vertex_index = 1; vertex_index < draw.vertex_count; ++vertex_index) {
            const NativeVertex& vertex =
                interpreted_stream_.vertices[draw.first_vertex + vertex_index];
            min_x = std::min(min_x, vertex.x);
            max_x = std::max(max_x, vertex.x);
            min_y = std::min(min_y, vertex.y);
            max_y = std::max(max_y, vertex.y);
            min_u = std::min(min_u, vertex.u);
            max_u = std::max(max_u, vertex.u);
            min_v = std::min(min_v, vertex.v);
            max_v = std::max(max_v, vertex.v);
            min_r = std::min(min_r, vertex.r);
            max_r = std::max(max_r, vertex.r);
            min_g = std::min(min_g, vertex.g);
            max_g = std::max(max_g, vertex.g);
            min_b = std::min(min_b, vertex.b);
            max_b = std::max(max_b, vertex.b);
            min_a = std::min(min_a, vertex.a);
            max_a = std::max(max_a, vertex.a);
        }
        const bool texture_matched = std::any_of(
            host_textures_.begin() + 1,
            host_textures_.end(),
            [&](const HostTexture& texture) {
                return host_texture_matches_draw(texture, draw);
            });
        log_.emit(
            "nv2a_presented_draw_details",
            {
                {"presented_index", std::to_string(draw_index - diagnostic_first_draw)},
                {"draw_index", std::to_string(draw_index)},
                {"targets_presented_surface", json_bool(targets_presented_surface)},
                {"surface_width", std::to_string(surface_width)},
                {"surface_height", std::to_string(surface_height)},
                {"primitive", std::to_string(draw.primitive)},
                {"first_vertex", std::to_string(draw.first_vertex)},
                {"vertex_count", std::to_string(draw.vertex_count)},
                {"indexed_array", json_bool(draw.indexed_array)},
                {"texture_address", std::to_string(draw.texture_address)},
                {"texture_enabled", json_bool(draw.texture_enabled)},
                {"texture_stage", std::to_string(draw.texture_stage)},
                {"texture_matched", json_bool(texture_matched)},
                {"texture_offset_0", std::to_string(draw.texture_offsets[0])},
                {"texture_offset_1", std::to_string(draw.texture_offsets[1])},
                {"texture_offset_2", std::to_string(draw.texture_offsets[2])},
                {"texture_offset_3", std::to_string(draw.texture_offsets[3])},
                {"texture_control_0", std::to_string(draw.texture_controls[0])},
                {"texture_control_1", std::to_string(draw.texture_controls[1])},
                {"texture_control_2", std::to_string(draw.texture_controls[2])},
                {"texture_control_3", std::to_string(draw.texture_controls[3])},
                {"texture_format_0", std::to_string(draw.texture_formats[0])},
                {"texture_format_1", std::to_string(draw.texture_formats[1])},
                {"texture_format_2", std::to_string(draw.texture_formats[2])},
                {"texture_format_3", std::to_string(draw.texture_formats[3])},
                {"texture_image_rect_0", std::to_string(draw.texture_image_rects[0])},
                {"texture_image_rect_1", std::to_string(draw.texture_image_rects[1])},
                {"texture_image_rect_2", std::to_string(draw.texture_image_rects[2])},
                {"texture_image_rect_3", std::to_string(draw.texture_image_rects[3])},
                {"blend_enable", std::to_string(draw.blend_enable)},
                {"blend_source_factor", std::to_string(draw.blend_source_factor)},
                {"blend_destination_factor", std::to_string(draw.blend_destination_factor)},
                {"blend_equation", std::to_string(draw.blend_equation)},
                {"surface_format", std::to_string(draw.surface_format)},
                {"surface_pitch", std::to_string(draw.surface_pitch)},
                {"surface_color_offset", std::to_string(draw.surface_color_offset)},
                {"surface_clip_horizontal", std::to_string(draw.surface_clip_horizontal)},
                {"surface_clip_vertical", std::to_string(draw.surface_clip_vertical)},
                {"color_mask", std::to_string(draw.color_mask)},
                {"depth_test_enable", std::to_string(draw.depth_test_enable)},
                {"depth_function", std::to_string(draw.depth_function)},
                {"depth_write_enable", std::to_string(draw.depth_write_enable)},
                {"cull_face_enable", std::to_string(draw.cull_face_enable)},
                {"cull_face", std::to_string(draw.cull_face)},
                {"front_face", std::to_string(draw.front_face)},
                {"shader_stage_program", std::to_string(draw.shader_stage_program)},
                {"combiner_control", std::to_string(draw.combiner_control)},
                {"combiner_color_input0", std::to_string(draw.combiner_color_inputs[0])},
                {"combiner_color_output0", std::to_string(draw.combiner_color_outputs[0])},
                {"combiner_alpha_input0", std::to_string(draw.combiner_alpha_inputs[0])},
                {"combiner_alpha_output0", std::to_string(draw.combiner_alpha_outputs[0])},
                {"combiner_factor0_0", std::to_string(draw.combiner_factors0[0])},
                {"combiner_factor1_0", std::to_string(draw.combiner_factors1[0])},
                {"final_combiner_inputs0", std::to_string(draw.final_combiner_inputs0)},
                {"final_combiner_inputs1", std::to_string(draw.final_combiner_inputs1)},
                {"final_combiner_factor0", std::to_string(draw.final_combiner_factors[0])},
                {"final_combiner_factor1", std::to_string(draw.final_combiner_factors[1])},
                {"fog_enable", std::to_string(draw.fog_enable)},
                {"fog_mode", std::to_string(draw.fog_mode)},
                {"fog_generation_mode", std::to_string(draw.fog_generation_mode)},
                {"fog_color", std::to_string(draw.fog_color)},
                {"vertex_format_0", std::to_string(draw.vertex_formats[0])},
                {"vertex_format_3", std::to_string(draw.vertex_formats[3])},
                {"vertex_format_9", std::to_string(draw.vertex_formats[9])},
                {"vertex_offset_0", std::to_string(draw.vertex_offsets[0])},
                {"vertex_offset_3", std::to_string(draw.vertex_offsets[3])},
                {"vertex_offset_9", std::to_string(draw.vertex_offsets[9])},
                {"transform_execution_mode", std::to_string(draw.transform_execution_mode)},
                {"transform_program_start", std::to_string(draw.transform_program_start)},
                {"transform_constant_0_x", std::to_string(draw.transform_constants[0][0])},
                {"viewport_scale_x", json_float(float_from_u32(draw.transform_constants[58][0]))},
                {"viewport_scale_y", json_float(float_from_u32(draw.transform_constants[58][1]))},
                {"viewport_offset_x", json_float(float_from_u32(draw.transform_constants[59][0]))},
                {"viewport_offset_y", json_float(float_from_u32(draw.transform_constants[59][1]))},
                {"transform_constant_96_x", std::to_string(draw.transform_constants[96][0])},
                {"min_x", json_float(min_x)},
                {"max_x", json_float(max_x)},
                {"min_y", json_float(min_y)},
                {"max_y", json_float(max_y)},
                {"min_u", json_float(min_u)},
                {"max_u", json_float(max_u)},
                {"min_v", json_float(min_v)},
                {"max_v", json_float(max_v)},
                {"min_r", json_float(min_r)},
                {"max_r", json_float(max_r)},
                {"min_g", json_float(min_g)},
                {"max_g", json_float(max_g)},
                {"min_b", json_float(min_b)},
                {"max_b", json_float(max_b)},
                {"min_a", json_float(min_a)},
                {"max_a", json_float(max_a)},
            });
        if ((draw.transform_execution_mode & 3u) == 2u) {
            for (uint32_t instruction = draw.transform_program_start;
                 instruction < draw.transform_program.size()
                     && instruction < draw.transform_program_start + 32u;
                 ++instruction) {
                const auto& token = draw.transform_program[instruction];
                log_.emit(
                    "nv2a_presented_transform_instruction",
                    {
                        {"presented_index", std::to_string(draw_index - diagnostic_first_draw)},
                        {"instruction", std::to_string(instruction)},
                        {"token_0", std::to_string(token[0])},
                        {"token_1", std::to_string(token[1])},
                        {"token_2", std::to_string(token[2])},
                        {"token_3", std::to_string(token[3])},
                        {"final", json_bool((token[3] & 1u) != 0u)},
                    });
                if ((token[3] & 1u) != 0u) {
                    break;
                }
            }
        }
        const uint32_t diagnostic_vertex_count = std::min<uint32_t>(
            draw.vertex_count,
            12u);
        for (uint32_t vertex_index = 0;
             vertex_index < diagnostic_vertex_count;
             ++vertex_index) {
            const NativeVertex& vertex =
                interpreted_stream_.vertices[draw.first_vertex + vertex_index];
            log_.emit(
                "nv2a_presented_vertex_details",
                {
                    {"presented_index", std::to_string(draw_index - diagnostic_first_draw)},
                    {"vertex_index", std::to_string(vertex_index)},
                    {"raw_x_bits", std::to_string(vertex.raw_x_bits)},
                    {"raw_y_bits", std::to_string(vertex.raw_y_bits)},
                    {"x", json_float(vertex.x)},
                    {"y", json_float(vertex.y)},
                    {"r", json_float(vertex.r)},
                    {"g", json_float(vertex.g)},
                    {"b", json_float(vertex.b)},
                    {"a", json_float(vertex.a)},
                    {"u", json_float(vertex.u)},
                    {"v", json_float(vertex.v)},
                });
        }
    }
    }
    if (interpreted_stream_.presented_draw_count != 0u) {
        const auto selected_begin = interpreted_stream_.draws.begin()
            + std::min<size_t>(
                interpreted_stream_.presented_draw_begin,
                interpreted_stream_.draws.size());
        const auto selected_end = interpreted_stream_.draws.begin()
            + std::min<size_t>(
                interpreted_stream_.presented_draw_begin
                    + interpreted_stream_.presented_draw_count,
                interpreted_stream_.draws.size());
        const auto selected = std::find_if(
            selected_begin,
            selected_end,
            [&](const NativeDraw& candidate) {
                return draw_targets_presented_surface(candidate)
                    && candidate.vertex_count != 0u
                    && candidate.first_vertex + candidate.vertex_count
                        <= interpreted_stream_.vertices.size();
            });
        if (selected != selected_end) {
            const NativeDraw& draw = *selected;
            const NativeVertex& vertex =
                interpreted_stream_.vertices[draw.first_vertex];
            const bool texture_matched = std::any_of(
                host_textures_.begin() + 1,
                host_textures_.end(),
                [&](const HostTexture& texture) {
                    return host_texture_matches_draw(texture, draw);
                });
            log_.emit(
                "nv2a_presented_draw_selected",
                {
                    {"texture_address", std::to_string(draw.texture_address)},
                    {"texture_matched", json_bool(texture_matched)},
                    {"first_vertex", std::to_string(draw.first_vertex)},
                    {"color_r", std::to_string(vertex.r)},
                    {"color_a", std::to_string(vertex.a)},
                    {"u", std::to_string(vertex.u)},
                    {"v", std::to_string(vertex.v)},
                });
        }
    }
    last_render_validation_us_ =
        std::chrono::duration_cast<std::chrono::microseconds>(
            std::chrono::steady_clock::now()
                - render_validation_begin).count();
}

VkDescriptorSet VulkanPresenter::descriptor_for_draw(const NativeDraw& draw) const {
    const size_t texture_index = host_texture_index_for_draw(draw);
    const uint32_t stage = std::min<uint32_t>(draw.texture_stage, 3u);
    const uint32_t address = draw.texture_enabled
            && draw.texture_stage < draw.texture_addresses.size()
        ? draw.texture_addresses[draw.texture_stage]
        : 0u;
    const auto match = std::find_if(
        host_texture_bindings_.begin(),
        host_texture_bindings_.end(),
        [&](const HostTextureBinding& binding) {
            return binding.texture_index == texture_index
                && binding.address == address
                && binding.format == draw.texture_formats[stage]
                && binding.control == draw.texture_controls[stage]
                && binding.filter == draw.texture_filters[stage];
        });
    if (match != host_texture_bindings_.end()) {
        return match->descriptor_set;
    }
    return host_texture_bindings_.front().descriptor_set;
}

const HostTexture* VulkanPresenter::presented_render_target_feedback_texture() const {
    const uint32_t selected_address = presented_surface_color_offset_;
    const size_t first_draw = std::min<size_t>(
        interpreted_stream_.presented_draw_begin,
        interpreted_stream_.draws.size());
    const size_t end_draw = std::min<size_t>(
        first_draw + interpreted_stream_.presented_draw_count,
        interpreted_stream_.draws.size());
    for (size_t draw_index = first_draw; draw_index < end_draw; ++draw_index) {
        const uint32_t address =
            interpreted_stream_.draws[draw_index].surface_color_offset;
        if (selected_address != 0u && address != selected_address) {
            continue;
        }
        const auto match = std::find_if(
            host_textures_.begin(),
            host_textures_.end(),
            [&](const HostTexture& texture) {
                return texture.render_target_feedback
                    && texture.guest_address == address
                    && texture.width == swapchain_extent_.width
                    && texture.height == swapchain_extent_.height
                    && texture.image_format == swapchain_format_;
            });
        if (match != host_textures_.end()) {
            return &*match;
        }
    }
    return nullptr;
}

bool VulkanPresenter::record_render_target_feedback(
    VkCommandBuffer command_buffer,
    VkImage swapchain_image) const {
    const HostTexture* target =
        presented_render_target_feedback_texture();
    if (target == nullptr) {
        return false;
    }
    VkImageMemoryBarrier to_transfer{};
    to_transfer.sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER;
    to_transfer.srcAccessMask = VK_ACCESS_SHADER_READ_BIT;
    to_transfer.dstAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
    to_transfer.oldLayout = VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL;
    to_transfer.newLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;
    to_transfer.srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
    to_transfer.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
    to_transfer.image = target->image;
    to_transfer.subresourceRange.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
    to_transfer.subresourceRange.levelCount = 1;
    to_transfer.subresourceRange.layerCount = 1;
    ++recording_barrier_count_;
    vkCmdPipelineBarrier(
        command_buffer,
        VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT,
        VK_PIPELINE_STAGE_TRANSFER_BIT,
        0,
        0,
        nullptr,
        0,
        nullptr,
        1,
        &to_transfer);

    VkImageCopy copy{};
    copy.srcSubresource.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
    copy.srcSubresource.layerCount = 1;
    copy.dstSubresource.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
    copy.dstSubresource.layerCount = 1;
    copy.extent = {
        swapchain_extent_.width,
        swapchain_extent_.height,
        1u,
    };
    vkCmdCopyImage(
        command_buffer,
        swapchain_image,
        VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
        target->image,
        VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
        1,
        &copy);

    VkImageMemoryBarrier to_shader = to_transfer;
    to_shader.srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
    to_shader.dstAccessMask = VK_ACCESS_SHADER_READ_BIT;
    to_shader.oldLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;
    to_shader.newLayout = VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL;
    ++recording_barrier_count_;
    vkCmdPipelineBarrier(
        command_buffer,
        VK_PIPELINE_STAGE_TRANSFER_BIT,
        VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT,
        0,
        0,
        nullptr,
        0,
        nullptr,
        1,
        &to_shader);
    return true;
}

void VulkanPresenter::record_native_draws_for_target(
    VkCommandBuffer command_buffer,
    VkExtent2D target_extent,
    std::optional<uint32_t> producer_address) const {
    if (interpreted_stream_.draws.empty() || vertex_buffer_ == VK_NULL_HANDLE) {
        return;
    }
    const VkDeviceSize offset = 0;
    vkCmdBindVertexBuffers(command_buffer, 0, 1, &vertex_buffer_, &offset);
    const VkViewport viewport{
        0.0f,
        0.0f,
        static_cast<float>(target_extent.width),
        static_cast<float>(target_extent.height),
        0.0f,
        1.0f,
    };
    vkCmdSetViewport(command_buffer, 0, 1, &viewport);
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
        const bool targets_requested_surface = producer_address.has_value()
            ? !draw_targets_presented_surface(draw)
                && nv2a_canonical_resource_address(
                    draw.surface_color_offset)
                    == nv2a_canonical_resource_address(
                        *producer_address)
            : draw_targets_presented_surface(draw);
        if (!targets_requested_surface
            || !draw_has_supported_host_transform(draw)
            || (draw.primitive != 5u && draw.primitive != 6u)
            || draw.vertex_count == 0u) {
            continue;
        }
        const NativePipelineState state = pipeline_state_for_draw(draw);
        const auto pipeline = std::find_if(
            graphics_pipelines_.begin(), graphics_pipelines_.end(),
            [&](const HostPipeline& candidate) { return candidate.state == state; });
        if (pipeline == graphics_pipelines_.end()) continue;
        vkCmdBindPipeline(command_buffer, VK_PIPELINE_BIND_POINT_GRAPHICS, pipeline->pipeline);
        const VkRect2D scissor = draw_scissor(draw, target_extent);
        if (scissor.extent.width == 0u || scissor.extent.height == 0u) {
            continue;
        }
        vkCmdSetScissor(command_buffer, 0, 1, &scissor);
        const VkDescriptorSet descriptor = descriptor_for_draw(draw);
        vkCmdBindDescriptorSets(command_buffer, VK_PIPELINE_BIND_POINT_GRAPHICS, pipeline_layout_, 0, 1, &descriptor, 0, nullptr);
        const NativeFragmentPushConstants fragment_state{
            static_cast<uint32_t>(presented_index),
        };
        vkCmdPushConstants(
            command_buffer,
            pipeline_layout_,
            VK_SHADER_STAGE_VERTEX_BIT | VK_SHADER_STAGE_FRAGMENT_BIT,
            0u,
            sizeof(fragment_state),
            &fragment_state);
        const uint64_t triangle_count = draw.primitive == 5u
            ? draw.vertex_count / 3u
            : (draw.vertex_count >= 3u ? draw.vertex_count - 2u : 0u);
        if (draw.gpu_raw_attribute_fetch) {
            ++recording_draw_count_;
            recording_triangle_count_ += triangle_count;
            vkCmdDraw(
                command_buffer,
                draw.vertex_count,
                1,
                0,
                0);
            continue;
        }
        const uint64_t draw_first = draw.first_vertex;
        const uint64_t draw_end = draw_first + draw.vertex_count;
        const uint64_t upload_first = uploaded_vertex_base_;
        const uint64_t upload_end = upload_first + uploaded_vertex_count_;
        if (draw_first < upload_first || draw_end > upload_end) {
            continue;
        }
        ++recording_draw_count_;
        recording_triangle_count_ += triangle_count;
        vkCmdDraw(
            command_buffer,
            draw.vertex_count,
            1,
            draw.first_vertex - uploaded_vertex_base_,
            0);
    }
}

void VulkanPresenter::record_native_draws(VkCommandBuffer command_buffer) const {
    record_native_draws_for_target(
        command_buffer,
        swapchain_extent_,
        std::nullopt);
}

uint32_t VulkanPresenter::record_offscreen_render_targets(
    VkCommandBuffer command_buffer) const {
    uint32_t recorded = 0u;
    for (const OffscreenRenderTarget& target : offscreen_render_targets_) {
        const auto texture = std::find_if(
            host_textures_.begin(),
            host_textures_.end(),
            [&](const HostTexture& candidate) {
                return candidate.render_target_feedback
                    && nv2a_canonical_resource_address(
                        candidate.guest_address)
                        == nv2a_canonical_resource_address(
                            target.spec.address)
                    && candidate.width == target.spec.width
                    && candidate.height == target.spec.height;
            });
        if (texture == host_textures_.end()
            || target.framebuffer == VK_NULL_HANDLE) {
            continue;
        }
        const VkExtent2D extent{
            target.spec.width,
            target.spec.height,
        };
        std::array<VkClearValue, 2> clear_values{};
        clear_values[1].depthStencil = {1.0f, 0u};
        VkRenderPassBeginInfo render_pass_info{};
        render_pass_info.sType =
            VK_STRUCTURE_TYPE_RENDER_PASS_BEGIN_INFO;
        render_pass_info.renderPass = render_pass_;
        render_pass_info.framebuffer = target.framebuffer;
        render_pass_info.renderArea = {{0, 0}, extent};
        render_pass_info.clearValueCount = static_cast<uint32_t>(
            clear_values.size());
        render_pass_info.pClearValues = clear_values.data();
        vkCmdBeginRenderPass(
            command_buffer,
            &render_pass_info,
            VK_SUBPASS_CONTENTS_INLINE);
        record_native_draws_for_target(
            command_buffer,
            extent,
            target.spec.producer_address);
        vkCmdEndRenderPass(command_buffer);

        VkImageMemoryBarrier to_shader{};
        to_shader.sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER;
        to_shader.srcAccessMask = VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT;
        to_shader.dstAccessMask = VK_ACCESS_SHADER_READ_BIT;
        to_shader.oldLayout = VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL;
        to_shader.newLayout = VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL;
        to_shader.srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
        to_shader.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
        to_shader.image = texture->image;
        to_shader.subresourceRange.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
        to_shader.subresourceRange.levelCount = 1;
        to_shader.subresourceRange.layerCount = 1;
        ++recording_barrier_count_;
        vkCmdPipelineBarrier(
            command_buffer,
            VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT,
            VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT,
            0,
            0,
            nullptr,
            0,
            nullptr,
            1,
            &to_shader);
        ++recorded;
    }
    return recorded;
}

VulkanPresenter::FrontendTextRaster VulkanPresenter::rasterize_frontend_text(
    const std::string& text,
    float layout_scale) const {
    FrontendTextRaster raster{};
    if (text.empty()) {
        return raster;
    }
    const std::wstring wide_text = widen(text);
    const int32_t pixel_height = std::max<int32_t>(
        1, static_cast<int32_t>(std::lround(28.0f * layout_scale)));
    HDC dc = CreateCompatibleDC(nullptr);
    if (dc == nullptr) {
        throw std::runtime_error("CreateCompatibleDC(frontend text) failed");
    }
    HFONT font = CreateFontW(
        -pixel_height,
        0,
        0,
        0,
        FW_NORMAL,
        FALSE,
        FALSE,
        FALSE,
        DEFAULT_CHARSET,
        OUT_TT_PRECIS,
        CLIP_DEFAULT_PRECIS,
        ANTIALIASED_QUALITY,
        DEFAULT_PITCH | FF_DONTCARE,
        L"Impact");
    if (font == nullptr) {
        DeleteDC(dc);
        throw std::runtime_error("CreateFontW(frontend text) failed");
    }
    const HGDIOBJ previous_font = SelectObject(dc, font);
    SIZE extent{};
    TEXTMETRICW metrics{};
    if (!GetTextExtentPoint32W(
            dc,
            wide_text.data(),
            static_cast<int>(wide_text.size()),
            &extent)
        || !GetTextMetricsW(dc, &metrics)) {
        SelectObject(dc, previous_font);
        DeleteObject(font);
        DeleteDC(dc);
        throw std::runtime_error("GDI frontend text measurement failed");
    }
    const LONG extra_space = static_cast<LONG>(std::lround(
        2.0f * layout_scale));
    const LONG space_count = static_cast<LONG>(std::count(
        wide_text.begin(), wide_text.end(), L' '));
    raster.width = static_cast<uint32_t>(std::max<LONG>(
        extent.cx + extra_space * space_count + 4,
        1));
    raster.height = static_cast<uint32_t>(
        std::max<LONG>(std::max(extent.cy, metrics.tmHeight) + 4, 1));
    BITMAPINFO bitmap_info{};
    bitmap_info.bmiHeader.biSize = sizeof(BITMAPINFOHEADER);
    bitmap_info.bmiHeader.biWidth = static_cast<LONG>(raster.width);
    bitmap_info.bmiHeader.biHeight = -static_cast<LONG>(raster.height);
    bitmap_info.bmiHeader.biPlanes = 1;
    bitmap_info.bmiHeader.biBitCount = 32;
    bitmap_info.bmiHeader.biCompression = BI_RGB;
    void* dib_pixels = nullptr;
    HBITMAP bitmap = CreateDIBSection(
        dc,
        &bitmap_info,
        DIB_RGB_COLORS,
        &dib_pixels,
        nullptr,
        0);
    if (bitmap == nullptr || dib_pixels == nullptr) {
        SelectObject(dc, previous_font);
        DeleteObject(font);
        DeleteDC(dc);
        throw std::runtime_error("CreateDIBSection(frontend text) failed");
    }
    const HGDIOBJ previous_bitmap = SelectObject(dc, bitmap);
    PatBlt(dc, 0, 0, raster.width, raster.height, BLACKNESS);
    SetBkMode(dc, TRANSPARENT);
    SetTextColor(dc, RGB(255, 255, 255));
    SIZE space_extent{};
    GetTextExtentPoint32W(dc, L" ", 1, &space_extent);
    LONG cursor = 2;
    size_t text_cursor = 0u;
    while (text_cursor < wide_text.size()) {
        if (wide_text[text_cursor] == L' ') {
            cursor += space_extent.cx + extra_space;
            ++text_cursor;
            continue;
        }
        const size_t word_end = wide_text.find(L' ', text_cursor);
        const size_t word_length = (word_end == std::wstring::npos
            ? wide_text.size()
            : word_end) - text_cursor;
        SIZE word_extent{};
        if (!GetTextExtentPoint32W(
                dc,
                wide_text.data() + text_cursor,
                static_cast<int>(word_length),
                &word_extent)
            || !TextOutW(
                dc,
                cursor,
                2,
                wide_text.data() + text_cursor,
                static_cast<int>(word_length))) {
            SelectObject(dc, previous_bitmap);
            SelectObject(dc, previous_font);
            DeleteObject(bitmap);
            DeleteObject(font);
            DeleteDC(dc);
            throw std::runtime_error("TextOutW(frontend text) failed");
        }
        cursor += word_extent.cx;
        text_cursor += word_length;
    }
    GdiFlush();
    raster.alpha.resize(
        static_cast<size_t>(raster.width) * raster.height);
    const uint8_t* bgra = static_cast<const uint8_t*>(dib_pixels);
    for (size_t pixel = 0; pixel < raster.alpha.size(); ++pixel) {
        raster.alpha[pixel] = std::max({
            bgra[pixel * 4u],
            bgra[pixel * 4u + 1u],
            bgra[pixel * 4u + 2u],
        });
    }
    SelectObject(dc, previous_bitmap);
    SelectObject(dc, previous_font);
    DeleteObject(bitmap);
    DeleteObject(font);
    DeleteDC(dc);
    return raster;
}

uint32_t VulkanPresenter::append_frontend_text_vertices(
    std::vector<NativeVertex>& vertices,
    const std::string& text) {
    frontend_text_first_vertex_ = static_cast<uint32_t>(vertices.size());
    frontend_text_vertex_count_ = 0u;
    if (text.empty()) {
        return 0;
    }
    float text_size = float_from_u32(
        recovered_source_.frontend_text_size_bits);
    if (!std::isfinite(text_size) || text_size <= 0.0f) {
        text_size = 25.0f;
    }
    const float layout_scale = std::clamp(
        text_size / 25.0f, 0.25f, 4.0f);
    const FrontendTextRaster raster = rasterize_frontend_text(
        text, layout_scale);
    if (raster.alpha.empty()) {
        return 0;
    }
    float center_x = float_from_u32(recovered_source_.frontend_text_x_bits);
    float top_y = float_from_u32(recovered_source_.frontend_text_y_bits);
    if (!std::isfinite(center_x)) {
        center_x = static_cast<float>(swapchain_extent_.width) * 0.5f;
    }
    if (!std::isfinite(top_y)) {
        top_y = static_cast<float>(swapchain_extent_.height) * 0.5f;
    }
    const int32_t origin_x = static_cast<int32_t>(std::lround(
        center_x - 4.0f * layout_scale
            - static_cast<float>(raster.width) * 0.5f));
    const int32_t origin_y = static_cast<int32_t>(std::lround(
        top_y + 7.0f * layout_scale));
    const uint32_t color_argb =
        recovered_source_.frontend_text_color_argb;
    const float color_r = static_cast<float>(
        (color_argb >> 16u) & 0xFFu) / 255.0f;
    const float color_g = static_cast<float>(
        (color_argb >> 8u) & 0xFFu) / 255.0f;
    const float color_b = static_cast<float>(
        color_argb & 0xFFu) / 255.0f;
    const float color_a = static_cast<float>(
        (color_argb >> 24u) & 0xFFu) / 255.0f;
    uint32_t rectangle_count = 0u;
    auto append_rectangle = [&](int32_t left,
                                int32_t top,
                                int32_t right,
                                int32_t bottom,
                                float alpha) {
        left = std::clamp<int32_t>(
            left, 0, static_cast<int32_t>(swapchain_extent_.width));
        right = std::clamp<int32_t>(
            right, 0, static_cast<int32_t>(swapchain_extent_.width));
        top = std::clamp<int32_t>(
            top, 0, static_cast<int32_t>(swapchain_extent_.height));
        bottom = std::clamp<int32_t>(
            bottom, 0, static_cast<int32_t>(swapchain_extent_.height));
        if (left >= right || top >= bottom) {
            return;
        }
        const float x0 = static_cast<float>(left) * 2.0f
            / static_cast<float>(swapchain_extent_.width) - 1.0f;
        const float x1 = static_cast<float>(right) * 2.0f
            / static_cast<float>(swapchain_extent_.width) - 1.0f;
        const float y0 = static_cast<float>(top) * 2.0f
            / static_cast<float>(swapchain_extent_.height) - 1.0f;
        const float y1 = static_cast<float>(bottom) * 2.0f
            / static_cast<float>(swapchain_extent_.height) - 1.0f;
        const std::array<std::array<float, 2>, 6> positions{{
            {x0, y0}, {x1, y0}, {x0, y1},
            {x0, y1}, {x1, y0}, {x1, y1},
        }};
        for (const auto& position : positions) {
            NativeVertex vertex{};
            vertex.x = position[0];
            vertex.y = position[1];
            vertex.r = color_r;
            vertex.g = color_g;
            vertex.b = color_b;
            vertex.a = alpha * color_a;
            vertices.push_back(vertex);
        }
        ++rectangle_count;
    };
    for (uint32_t row = 0u; row < raster.height; ++row) {
        uint32_t column = 0u;
        while (column < raster.width) {
            const uint32_t coverage = raster.alpha[
                static_cast<size_t>(row) * raster.width + column];
            const uint32_t quantized = coverage < 16u
                ? 0u
                : std::min(255u, ((coverage + 15u) / 32u) * 32u);
            if (quantized == 0u) {
                ++column;
                continue;
            }
            const uint32_t run_begin = column++;
            while (column < raster.width) {
                const uint32_t next_coverage = raster.alpha[
                    static_cast<size_t>(row) * raster.width + column];
                const uint32_t next_quantized = next_coverage < 16u
                    ? 0u
                    : std::min(
                        255u,
                        ((next_coverage + 15u) / 32u) * 32u);
                if (next_quantized != quantized) {
                    break;
                }
                ++column;
            }
            append_rectangle(
                origin_x + static_cast<int32_t>(run_begin),
                origin_y + static_cast<int32_t>(row),
                origin_x + static_cast<int32_t>(column),
                origin_y + static_cast<int32_t>(row + 1u),
                static_cast<float>(quantized) / 255.0f);
        }
    }
    frontend_text_vertex_count_ = rectangle_count * 6u;
    return rectangle_count;
}

void VulkanPresenter::record_frontend_text(VkCommandBuffer command_buffer) const {
    if (frontend_text_vertex_count_ == 0u
        || frontend_text_fragment_state_index_
            == std::numeric_limits<uint32_t>::max()
        || vertex_buffer_ == VK_NULL_HANDLE
        || host_texture_bindings_.empty()) {
        return;
    }
    const NativePipelineState state = frontend_text_pipeline_state();
    const auto pipeline = std::find_if(
        graphics_pipelines_.begin(),
        graphics_pipelines_.end(),
        [&](const HostPipeline& candidate) {
            return candidate.state == state;
        });
    if (pipeline == graphics_pipelines_.end()) {
        return;
    }
    const VkDeviceSize offset = 0;
    vkCmdBindVertexBuffers(command_buffer, 0, 1, &vertex_buffer_, &offset);
    const VkViewport viewport{
        0.0f,
        0.0f,
        static_cast<float>(swapchain_extent_.width),
        static_cast<float>(swapchain_extent_.height),
        0.0f,
        1.0f,
    };
    vkCmdSetViewport(command_buffer, 0, 1, &viewport);
    const VkRect2D scissor{{0, 0}, swapchain_extent_};
    vkCmdSetScissor(command_buffer, 0, 1, &scissor);
    vkCmdBindPipeline(
        command_buffer,
        VK_PIPELINE_BIND_POINT_GRAPHICS,
        pipeline->pipeline);
    const VkDescriptorSet descriptor =
        host_texture_bindings_.front().descriptor_set;
    vkCmdBindDescriptorSets(
        command_buffer,
        VK_PIPELINE_BIND_POINT_GRAPHICS,
        pipeline_layout_,
        0,
        1,
        &descriptor,
        0,
        nullptr);
    const NativeFragmentPushConstants fragment_state{
        frontend_text_fragment_state_index_,
    };
    vkCmdPushConstants(
        command_buffer,
        pipeline_layout_,
        VK_SHADER_STAGE_VERTEX_BIT | VK_SHADER_STAGE_FRAGMENT_BIT,
        0u,
        sizeof(fragment_state),
        &fragment_state);
    ++recording_draw_count_;
    recording_triangle_count_ += frontend_text_vertex_count_ / 3u;
    vkCmdDraw(
        command_buffer,
        frontend_text_vertex_count_,
        1,
        frontend_text_first_vertex_,
        0);
}

void VulkanPresenter::create_command_buffers() {
    command_buffers_.resize(framebuffers_.size());
    const std::vector<RecoveredD3DCommand>& recovered_d3d_stream =
        recovered_source_.commands;
    const InterpretedD3DStream& interpreted_stream = interpreted_stream_;
    if (options_.live_render_stream) {
        if (counted_command_render_work_generation_
            != render_work_generation_) {
            if (last_command_cursor_reset_) {
                recovered_d3d_mmio_count_ = 0u;
            }
            recovered_d3d_mmio_count_ += last_live_command_mmio_count_;
            counted_command_render_work_generation_ =
                render_work_generation_;
        }
        counted_recovered_command_count_ = 0u;
        recovered_d3d_command_count_ = static_cast<uint32_t>(
            std::min<size_t>(
                interpreted_source_command_count_,
                std::numeric_limits<uint32_t>::max()));
    } else {
        if (counted_recovered_command_count_ > recovered_d3d_stream.size()) {
            counted_recovered_command_count_ = 0u;
            recovered_d3d_mmio_count_ = 0u;
        }
        for (size_t index = counted_recovered_command_count_;
             index < recovered_d3d_stream.size();
             ++index) {
            recovered_d3d_mmio_count_ += static_cast<uint32_t>(
                recovered_d3d_stream[index].kind
                    == RecoveredD3DCommandKind::MmioWrite);
        }
        counted_recovered_command_count_ = recovered_d3d_stream.size();
        recovered_d3d_command_count_ = static_cast<uint32_t>(
            recovered_d3d_stream.size());
    }
    recovered_d3d_push_buffer_count_ =
        recovered_d3d_command_count_ - recovered_d3d_mmio_count_;
    interpreted_push_buffer_word_count_ = interpreted_stream.push_buffer_word_count;
    interpreted_method_packet_count_ = interpreted_stream.method_packet_count;
    interpreted_method_count_ = interpreted_stream.interpreted_method_count;
    interpreted_zero_count_method_word_count_ = interpreted_stream.zero_count_method_word_count;
    translated_command_count_ =
        interpreted_stream.method_packet_count +
        interpreted_stream.zero_count_method_word_count +
        interpreted_stream.control_flow_packet_count +
        interpreted_stream.mmio_setup_write_count +
        interpreted_stream.submission_kick_count +
        interpreted_stream.unknown_packet_count;
    VkCommandBufferAllocateInfo allocate_info{};
    allocate_info.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO;
    allocate_info.commandPool = command_pool_;
    allocate_info.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY;
    allocate_info.commandBufferCount = static_cast<uint32_t>(command_buffers_.size());
    vk_check(vkAllocateCommandBuffers(device_, &allocate_info, command_buffers_.data()), "vkAllocateCommandBuffers");
    command_buffer_allocation_count_ += command_buffers_.size();
    command_buffer_draw_counts_.assign(command_buffers_.size(), 0u);
    command_buffer_triangle_counts_.assign(command_buffers_.size(), 0u);
    command_buffer_barrier_counts_.assign(command_buffers_.size(), 0u);

    const bool record_frame_readback = readback_enabled()
        && (options_.flip_audit_ack.empty()
            || !flip_audit_health_check_reason().empty());
    current_work_has_readback_ = record_frame_readback;
    uint32_t recorded_render_target_feedback_copy_count = 0;
    uint32_t recorded_offscreen_render_target_pass_count = 0;
    for (size_t index = 0; index < command_buffers_.size(); ++index) {
        recording_draw_count_ = 0u;
        recording_triangle_count_ = 0u;
        recording_barrier_count_ = 0u;
        VkCommandBufferBeginInfo begin_info{};
        begin_info.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO;
        vk_check(vkBeginCommandBuffer(command_buffers_[index], &begin_info), "vkBeginCommandBuffer");
        if (gpu_timing_query_pool_ != VK_NULL_HANDLE) {
            const uint32_t first_query = static_cast<uint32_t>(index) * 2u;
            vkCmdResetQueryPool(
                command_buffers_[index], gpu_timing_query_pool_, first_query, 2u);
            vkCmdWriteTimestamp(
                command_buffers_[index],
                VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT,
                gpu_timing_query_pool_,
                first_query);
        }

        recorded_offscreen_render_target_pass_count +=
            record_offscreen_render_targets(command_buffers_[index]);

        VkRenderPassBeginInfo render_pass_info{};
        render_pass_info.sType = VK_STRUCTURE_TYPE_RENDER_PASS_BEGIN_INFO;
        render_pass_info.renderPass = render_pass_;
        render_pass_info.framebuffer = framebuffers_[index];
        render_pass_info.renderArea.offset = {0, 0};
        render_pass_info.renderArea.extent = swapchain_extent_;
        std::array<VkClearValue, 2> clear_values{};
        clear_values[0] = interpreted_stream.diagnostic_clear_color;
        clear_values[1].depthStencil = {1.0f, 0u};
        render_pass_info.clearValueCount =
            static_cast<uint32_t>(clear_values.size());
        render_pass_info.pClearValues = clear_values.data();

        vkCmdBeginRenderPass(command_buffers_[index], &render_pass_info, VK_SUBPASS_CONTENTS_INLINE);
        record_native_draws(command_buffers_[index]);
        record_frontend_text(command_buffers_[index]);
        vkCmdEndRenderPass(command_buffers_[index]);
        recorded_render_target_feedback_copy_count +=
            record_render_target_feedback(
                command_buffers_[index],
                swapchain_images_[index])
            ? 1u
            : 0u;
        if (record_frame_readback) {
            VkImageMemoryBarrier readback_barrier{};
            readback_barrier.sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER;
            readback_barrier.srcAccessMask =
                VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT
                | VK_ACCESS_TRANSFER_READ_BIT;
            readback_barrier.dstAccessMask = VK_ACCESS_TRANSFER_READ_BIT;
            readback_barrier.oldLayout = VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL;
            readback_barrier.newLayout = VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL;
            readback_barrier.srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
            readback_barrier.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
            readback_barrier.image = swapchain_images_[index];
            readback_barrier.subresourceRange.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
            readback_barrier.subresourceRange.levelCount = 1;
            readback_barrier.subresourceRange.layerCount = 1;
            ++recording_barrier_count_;
            vkCmdPipelineBarrier(
                command_buffers_[index],
                VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT
                    | VK_PIPELINE_STAGE_TRANSFER_BIT,
                VK_PIPELINE_STAGE_TRANSFER_BIT,
                0,
                0,
                nullptr,
                0,
                nullptr,
                1,
                &readback_barrier);

            VkBufferImageCopy copy{};
            copy.imageSubresource.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
            copy.imageSubresource.mipLevel = 0;
            copy.imageSubresource.baseArrayLayer = 0;
            copy.imageSubresource.layerCount = 1;
            copy.imageExtent = {swapchain_extent_.width, swapchain_extent_.height, 1};
            vkCmdCopyImageToBuffer(
                command_buffers_[index],
                swapchain_images_[index],
                VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
                readback_buffer_,
                1,
                &copy);

            VkImageMemoryBarrier present_barrier{};
            present_barrier.sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER;
            present_barrier.srcAccessMask = VK_ACCESS_TRANSFER_READ_BIT;
            present_barrier.dstAccessMask = VK_ACCESS_MEMORY_READ_BIT;
            present_barrier.oldLayout = VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL;
            present_barrier.newLayout = VK_IMAGE_LAYOUT_PRESENT_SRC_KHR;
            present_barrier.srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
            present_barrier.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
            present_barrier.image = swapchain_images_[index];
            present_barrier.subresourceRange.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
            present_barrier.subresourceRange.levelCount = 1;
            present_barrier.subresourceRange.layerCount = 1;
            ++recording_barrier_count_;
            vkCmdPipelineBarrier(
                command_buffers_[index],
                VK_PIPELINE_STAGE_TRANSFER_BIT,
                VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT,
                0,
                0,
                nullptr,
                0,
                nullptr,
                1,
                &present_barrier);
        } else {
            VkImageMemoryBarrier present_barrier{};
            present_barrier.sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER;
            present_barrier.srcAccessMask =
                VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT
                | VK_ACCESS_TRANSFER_READ_BIT;
            present_barrier.dstAccessMask = VK_ACCESS_MEMORY_READ_BIT;
            present_barrier.oldLayout = VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL;
            present_barrier.newLayout = VK_IMAGE_LAYOUT_PRESENT_SRC_KHR;
            present_barrier.srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
            present_barrier.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
            present_barrier.image = swapchain_images_[index];
            present_barrier.subresourceRange.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
            present_barrier.subresourceRange.levelCount = 1;
            present_barrier.subresourceRange.layerCount = 1;
            ++recording_barrier_count_;
            vkCmdPipelineBarrier(
                command_buffers_[index],
                VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT
                    | VK_PIPELINE_STAGE_TRANSFER_BIT,
                VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT,
                0,
                0,
                nullptr,
                0,
                nullptr,
                1,
                &present_barrier);
        }
        if (gpu_timing_query_pool_ != VK_NULL_HANDLE) {
            vkCmdWriteTimestamp(
                command_buffers_[index],
                VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT,
                gpu_timing_query_pool_,
                static_cast<uint32_t>(index) * 2u + 1u);
        }
        vk_check(vkEndCommandBuffer(command_buffers_[index]), "vkEndCommandBuffer");
        command_buffer_draw_counts_[index] = recording_draw_count_;
        command_buffer_triangle_counts_[index] = recording_triangle_count_;
        command_buffer_barrier_counts_[index] = recording_barrier_count_;
    }
    recording_draw_count_ = 0u;
    recording_triangle_count_ = 0u;
    recording_barrier_count_ = 0u;
    log_.emit(
        "recovered_d3d_command_stream_loaded",
        {
            {"commands", std::to_string(recovered_d3d_stream.size())},
            {"mmio_writes", std::to_string(recovered_d3d_mmio_count_)},
            {"push_buffer_writes", std::to_string(recovered_d3d_push_buffer_count_)},
            {"source", json_string(recovered_source_.source)},
            {"interpreter_bootstrap", recovered_source_.interpreter_bootstrap_source.empty()
                ? "null"
                : json_string(recovered_source_.interpreter_bootstrap_source.string())},
            {"interpreter_bootstrap_applied", json_bool(
                !recovered_source_.interpreter_bootstrap_source.empty())},
        });
    log_.emit(
        "d3d8_stream_interpreted",
        {
            {"push_buffer_words", std::to_string(interpreted_stream.push_buffer_word_count)},
            {"method_packets", std::to_string(interpreted_stream.method_packet_count)},
            {"interpreted_methods", std::to_string(interpreted_stream.interpreted_method_count)},
            {"bulk_indexed_methods", std::to_string(
                interpreted_stream.bulk_indexed_method_count)},
            {"bulk_inline_methods", std::to_string(
                interpreted_stream.bulk_inline_method_count)},
            {"last_interpreted_methods", std::to_string(
                interpreted_stream.last_interpreted_method_count)},
            {"last_bulk_indexed_methods", std::to_string(
                interpreted_stream.last_bulk_indexed_method_count)},
            {"last_bulk_inline_methods", std::to_string(
                interpreted_stream.last_bulk_inline_method_count)},
            {"push_buffer_collect_us", std::to_string(
                interpreted_stream.last_push_buffer_collect_us)},
            {"method_apply_us", std::to_string(
                interpreted_stream.last_method_apply_us)},
            {"method_finalize_us", std::to_string(
                interpreted_stream.last_method_finalize_us)},
            {"state_seed_updates_required", json_bool(
                interpreted_stream.state_seed_updates_required)},
            {"zero_count_method_words", std::to_string(interpreted_stream.zero_count_method_word_count)},
            {"zero_count_indexed_array_noop_packets", std::to_string(interpreted_stream.zero_count_indexed_array_noop_packet_count)},
            {"control_flow_packets", std::to_string(interpreted_stream.control_flow_packet_count)},
            {"mmio_setup_writes", std::to_string(interpreted_stream.mmio_setup_write_count)},
            {"submission_kicks", std::to_string(interpreted_stream.submission_kick_count)},
            {"unknown_packets", std::to_string(interpreted_stream.unknown_packet_count)},
            {"truncated_packets", std::to_string(interpreted_stream.truncated_packet_count)},
            {"pending_method_packet", json_bool(interpreted_stream.pending_method_packet)},
            {"converted_quad_count", std::to_string(interpreted_stream.converted_quad_count)},
            {"discarded_quad_vertex_count", std::to_string(interpreted_stream.discarded_quad_vertex_count)},
            {"indexed_array_draws", std::to_string(interpreted_stream.indexed_array_draw_count)},
            {"indexed_array_elements", std::to_string(interpreted_stream.indexed_array_element_count)},
            {"materialized_indexed_draws", std::to_string(interpreted_stream.materialized_indexed_draw_count)},
            {"materialized_indexed_vertices", std::to_string(interpreted_stream.materialized_indexed_vertex_count)},
            {"missing_indexed_resource_draws", std::to_string(interpreted_stream.missing_indexed_resource_draw_count)},
            {"launch_transform_program_count", std::to_string(interpreted_stream.launch_transform_program_count)},
            {"failed_launch_transform_program_count", std::to_string(interpreted_stream.failed_launch_transform_program_count)},
            {"surface_payload_samples", std::to_string(interpreted_stream.surface_payload_sample_count)},
            {"surface_payload_dominant_count", std::to_string(interpreted_stream.surface_payload_dominant_count)},
            {"surface_payload_color_valid", interpreted_stream.surface_payload_color_valid ? "true" : "false"},
            {"surface_payload_argb", std::to_string(interpreted_stream.surface_payload_argb)},
            {"surface_payload_scans_skipped", std::to_string(
                interpreted_stream.surface_payload_scan_skipped_count)},
            {"ordered_push_buffer_appends", std::to_string(
                interpreted_stream.ordered_push_buffer_append_count)},
            {"indexed_word_push_buffer_appends", std::to_string(
                interpreted_stream.indexed_word_push_buffer_append_count)},
            {"reconstructed_push_buffer_appends", std::to_string(
                interpreted_stream.reconstructed_push_buffer_append_count)},
            {
                "diagnostic_clear_source",
                json_string(
                    interpreted_stream.clear_color_valid ? "d3d8_clear_color_method" :
                    interpreted_stream.surface_payload_color_valid ? "recovered_surface_payload" :
                    "state_seed")
            },
            {"state_seed", std::to_string(interpreted_stream.state_seed)},
            {"clear_color_valid", interpreted_stream.clear_color_valid ? "true" : "false"},
            {"clear_color_argb", std::to_string(interpreted_stream.clear_color_argb)},
            {"native_vertices", std::to_string(interpreted_stream.vertices.size())},
            {"native_draws", std::to_string(interpreted_stream.draws.size())},
            {"translation_semantics", json_string("d3d8-nv2a-method-push-buffer-interpretation")},
        });
    if (!options_.live_render_stream) {
        for (size_t index = 0; index < recovered_d3d_stream.size(); ++index) {
            const RecoveredD3DCommand& command = recovered_d3d_stream[index];
            log_.emit(
                "recovered_d3d_command",
                {
                    {"index", std::to_string(index)},
                    {"kind", json_string(recovered_d3d_command_kind_name(command.kind))},
                    {"address", std::to_string(command.address)},
                    {"value", std::to_string(command.value)},
                });
        }
    }
    log_.emit(
        "translated_render_work_recorded",
        {
            {"commands", std::to_string(translated_command_count_)},
            {"source_commands", std::to_string(recovered_d3d_command_count_)},
            {"method_packets", std::to_string(interpreted_stream.method_packet_count)},
            {"interpreted_methods", std::to_string(interpreted_stream.interpreted_method_count)},
            {"zero_count_method_words", std::to_string(interpreted_stream.zero_count_method_word_count)},
            {"zero_count_indexed_array_noop_packets", std::to_string(interpreted_stream.zero_count_indexed_array_noop_packet_count)},
            {"native_vertices", std::to_string(interpreted_stream.vertices.size())},
            {"native_draws", std::to_string(interpreted_stream.draws.size())},
            {"native_textures", std::to_string(host_textures_.size() - 1u)},
            {"translation_semantics", json_string("d3d8-nv2a-method-push-buffer-interpretation")},
        });
    if (!recovered_frontend_text_.empty() && frontend_text_rectangle_count_ != 0u) {
        log_.emit(
            "frontend_text_draw_recorded",
            {
                {"characters", std::to_string(recovered_frontend_text_.size())},
                {"rectangles", std::to_string(frontend_text_rectangle_count_)},
                {"text", json_string(recovered_frontend_text_)},
                {"x", json_float(float_from_u32(
                    recovered_source_.frontend_text_x_bits))},
                {"y", json_float(float_from_u32(
                    recovered_source_.frontend_text_y_bits))},
                {"size", json_float(float_from_u32(
                    recovered_source_.frontend_text_size_bits))},
                {"color_argb", std::to_string(
                    recovered_source_.frontend_text_color_argb)},
                {"opacity", json_float(static_cast<float>(
                    (recovered_source_.frontend_text_color_argb >> 24u)
                        & 0xFFu) / 255.0f)},
                {"font", json_string("Impact")},
                {"translation_semantics", json_string("recovered-title-text-hle")},
            });
    }
    log_.emit(
        "command_buffers_recorded",
        {
            {"count", std::to_string(command_buffers_.size())},
            {"translated_commands", std::to_string(translated_command_count_)},
            {"source_d3d_commands", std::to_string(recovered_d3d_stream.size())},
            {"render_target_feedback_copies", std::to_string(recorded_render_target_feedback_copy_count)},
            {"offscreen_render_target_passes", std::to_string(recorded_offscreen_render_target_pass_count)},
        });
}

void VulkanPresenter::create_sync_objects() {
    VkSemaphoreCreateInfo semaphore_info{};
    semaphore_info.sType = VK_STRUCTURE_TYPE_SEMAPHORE_CREATE_INFO;
    VkFenceCreateInfo fence_info{};
    fence_info.sType = VK_STRUCTURE_TYPE_FENCE_CREATE_INFO;
    fence_info.flags = VK_FENCE_CREATE_SIGNALED_BIT;
    vk_check(vkCreateSemaphore(device_, &semaphore_info, nullptr, &image_available_), "vkCreateSemaphore");
    vk_check(vkCreateSemaphore(device_, &semaphore_info, nullptr, &render_finished_), "vkCreateSemaphore");
    vk_check(vkCreateFence(device_, &fence_info, nullptr, &in_flight_), "vkCreateFence");
    log_.emit("sync_objects_created");
}

bool VulkanPresenter::readback_enabled() const {
    return !options_.screenshot.empty()
        || !options_.hotkey_screenshot_directory.empty()
        || !options_.flip_audit_frame_directory.empty();
}

std::filesystem::path VulkanPresenter::current_flip_audit_frame_path() const {
    std::wostringstream name;
    name << L"flip-" << std::setfill(L'0') << std::setw(6)
         << current_audit_flip_ << L".bmp";
    return options_.flip_audit_frame_directory / name.str();
}

uint64_t VulkanPresenter::wait_for_in_flight_fence(const char* operation) {
    const auto wait_begin = std::chrono::steady_clock::now();
    const VkResult result = vkWaitForFences(
        device_, 1, &in_flight_, VK_TRUE, UINT64_MAX);
    const uint64_t wait_us = static_cast<uint64_t>(
        std::chrono::duration_cast<std::chrono::microseconds>(
            std::chrono::steady_clock::now() - wait_begin).count());
    active_frame_fence_wait_us_ += wait_us;
    vk_check(result, operation);
    return wait_us;
}

void VulkanPresenter::update_gpu_frame_time() {
    if (gpu_timing_query_pool_ == VK_NULL_HANDLE
        || last_submitted_image_index_
            == std::numeric_limits<uint32_t>::max()) {
        return;
    }
    std::array<uint64_t, 2> timestamps{};
    const VkResult result = vkGetQueryPoolResults(
        device_,
        gpu_timing_query_pool_,
        last_submitted_image_index_ * 2u,
        2u,
        sizeof(timestamps),
        timestamps.data(),
        sizeof(uint64_t),
        VK_QUERY_RESULT_64_BIT);
    if (result != VK_SUCCESS) {
        return;
    }
    uint64_t tick_delta = timestamps[1] - timestamps[0];
    if (gpu_timestamp_valid_bits_ < 64u) {
        const uint64_t mask = (uint64_t{1} << gpu_timestamp_valid_bits_) - 1u;
        tick_delta = (timestamps[1] - timestamps[0]) & mask;
    }
    last_gpu_frame_ms_ = static_cast<double>(tick_delta)
        * static_cast<double>(gpu_timestamp_period_ns_) / 1'000'000.0;
    gpu_frame_time_valid_ = true;
}

void VulkanPresenter::draw_frame() {
    last_draw_fence_wait_us_ =
        wait_for_in_flight_fence("vkWaitForFences");
    const auto gpu_query_begin = std::chrono::steady_clock::now();
    update_gpu_frame_time();
    last_gpu_query_us_ = static_cast<uint64_t>(
        std::chrono::duration_cast<std::chrono::microseconds>(
            std::chrono::steady_clock::now() - gpu_query_begin).count());
    const auto fence_reset_begin = std::chrono::steady_clock::now();
    vk_check(vkResetFences(device_, 1, &in_flight_), "vkResetFences");
    last_fence_reset_us_ = static_cast<uint64_t>(
        std::chrono::duration_cast<std::chrono::microseconds>(
            std::chrono::steady_clock::now() - fence_reset_begin).count());

    uint32_t image_index = 0;
    const auto acquire_begin = std::chrono::steady_clock::now();
    VkResult acquire = vkAcquireNextImageKHR(
        device_, swapchain_, UINT64_MAX, image_available_, VK_NULL_HANDLE, &image_index);
    last_acquire_us_ = static_cast<uint64_t>(
        std::chrono::duration_cast<std::chrono::microseconds>(
            std::chrono::steady_clock::now() - acquire_begin).count());
    if (acquire == VK_ERROR_OUT_OF_DATE_KHR) {
        running_ = false;
        log_.emit("swapchain_out_of_date");
        return;
    }
    vk_check(acquire, "vkAcquireNextImageKHR");

    VkSemaphore wait_semaphores[] = {image_available_};
    VkPipelineStageFlags wait_stages[] = {VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT};
    VkSemaphore signal_semaphores[] = {render_finished_};

    VkSubmitInfo submit_info{};
    submit_info.sType = VK_STRUCTURE_TYPE_SUBMIT_INFO;
    submit_info.waitSemaphoreCount = 1;
    submit_info.pWaitSemaphores = wait_semaphores;
    submit_info.pWaitDstStageMask = wait_stages;
    submit_info.commandBufferCount = 1;
    submit_info.pCommandBuffers = &command_buffers_[image_index];
    submit_info.signalSemaphoreCount = 1;
    submit_info.pSignalSemaphores = signal_semaphores;
    const auto submit_begin = std::chrono::steady_clock::now();
    vk_check(vkQueueSubmit(graphics_queue_, 1, &submit_info, in_flight_), "vkQueueSubmit");
    last_submit_us_ = static_cast<uint64_t>(
        std::chrono::duration_cast<std::chrono::microseconds>(
            std::chrono::steady_clock::now() - submit_begin).count());
    ++queue_submission_count_;
    if (image_index < command_buffer_draw_counts_.size()) {
        draw_count_ += command_buffer_draw_counts_[image_index];
        triangle_count_ += command_buffer_triangle_counts_[image_index];
        barrier_count_ += command_buffer_barrier_counts_[image_index];
    }
    if (current_work_has_readback_) {
        readback_bytes_ += static_cast<uint64_t>(readback_size_);
    }
    last_submitted_image_index_ = image_index;

    const bool capture_automatic =
        !options_.screenshot.empty() && !screenshot_captured_;
    const bool capture_flip_audit =
        !options_.flip_audit_frame_directory.empty()
        && current_audit_flip_ > acknowledged_audit_flip_;
    std::filesystem::path flip_audit_frame_path;
    std::optional<FrameReadback> flip_audit_readback;
    const std::string audit_health_reason = capture_flip_audit
        ? flip_audit_health_check_reason() : std::string{};
    const bool analyze_flip_health = capture_flip_audit
        && !audit_health_reason.empty() && current_work_has_readback_;
    const bool capture_hotkey = hotkey_screenshot_pending_
        && current_work_has_readback_;
    if (capture_automatic || capture_hotkey || analyze_flip_health) {
        last_readback_wait_us_ =
            wait_for_in_flight_fence("vkWaitForFences(readback)");
        const auto readback_process_begin =
            std::chrono::steady_clock::now();
        if (capture_automatic) {
            capture_screenshot(options_.screenshot, "automatic");
            screenshot_captured_ = true;
        }
        if (capture_hotkey) {
            capture_screenshot(pending_hotkey_screenshot_path_, "f12");
            hotkey_screenshot_pending_ = false;
            pending_hotkey_screenshot_path_.clear();
            pending_hotkey_render_capture_manifest_.clear();
        }
        if (analyze_flip_health) {
            flip_audit_readback = read_frame();
            if (flip_audit_readback->visual_issue()) {
                flip_audit_frame_path = current_flip_audit_frame_path();
                write_screenshot(
                    flip_audit_frame_path,
                    *flip_audit_readback,
                    "lossless_flip_audit_issue");
            }
        }
        last_readback_process_us_ = static_cast<uint64_t>(
            std::chrono::duration_cast<std::chrono::microseconds>(
                std::chrono::steady_clock::now()
                - readback_process_begin).count());
    }

    VkPresentInfoKHR present_info{};
    present_info.sType = VK_STRUCTURE_TYPE_PRESENT_INFO_KHR;
    present_info.waitSemaphoreCount = 1;
    present_info.pWaitSemaphores = signal_semaphores;
    present_info.swapchainCount = 1;
    present_info.pSwapchains = &swapchain_;
    present_info.pImageIndices = &image_index;
    const auto present_begin = std::chrono::steady_clock::now();
    const VkResult present = vkQueuePresentKHR(graphics_queue_, &present_info);
    last_present_us_ = static_cast<uint64_t>(
        std::chrono::duration_cast<std::chrono::microseconds>(
            std::chrono::steady_clock::now() - present_begin).count());
    if (present == VK_ERROR_OUT_OF_DATE_KHR || present == VK_SUBOPTIMAL_KHR) {
        running_ = false;
        log_.emit("swapchain_present_suboptimal", {{"result", std::to_string(present)}});
        return;
    }
    vk_check(present, "vkQueuePresentKHR");
    if (capture_flip_audit) {
        const auto audit_ack_begin = std::chrono::steady_clock::now();
        acknowledge_current_flip_audit(
            flip_audit_readback ? &*flip_audit_readback : nullptr,
            flip_audit_frame_path,
            audit_health_reason);
        last_audit_ack_us_ = static_cast<uint64_t>(
            std::chrono::duration_cast<std::chrono::microseconds>(
                std::chrono::steady_clock::now()
                - audit_ack_begin).count());
    }
}

void VulkanPresenter::cleanup() {
    transport_.close();
    if (publication_event_) {
        CloseHandle(publication_event_);
        publication_event_ = nullptr;
    }
    if (presentation_ack_event_) {
        CloseHandle(presentation_ack_event_);
        presentation_ack_event_ = nullptr;
    }
    if (presentation_ack_file_ != INVALID_HANDLE_VALUE) {
        CloseHandle(presentation_ack_file_);
        presentation_ack_file_ = INVALID_HANDLE_VALUE;
    }
    if (frame_pacing_timer_) {
        CloseHandle(frame_pacing_timer_);
        frame_pacing_timer_ = nullptr;
    }
    if (device_) {
        persist_pipeline_cache();
        if (in_flight_) {
            vkDestroyFence(device_, in_flight_, nullptr);
        }
        if (render_finished_) {
            vkDestroySemaphore(device_, render_finished_, nullptr);
        }
        if (image_available_) {
            vkDestroySemaphore(device_, image_available_, nullptr);
        }
        if (command_pool_) {
            command_buffers_.clear();
            vkDestroyCommandPool(device_, command_pool_, nullptr);
        }
        if (gpu_timing_query_pool_) {
            vkDestroyQueryPool(device_, gpu_timing_query_pool_, nullptr);
            gpu_timing_query_pool_ = VK_NULL_HANDLE;
        }
        destroy_host_texture_bindings();
        if (vertex_mapped_) {
            vkUnmapMemory(device_, vertex_memory_);
            vertex_mapped_ = nullptr;
        }
        if (vertex_buffer_) {
            vkDestroyBuffer(device_, vertex_buffer_, nullptr);
        }
        if (vertex_memory_) {
            vkFreeMemory(device_, vertex_memory_, nullptr);
        }
        destroy_offscreen_render_targets();
        if (fragment_state_mapped_) {
            vkUnmapMemory(device_, fragment_state_memory_);
            fragment_state_mapped_ = nullptr;
        }
        if (fragment_state_buffer_) {
            vkDestroyBuffer(device_, fragment_state_buffer_, nullptr);
        }
        if (fragment_state_memory_) {
            vkFreeMemory(device_, fragment_state_memory_, nullptr);
        }
        if (vertex_program_state_mapped_) {
            vkUnmapMemory(device_, vertex_program_state_memory_);
            vertex_program_state_mapped_ = nullptr;
        }
        if (vertex_program_state_buffer_) {
            vkDestroyBuffer(
                device_, vertex_program_state_buffer_, nullptr);
        }
        if (vertex_program_state_memory_) {
            vkFreeMemory(
                device_, vertex_program_state_memory_, nullptr);
        }
        if (raw_vertex_resource_mapped_) {
            vkUnmapMemory(device_, raw_vertex_resource_memory_);
            raw_vertex_resource_mapped_ = nullptr;
        }
        if (raw_vertex_resource_buffer_) {
            vkDestroyBuffer(
                device_, raw_vertex_resource_buffer_, nullptr);
        }
        if (raw_vertex_resource_memory_) {
            vkFreeMemory(
                device_, raw_vertex_resource_memory_, nullptr);
        }
        for (const HostTexture& texture : host_textures_) {
            if (texture.view) vkDestroyImageView(device_, texture.view, nullptr);
            if (texture.image) vkDestroyImage(device_, texture.image, nullptr);
            if (texture.memory) vkFreeMemory(device_, texture.memory, nullptr);
        }
        for (HostTexture& texture :
             render_target_feedback_image_cache_) {
            destroy_host_texture(texture);
        }
        render_target_feedback_image_cache_.clear();
        for (const HostPipeline& host_pipeline : graphics_pipelines_) {
            if (host_pipeline.pipeline) vkDestroyPipeline(device_, host_pipeline.pipeline, nullptr);
        }
        graphics_pipelines_.clear();
        if (texture_convert_pipeline_) {
            vkDestroyPipeline(
                device_, texture_convert_pipeline_, nullptr);
        }
        if (texture_convert_pipeline_layout_) {
            vkDestroyPipelineLayout(
                device_, texture_convert_pipeline_layout_, nullptr);
        }
        if (texture_convert_descriptor_layout_) {
            vkDestroyDescriptorSetLayout(
                device_, texture_convert_descriptor_layout_, nullptr);
        }
        if (pipeline_layout_) {
            vkDestroyPipelineLayout(device_, pipeline_layout_, nullptr);
        }
        if (texture_descriptor_layout_) {
            vkDestroyDescriptorSetLayout(device_, texture_descriptor_layout_, nullptr);
        }
        if (readback_buffer_) {
            vkDestroyBuffer(device_, readback_buffer_, nullptr);
        }
        if (readback_memory_) {
            vkFreeMemory(device_, readback_memory_, nullptr);
        }
        for (VkFramebuffer framebuffer : framebuffers_) {
            vkDestroyFramebuffer(device_, framebuffer, nullptr);
        }
        if (depth_image_view_) {
            vkDestroyImageView(device_, depth_image_view_, nullptr);
        }
        if (depth_image_) {
            vkDestroyImage(device_, depth_image_, nullptr);
        }
        if (depth_memory_) {
            vkFreeMemory(device_, depth_memory_, nullptr);
        }
        if (render_pass_) {
            vkDestroyRenderPass(device_, render_pass_, nullptr);
        }
        for (VkImageView image_view : swapchain_image_views_) {
            vkDestroyImageView(device_, image_view, nullptr);
        }
        if (swapchain_) {
            vkDestroySwapchainKHR(device_, swapchain_, nullptr);
        }
        if (pipeline_cache_) {
            vkDestroyPipelineCache(device_, pipeline_cache_, nullptr);
            pipeline_cache_ = VK_NULL_HANDLE;
        }
        vkDestroyDevice(device_, nullptr);
        device_ = VK_NULL_HANDLE;
    }
    if (surface_) {
        vkDestroySurfaceKHR(instance_, surface_, nullptr);
        surface_ = VK_NULL_HANDLE;
    }
    if (instance_) {
        vkDestroyInstance(instance_, nullptr);
        instance_ = VK_NULL_HANDLE;
    }
    platform_.shutdown();
}

}  // namespace b2r::host::vulkan_detail
