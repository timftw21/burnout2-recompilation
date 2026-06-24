#define NOMINMAX
#define WIN32_LEAN_AND_MEAN
#define VK_USE_PLATFORM_WIN32_KHR

#include <windows.h>
#include <shellapi.h>
#include <vulkan/vulkan.h>

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <utility>
#include <vector>

namespace {

constexpr uint32_t kDefaultWidth = 640;
constexpr uint32_t kDefaultHeight = 480;
constexpr uint32_t kDefaultFrames = 120;

std::string narrow(const std::wstring& value) {
    if (value.empty()) {
        return {};
    }
    const int size = WideCharToMultiByte(
        CP_UTF8, 0, value.data(), static_cast<int>(value.size()), nullptr, 0, nullptr, nullptr);
    if (size <= 0) {
        return {};
    }
    std::string result(static_cast<size_t>(size), '\0');
    WideCharToMultiByte(
        CP_UTF8, 0, value.data(), static_cast<int>(value.size()), result.data(), size, nullptr, nullptr);
    return result;
}

std::wstring widen(const std::string& value) {
    if (value.empty()) {
        return {};
    }
    const int size = MultiByteToWideChar(CP_UTF8, 0, value.data(), static_cast<int>(value.size()), nullptr, 0);
    if (size <= 0) {
        return {};
    }
    std::wstring result(static_cast<size_t>(size), L'\0');
    MultiByteToWideChar(CP_UTF8, 0, value.data(), static_cast<int>(value.size()), result.data(), size);
    return result;
}

std::string escape_json(const std::string& value) {
    std::ostringstream out;
    for (const unsigned char ch : value) {
        switch (ch) {
            case '\\':
                out << "\\\\";
                break;
            case '"':
                out << "\\\"";
                break;
            case '\b':
                out << "\\b";
                break;
            case '\f':
                out << "\\f";
                break;
            case '\n':
                out << "\\n";
                break;
            case '\r':
                out << "\\r";
                break;
            case '\t':
                out << "\\t";
                break;
            default:
                if (ch < 0x20) {
                    out << "\\u" << std::hex << std::setw(4) << std::setfill('0')
                        << static_cast<int>(ch);
                } else {
                    out << ch;
                }
                break;
        }
    }
    return out.str();
}

std::string json_string(const std::string& value) {
    return "\"" + escape_json(value) + "\"";
}

std::string json_bool(bool value) {
    return value ? "true" : "false";
}

void vk_check(VkResult result, const char* operation) {
    if (result != VK_SUCCESS) {
        std::ostringstream message;
        message << operation << " failed with VkResult " << result;
        throw std::runtime_error(message.str());
    }
}

struct Options {
    uint32_t width = kDefaultWidth;
    uint32_t height = kDefaultHeight;
    uint32_t max_frames = kDefaultFrames;
    bool inject_input = false;
    bool list_adapters_only = false;
    std::wstring title = L"B2 Recomp - First Interactive Frame";
    std::filesystem::path debug_json;
};

struct QueueFamilySelection {
    uint32_t index = 0;
};

struct SwapchainSupport {
    VkSurfaceCapabilitiesKHR capabilities{};
    std::vector<VkSurfaceFormatKHR> formats;
    std::vector<VkPresentModeKHR> present_modes;
};

class DebugLog {
public:
    void open(const std::filesystem::path& path) {
        if (path.empty()) {
            return;
        }
        if (path.has_parent_path()) {
            std::filesystem::create_directories(path.parent_path());
        }
        file_.open(path, std::ios::out | std::ios::trunc);
        if (!file_) {
            throw std::runtime_error("failed to open debug json log: " + path.string());
        }
        path_ = path;
    }

    void emit(
        const std::string& event,
        std::initializer_list<std::pair<std::string, std::string>> fields = {}) {
        std::ostringstream line;
        line << "{\"sequence\":" << sequence_++ << ",\"event\":" << json_string(event);
        for (const auto& [key, value] : fields) {
            line << "," << json_string(key) << ":" << value;
        }
        line << "}";
        if (file_) {
            file_ << line.str() << "\n";
            file_.flush();
        }
        std::cout << line.str() << "\n";
    }

