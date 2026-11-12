#include "CaptureViewAction.h"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <filesystem>
#include <limits>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <thread>

#include <MaaUtils/Logger.h>
#include <MaaUtils/Platform.h>

#include "../Common/paths.h"
#include "../MapLocator/MapLocateAction.h"
#include "../MapNavigator/Backend/Desktop/desktop_input_backend.h"
#include "../MapNavigator/control/action_wrapper.h"
#include "../MapNavigator/control/controller_type_utils.h"
#include "../MapNavigator/navi_math.h"
#include "../MapNavigator/navi_param.h"
#include "../MapNavigator/planning/navmesh_cache.h"
#include "../MapNavigator/planning/navmesh_queries.h"
#include "../MapNavigator/sensing/position_provider.h"
#include "NativeFrameStore.h"

namespace capture
{
namespace
{

constexpr double kYawTolerance = 3.0;
constexpr double kStillDistance = 0.35;
constexpr int kStillReads = 3;
constexpr auto kReadInterval = std::chrono::milliseconds(100);
constexpr std::size_t kMaxRawBytes = 256 * 1024 * 1024;
std::mutex g_photography_mutex;

struct ViewParam
{
    std::string session_id;
    std::string frame_id;
    std::string expected_zone;
    std::vector<double> target;
    double yaw_degrees = 0.0;
    std::string output_dir;
    double arrival_radius = 3.0;
    int timeout_ms = 20000;

    MEO_JSONIZATION(session_id, frame_id, expected_zone, target, yaw_degrees, output_dir, MEO_OPT arrival_radius, MEO_OPT timeout_ms)
};

bool valid_id(const std::string& value)
{
    return !value.empty() && value.size() <= 128
           && value.find_first_not_of("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-") == std::string::npos;
}

bool release_movement(MaaController* controller)
{
    const auto keys = mapnavigator::backend::desktop::MakeDesktopKeyCodes();
    bool released = true;
    // Match the dedicated worker's cancellation releases, including navigation interactions and modifiers.
    for (const auto key : { keys.move_forward,
                            keys.move_left,
                            keys.move_backward,
                            keys.move_right,
                            keys.interact,
                            keys.jump,
                            keys.walk_toggle,
                            0x10,
                            0x12 }) {
        const auto id = MaaControllerPostKeyUp(controller, key);
        released = id != MaaInvalidId && MaaControllerWait(controller, id) == MaaStatus_Succeeded && released;
    }
    for (const int contact : { 0, 1 }) {
        const auto id = MaaControllerPostTouchUp(controller, contact);
        released = id != MaaInvalidId && MaaControllerWait(controller, id) == MaaStatus_Succeeded && released;
    }
    return released;
}

class PhotographyScope
{
public:
    explicit PhotographyScope(MaaController* controller)
        : controller_(controller)
    {
    }

    ~PhotographyScope()
    {
        if (!restored_ && !restore()) {
            LogError << "CaptureViewAction could not restore navigation screenshot settings or release movement.";
        }
    }

    void begin()
    {
        bool raw = true;
        if (!release_movement(controller_) || !MaaControllerSetOption(controller_, MaaCtrlOption_ScreenshotUseRawSize, &raw, sizeof(raw))) {
            throw std::runtime_error("Cannot release movement or enable raw photography");
        }
    }

