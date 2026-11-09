#include "FrameCompletion.h"

#include <chrono>
#include <mutex>
#include <utility>

#include <MaaUtils/Uuid.h>

namespace capture
{

CompletionSubscription::CompletionSubscription(const std::shared_ptr<CompletionSlot>& slot)
    : slot_(slot)
{
}

CompletionSubscription::~CompletionSubscription()
{
    reset();
}

CompletionSubscription::CompletionSubscription(CompletionSubscription&& other) noexcept
    : slot_(std::exchange(other.slot_, { }))
{
}

CompletionSubscription& CompletionSubscription::operator=(CompletionSubscription&& other) noexcept
{
    if (this != &other) {
        reset();
        slot_ = std::exchange(other.slot_, { });
    }
    return *this;
}

void CompletionSubscription::reset() noexcept
{
    if (auto slot = slot_.lock()) {
        slot->observer = { };
    }
    slot_.reset();
}

std::int64_t MonotonicNowNs()
{
    return std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now().time_since_epoch()).count();
}

CaptureClock NewCaptureClock()
{
    static std::mutex uuid_mutex;
    const std::lock_guard lock(uuid_mutex);
    return {
        .session_id = MAA_NS::make_uuid(),
        .clock_id = MAA_NS::make_uuid(),
        .monotonic_anchor_ns = MonotonicNowNs(),
        .utc_anchor_unix_ns =
            std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::system_clock::now().time_since_epoch()).count(),
    };
}

} // namespace capture
