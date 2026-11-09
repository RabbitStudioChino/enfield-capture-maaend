#include "position_provider.h"

#include <atomic>
#include <chrono>
#include <cmath>
#include <stdexcept>

#include <MaaUtils/Logger.h>

#include "../../utils.h"
#include "../control/controller_info_utils.h"
#include "../control/controller_type_utils.h"
#include "../navi_config.h"
#include "../navi_time.h"
#include "latency_observer.h"

namespace mapnavigator
{

namespace
{

// Existing minimap templates and ROI use a 720-high landscape reference.
constexpr int kPhotographyReferenceHeight = 720;
std::atomic<std::uint64_t> capture_attempt_sequence = 0;

bool is_black_screen(const cv::Mat& image)
{
    if (image.empty()) {
        return false;
    }

    cv::Mat gray;
    switch (image.channels()) {
    case 4:
        cv::cvtColor(image, gray, cv::COLOR_BGRA2GRAY);
        break;
    case 3:
        cv::cvtColor(image, gray, cv::COLOR_BGR2GRAY);
        break;
    default:
        gray = image;
        break;
    }

    cv::Scalar mean_luma;
    cv::Scalar stddev_luma;
    cv::meanStdDev(gray, mean_luma, stddev_luma);

    cv::Mat dark_mask;
    cv::threshold(gray, dark_mask, 24, 255, cv::THRESH_BINARY_INV);
    const double dark_ratio = static_cast<double>(cv::countNonZero(dark_mask)) / static_cast<double>(gray.total());

    return mean_luma[0] <= 12.0 && stddev_luma[0] <= 10.0 && dark_ratio >= 0.98;
}

void log_locate_result(const maplocator::LocateResult& locate_result)
{
    const int status = static_cast<int>(locate_result.status);
    if (locate_result.position) {
        const auto& position = *locate_result.position;
        const double cam_rot = locate_result.camRot ? locate_result.camRot->rot : -1.0;
        const double cam_rot_conf = locate_result.camRot ? locate_result.camRot->confidence : -1.0;
        LogInfo << "MapLocator" << VAR(status) << VAR(locate_result.debugMessage) << VAR(position.zoneId) << VAR(position.x)
                << VAR(position.y) << VAR(position.score) << VAR(position.sliceIndex) << VAR(position.angle) << VAR(cam_rot)
                << VAR(cam_rot_conf) << VAR(position.latencyMs);
    }
    else {
        LogInfo << "MapLocator" << VAR(status) << VAR(locate_result.debugMessage) << "position=null";
    }
}

std::optional<double> confident_camera_heading(const maplocator::LocateResult& locate_result)
{
    return locate_result.camRot && std::isfinite(locate_result.camRot->confidence)
                   && locate_result.camRot->confidence >= kNavigationCameraMinConfidence
               ? std::optional<double>(locate_result.camRot->rot)
               : std::nullopt;
}

} // namespace

PositionProvider::PositionProvider(
    MaaController* controller,
    std::shared_ptr<maplocator::MapLocator> locator,
    HeadingSource heading_source,
    capture::CaptureClock clock)
    : controller_(controller)
    , locator_(std::move(locator))
    , heading_source_(heading_source)
    , capture_clock_(std::move(clock))
    , adb_like_controller_(IsAdbLikeControllerType(DetectControllerType(controller_)))
{
}

void PositionProvider::setFrameObserver(std::function<void(const cv::Mat&)> observer)
{
    frame_observer_ = std::move(observer);
}

bool PositionProvider::capture(
    NaviPosition* out_pos,
    bool force_global_search,
    const std::string& expected_zone_id,
    const std::vector<maplocator::SearchHint>& search_hints,
    std::optional<double> camera_heading_prior)
{
    maplocator::LocateOptions options;
    options.force_global_search = force_global_search;
    options.expected_zone_id = expected_zone_id;
    options.search_hints = search_hints;
    options.camera_heading_prior = camera_heading_prior;
    return captureWith(out_pos, std::move(options), heading_source_);
}

bool PositionProvider::captureForStrictGoal(NaviPosition* out_pos, const std::string& expected_zone_id)
{
    maplocator::LocateOptions options;
    options.precise_position = true;
    options.expected_zone_id = expected_zone_id;
    return captureWith(out_pos, std::move(options), HeadingSource::Camera);
}

bool PositionProvider::captureForPhotography(NaviPosition* out_pos)
{
    maplocator::LocateOptions options;
    options.precise_position = true;
    return captureWith(out_pos, std::move(options), HeadingSource::Camera, true);
}

bool PositionProvider::captureWith(
    NaviPosition* out_pos,
    maplocator::LocateOptions options,
    HeadingSource heading_source,
    bool raw_photography)
{
    // buffer 必须比完成回调活得更久，回调返回前 image 仍借用其内存。
    std::optional<ScopedImageBuffer> buffer;
    cv::Mat image;
    capture::FrameCompletion completion;
    completion.clock = capture_clock_;
    completion.frame_id = capture_clock_.session_id + "-" + std::to_string(capture_attempt_sequence.fetch_add(1) + 1);
    completion.request_monotonic_ns = capture::MonotonicNowNs();
    completion.heading_source = heading_source;
    if (position_normalizer_ && !normalization_id_.empty()) {
        completion.normalization_id = normalization_id_;
    }
    const auto perform_capture = [&]() -> bool {
        completion.outcome = "invalid_output";
        if (out_pos == nullptr) {
            return false;
        }

        out_pos->valid = false;
        last_capture_was_black_screen_ = false;
        last_capture_minimap_absent_ = false;
        last_locate_message_ = "screencap failed";
        const auto capture_started_at = std::chrono::steady_clock::now();

        completion.outcome = "screencap_failed";
        const bool screenshot_ready = waitForScreencap();
        completion.screencap_completed_monotonic_ns = capture::MonotonicNowNs();
        if (!screenshot_ready) {
            return false;
        }
        buffer.emplace();
        completion.outcome = "image_unavailable";

        if (!MaaControllerCachedImage(controller_, buffer->Get()) || MaaImageBufferIsEmpty(buffer->Get())) {
            return false;
        }
        const auto screencap_done_at = std::chrono::steady_clock::now();

        image = to_mat(buffer->Get());
        cv::Mat localization_image = image;
        if (raw_photography && image.rows != kPhotographyReferenceHeight && !image.empty()) {
            const double scale = static_cast<double>(kPhotographyReferenceHeight) / image.rows;
            cv::resize(image, localization_image, cv::Size(), scale, scale, cv::INTER_AREA);
        }
        last_capture_was_black_screen_ = is_black_screen(image);
        if (options.precise_position && last_capture_was_black_screen_) {
            completion.outcome = "black_screen";
            last_locate_message_ = "black screen";
            return false;
        }
        if (frame_observer_) {
            frame_observer_(localization_image);
        }
        cv::Mat minimap;
        if (!maplocator::TryExtractMinimap(localization_image, adb_like_controller_, &minimap)) {
            completion.outcome = "minimap_unavailable";
            last_locate_message_ = "minimap not extracted";
            return false;
        }
        const auto image_prep_done_at = std::chrono::steady_clock::now();

        options.reject_occluded_frames = heading_source != HeadingSource::Camera;
        completion.locate_started_monotonic_ns = capture::MonotonicNowNs();
        const auto locate_result = locator_->locate(minimap, options);
        completion.locate_completed_monotonic_ns = capture::MonotonicNowNs();
        completion.locate_result = locate_result;
        const auto locate_done_at = std::chrono::steady_clock::now();
        log_locate_result(locate_result);
        last_capture_minimap_absent_ = locate_result.minimap_absent;
        last_locate_message_ = locate_result.debugMessage;
        completion.outcome = "localization_failed";
        if (locate_result.status != maplocator::LocateStatus::Success || !locate_result.position) {
            return false;
        }

        if (options.precise_position
            && (!std::isfinite(locate_result.position->x) || !std::isfinite(locate_result.position->y)
                || !std::isfinite(locate_result.position->score) || locate_result.position->score < options.loc_threshold)) {
            completion.outcome = "precise_position_rejected";
            last_locate_message_ = "precise position rejected";
            return false;
        }

        const std::optional<double> confident_camera = confident_camera_heading(locate_result);
        const std::optional<double> heading = heading_source == HeadingSource::Camera ? confident_camera : locate_result.rot;
        if (!heading || !std::isfinite(*heading) || *heading < 0.0 || *heading >= 360.0) {
            completion.outcome = "heading_unavailable";
            last_locate_message_ = "heading unavailable";
            return false;
        }

        // 全局搜索本就更慢, 不计入耗时统计
        if (!options.force_global_search) {
            latency::RecordStage(latency::Stage::Screencap, ElapsedMs(capture_started_at, screencap_done_at));
            latency::RecordStage(latency::Stage::ImagePrep, ElapsedMs(screencap_done_at, image_prep_done_at));
            latency::RecordStage(latency::Stage::Locate, ElapsedMs(image_prep_done_at, locate_done_at));
        }

        fillPosition(out_pos, locate_result, *heading, confident_camera, capture_started_at);
        // floor_y 属于导航状态，本帧定位没有测量高度。
        completion.position_before_normalization = *out_pos;
        completion.position_before_normalization->floor_y.reset();

        // 所有取位都经过这里, 且 out_pos 刚由本次定位结果填好, 换算每次恰好一次
        if (position_normalizer_) {
            position_normalizer_(*out_pos);
        }
        completion.position_after_normalization = *out_pos;
        completion.position_after_normalization->floor_y.reset();
        completion.outcome = "complete";
        return true;
    };
    try {
        completion.navigation_valid = perform_capture();
        completion.reason = out_pos == nullptr ? "output position is null" : last_locate_message_;
    }
    catch (const std::exception& error) {
        completion.outcome = "exception";
        completion.reason = error.what();
        publishCompletion(completion, image);
        throw;
    }
    catch (...) {
        completion.outcome = "exception";
        completion.reason = "unknown capture exception";
        publishCompletion(completion, image);
        throw;
    }
    publishCompletion(completion, image);
    return completion.navigation_valid;
}

capture::CompletionSubscription PositionProvider::subscribeCompletion(capture::CompletionObserver observer)
{
    if (!observer || completion_slot_->observer) {
        throw std::logic_error("PositionProvider requires one nonempty completion subscriber");
    }
    completion_slot_->observer = std::move(observer);
    return capture::CompletionSubscription(completion_slot_);
}

const capture::CaptureClock& PositionProvider::captureClock() const
{
    return capture_clock_;
}

void PositionProvider::publishCompletion(capture::FrameCompletion& completion, const cv::Mat& image) noexcept
{
    completion.completed_monotonic_ns = capture::MonotonicNowNs();
    try {
        const auto observer = completion_slot_->observer;
        if (observer) {
            observer(completion, image);
        }
    }
    catch (const std::exception& error) {
        LogError << "Capture completion subscriber failed." << error.what();
    }
    catch (...) {
        LogError << "Capture completion subscriber failed with an unknown exception.";
    }
}

bool PositionProvider::waitForScreencap() const
{
    const MaaCtrlId screencap_id = MaaControllerPostScreencap(controller_);
    if (screencap_id == MaaInvalidId) {
        LogWarn << "Position capture failed to dispatch screencap.";
        return false;
    }
    const MaaStatus capture_status = MaaControllerWait(controller_, screencap_id);
    if (capture_status != MaaStatus_Succeeded) {
        LogWarn << "Position capture screencap did not succeed." << VAR(screencap_id) << VAR(capture_status);
        return false;
    }
    return true;
}

void PositionProvider::fillPosition(
    NaviPosition* out_pos,
    const maplocator::LocateResult& locate_result,
    double heading,
    std::optional<double> confident_camera,
    std::chrono::steady_clock::time_point captured_at) const
{
    out_pos->x = locate_result.position->x;
    out_pos->y = locate_result.position->y;
    out_pos->angle = heading;
    out_pos->score = locate_result.position->score;
    out_pos->zone_id = locate_result.position->zoneId;
    out_pos->camera_angle =
        adb_like_controller_ ? confident_camera : (locate_result.camRot ? std::optional<double>(locate_result.camRot->rot) : std::nullopt);
    out_pos->valid = true;
    out_pos->timestamp = captured_at;
}

void PositionProvider::setPositionNormalizer(std::function<void(NaviPosition&)> normalizer, std::string normalization_id)
{
    position_normalizer_ = std::move(normalizer);
    normalization_id_ = std::move(normalization_id);
}

bool PositionProvider::waitForFix(
    NaviPosition* out_pos,
    const std::string& expected_zone_id,
    int max_retries,
    int retry_interval_ms,
    const std::function<bool()>& should_stop)
{
    for (int retry = 0; retry < max_retries; ++retry) {
        if (should_stop()) {
            return false;
        }
        if (capture(out_pos, !expected_zone_id.empty(), expected_zone_id)) {
            return true;
        }
        SleepFor(retry_interval_ms);
    }
    return false;
}

void PositionProvider::resetTracking()
{
    locator_->resetTrackingState();
    last_capture_was_black_screen_ = false;
    last_capture_minimap_absent_ = false;
}

bool PositionProvider::lastCaptureWasBlackScreen() const
{
    return last_capture_was_black_screen_;
}

bool PositionProvider::lastCaptureMinimapAbsent() const
{
    return last_capture_minimap_absent_;
}

const std::string& PositionProvider::lastLocateMessage() const
{
    return last_locate_message_;
}

} // namespace mapnavigator
