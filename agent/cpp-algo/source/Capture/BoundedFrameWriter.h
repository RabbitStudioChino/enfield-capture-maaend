#pragma once

#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <functional>
#include <mutex>
#include <string>
#include <thread>

#include "FrameCompletion.h"

namespace capture
{

struct WriterLimits
{
    // Initial bounds for uncompressed owned images, including cloning and the in-flight write.
    std::size_t max_frames = 8;
    std::size_t max_bytes = 256 * 1024 * 1024;
};

enum class SubmitResult
{
    Accepted,
    Full,
    Oversized,
    UnsupportedImage,
    Stopped,
    CloneFailed,
};

struct WriterStats
{
    std::uint64_t accepted = 0;
    std::uint64_t written = 0;
    std::uint64_t dropped = 0;
    std::uint64_t aborted = 0;
    std::size_t reserved_frames = 0;
    std::size_t reserved_bytes = 0;
    std::size_t peak_frames = 0;
    std::size_t peak_bytes = 0;
    std::string last_dropped_frame_id;
    std::string last_drop_reason;
    std::string error;
};

class BoundedFrameWriter
{
public:
    using ErrorObserver = std::function<void(const std::string&)>;

    BoundedFrameWriter(CompletionObserver write_frame, ErrorObserver on_error = { }, WriterLimits limits = { });
    ~BoundedFrameWriter();
    BoundedFrameWriter(const BoundedFrameWriter&) = delete;
    BoundedFrameWriter& operator=(const BoundedFrameWriter&) = delete;

    // No encoding or disk access runs here. Rejected submissions never clone the borrowed image.
    SubmitResult submit(const FrameCompletion& completion, const cv::Mat& image);
    WriterStats stats() const;
    void stop(bool drain = true);

private:
    struct PendingFrame
    {
        FrameCompletion completion;
        cv::Mat image;
        std::size_t bytes = 0;
    };

    void run();
    void release(std::size_t bytes);
    void discardQueued();
    SubmitResult reject(const FrameCompletion& completion, SubmitResult result);

    const WriterLimits limits_;
    CompletionObserver write_frame_;
    ErrorObserver on_error_;
    mutable std::mutex mutex_;
    std::condition_variable ready_;
    std::deque<PendingFrame> queue_;
    WriterStats stats_;
    std::size_t cloning_ = 0;
    bool stopping_ = false;
    bool drain_ = true;
    std::thread thread_;
};

} // namespace capture