    const std::filesystem::path& path() const {
        return path_;
    }

private:
    uint64_t sequence_ = 0;
    std::ofstream file_;
    std::filesystem::path path_;
};

class VulkanFirstFrameApp {
public:
    explicit VulkanFirstFrameApp(Options options) : options_(std::move(options)) {}

    int run() {
        log_.open(options_.debug_json);
        log_.emit(
            "startup",
            {
                {"target_platform", json_string("windows")},
                {"renderer_backend", json_string("vulkan")},
                {"width", std::to_string(options_.width)},
                {"height", std::to_string(options_.height)},
                {"max_frames", std::to_string(options_.max_frames)},
            });

        create_instance();
        if (options_.list_adapters_only) {
            list_adapters();
            cleanup();
            return 0;
        }
        create_window();
        create_surface();
        pick_physical_device();
        create_logical_device();
        create_swapchain();
        create_image_views();
        create_render_pass();
        create_framebuffers();
        create_command_pool();
        create_command_buffers();
        create_sync_objects();
        main_loop();
        vkDeviceWaitIdle(device_);
        cleanup();
        log_.emit("shutdown", {{"status", json_string("ok")}});
        return 0;
    }

private:
    void create_instance() {
        VkApplicationInfo app_info{};
        app_info.sType = VK_STRUCTURE_TYPE_APPLICATION_INFO;
        app_info.pApplicationName = "b2_recomp_first_frame";
        app_info.applicationVersion = VK_MAKE_VERSION(0, 6, 0);
        app_info.pEngineName = "b2_recomp";
        app_info.engineVersion = VK_MAKE_VERSION(0, 6, 0);
        app_info.apiVersion = VK_API_VERSION_1_1;

        const std::vector<const char*> extensions = {
            VK_KHR_SURFACE_EXTENSION_NAME,
            VK_KHR_WIN32_SURFACE_EXTENSION_NAME,
        };

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
                {"surface_extension", json_string(VK_KHR_WIN32_SURFACE_EXTENSION_NAME)},
            });
    }

    void list_adapters() {
        uint32_t device_count = 0;
        vk_check(vkEnumeratePhysicalDevices(instance_, &device_count, nullptr), "vkEnumeratePhysicalDevices");
        std::vector<VkPhysicalDevice> devices(device_count);
        if (device_count > 0) {
            vk_check(
                vkEnumeratePhysicalDevices(instance_, &device_count, devices.data()),
                "vkEnumeratePhysicalDevices");
        }
        for (uint32_t index = 0; index < device_count; ++index) {
            VkPhysicalDeviceProperties properties{};
            vkGetPhysicalDeviceProperties(devices[index], &properties);
            log_.emit(
                "vulkan_adapter",
                {
                    {"index", std::to_string(index)},
                    {"name", json_string(properties.deviceName)},
                    {"vendor_id", std::to_string(properties.vendorID)},
                    {"device_id", std::to_string(properties.deviceID)},
                });
        }
    }

    void create_window() {
        hinstance_ = GetModuleHandleW(nullptr);
        const wchar_t* class_name = L"B2RecompFirstFrameWindow";

        WNDCLASSEXW wc{};
        wc.cbSize = sizeof(wc);
        wc.lpfnWndProc = &VulkanFirstFrameApp::window_proc;
        wc.hInstance = hinstance_;
        wc.hCursor = LoadCursor(nullptr, IDC_ARROW);
        wc.lpszClassName = class_name;
        RegisterClassExW(&wc);

        RECT rect{0, 0, static_cast<LONG>(options_.width), static_cast<LONG>(options_.height)};
        AdjustWindowRect(&rect, WS_OVERLAPPEDWINDOW, FALSE);
        hwnd_ = CreateWindowExW(
            0,
            class_name,
            options_.title.c_str(),
            WS_OVERLAPPEDWINDOW,
            CW_USEDEFAULT,
            CW_USEDEFAULT,
            rect.right - rect.left,
            rect.bottom - rect.top,
            nullptr,
            nullptr,
            hinstance_,
            this);
        if (!hwnd_) {
            throw std::runtime_error("CreateWindowExW failed");
        }
        ShowWindow(hwnd_, SW_SHOWNORMAL);
        UpdateWindow(hwnd_);
        log_.emit(
            "window_created",
            {
                {"title", json_string(narrow(options_.title))},
                {"width", std::to_string(options_.width)},
                {"height", std::to_string(options_.height)},
            });
    }

    void create_surface() {
        VkWin32SurfaceCreateInfoKHR create_info{};
        create_info.sType = VK_STRUCTURE_TYPE_WIN32_SURFACE_CREATE_INFO_KHR;
        create_info.hinstance = hinstance_;
        create_info.hwnd = hwnd_;
        vk_check(vkCreateWin32SurfaceKHR(instance_, &create_info, nullptr, &surface_), "vkCreateWin32SurfaceKHR");
        log_.emit("vulkan_surface_created", {{"platform", json_string("win32")}});
    }

    void pick_physical_device() {
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

    std::optional<QueueFamilySelection> find_queue_family(VkPhysicalDevice device) const {
        uint32_t count = 0;
        vkGetPhysicalDeviceQueueFamilyProperties(device, &count, nullptr);
        std::vector<VkQueueFamilyProperties> families(count);
        vkGetPhysicalDeviceQueueFamilyProperties(device, &count, families.data());
        for (uint32_t index = 0; index < count; ++index) {
            VkBool32 present_supported = VK_FALSE;
            vkGetPhysicalDeviceSurfaceSupportKHR(device, index, surface_, &present_supported);
            if ((families[index].queueFlags & VK_QUEUE_GRAPHICS_BIT) && present_supported == VK_TRUE) {
                return QueueFamilySelection{index};
            }
        }
        return std::nullopt;
    }

    bool device_supports_swapchain(VkPhysicalDevice device) const {
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

    SwapchainSupport query_swapchain_support(VkPhysicalDevice device) const {
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

    void create_logical_device() {
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
        log_.emit("logical_device_created", {{"queue_family", std::to_string(queue_family_.index)}});
    }

    void create_swapchain() {
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
        create_info.imageUsage = VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT;
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
            });
    }

    VkSurfaceFormatKHR choose_surface_format(const std::vector<VkSurfaceFormatKHR>& formats) const {
        const auto preferred = std::find_if(
            formats.begin(),
            formats.end(),
            [](const VkSurfaceFormatKHR& format) {
                return format.format == VK_FORMAT_B8G8R8A8_SRGB
                    && format.colorSpace == VK_COLOR_SPACE_SRGB_NONLINEAR_KHR;
            });
        if (preferred != formats.end()) {
            return *preferred;
        }
        return formats.front();
    }

    VkExtent2D choose_extent(const VkSurfaceCapabilitiesKHR& capabilities) const {
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

    void create_image_views() {
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

    void create_render_pass() {
        VkAttachmentDescription color_attachment{};
        color_attachment.format = swapchain_format_;
        color_attachment.samples = VK_SAMPLE_COUNT_1_BIT;
        color_attachment.loadOp = VK_ATTACHMENT_LOAD_OP_CLEAR;
        color_attachment.storeOp = VK_ATTACHMENT_STORE_OP_STORE;
        color_attachment.stencilLoadOp = VK_ATTACHMENT_LOAD_OP_DONT_CARE;
        color_attachment.stencilStoreOp = VK_ATTACHMENT_STORE_OP_DONT_CARE;
        color_attachment.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
        color_attachment.finalLayout = VK_IMAGE_LAYOUT_PRESENT_SRC_KHR;

        VkAttachmentReference color_ref{};
        color_ref.attachment = 0;
        color_ref.layout = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;

        VkSubpassDescription subpass{};
        subpass.pipelineBindPoint = VK_PIPELINE_BIND_POINT_GRAPHICS;
        subpass.colorAttachmentCount = 1;
        subpass.pColorAttachments = &color_ref;

        VkSubpassDependency dependency{};
        dependency.srcSubpass = VK_SUBPASS_EXTERNAL;
        dependency.dstSubpass = 0;
        dependency.srcStageMask = VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT;
        dependency.dstStageMask = VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT;
        dependency.dstAccessMask = VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT;

        VkRenderPassCreateInfo create_info{};
        create_info.sType = VK_STRUCTURE_TYPE_RENDER_PASS_CREATE_INFO;
        create_info.attachmentCount = 1;
        create_info.pAttachments = &color_attachment;
        create_info.subpassCount = 1;
        create_info.pSubpasses = &subpass;
        create_info.dependencyCount = 1;
        create_info.pDependencies = &dependency;
        vk_check(vkCreateRenderPass(device_, &create_info, nullptr, &render_pass_), "vkCreateRenderPass");
        log_.emit("render_pass_created");
    }

    void create_framebuffers() {
        framebuffers_.resize(swapchain_image_views_.size());
        for (size_t index = 0; index < swapchain_image_views_.size(); ++index) {
            VkImageView attachments[] = {swapchain_image_views_[index]};
            VkFramebufferCreateInfo create_info{};
            create_info.sType = VK_STRUCTURE_TYPE_FRAMEBUFFER_CREATE_INFO;
            create_info.renderPass = render_pass_;
            create_info.attachmentCount = 1;
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

    void create_command_pool() {
        VkCommandPoolCreateInfo create_info{};
        create_info.sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO;
        create_info.flags = VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT;
        create_info.queueFamilyIndex = queue_family_.index;
        vk_check(vkCreateCommandPool(device_, &create_info, nullptr, &command_pool_), "vkCreateCommandPool");
        log_.emit("command_pool_created");
    }

    void create_command_buffers() {
        command_buffers_.resize(framebuffers_.size());
        VkCommandBufferAllocateInfo allocate_info{};
        allocate_info.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO;
        allocate_info.commandPool = command_pool_;
        allocate_info.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY;
        allocate_info.commandBufferCount = static_cast<uint32_t>(command_buffers_.size());
        vk_check(vkAllocateCommandBuffers(device_, &allocate_info, command_buffers_.data()), "vkAllocateCommandBuffers");

        for (size_t index = 0; index < command_buffers_.size(); ++index) {
            VkCommandBufferBeginInfo begin_info{};
            begin_info.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO;
            vk_check(vkBeginCommandBuffer(command_buffers_[index], &begin_info), "vkBeginCommandBuffer");

            VkClearValue clear_color{};
            clear_color.color = {{0.02f, 0.11f, 0.19f, 1.0f}};
            VkRenderPassBeginInfo render_pass_info{};
            render_pass_info.sType = VK_STRUCTURE_TYPE_RENDER_PASS_BEGIN_INFO;
            render_pass_info.renderPass = render_pass_;
            render_pass_info.framebuffer = framebuffers_[index];
            render_pass_info.renderArea.offset = {0, 0};
            render_pass_info.renderArea.extent = swapchain_extent_;
            render_pass_info.clearValueCount = 1;
            render_pass_info.pClearValues = &clear_color;

            vkCmdBeginRenderPass(command_buffers_[index], &render_pass_info, VK_SUBPASS_CONTENTS_INLINE);
            vkCmdEndRenderPass(command_buffers_[index]);
            vk_check(vkEndCommandBuffer(command_buffers_[index]), "vkEndCommandBuffer");
        }
        log_.emit("command_buffers_recorded", {{"count", std::to_string(command_buffers_.size())}});
    }

    void create_sync_objects() {
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

    void main_loop() {
        log_.emit("main_loop_enter");
        auto last_frame = std::chrono::steady_clock::now();
        while (running_ && frame_count_ < options_.max_frames) {
            MSG message{};
            while (PeekMessageW(&message, nullptr, 0, 0, PM_REMOVE)) {
                if (message.message == WM_QUIT) {
                    running_ = false;
                }
                TranslateMessage(&message);
                DispatchMessageW(&message);
            }
            if (!running_) {
                break;
            }
            if (options_.inject_input && !input_injected_) {
                PostMessageW(hwnd_, WM_KEYDOWN, VK_SPACE, 0);
                PostMessageW(hwnd_, WM_KEYUP, VK_SPACE, 0);
                input_injected_ = true;
                log_.emit("input_injected", {{"virtual_key", std::to_string(VK_SPACE)}});
            }
            draw_frame();
            ++frame_count_;
            const auto now = std::chrono::steady_clock::now();
            const auto elapsed_ms = std::chrono::duration_cast<std::chrono::milliseconds>(now - last_frame).count();
            last_frame = now;
            log_.emit(
                "frame_presented",
                {
                    {"frame", std::to_string(frame_count_)},
                    {"elapsed_ms", std::to_string(elapsed_ms)},
                    {"clear_color", json_string("#051C30")},
                });
            std::this_thread::sleep_for(std::chrono::milliseconds(16));
        }
        log_.emit(
            "main_loop_exit",
            {
                {"frames", std::to_string(frame_count_)},
                {"input_events", std::to_string(input_events_)},
                {"closed_by_user", json_bool(closed_by_user_)},
            });
    }

    void draw_frame() {
        vk_check(vkWaitForFences(device_, 1, &in_flight_, VK_TRUE, UINT64_MAX), "vkWaitForFences");
        vk_check(vkResetFences(device_, 1, &in_flight_), "vkResetFences");

        uint32_t image_index = 0;
        VkResult acquire = vkAcquireNextImageKHR(
            device_, swapchain_, UINT64_MAX, image_available_, VK_NULL_HANDLE, &image_index);
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
        vk_check(vkQueueSubmit(graphics_queue_, 1, &submit_info, in_flight_), "vkQueueSubmit");

        VkPresentInfoKHR present_info{};
        present_info.sType = VK_STRUCTURE_TYPE_PRESENT_INFO_KHR;
        present_info.waitSemaphoreCount = 1;
        present_info.pWaitSemaphores = signal_semaphores;
        present_info.swapchainCount = 1;
        present_info.pSwapchains = &swapchain_;
        present_info.pImageIndices = &image_index;
        const VkResult present = vkQueuePresentKHR(graphics_queue_, &present_info);
        if (present == VK_ERROR_OUT_OF_DATE_KHR || present == VK_SUBOPTIMAL_KHR) {
            running_ = false;
            log_.emit("swapchain_present_suboptimal", {{"result", std::to_string(present)}});
            return;
        }
        vk_check(present, "vkQueuePresentKHR");
    }

    void on_key(UINT message, WPARAM key) {
        ++input_events_;
        log_.emit(
            "input_event",
            {
                {"message", json_string(message == WM_KEYDOWN ? "keydown" : "keyup")},
                {"virtual_key", std::to_string(static_cast<uint32_t>(key))},
            });
        if (message == WM_KEYDOWN && key == VK_ESCAPE) {
            closed_by_user_ = true;
            running_ = false;
            PostQuitMessage(0);
        }
    }

    static LRESULT CALLBACK window_proc(HWND hwnd, UINT message, WPARAM wparam, LPARAM lparam) {
        auto* app = reinterpret_cast<VulkanFirstFrameApp*>(GetWindowLongPtrW(hwnd, GWLP_USERDATA));
        if (message == WM_NCCREATE) {
            auto* create = reinterpret_cast<CREATESTRUCTW*>(lparam);
            app = reinterpret_cast<VulkanFirstFrameApp*>(create->lpCreateParams);
            SetWindowLongPtrW(hwnd, GWLP_USERDATA, reinterpret_cast<LONG_PTR>(app));
        }
        if (app) {
            switch (message) {
                case WM_CLOSE:
                    app->closed_by_user_ = true;
                    app->running_ = false;
                    DestroyWindow(hwnd);
                    return 0;
                case WM_DESTROY:
                    if (app->hwnd_ == hwnd) {
                        app->hwnd_ = nullptr;
                    }
                    PostQuitMessage(0);
                    return 0;
                case WM_KEYDOWN:
                case WM_KEYUP:
                    app->on_key(message, wparam);
                    return 0;
                default:
                    break;
            }
        }
        return DefWindowProcW(hwnd, message, wparam, lparam);
    }

    void cleanup() {
        if (device_) {
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
                vkDestroyCommandPool(device_, command_pool_, nullptr);
            }
            for (VkFramebuffer framebuffer : framebuffers_) {
                vkDestroyFramebuffer(device_, framebuffer, nullptr);
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
            vkDestroyDevice(device_, nullptr);
            device_ = VK_NULL_HANDLE;
        }
        if (surface_) {
            vkDestroySurfaceKHR(instance_, surface_, nullptr);
            surface_ = VK_NULL_HANDLE;
        }
        if (hwnd_) {
            DestroyWindow(hwnd_);
            hwnd_ = nullptr;
        }
        if (instance_) {
            vkDestroyInstance(instance_, nullptr);
            instance_ = VK_NULL_HANDLE;
        }
    }

    Options options_;
    DebugLog log_;
    HINSTANCE hinstance_ = nullptr;
    HWND hwnd_ = nullptr;
    bool running_ = true;
    bool closed_by_user_ = false;
    bool input_injected_ = false;
    uint32_t frame_count_ = 0;
    uint32_t input_events_ = 0;

    VkInstance instance_ = VK_NULL_HANDLE;
    VkSurfaceKHR surface_ = VK_NULL_HANDLE;
    VkPhysicalDevice physical_device_ = VK_NULL_HANDLE;
    QueueFamilySelection queue_family_{};
    VkDevice device_ = VK_NULL_HANDLE;
    VkQueue graphics_queue_ = VK_NULL_HANDLE;
    VkSwapchainKHR swapchain_ = VK_NULL_HANDLE;
    VkFormat swapchain_format_ = VK_FORMAT_UNDEFINED;
    VkExtent2D swapchain_extent_{};
    std::vector<VkImage> swapchain_images_;
    std::vector<VkImageView> swapchain_image_views_;
    VkRenderPass render_pass_ = VK_NULL_HANDLE;
    std::vector<VkFramebuffer> framebuffers_;
    VkCommandPool command_pool_ = VK_NULL_HANDLE;
    std::vector<VkCommandBuffer> command_buffers_;
    VkSemaphore image_available_ = VK_NULL_HANDLE;
    VkSemaphore render_finished_ = VK_NULL_HANDLE;
    VkFence in_flight_ = VK_NULL_HANDLE;
};

std::vector<std::wstring> command_line_args() {
    int argc = 0;
    LPWSTR* argv = CommandLineToArgvW(GetCommandLineW(), &argc);
    if (!argv) {
        throw std::runtime_error("CommandLineToArgvW failed");
    }
    std::vector<std::wstring> result;
    for (int index = 0; index < argc; ++index) {
        result.emplace_back(argv[index]);
    }
    LocalFree(argv);
    return result;
}

uint32_t parse_u32(const std::wstring& value, const wchar_t* label) {
    wchar_t* end = nullptr;
    const unsigned long parsed = wcstoul(value.c_str(), &end, 10);
    if (!end || *end != L'\0' || parsed > UINT32_MAX) {
        throw std::runtime_error("invalid integer for " + narrow(label));
    }
    return static_cast<uint32_t>(parsed);
}

Options parse_options() {
    Options options;
    const std::vector<std::wstring> args = command_line_args();
    for (size_t index = 1; index < args.size(); ++index) {
        const std::wstring& arg = args[index];
        auto require_value = [&](const wchar_t* name) -> const std::wstring& {
            if (index + 1 >= args.size()) {
                throw std::runtime_error("missing value for " + narrow(name));
            }
            return args[++index];
        };

        if (arg == L"--width") {
            options.width = parse_u32(require_value(L"--width"), L"--width");
        } else if (arg == L"--height") {
            options.height = parse_u32(require_value(L"--height"), L"--height");
        } else if (arg == L"--max-frames") {
            options.max_frames = parse_u32(require_value(L"--max-frames"), L"--max-frames");
        } else if (arg == L"--debug-json") {
            options.debug_json = std::filesystem::path(require_value(L"--debug-json"));
        } else if (arg == L"--title") {
            options.title = require_value(L"--title");
        } else if (arg == L"--inject-input") {
            options.inject_input = true;
        } else if (arg == L"--list-adapters") {
            options.list_adapters_only = true;
        } else if (arg == L"--help" || arg == L"-h") {
            std::wcout
                << L"Usage: b2_first_frame.exe [--width N] [--height N] [--max-frames N]\n"
                << L"                          [--debug-json PATH] [--inject-input]\n"
                << L"                          [--list-adapters]\n";
            ExitProcess(0);
        } else {
            throw std::runtime_error("unknown argument: " + narrow(arg));
        }
    }
    if (options.max_frames == 0) {
        throw std::runtime_error("--max-frames must be greater than zero");
    }
    return options;
}

}  // namespace

int main() {
    try {
        VulkanFirstFrameApp app(parse_options());
        return app.run();
    } catch (const std::exception& exc) {
        std::cerr << "{\"event\":\"fatal\",\"error\":" << json_string(exc.what()) << "}\n";
        return 1;
    }
}
