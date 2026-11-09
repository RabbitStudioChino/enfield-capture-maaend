#pragma once

#include <cstdint>
#include <functional>
#include <memory>
#include <optional>
#include <string>

#include "../MapLocator/MapTypes.h"
#include "../MapNavigator/navi_position.h"

namespace capture
{

struct CaptureClock
{
    std::string session_id;
    std::string clock_id;
    std::int64_t monotonic_anchor_ns = 0;
    std::int64_t utc_anchor_unix_ns = 0;
};

struct FrameCompletion
{
    CaptureClock clock;
    std::string frame_id;
    std::int64_t request_monotonic_ns = 0;
    std::optional<std::int64_t> screencap_completed_monotonic_ns;
    std::optional<std::int64_t> locate_started_monotonic_ns;
    std::optional<std::int64_t> locate_completed_monotonic_ns;
    std::int64_t completed_monotonic_ns = 0;
    std::optional<maplocator::LocateResult> locate_result;
    std::optional<mapnavigator::NaviPosition> position_before_normalization;
    std::optional<mapnavigator::NaviPosition> position_after_normalization;
    std::optional<std::string> normalization_id;
    mapnavigator::HeadingSource heading_source = mapnavigator::HeadingSource::Character;
    bool navigation_valid = false;
    std::string outcome;
    std::string reason;
};

// The image is borrowed until this synchronous callback returns. Async users must reserve capacity before cloning it.
using CompletionObserver = std::function<void(const FrameCompletion&, const cv::Mat&)>;

struct CompletionSlot
{
    CompletionObserver observer;
};

// Register and reset on the provider's owning thread. A token may outlive its provider.
class CompletionSubscription
{
public:
    CompletionSubscription() = default;
    explicit CompletionSubscription(const std::shared_ptr<CompletionSlot>& slot);
    ~CompletionSubscription();
    CompletionSubscription(const CompletionSubscription&) = delete;
    CompletionSubscription& operator=(const CompletionSubscription&) = delete;
    CompletionSubscription(CompletionSubscription&& other) noexcept;
    CompletionSubscription& operator=(CompletionSubscription&& other) noexcept;
    void reset() noexcept;

private:
    std::weak_ptr<CompletionSlot> slot_;
};

CaptureClock NewCaptureClock();
std::int64_t MonotonicNowNs();

} // namespace capture