    bool restore()
    {
        if (restored_) {
            return true;
        }
        bool raw = false;
        int short_side = 720;
        const bool raw_restored = MaaControllerSetOption(controller_, MaaCtrlOption_ScreenshotUseRawSize, &raw, sizeof(raw));
        const bool size_restored =
            MaaControllerSetOption(controller_, MaaCtrlOption_ScreenshotTargetShortSide, &short_side, sizeof(short_side));
        const bool movement_released = release_movement(controller_);
        restored_ = raw_restored && size_restored && movement_released;
        return restored_;
    }

private:
    MaaController* controller_;
    bool restored_ = false;
};

std::filesystem::path checked_output(const std::string& value)
{
    const auto output = MAA_NS::path(value).lexically_normal();
    const auto root = common::UserDataDir();
    AssertCaptureDirectory(root);
    AssertCaptureDirectory(output);
    const auto relative = output.lexically_relative(root);
    if (relative.empty() || relative == "." || *relative.begin() == "..") {
        throw std::invalid_argument("Capture output must be inside PI_USER_DATA_DIR");
    }
    if (std::filesystem::exists(output / "result.json") || std::filesystem::exists(output / "frame.png")) {
        throw std::runtime_error("Capture output directory already contains a receipt or frame");
    }
    return output;
}

json::object clock_json()
{
    const auto& clock = ProcessCaptureClock();
    return { { "clock_id", clock.clock_id },
             { "monotonic_anchor_ns", std::to_string(clock.monotonic_anchor_ns) },
             { "utc_anchor_unix_ns", std::to_string(clock.utc_anchor_unix_ns) } };
}

bool photograph(MaaContext* context, const ViewParam& param, const std::filesystem::path& output, json::object& result)
{
    std::unique_lock photography_lock(g_photography_mutex, std::try_to_lock);
    if (!photography_lock.owns_lock()) {
        throw std::runtime_error("Another CaptureViewAction owns photography");
    }
    mapnavigator::ActionWrapper action(context);
    if (!action.isSupported() || !mapnavigator::EqualsIgnoreCase(action.controllerType(), "win32")) {
        throw std::runtime_error("CaptureViewAction requires a supported Win32 controller");
    }
    PhotographyScope scope(action.getCtrl());
    scope.begin();
    auto clock = ProcessCaptureClock();
    clock.session_id = param.session_id;
    mapnavigator::PositionProvider provider(action.getCtrl(), maplocator::getOrInitLocator(), mapnavigator::HeadingSource::Camera, clock);
    mapnavigator::NaviParam navigation;
    navigation.map_name = param.expected_zone;
    const auto zone = mapnavigator::LoadZoneNavmesh(navigation, param.expected_zone);
    if (!zone.mesh) {
        throw std::runtime_error("Photography requires the selected zone's navmesh");
    }
    const auto* geometry = zone.mesh->pack.findZoneByName(zone.navmesh_zone);
    if (!geometry || param.target[0] < 0.0 || param.target[1] < 0.0 || param.target[0] > geometry->width
        || param.target[1] > geometry->height) {
        throw std::invalid_argument("Photography target lies outside the selected base map");
    }
    const auto normalization_id = "basenav:" + std::to_string(zone.mesh->pack.buildHash()) + ":" + zone.navmesh_zone;
    provider.setPositionNormalizer(
        [&](auto& position) { mapnavigator::NormalizeLivePositionToBase(navigation, position); },
        normalization_id);
    std::optional<FrameCompletion> completed;
    cv::Mat raw_image;
    auto subscription = provider.subscribeCompletion([&](const auto& completion, const cv::Mat& image) {
        completed = completion;
        raw_image.release();
        if (!image.empty() && image.type() == CV_8UC3 && image.total() <= kMaxRawBytes / image.elemSize()) {
            raw_image = image.clone();
        }
    });
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(param.timeout_ms);
    const auto stopped = [&] {
        return MaaTaskerStopping(MaaContextGetTasker(context));
    };
    std::optional<mapnavigator::NaviPosition> previous;
    int still_reads = 0;
    json::object stability { { "method", "position_and_camera_heading" },
                             { "stable_reads", 0 },
                             { "required_reads", kStillReads },
                             { "position_change_map_pixels", nullptr },
                             { "heading_change_degrees", nullptr } };
    bool ready = false;
    while (!stopped() && std::chrono::steady_clock::now() < deadline) {
        stability["stable_reads"] = 0;
        stability["position_change_map_pixels"] = nullptr;
        stability["heading_change_degrees"] = nullptr;
        result["stability"] = stability;
        completed.reset();
        raw_image.release();
        mapnavigator::NaviPosition position;
        const bool located = provider.captureForPhotography(&position);
        if (completed) {
            result["native_attempt_id"] = completed->frame_id;
            auto published = *completed;
            published.frame_id = param.frame_id;
            result["completion"] = SerializeCompletion(published);
        }
        if (located && completed && raw_image.empty()) {
            throw std::runtime_error("Photography raw image is unsupported, oversized, or could not be copied");
        }
        if (!located || !completed || raw_image.empty() || mapnavigator::InferBaseNavZone(position.zone_id, { }) != zone.navmesh_zone) {
            previous.reset();
            still_reads = 0;
            std::this_thread::sleep_for(kReadInterval);
            continue;
        }
        const double distance = std::hypot(position.x - param.target[0], position.y - param.target[1]);
        const double yaw_error = mapnavigator::NaviMath::calcDeltaRotation(position.angle, param.yaw_degrees);
        result["arrival"] = json::object { { "target", param.target },
                                           { "actual", json::array { position.x, position.y } },
                                           { "distance", distance },
                                           { "tolerance", param.arrival_radius } };
        result["yaw"] = json::object { { "target", param.yaw_degrees }, { "actual", position.angle }, { "tolerance", kYawTolerance } };
        if (distance > param.arrival_radius) {
            throw std::runtime_error("Photography target was not reached");
        }
        if (std::abs(yaw_error) > kYawTolerance) {
            const auto profile = action.steeringProfile();
            const double delta = std::clamp(yaw_error, -profile.max_batch_delta_deg, profile.max_batch_delta_deg);
            const double units_per_degree = action.defaultTurnUnitsPerDegree();
            const double units = delta * units_per_degree;
            if (!std::isfinite(units_per_degree) || units_per_degree <= 0.0 || !std::isfinite(units)
                || std::abs(units) > std::numeric_limits<int>::max()
                || !action.sendViewDeltaSync(static_cast<int>(std::lround(units)), 0)) {
                throw std::runtime_error("Photography camera turn failed");
            }
            previous.reset();
            still_reads = 0;
        }
        else {
            const double position_change = previous ? std::hypot(position.x - previous->x, position.y - previous->y) : 0.0;
            const double heading_change =
                previous ? std::abs(mapnavigator::NaviMath::calcDeltaRotation(previous->angle, position.angle)) : 0.0;
            // Effects and animated actors can change pixels while position and camera heading remain still.
            const bool still = previous && position_change <= kStillDistance && heading_change <= 1.0;
            still_reads = still ? still_reads + 1 : 1;
            stability["stable_reads"] = still_reads;
            stability["position_change_map_pixels"] = previous ? json::value(position_change) : json::value(nullptr);
            stability["heading_change_degrees"] = previous ? json::value(heading_change) : json::value(nullptr);
            result["stability"] = stability;
            previous = position;
            if (still_reads >= kStillReads) {
                ready = true;
                break;
            }
        }
        std::this_thread::sleep_for(kReadInterval);
    }
    if (!ready || stopped() || std::chrono::steady_clock::now() >= deadline) {
        throw std::runtime_error(stopped() ? "Photography stopped" : "Photography yaw or location did not settle before timeout");
    }
    int32_t width = 0;
    int32_t height = 0;
    if (!MaaControllerGetResolution(action.getCtrl(), &width, &height) || width != raw_image.cols || height != raw_image.rows) {
        throw std::runtime_error("Photography image does not match raw controller resolution");
    }
    if (!scope.restore()) {
        throw std::runtime_error("Cannot restore navigation settings after photography");
    }
    if (stopped()) {
        throw std::runtime_error("Photography stopped before image publication");
    }
    WriteCapturePng(output / "frame.png", raw_image);
    if (stopped()) {
        std::filesystem::remove(output / "frame.png");
        throw std::runtime_error("Photography stopped during image publication");
    }
    result["image"] = json::object { { "uri", "frame.png" },
                                     { "width", width },
                                     { "height", height },
                                     { "channels", 3 },
                                     { "pixel_format", "RGB8" },
                                     { "source_format", "BGR8" },
                                     { "backend", "Win32Controller" } };
    return true;
}

} // namespace

MaaBool MAA_CALL CaptureViewActionRun(
    MaaContext* context,
    [[maybe_unused]] MaaTaskId task_id,
    [[maybe_unused]] const char* node_name,
    [[maybe_unused]] const char* custom_action_name,
    const char* custom_action_param,
    [[maybe_unused]] MaaRecoId reco_id,
    [[maybe_unused]] const MaaRect* box,
    [[maybe_unused]] void* trans_arg)
{
    std::optional<std::filesystem::path> output;
    json::object result { { "schema_version", "capture.view.result/1.0" },
                          { "ok", false },
                          { "clock", clock_json() },
                          { "image", nullptr },
                          { "completion", nullptr },
                          { "error", nullptr } };
    try {
        const auto parsed = custom_action_param ? json::parse(custom_action_param) : std::nullopt;
        ViewParam param;
        if (!context || !parsed || !param.from_json(*parsed) || !valid_id(param.session_id) || !valid_id(param.frame_id)
            || param.expected_zone.empty() || param.target.size() != 2 || !std::isfinite(param.target[0]) || !std::isfinite(param.target[1])
            || !std::isfinite(param.yaw_degrees) || param.yaw_degrees < 0.0 || param.yaw_degrees >= 360.0
            || !std::isfinite(param.arrival_radius) || param.arrival_radius <= 0.0 || param.arrival_radius > 20.0 || param.timeout_ms < 1000
            || param.timeout_ms > 120000) {
            throw std::invalid_argument("Invalid CaptureViewAction parameters");
        }
        output = checked_output(param.output_dir);
        result["frame_id"] = param.frame_id;
        result["session_id"] = param.session_id;
        result["arrival"] = json::object { { "target", param.target },
                                           { "actual", nullptr },
                                           { "distance", nullptr },
                                           { "tolerance", param.arrival_radius } };
        result["yaw"] = json::object { { "target", param.yaw_degrees }, { "actual", nullptr }, { "tolerance", kYawTolerance } };
        result["ok"] = photograph(context, param, *output, result);
    }
    catch (const std::exception& error) {
        result["error"] = std::string(error.what()).empty() ? "Unknown photography failure" : error.what();
        LogError << "CaptureViewAction failed." << error.what();
    }
    catch (...) {
        result["error"] = "Unknown photography failure";
        LogError << "CaptureViewAction failed with an unknown exception.";
    }
    if (output) {
        try {
            WriteCaptureReceipt(*output / "result.json", result);
        }
        catch (const std::exception& error) {
            LogError << "CaptureViewAction could not persist its receipt." << error.what();
            return false;
        }
    }
    return result.at("ok").as_boolean();
}

} // namespace capture
