#pragma once

#include <atomic>
#include <filesystem>
#include <memory>

#include "NativeFrameStore.h"

namespace mapnavigator
{
class PositionProvider;
}

namespace capture
{

class PassiveRecorder
{
public:
    PassiveRecorder(mapnavigator::PositionProvider& provider, const std::filesystem::path& workspace);
    ~PassiveRecorder();
    PassiveRecorder(const PassiveRecorder&) = delete;
    PassiveRecorder& operator=(const PassiveRecorder&) = delete;

    bool failed() const;
    bool stop();
    WriterStats stats() const;
    const std::filesystem::path& runPath() const;

private:
    NativeFrameStore store_;
    std::atomic<bool> failed_ = false;
    BoundedFrameWriter writer_;
    CompletionSubscription subscription_;
    bool stopped_ = false;
};

// shortcut: passive diagnostics use an explicit environment opt-in, replace with formal task attachment when C03 is verified.
std::unique_ptr<PassiveRecorder> AttachPassiveRecorder(mapnavigator::PositionProvider& provider);

} // namespace capture
